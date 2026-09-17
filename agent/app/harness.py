"""Harness state machine — drives Planner → Executor → Validator → Replanner.

In context_pack mode, ContextView is projected BEFORE each phase and passed as
the phase's authoritative input.  The phase must not reconstruct its own context
from raw TaskState when a View is supplied.

A centralized pre-validation gate (_validate_view_state_metadata) runs before
phase dispatch: when a View is present, it verifies that View identity fields
(task_id, plan_id, step_id, tool_name) match the current TaskState.  Mismatches
stop the phase, the tool call, and the business Validator — they are NOT
silently substituted by live state.

V2: the explicit control-plane graph in agent/app/graph reuses these phase
functions as its nodes (planner/executor/validator/replanner), the same View
projection helpers, and the same decision functions.  The V1 Harness remains
the production entry point behind graph/react_graph.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from .agent_trace import TraceBuilder
from .context_pack import ContextPack
from .context_view import (
    ContextProjector,
    ExecutorContextView,
    PlannerContextView,
    ValidatorContextView,
    ReplannerContextView,
    FinalAnswerContextView,
)
from .executor import (
    ExecutorRunResult,
    ExecutorSelectionError,
    NormalizedStepOutput,
    StepExecutionResult,
    ToolCaller,
    run_executor_step,
    recover_expired_executor_claim,
    select_next_plan_step,
)
from .planner import run_planner_phase
from .planning import PlannerResult, TaskPlan
from .replanner import (
    ReplannerResult,
    run_replanner_phase,
    should_run_replanner,
)
from .settings import settings
from .task_state import TaskState, TaskFact, TaskConstraint
from .tools import call_tool
from .validator import ValidatorResult, run_validator_phase
from .validation_contracts import (
    ExpectedOutputContractError,
    validate_normalized_output_values,
    validated_evidence_fields_for_tool,
)
from .domains.ecommerce.ranking_contract import (
    TwoStageRankingContractError,
    normalize_persisted_scope_rerank_values,
    project_validated_search_product_presentations,
)
from .domains.ecommerce.models import (
    CandidateScope,
    ShoppingGuideState,
    compiled_shopping_requirements,
)

# ── Type aliases ────────────────────────────────────────────────────────────

HarnessPlanningAction = Literal[
    "continue_to_executor",
    "ready_for_validation",
    "ask_user",
    "stop_turn",
]
HarnessAction = Literal[
    "continue_to_executor",
    "ready_for_validation",
    "task_completed",
    "ready_for_replanning",
    "ask_user",
    "stop_turn",
]

# ── Result DTOs ─────────────────────────────────────────────────────────────


class HarnessPlanningStepResult(BaseModel):
    """The observable result of one Harness planning step."""

    model_config = ConfigDict(frozen=True)

    action: HarnessPlanningAction
    planner_result: PlannerResult | None = None
    task_state: TaskState


class HarnessStepResult(BaseModel):
    """One observable Harness transition with at most one Executor step."""

    model_config = ConfigDict(frozen=True)

    action: HarnessAction
    planner_result: PlannerResult | None = None
    executor_result: ExecutorRunResult | None = None
    validator_result: ValidatorResult | None = None
    replanner_result: ReplannerResult | None = None
    task_state: TaskState


# ── ContextView helpers (used by harness and replay) ────────────────────────


def _make_plan_summary(state: TaskState) -> dict[str, Any]:
    """Build a compact plan summary for the ReplannerContextView."""
    plan = state.active_plan
    if plan is None:
        return {"planId": "", "steps": [], "status": "no_plan"}
    return {
        "planId": plan.plan_id,
        "steps": [
            {
                "stepId": s.step_id,
                "description": s.description,
                "toolName": s.tool_name,
                "status": s.status,
            }
            for s in plan.steps
        ],
        "status": plan.status,
    }


def _reusable_step_output_payloads(state: TaskState) -> dict[str, dict[str, Any]]:
    """Project only identity-valid outputs belonging to the active Plan."""
    plan = state.active_plan
    raw_outputs = state.domain_state.get("stepOutputs", {})
    if plan is None or not isinstance(raw_outputs, dict):
        return {}
    plan_step_ids = {step.step_id for step in plan.steps}
    projected: dict[str, dict[str, Any]] = {}
    for step_id, raw_output in raw_outputs.items():
        if step_id not in plan_step_ids or not isinstance(raw_output, dict):
            continue
        output = NormalizedStepOutput.model_validate(raw_output)
        if (
            output.task_id != state.task_id
            or output.plan_id != plan.plan_id
            or output.step_id != step_id
        ):
            raise ValueError("Reusable step output does not belong to active Plan")
        projected[step_id] = output.model_dump(by_alias=True, mode="json")
    return projected


def _candidate_tool_names(
    candidate_tool_schemas: list[dict[str, Any]],
) -> list[str]:
    return [
        str(schema.get("function", schema).get("name", ""))
        for schema in candidate_tool_schemas
        if schema.get("function", schema).get("name")
    ]


def _build_executed_steps(state: TaskState) -> list[dict[str, Any]]:
    """Build Validator evidence from the Executor's persisted contracts only."""
    plan = state.active_plan
    if plan is None:
        return []

    raw_history = state.domain_state.get("stepExecutionResults", [])
    if not isinstance(raw_history, list):
        raise ValueError("TaskState.domainState.stepExecutionResults must be a list")
    history = [StepExecutionResult.model_validate(item) for item in raw_history]

    raw_outputs = state.domain_state.get("stepOutputs", {})
    if not isinstance(raw_outputs, dict):
        raise ValueError("TaskState.domainState.stepOutputs must be an object")

    outputs: dict[str, NormalizedStepOutput] = {}
    for step_id, raw_output in raw_outputs.items():
        output = NormalizedStepOutput.model_validate(raw_output)
        if output.step_id != step_id:
            raise ValueError("stepOutputs key does not match NormalizedStepOutput.stepId")
        outputs[step_id] = output

    steps: list[dict[str, Any]] = []
    for s in plan.steps:
        if s.status not in ("executed", "blocked", "failed"):
            continue
        result = next(
            (
                item
                for item in reversed(history)
                if item.task_id == state.task_id
                and item.plan_id == plan.plan_id
                and item.step_id == s.step_id
                and item.tool_name == s.tool_name
            ),
            None,
        )
        if result is None:
            continue
        detail = (
            result.tool_trace.detail
            if result.tool_trace is not None
            and isinstance(result.tool_trace.detail, dict)
            else {}
        )
        validator_evidence = _project_validator_tool_evidence(s.tool_name, detail)
        normalized = outputs.get(s.step_id)
        normalized_values=dict(normalized.values) if normalized is not None else {}
        if s.tool_name=='search_products' and any(step.expected_output.get('requiresEvidenceComparison') for step in plan.steps):
            from .product_knowledge.projection import pack_proof
            normalized_values=pack_proof(normalized_values)
        steps.append({
            "stepId": s.step_id,
            "toolName": s.tool_name,
            "outcome": result.outcome,
            "resolvedArguments": dict(result.resolved_arguments),
            "evidenceRefs": _collect_evidence_refs(validator_evidence),
            "detailKeys": list(validator_evidence.keys()),
            "evidenceValues": validator_evidence,
            "normalizedOutput": (
                normalized_values
            ),
        })
    return steps


def _collect_evidence_refs(value: Any) -> list[str]:
    """Collect stable evidence references from a validated tool payload."""
    refs: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            direct_refs = item.get("evidenceRefs")
            if isinstance(direct_refs, list):
                refs.extend(ref for ref in direct_refs if isinstance(ref, str))
            compact_refs = item.get("refs")
            if isinstance(compact_refs, list):
                refs.extend(ref for ref in compact_refs if isinstance(ref, str))
            ref = item.get("ref")
            if isinstance(ref, str):
                refs.append(ref)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return list(dict.fromkeys(refs))


def _build_final_answer_validated_results(
    state: TaskState,
    tool_traces: list[Any],
) -> list[dict[str, Any]]:
    """Extract validated results for FinalAnswerContextView from the state."""
    results: list[dict[str, Any]] = []
    guide = state.domain_state.get("shoppingGuide")
    if isinstance(guide, dict):
        products = guide.get("products", [])
        if isinstance(products, list):
            for p in products:
                if isinstance(p, dict):
                    results.append({
                        "product": p.get("name", ""),
                        "score": p.get("score"),
                    })
    return results


def _build_validated_results(
    state: TaskState,
    _tool_traces: list[Any],
) -> list[dict[str, Any]]:
    """Build FinalAnswer evidence from the persisted Validator whitelist.

    Accumulated ToolTrace objects are deliberately ignored: they may belong to
    a failed plan or a pre-replan attempt.  Only satisfied steps from the
    current, passed ValidatorResult may reach FinalAnswerContextView.
    """
    raw_validation = state.domain_state.get("validationResult")
    if not isinstance(raw_validation, dict):
        return []
    validation = ValidatorResult.model_validate(raw_validation)
    if validation.outcome != "passed":
        return []

    plan = state.active_plan
    if (
        validation.task_id != state.task_id
        or plan is None
        or validation.plan_id != plan.plan_id
        or plan.status != "completed"
        or validation.based_on_revision + 1 != state.revision
    ):
        raise ValueError("Validated result does not belong to the current TaskState")

    plan_steps = {item.step_id: item for item in plan.steps}
    raw_outputs = state.domain_state.get("stepOutputs", {})
    if not isinstance(raw_outputs, dict):
        raise ValueError("TaskState.domainState.stepOutputs must be an object")

    allowed_steps = {
        item.step_id: item
        for item in validation.step_results
        if item.outcome == "satisfied"
    }
    results: list[dict[str, Any]] = []
    for step_id, step_validation in allowed_steps.items():
        plan_step = plan_steps.get(step_id)
        if plan_step is None or plan_step.status != "executed":
            raise ValueError("Validated step does not belong to the completed Plan")

        summary = dict(step_validation.evidence_summary)
        evidence: dict[str, Any] = dict(summary)
        if plan_step.tool_name == "search_products":
            raw_output = raw_outputs.get(step_id)
            if not isinstance(raw_output, dict):
                raise ValueError("Validated search step is missing normalized output")
            output = NormalizedStepOutput.model_validate(raw_output)
            if (
                output.task_id != state.task_id
                or output.plan_id != plan.plan_id
                or output.step_id != step_id
            ):
                raise ValueError("Normalized search output identity mismatch")
            try:
                validate_normalized_output_values(plan_step.tool_name, output.values)
            except ExpectedOutputContractError as exc:
                raise ValueError(str(exc)) from exc
            candidate_summary = summary.get("requiresProductCandidates")
            expected_summary = {
                "candidatePoolCount": len(output.values["candidatePoolIds"]),
                "rankedItemCount": len(output.values["rankedItemIds"]),
                "candidatePoolIds": output.values["candidatePoolIds"],
                "rankedItemIds": output.values["rankedItemIds"],
                "productIds": output.values["productIds"],
                "evidenceRefs": output.values["evidenceRefs"],
                **output.values["candidateSupport"],
            }
            if candidate_summary != expected_summary:
                raise ValueError("Validated search summary differs from normalized output")
            evidence = dict(output.values)
            compact_support = dict(evidence["candidateSupport"])
            # Product presentations are already present in validationSummary,
            # which is the sole FinalAnswer/UI consumption path.  Do not copy
            # them into evidence a second time and waste the ContextView budget.
            compact_support.pop("productPresentations", None)
            evidence["candidateSupport"] = compact_support
        elif plan_step.tool_name == "rerank_products_in_scope":
            raw_output = raw_outputs.get(step_id)
            if not isinstance(raw_output, dict):
                raise ValueError("Validated rerank step is missing normalized output")
            output = NormalizedStepOutput.model_validate(raw_output)
            if (
                output.task_id != state.task_id
                or output.plan_id != plan.plan_id
                or output.step_id != step_id
            ):
                raise ValueError("Normalized rerank output identity mismatch")
            try:
                rerank_output = normalize_persisted_scope_rerank_values(
                    output.values
                )
            except TwoStageRankingContractError as exc:
                raise ValueError(str(exc)) from exc
            rerank_summary = summary.get("requiresScopeRerank")
            expected_summary = {
                "scopeId": rerank_output.scope_id,
                "inputCount": len(rerank_output.input_product_ids),
                "outputCount": len(rerank_output.ranked_item_ids),
                "rankedItemIds": list(rerank_output.ranked_item_ids),
                "productIds": list(rerank_output.ranked_item_ids),
                "evidenceRefs": list(rerank_output.evidence_refs),
                **rerank_output.candidate_support,
            }
            if rerank_summary != expected_summary:
                raise ValueError("Validated rerank summary differs from normalized output")
            evidence = dict(output.values)
            compact_support = dict(evidence["candidateSupport"])
            compact_support.pop("productPresentations", None)
            evidence["candidateSupport"] = compact_support
        elif plan_step.expected_output.get("requiresEvidenceComparison") is True:
            evidence = summary.get("requiresEvidenceComparison")
            if not isinstance(evidence, dict):
                raise ValueError("Validated evidence comparison summary is missing")
            # The checked body appears once in the parent view. Full Validator
            # receipt remains persisted; this is only its presentation header.
            summary = {"requiresEvidenceComparison": {
                "contractVersion": evidence["contractVersion"],
                "productIds": evidence["productIds"],
            }}
        elif plan_step.expected_output.get("requiresProductEvidence") is True:
            evidence = summary.get("requiresProductEvidence")
            if not isinstance(evidence, dict):
                raise ValueError("Validated product evidence summary is missing")
        elif plan_step.tool_name == "compare_products":
            guide_summary = summary.get("requiresGuideDecision")
            if not isinstance(guide_summary, dict):
                raise ValueError("Validated comparison summary is missing")
            evidence = {
                "rankedFinalists": guide_summary.get("rankedFinalists", []),
                "hasCompleteMatch": guide_summary.get("hasCompleteMatch"),
            }
        results.append({
            "stepId": step_id,
            "tool": plan_step.tool_name,
            "evidence": evidence,
            "evidenceRefs": _collect_evidence_refs(summary),
            "validationSummary": summary,
        })
    return results


def _build_validated_scope_results(
    state: TaskState,
    answer_context_ref: str | None,
) -> list[dict[str, Any]]:
    """Re-project a still-current CandidateScope from its original whitelist.

    A later read-only turn may clear ``activePlan`` while retaining the exact
    Validator result, normalized output and CandidateScope identities.  This
    path accepts that persisted evidence only when every source identity and
    current shopping constraint still matches; it never trusts raw ToolTrace.
    """

    raw_scope = state.domain_state.get("candidateScope")
    raw_guide = state.domain_state.get("shoppingGuide")
    if not all(isinstance(item, dict) for item in (raw_scope, raw_guide)):
        raise ValueError("Validated scope evidence is incomplete")
    scope = CandidateScope.model_validate(raw_scope)
    guide = ShoppingGuideState.model_validate(raw_guide)
    expected_ref = f"validated-scope:{scope.scope_id}"
    if answer_context_ref != expected_ref:
        raise ValueError("Answer reference does not identify CandidateScope")
    if scope.task_id != state.task_id or scope.status != "active":
        raise ValueError("CandidateScope does not belong to active task")
    current_requirements = [
        item.model_dump(by_alias=True, mode="json")
        for item in compiled_shopping_requirements(guide)
    ]
    scope_requirements = [
        item.model_dump(by_alias=True, mode="json")
        for item in scope.requirements_snapshot
    ]
    current_avoidances = [
        item.model_dump(by_alias=True, mode="json")
        for item in guide.brand_avoidances
    ]
    scope_avoidances = [
        item.model_dump(by_alias=True, mode="json")
        for item in scope.brand_avoidances_snapshot
    ]
    if (
        scope.category != guide.category
        or scope_requirements != current_requirements
        or scope_avoidances != current_avoidances
    ):
        raise ValueError("CandidateScope no longer matches current constraints")

    # ReAct deliberately reuses one generic step ID and replaces ``stepOutputs``
    # on each Plan.  Therefore the current one-slot projection may belong to a
    # later comparison even while the original CandidateScope remains active.
    # Recover the source output from the append-only Executor receipt whose full
    # task/plan/step/tool identity is bound by CandidateScope, then re-run the
    # ranking contract over its raw tool observation.  A matching legacy
    # ``stepOutputs`` record remains a compatibility fallback only.
    output: NormalizedStepOutput | None = None
    raw_history = state.domain_state.get("stepExecutionResults", [])
    if not isinstance(raw_history, list):
        raise ValueError("CandidateScope execution history is invalid")
    source_executions: list[StepExecutionResult] = []
    try:
        for raw_execution in raw_history:
            execution = StepExecutionResult.model_validate(raw_execution)
            if (
                execution.task_id == state.task_id
                and execution.plan_id == scope.source_plan_id
                and execution.step_id == scope.source_step_id
                and execution.tool_name == "search_products"
                and execution.outcome == "tool_succeeded"
            ):
                source_executions.append(execution)
    except ValueError as exc:
        raise ValueError("CandidateScope execution history is invalid") from exc
    if source_executions:
        reconstructed_values: dict[str, Any] | None = None
        for execution in source_executions:
            detail = (
                execution.tool_trace.detail
                if execution.tool_trace is not None
                else None
            )
            try:
                normalized, _ = project_validated_search_product_presentations(
                    detail,
                    requirements=execution.resolved_arguments.get("requirements"),
                    category=execution.resolved_arguments.get("category"),
                )
            except TwoStageRankingContractError as exc:
                raise ValueError(
                    "CandidateScope source execution is invalid"
                ) from exc
            values = normalized.normalized_values()
            if reconstructed_values is not None and values != reconstructed_values:
                raise ValueError("CandidateScope source executions conflict")
            reconstructed_values = values
        output = NormalizedStepOutput(
            taskId=state.task_id,
            planId=scope.source_plan_id,
            stepId=scope.source_step_id,
            values=reconstructed_values,
        )
    else:
        raw_outputs = state.domain_state.get("stepOutputs")
        raw_output = (
            raw_outputs.get(scope.source_step_id)
            if isinstance(raw_outputs, dict)
            else None
        )
        if isinstance(raw_output, dict):
            candidate = NormalizedStepOutput.model_validate(raw_output)
            if (
                candidate.task_id == state.task_id
                and candidate.plan_id == scope.source_plan_id
                and candidate.step_id == scope.source_step_id
            ):
                output = candidate
    if output is None:
        raise ValueError("CandidateScope normalized output is missing")
    try:
        validate_normalized_output_values("search_products", output.values)
    except ExpectedOutputContractError as exc:
        raise ValueError(str(exc)) from exc
    if (
        list(output.values["candidatePoolIds"]) != list(scope.candidate_pool_ids)
        or list(output.values["rankedItemIds"]) != list(scope.ranked_item_ids)
        or list(output.values["evidenceRefs"]) != list(scope.evidence_refs)
    ):
        raise ValueError("CandidateScope differs from normalized search output")

    expected_summary = {
        "candidatePoolCount": len(output.values["candidatePoolIds"]),
        "rankedItemCount": len(output.values["rankedItemIds"]),
        "candidatePoolIds": output.values["candidatePoolIds"],
        "rankedItemIds": output.values["rankedItemIds"],
        "productIds": output.values["productIds"],
        "evidenceRefs": output.values["evidenceRefs"],
        **output.values["candidateSupport"],
    }
    summary = {"requiresProductCandidates": expected_summary}

    # ``validationResult`` belongs to the current turn.  A later comparison
    # legitimately replaces it while the CandidateScope remains active.  Only
    # when the current receipt still claims to be the scope's source do we use
    # it as source proof and require byte-equivalent normalized evidence.  For
    # later Plan identities, the immutable server-owned CandidateScope plus its
    # original Executor-owned normalized output is the stable scope proof.
    raw_validation = state.domain_state.get("validationResult")
    if isinstance(raw_validation, dict) and (
        raw_validation.get("planId") == scope.source_plan_id
        or raw_validation.get("basedOnRevision") == scope.source_revision
    ):
        try:
            validation = ValidatorResult.model_validate(raw_validation)
        except ValueError as exc:
            raise ValueError("CandidateScope source Validator receipt invalid") from exc
        if (
            validation.outcome != "passed"
            or validation.task_id != state.task_id
            or validation.plan_id != scope.source_plan_id
            or validation.based_on_revision != scope.source_revision
        ):
            raise ValueError("CandidateScope Validator identity mismatch")
        step_validation = next(
            (
                item for item in validation.step_results
                if item.step_id == scope.source_step_id
                and item.outcome == "satisfied"
            ),
            None,
        )
        if step_validation is None:
            raise ValueError("CandidateScope source step was not validated")
        summary = dict(step_validation.evidence_summary)
        if summary.get("requiresProductCandidates") != expected_summary:
            raise ValueError("CandidateScope validation summary mismatch")
    evidence = dict(output.values)
    compact_support = dict(evidence["candidateSupport"])
    compact_support.pop("productPresentations", None)
    evidence["candidateSupport"] = compact_support
    return [{
        "stepId": scope.source_step_id,
        "tool": "search_products",
        "evidence": evidence,
        "evidenceRefs": _collect_evidence_refs(summary),
        "validationSummary": summary,
    }]


def _build_persisted_comparison_results(
    state: TaskState,
    answer_context_ref: str | None,
) -> list[dict[str, Any]] | None:
    """Project the last validated comparison after its Plan is retired.

    A presentation-only follow-up such as ``根据已有属性告诉我怎么选`` must
    reuse the exact compared subset, not silently widen back to all Top50
    candidates.  The comparison receipt is accepted only while the original
    CandidateScope and shopping constraints remain active and while the
    Validator summary, normalized output and guide selection agree exactly.
    """

    raw_scope = state.domain_state.get("candidateScope")
    raw_guide = state.domain_state.get("shoppingGuide")
    raw_validation = state.domain_state.get("validationResult")
    raw_outputs = state.domain_state.get("stepOutputs")
    if not all(isinstance(item, dict) for item in (
        raw_scope, raw_guide, raw_validation, raw_outputs,
    )):
        return None
    scope = CandidateScope.model_validate(raw_scope)
    guide = ShoppingGuideState.model_validate(raw_guide)
    if guide.mode != "compare" or len(guide.compared_ids) not in {2, 3}:
        return None
    if answer_context_ref != f"validated-scope:{scope.scope_id}":
        raise ValueError("Answer reference does not identify comparison scope")
    if scope.task_id != state.task_id or scope.status != "active":
        raise ValueError("Comparison CandidateScope is not active")
    if (
        scope.category != guide.category
        or list(scope.requirements_snapshot) != list(
            compiled_shopping_requirements(guide)
        )
        or list(scope.brand_avoidances_snapshot) != list(guide.brand_avoidances)
    ):
        raise ValueError("Comparison no longer matches current constraints")
    validation = ValidatorResult.model_validate(raw_validation)
    if (
        validation.outcome != "passed"
        or validation.task_id != state.task_id
        or validation.based_on_revision >= state.revision
        or len(validation.step_results) != 1
    ):
        raise ValueError("Persisted comparison Validator identity mismatch")
    step_validation = validation.step_results[0]
    if (
        step_validation.outcome != "satisfied"
        or step_validation.expected_output != {"requiresGuideDecision": True}
    ):
        raise ValueError("Persisted comparison was not validated")
    raw_output = raw_outputs.get(step_validation.step_id)
    if not isinstance(raw_output, dict):
        raise ValueError("Persisted comparison normalized output is missing")
    output = NormalizedStepOutput.model_validate(raw_output)
    if (
        output.task_id != state.task_id
        or output.plan_id != validation.plan_id
        or output.step_id != step_validation.step_id
    ):
        raise ValueError("Persisted comparison normalized identity mismatch")
    try:
        validate_normalized_output_values("compare_products", output.values)
    except ExpectedOutputContractError as exc:
        raise ValueError(str(exc)) from exc
    summary = dict(step_validation.evidence_summary)
    guide_summary = summary.get("requiresGuideDecision")
    if not isinstance(guide_summary, dict):
        raise ValueError("Persisted comparison summary is missing")
    product_ids = output.values.get("productIds")
    finalist_ids = guide_summary.get("finalistIds")
    ranked_finalists = guide_summary.get("rankedFinalists")
    ranked_ids = (
        [item.get("productId") for item in ranked_finalists]
        if isinstance(ranked_finalists, list)
        and all(isinstance(item, dict) for item in ranked_finalists)
        else None
    )
    if (
        product_ids != list(guide.compared_ids)
        or finalist_ids != product_ids
        or ranked_ids != product_ids
        or len(set(product_ids)) != len(product_ids)
        or not set(product_ids).issubset(scope.ranked_item_ids)
    ):
        raise ValueError("Persisted comparison selection mismatch")
    return [{
        "stepId": output.step_id,
        "tool": "compare_products",
        "evidence": {
            "rankedFinalists": ranked_finalists,
            "hasCompleteMatch": guide_summary.get("hasCompleteMatch"),
        },
        "evidenceRefs": _collect_evidence_refs(summary),
        "validationSummary": summary,
    }]


def _guide_product_from_presentation(card: object) -> dict[str, Any] | None:
    if not isinstance(card, dict):
        return None
    attributes = card.get("attributes", [])
    if not isinstance(attributes, list):
        return None
    product_id = card.get("productId")
    if type(product_id) not in {int, str}:
        return None
    return {
        "product": {
            # JSON numbers above 2**53 lose precision in the browser.  Product
            # identity is opaque, so publish it as text at the UI boundary.
            "id": str(product_id),
            "title": card.get("title"),
            "brand": card.get("brand"),
            "snapshotPriceMinor": (
                card.get("priceMinor")
                if card.get("priceStatus") == "verified" else None
            ),
            "syntheticReferencePriceMinor": (
                card.get("priceMinor")
                if card.get("priceStatus") == "synthetic" else None
            ),
            "currency": card.get("currency"),
            "priceStatus": card.get("priceStatus"),
            "priceDataNature": card.get("priceDataNature"),
            "pricePolicy": card.get("pricePolicy"),
            "priceDisclosure": card.get("priceDisclosure"),
        },
        "attributes": attributes,
        "selectionType": card.get("selectionType"),
        "evidenceRefs": [
            ref for ref in (
                card.get("titleEvidenceRef"),
                card.get("brandEvidenceRef"),
                card.get("priceEvidenceRef"),
                *(
                    item.get("evidenceRef")
                    for item in attributes
                    if isinstance(item, dict)
                ),
            )
            if isinstance(ref, str)
        ],
    }


def _expanded_search_presentations(
    state: TaskState,
    result: dict[str, Any],
    candidate_summary: dict[str, Any],
    compact_presentations: list[Any],
) -> list[dict[str, Any]]:
    """Revalidate the persisted raw receipt and project all ranked UI cards."""

    plan = state.active_plan
    step_id = result.get("stepId")
    raw_history = state.domain_state.get("stepExecutionResults", [])
    if plan is None or not isinstance(step_id, str) or not isinstance(raw_history, list):
        return []
    try:
        history = [StepExecutionResult.model_validate(item) for item in raw_history]
    except ValueError:
        return []
    execution = next((
        item for item in reversed(history)
        if item.task_id == state.task_id
        and item.plan_id == plan.plan_id
        and item.step_id == step_id
        and item.tool_name == "search_products"
        and item.outcome == "tool_succeeded"
    ), None)
    if (
        execution is None
        or execution.tool_trace is None
        or not execution.tool_trace.ok
        or not isinstance(execution.tool_trace.detail, dict)
    ):
        return []
    try:
        normalized, expanded = project_validated_search_product_presentations(
            execution.tool_trace.detail,
            requirements=execution.resolved_arguments.get("requirements"),
            category=execution.resolved_arguments.get("category"),
        )
    except TwoStageRankingContractError:
        return []
    expected_summary = {
        "candidatePoolCount": len(normalized.candidate_pool_ids),
        "rankedItemCount": len(normalized.ranked_item_ids),
        "candidatePoolIds": list(normalized.candidate_pool_ids),
        "rankedItemIds": list(normalized.ranked_item_ids),
        "productIds": list(normalized.ranked_item_ids),
        "evidenceRefs": list(normalized.evidence_refs),
        **normalized.candidate_support,
    }
    if expected_summary != candidate_summary:
        return []
    if expanded[:len(compact_presentations)] != compact_presentations:
        return []
    return expanded


def _memory_reranked_presentations(
    state: TaskState,
    result: dict[str, Any],
    candidate_summary: dict[str, Any],
    compact_presentations: list[dict[str, Any]],
    *,
    memory_run_binding: Any | None,
    memory_rerank_weight: float,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    expanded = _expanded_search_presentations(
        state, result, candidate_summary, compact_presentations,
    )
    if not expanded or memory_run_binding is None:
        return expanded, None
    try:
        if memory_run_binding.payload_for_phase("final_answer") is None:
            return expanded, None
    except (AttributeError, ValueError):
        return expanded, None
    guide = state.domain_state.get("shoppingGuide")
    category = guide.get("category") if isinstance(guide, dict) else None
    if category != getattr(memory_run_binding, "category_id", None):
        return expanded, None
    from .domains.ecommerce.shopping_task_state_v2 import canonical_requirement_key
    from .memory.v3_runtime import rerank_product_presentations

    suppressed: set[str] = set()
    for requirement in guide.get("requirements", []):
        if not isinstance(requirement, dict):
            continue
        try:
            suppressed.add(canonical_requirement_key(requirement.get("key")))
        except ValueError:
            return expanded, None
    try:
        reranked, receipt = rerank_product_presentations(
            memory_run_binding,
            expanded,
            weight=memory_rerank_weight,
            suppressed_attribute_keys=frozenset(suppressed),
        )
    except (TypeError, ValueError):
        return expanded, None
    if receipt["inputProductIds"] == receipt["outputProductIds"]:
        return expanded, None
    return reranked, receipt


def apply_memory_rerank_to_validated_results(
    state: TaskState,
    validated_results: list[dict[str, Any]],
    *,
    memory_run_binding: Any | None,
    memory_rerank_weight: float,
) -> list[dict[str, Any]]:
    """Return a presentation-only copy; Validator/TaskState remain untouched."""
    copied = deepcopy(validated_results)
    for result in copied:
        if result.get("tool") != "search_products":
            continue
        summary = result.get("validationSummary")
        candidate_summary = (
            summary.get("requiresProductCandidates")
            if isinstance(summary, dict) else None
        )
        compact = (
            candidate_summary.get("productPresentations")
            if isinstance(candidate_summary, dict) else None
        )
        if not isinstance(compact, list) or not compact:
            continue
        reranked, receipt = _memory_reranked_presentations(
            state, result, candidate_summary, compact,
            memory_run_binding=memory_run_binding,
            memory_rerank_weight=memory_rerank_weight,
        )
        if receipt is None:
            continue
        candidate_summary["productPresentations"] = reranked[:3]
        result["memoryOrderAdjusted"] = True
    return copied


def build_validated_guide_result(
    state: TaskState | None,
    *,
    memory_run_binding: Any | None = None,
    memory_rerank_weight: float = 0.01,
) -> dict[str, Any] | None:
    """Build the browser product view only from the passed Validator boundary.

    The default cards come from the compact Validator summary.  For the
    optional browser-only expansion, the persisted raw receipt is fully
    revalidated and must reproduce that exact compact summary before any
    additional ranked presentation is exposed.  It never enters model context.
    """

    if state is None or state.task_type != "ecommerce_guide":
        return None
    try:
        raw_scope = state.domain_state.get("candidateScope")
        scope_id = (
            raw_scope.get("scopeId")
            if isinstance(raw_scope, dict)
            else None
        )
        if state.active_plan is None:
            if not isinstance(scope_id, str) or not scope_id:
                return None
            results = _build_validated_scope_results(
                state,
                f"validated-scope:{scope_id}",
            )
        else:
            try:
                results = _build_validated_results(state, [])
            except (ValueError, TypeError):
                # Final-answer publication advances the task revision while the
                # terminal Plan may still be retained for observability.  The
                # Plan-bound projection is then intentionally stale, but the
                # exact Validator-owned CandidateScope remains reusable when
                # all scope/source/constraint identities still match.
                if not isinstance(scope_id, str) or not scope_id:
                    raise
                results = _build_validated_scope_results(
                    state,
                    f"validated-scope:{scope_id}",
                )
    except (ValueError, TypeError):
        return None
    if (any(r.get('evidence',{}).get('contractVersion')=='product-evidence-comparison-v1' for r in results)
            and not any(r.get('tool')=='search_products' for r in results)
            and isinstance(scope_id,str)):
        try:
            # Recommendations do not silently renumber the existing cards.
            # Recover their exact search receipt through the existing guard.
            results = [*_build_validated_scope_results(state,f'validated-scope:{scope_id}'),*results]
        except (ValueError,TypeError):
            return None
    for result in reversed(results):
        if result.get("tool") == "rerank_products_in_scope":
            summary = result.get("validationSummary")
            rerank_summary = (
                summary.get("requiresScopeRerank")
                if isinstance(summary, dict) else None
            )
            presentations = (
                rerank_summary.get("productPresentations")
                if isinstance(rerank_summary, dict) else None
            )
            if not isinstance(presentations, list) or not presentations:
                continue
            products = [
                product
                for card in presentations[:3]
                if (product := _guide_product_from_presentation(card)) is not None
            ]
            if len(products) != min(3, len(presentations)):
                return None
            return {
                "contractVersion": "validated-product-presentation-v1",
                "category": "phone",
                "products": products,
                "hasCompleteMatch": rerank_summary.get("hasCompleteMatch"),
                "rankedItemCount": rerank_summary.get("outputCount"),
                "snapshotNotice": (
                    "仅按上一轮候选的商品标题/公开文本与排序意图相关性重排；"
                    "不代表真实相机、性能等能力结论。"
                ),
            }
        if result.get("tool") == "search_products":
            summary = result.get("validationSummary")
            candidate_summary = (
                summary.get("requiresProductCandidates")
                if isinstance(summary, dict)
                else None
            )
            presentations = (
                candidate_summary.get("productPresentations")
                if isinstance(candidate_summary, dict)
                else None
            )
            if not isinstance(presentations, list) or not presentations:
                continue
            products = [
                product
                for card in presentations[:3]
                if (product := _guide_product_from_presentation(card)) is not None
            ]
            if len(products) != min(3, len(presentations)):
                return None
            expanded_presentations, memory_receipt = _memory_reranked_presentations(
                state,
                result,
                candidate_summary,
                presentations,
                memory_run_binding=memory_run_binding,
                memory_rerank_weight=memory_rerank_weight,
            )
            expanded_products = [
                product
                for card in expanded_presentations
                if (product := _guide_product_from_presentation(card)) is not None
            ]
            guide_result = {
                "contractVersion": "validated-product-presentation-v1",
                "category": "phone",
                "products": products,
                "hasCompleteMatch": candidate_summary.get("hasCompleteMatch"),
                "rankedItemCount": candidate_summary.get("rankedItemCount"),
                "snapshotNotice": (
                    "冻结的历史公开商品快照；模拟参考价为 AI 合成，非真实报价。"
                    "仅在显式 policy 下参与预算判断，不代表实时价格或库存。"
                ),
            }
            if len(expanded_products) > len(products):
                guide_result["expandedProducts"] = expanded_products
            if memory_receipt is not None:
                guide_result["products"] = expanded_products[:3]
                guide_result["memoryRerank"] = {
                    "applied": True,
                    "orderChanged": True,
                    "filteredProductCount": 0,
                }
            return guide_result
        if result.get("tool") == "compare_products":
            evidence = result.get("evidence")
            finalists = (
                evidence.get("rankedFinalists")
                if isinstance(evidence, dict)
                else None
            )
            if not isinstance(finalists, list) or not finalists:
                continue
            products = []
            for finalist in finalists[:3]:
                if not isinstance(finalist, dict):
                    return None
                product_id = finalist.get("productId")
                if type(product_id) is not int:
                    return None
                field_evidence = finalist.get("fieldEvidence")
                if not isinstance(field_evidence, list):
                    return None
                attributes = []
                for field in field_evidence:
                    if not isinstance(field, dict):
                        return None
                    key = field.get("key")
                    status = field.get("status")
                    actual = field.get("actual")
                    if not isinstance(key, str) or status not in {
                        "known", "unknown", "conflict",
                    }:
                        return None
                    attributes.append({
                        "key": key,
                        "status": status,
                        "value": actual if isinstance(actual, str) else None,
                        "evidenceRef": field.get("evidenceRef"),
                    })
                checks = finalist.get("checks")
                price_check = next((
                    item for item in checks
                    if isinstance(item, dict)
                    and item.get("key") == "price_minor"
                    and item.get("status") in {"pass", "fail"}
                    and type(item.get("actual")) in {int, float}
                    and isinstance(item.get("evidenceRef"), str)
                ), None) if isinstance(checks, list) else None
                price_ref = price_check.get("evidenceRef") if price_check else None
                price_minor = int(price_check["actual"]) if price_check else None
                synthetic_price = (
                    price_minor
                    if isinstance(price_ref, str)
                    and price_ref.endswith(":syntheticReferencePriceMinor")
                    else None
                )
                snapshot_price = (
                    price_minor
                    if isinstance(price_ref, str)
                    and price_ref.endswith(":snapshotPriceMinor")
                    else None
                )
                products.append({
                    "product": {
                        "id": str(product_id),
                        "title": finalist.get("title") or f"商品 {product_id}",
                        "brand": finalist.get("brand"),
                        "snapshotPriceMinor": snapshot_price,
                        "syntheticReferencePriceMinor": synthetic_price,
                        "currency": "CNY" if price_minor is not None else None,
                        "priceStatus": (
                            "verified" if snapshot_price is not None
                            else "synthetic" if synthetic_price is not None
                            else "unverified"
                        ),
                    },
                    # Comparison cards consume the same attribute shape as
                    # search cards.  These values remain Validator-owned: the
                    # source fieldEvidence was rebuilt from controlled product
                    # evidence and passed the active task/plan/step checks.
                    "attributes": attributes,
                    "selectionType": "validated_comparison",
                })
            return {
                "contractVersion": "validated-product-presentation-v1",
                "category": "phone",
                "products": products,
                "hasCompleteMatch": evidence.get("hasCompleteMatch"),
                "snapshotNotice": "冻结的历史公开商品快照，不代表实时价格或库存。",
            }
    return None


def _project_validated_tool_evidence(
    tool_name: str,
    detail: Any,
) -> dict[str, Any]:
    """Project only the fact fields that a satisfied tool contract may expose.

    A satisfied search step proves candidate identity, not every unverified field
    present in its raw ranking payload.  Search bodies therefore stay out of the
    final model input.  Authority/detail tools expose only their contract fields.
    """
    if not isinstance(detail, dict):
        return {}
    allowed_keys = validated_evidence_fields_for_tool(tool_name)
    projected = {
        key: detail[key]
        for key in allowed_keys
        if key in detail
    }
    if tool_name == "compare_products" and detail.get("contractVersion") != "product-evidence-comparison-v1":
        projected["products"] = _compact_compare_products(
            projected.get("products")
        )
        projected["evidence"] = _compact_compare_evidence(
            projected.get("evidence"), projected.get("products")
        )
        # The top-level list is a byte-for-byte duplicate of the refs already
        # carried by the compact evidence groups.  ValidatorContextView also
        # publishes its own collected ref list, so retaining this third copy
        # spends protected context without adding a proof obligation.
        projected.pop("evidenceRefs", None)
    return projected


def _project_validator_tool_evidence(
    tool_name: str,
    detail: Any,
) -> dict[str, Any]:
    """Project the minimum evidence needed by the deterministic Validator.

    ``search_products`` is validated from the Executor-owned normalized
    two-stage output.  Recopying its complete candidate rows and evidence into
    ValidatorContextView adds no validation proof and can exceed the protected
    phase budget for a legitimate 50-item pool.  The raw result is still
    contract-checked before persistence and remains available to the
    post-validation final-answer projection.
    """

    if detail.get('contractVersion') == 'product-evidence-comparison-v1':
        from .product_knowledge.projection import pack_proof
        return pack_proof(detail)
    if tool_name == "search_products":
        return {}
    if tool_name == "rerank_products_in_scope":
        # Rerank proof is the compact scope identity + ranked order carried by
        # the normalized output (normalize_persisted_scope_rerank_values), never
        # the full candidate rows.  Recopying `candidates` and `evidence` into
        # ValidatorContextView adds no validation proof and, stacked on top of
        # the search step already in scope, blows the protected phase budget.
        # NOTE: `evidenceRefs` is deliberately NOT projected here.  The raw
        # rerank detail carries one evidence ref per supporting fact (256 for a
        # 20-item scope).  The rerank proof lives in the normalized output's own
        # compact ref list (normalize_persisted_scope_rerank_values), and
        # `_collect_evidence_refs` below recopies the raw list into BOTH the
        # step's evidenceRefs AND the view's top-level evidenceRefs — three
        # copies of ~3.3k tokens stacked on the search step already in scope.
        # Dropping it mirrors compare_products' precedent at the line above.
        return {
            key: detail[key]
            for key in (
                "contractVersion", "scopeId", "inputProductIds", "rankedItemIds",
                "productIds", "rankingSignal", "degraded", "noFullSearch",
                "citationTrace", "rankingTrace", "eliminated",
            )
            if key in detail
        }
    return _project_validated_tool_evidence(tool_name, detail)


def _compact_compare_products(value: Any) -> list[dict[str, Any]]:
    """Keep the complete Validator proof without duplicated display payload."""

    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    row_fields = (
        "checks", "hardFailures", "hardUnknowns", "softScore",
        "softPreferenceScore", "budgetSortPriceMinor", "fullyMatched",
        "selectionType", "scoreBreakdown",
    )
    for row in value:
        product = row.get("product") if isinstance(row, dict) else None
        if not isinstance(product, dict):
            result.append({})
            continue
        compact_attributes: list[dict[str, Any]] = []
        for attribute in product.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            compact = {
                key: attribute[key]
                for key in (
                    "key", "normalizedNumber", "normalizedBoolean",
                    "normalizedText", "evidenceField", "extractionMethod",
                )
                if key in attribute and attribute[key] is not None
            }
            # Known values are already bound to the canonical evidence record;
            # retain rawValue only for unknown/conflict proof with no ref.
            if all(
                compact.get(key) is None
                for key in (
                    "normalizedNumber", "normalizedBoolean", "normalizedText",
                )
            ) and "rawValue" in attribute:
                compact["rawValue"] = attribute["rawValue"]
            compact_attributes.append(compact)
        compact_row = {
            key: row[key] for key in row_fields if key in row
        }
        compact_row["product"] = {
            "id": product.get("id"),
            "title": product.get("title"),
            "brand": product.get("brand"),
            "priceStatus": product.get("priceStatus"),
            "attributes": compact_attributes,
        }
        result.append(compact_row)
    return result


def _compact_compare_evidence(
    value: Any,
    products: Any,
) -> list[dict[str, Any]]:
    """Group identical controlled-attribute proof bodies by product.

    The Java comparison tool emits one evidence record per controlled field.
    All seven records for a product intentionally share the same immutable
    ``relevance.attr_value`` body, so copying that body seven times can exceed
    the protected Validator budget.  This representation removes only that
    duplication: every original ref remains explicit and Validator expands the
    groups before performing the existing ref/field/value checks.
    """

    if not isinstance(value, list):
        return []
    required_refs = {
        check.get("evidenceRef")
        for row in (products if isinstance(products, list) else [])
        for check in (row.get("checks") or []) if isinstance(row, dict)
        if isinstance(check, dict) and isinstance(check.get("evidenceRef"), str)
    }
    for row in products if isinstance(products, list) else []:
        product = row.get("product") if isinstance(row, dict) else None
        product_id = product.get("id") if isinstance(product, dict) else None
        if type(product_id) is int:
            required_refs.update({
                f"product:{product_id}:title",
                f"product:{product_id}:brand",
            })
    result: list[dict[str, Any]] = []
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        if (
            not isinstance(ref, str)
            or not isinstance(item.get("rawValue"), str)
        ):
            if ref in required_refs:
                result.append(dict(item))
            continue
        if ":attribute:" not in ref and ref not in required_refs:
            continue
        identity = (
            item.get("field"), item.get("method"), item.get("rawValue"),
            item.get("confidence"), item.get("source"), item.get("provenanceUrl"),
        )
        group = groups.setdefault(identity, {
            "refs": [],
            **{
                key: item[key]
                for key in (
                    "field", "method", "rawValue", "confidence",
                    "source", "provenanceUrl",
                )
                if key in item
            },
        })
        if ref not in group["refs"]:
            group["refs"].append(ref)
    result.extend(groups.values())
    return result


def _build_validated_evidence_refs(
    validated_results: list[dict[str, Any]],
) -> list[str]:
    """Return only references attached to the current validated result set."""
    refs: list[str] = []
    for result in validated_results:
        result_refs = result.get("evidenceRefs")
        if isinstance(result_refs, list):
            refs.extend(ref for ref in result_refs if isinstance(ref, str))
    return list(dict.fromkeys(refs))


def _extract_tool_schema_dict(
    tool_name: str,
    candidate_tool_schemas: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find a tool's full schema from the candidate list."""
    for schema in candidate_tool_schemas:
        fn = schema.get("function", schema)
        name = fn.get("name", "")
        if name == tool_name:
            return fn
    return None


def _extract_fact_keys_for_step(step: Any) -> list[str]:
    """Extract fact keys referenced by this plan step's argument sources."""
    keys: list[str] = []
    for source in getattr(step, "argument_sources", {}).values():
        if getattr(source, "kind", None) == "task_state":
            ref = getattr(source, "reference", None)
            if ref and ref.startswith("facts."):
                keys.append(ref.split(".", 1)[1])
    return keys


def _extract_constraint_keys_for_step(step: Any) -> list[str]:
    """Extract constraint keys referenced by this plan step's argument sources."""
    keys: list[str] = []
    for source in getattr(step, "argument_sources", {}).values():
        if getattr(source, "kind", None) == "task_state":
            ref = getattr(source, "reference", None)
            if ref and ref.startswith("constraints."):
                keys.append(ref.split(".", 1)[1])
    return keys


def _project_step_arguments(step: Any, prior_outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Resolve projected prior fields; Executor independently rechecks provenance."""
    from copy import deepcopy
    arguments = deepcopy(step.arguments)
    for name, source in step.argument_sources.items():
        if source.kind == 'prior_step':
            source_step, field = source.reference.split('.', 1)
            arguments[name] = deepcopy(prior_outputs[source_step][field])
    return arguments


def _project_prior_step_outputs_for_step(
    state: TaskState,
    plan: TaskPlan,
    step: Any,
) -> dict[str, dict[str, Any]]:
    """Project only the exact prior-step fields referenced by this step."""

    requested: dict[str, set[str]] = {}
    for source in getattr(step, "argument_sources", {}).values():
        if getattr(source, "kind", None) != "prior_step":
            continue
        reference = getattr(source, "reference", None)
        if not isinstance(reference, str) or "." not in reference:
            continue
        source_step_id, output_field = reference.split(".", 1)
        requested.setdefault(source_step_id, set()).add(output_field)

    # Unreferenced historical outputs are outside this step's ContextView.
    # In particular, a completed Plan may leave a strictly validated output in
    # TaskState while the next user turn creates a new, unrelated Plan.  The
    # new step must neither copy nor validate data it did not reference.
    if not requested:
        return {}

    raw_outputs = state.domain_state.get("stepOutputs", {})
    if not isinstance(raw_outputs, dict):
        raise ValueError("TaskState.domainState.stepOutputs must be an object")
    projected: dict[str, dict[str, Any]] = {}
    for step_id, fields in requested.items():
        raw = raw_outputs.get(step_id)
        if not isinstance(raw, dict):
            raise ValueError(f"referenced prior step output is missing: {step_id}")
        normalized = NormalizedStepOutput.model_validate(raw)
        if (
            normalized.task_id != state.task_id
            or normalized.plan_id != plan.plan_id
            or normalized.step_id != step_id
        ):
            raise ValueError("Prior step output does not belong to active Plan")
        source_step = next(
            (item for item in plan.steps if item.step_id == step_id),
            None,
        )
        if source_step is None:
            raise ValueError("stepOutputs contains a step outside the active Plan")
        try:
            validate_normalized_output_values(
                source_step.tool_name,
                normalized.values,
            )
        except ExpectedOutputContractError as exc:
            raise ValueError(
                f"invalid normalized output for {step_id}: {exc.code}"
            ) from exc
        missing = sorted(field for field in fields if field not in normalized.values)
        if missing:
            raise ValueError(
                f"prior step output fields are missing for {step_id}: "
                + ", ".join(missing)
            )
        projected[step_id] = {
            field: deepcopy(normalized.values[field])
            for field in sorted(fields)
        }
    return projected


# ── Harness state machine helpers ───────────────────────────────────────────


def should_run_planner(state: TaskState) -> bool:
    return (
        state.status == "ready"
        and not state.pending_questions
        and state.active_plan is None
    )


async def run_planner_if_needed(
    state: TaskState,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    *,
    client: AsyncOpenAI,
    model: str,
    system_policies: dict[str, Any] | None = None,
    planner_view: PlannerContextView | None = None,
) -> tuple[PlannerResult | None, TaskState]:
    if not should_run_planner(state):
        return None, state
    return await run_planner_phase(
        state,
        user_message,
        candidate_tool_schemas,
        client=client,
        model=model,
        system_policies=system_policies,
        context_view=planner_view,
    )


def decide_after_planning(result: PlannerResult) -> HarnessPlanningAction:
    if result.outcome == "planned":
        return "continue_to_executor"
    if result.outcome == "needs_user_input":
        return "ask_user"
    return "stop_turn"


def _decide_when_planner_skipped(state: TaskState) -> HarnessPlanningAction:
    if state.pending_questions:
        return "ask_user"
    if state.status in {"ready", "executing"} and state.active_plan is not None:
        try:
            select_next_plan_step(state.active_plan)
        except ExecutorSelectionError as exc:
            if exc.code == "plan_has_no_pending_step":
                return "ready_for_validation"
            return "stop_turn"
        return "continue_to_executor"
    return "stop_turn"


def decide_after_execution(result: ExecutorRunResult) -> HarnessAction:
    if result.outcome != "step_executed":
        return "stop_turn"
    if result.task_state.active_plan is None:
        return "stop_turn"
    try:
        select_next_plan_step(result.task_state.active_plan)
    except ExecutorSelectionError as exc:
        if exc.code == "plan_has_no_pending_step":
            return "ready_for_validation"
        return "stop_turn"
    return "continue_to_executor"


def decide_after_validation(result: ValidatorResult) -> HarnessAction:
    if result.outcome == "passed":
        return "task_completed"
    if result.outcome == "insufficient_evidence":
        if result.error_code == "product_candidates_missing" and any(
            isinstance(summary, dict)
            and int(summary.get("candidatePoolCount", 0) or 0) > 0
            and int(summary.get("rankedItemCount", 0) or 0) == 0
            for step_result in result.step_results
            for summary in [
                step_result.evidence_summary.get("requiresProductCandidates")
            ]
        ):
            return "stop_turn"
        return "ready_for_replanning"
    return "stop_turn"


def decide_after_replanning(result: ReplannerResult) -> HarnessAction:
    if result.outcome == "replanned":
        return "continue_to_executor"
    if result.outcome == "needs_user_input":
        return "ask_user"
    return "stop_turn"


# ── Phase runners (with optional typed ContextView) ─────────────────────────


async def _run_replanning_step(
    state: TaskState,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    *,
    client: AsyncOpenAI,
    model: str,
    system_policies: dict[str, Any] | None = None,
    planner_result: PlannerResult | None = None,
    executor_result: ExecutorRunResult | None = None,
    validator_result: ValidatorResult | None = None,
    replanner_view: ReplannerContextView | None = None,
) -> HarnessStepResult:
    replanner_result, replanned_state = await run_replanner_phase(
        state,
        user_message,
        candidate_tool_schemas,
        client=client,
        model=model,
        system_policies=system_policies,
        context_view=replanner_view,
    )
    return HarnessStepResult(
        action=decide_after_replanning(replanner_result),
        planner_result=planner_result,
        executor_result=executor_result,
        validator_result=validator_result,
        replanner_result=replanner_result,
        task_state=replanned_state,
    )


async def _run_validation_step(
    planning_step: HarnessPlanningStepResult,
    executor_result: ExecutorRunResult | None = None,
    validator_view: ValidatorContextView | None = None,
) -> HarnessStepResult:
    state = (
        executor_result.task_state
        if executor_result is not None
        else planning_step.task_state
    )
    validator_result, validated_state = await run_validator_phase(
        state,
        context_view=validator_view,
    )
    return HarnessStepResult(
        action=decide_after_validation(validator_result),
        planner_result=planning_step.planner_result,
        executor_result=executor_result,
        validator_result=validator_result,
        task_state=validated_state,
    )


async def _run_validation_and_recovery(
    planning_step: HarnessPlanningStepResult,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    *,
    client: AsyncOpenAI,
    model: str,
    system_policies: dict[str, Any] | None = None,
    executor_result: ExecutorRunResult | None = None,
    validator_view: ValidatorContextView | None = None,
    projector: ContextProjector | None = None,
    trace_builder: TraceBuilder | None = None,
) -> HarnessStepResult:
    if trace_builder is not None:
        trace_builder.start_phase("validator")
        if validator_view is not None:
            _record_view(trace_builder, "validator", validator_view)
            trace_builder.record_phase_task_revision(
                "validator", validator_view.phase_task_revision
            )

    validated = await _run_validation_step(
        planning_step, executor_result, validator_view=validator_view
    )
    if trace_builder is not None:
        trace_builder.end_phase(
            "passed"
            if validated.action == "task_completed"
            else "insufficient_evidence"
            if validated.action == "ready_for_replanning"
            else "failed"
        )
    if validated.action != "ready_for_replanning":
        return validated

    replanner_view = None
    if projector is not None:
        max_attempts = int((system_policies or {}).get("maxReplanAttempts", 3))
        replanner_view = projector.replanner_view(
            failed_plan_summary=_make_plan_summary(validated.task_state),
            failed_plan=validated.task_state.active_plan,
            failure=(
                validated.validator_result.model_dump(by_alias=True, mode="json")
                if validated.validator_result is not None
                else {}
            ),
            failure_reason="Validator rejected: insufficient evidence",
            remaining_tool_names=_candidate_tool_names(candidate_tool_schemas),
            candidate_tool_schemas=candidate_tool_schemas,
            system_policies=system_policies,
            replan_attempt=int(
                validated.task_state.domain_state.get("replanAttemptCount", 0)
            ) + 1,
            max_replan_attempts=max_attempts,
            user_message=user_message,
            reusable_step_outputs=_reusable_step_output_payloads(
                validated.task_state
            ),
            phase_task_revision=validated.task_state.revision,
        )
        _validate_view_and_record(
            replanner_view,
            validated.task_state,
            trace_builder,
            "replanner",
            projector=projector,
        )
    if trace_builder is not None:
        trace_builder.start_phase("replanner")
        if replanner_view is not None:
            _record_view(trace_builder, "replanner", replanner_view)
            trace_builder.record_phase_task_revision(
                "replanner", replanner_view.phase_task_revision
            )

    replanned = await _run_replanning_step(
        validated.task_state,
        user_message,
        candidate_tool_schemas,
        client=client,
        model=model,
        system_policies=system_policies,
        planner_result=validated.planner_result,
        executor_result=validated.executor_result,
        validator_result=validated.validator_result,
        replanner_view=replanner_view,
    )
    if trace_builder is not None:
        trace_builder.end_phase(
            replanned.replanner_result.outcome
            if replanned.replanner_result is not None
            else "failed"
        )
    return replanned


async def run_planning_step(
    state: TaskState,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    *,
    client: AsyncOpenAI,
    model: str,
    system_policies: dict[str, Any] | None = None,
    planner_view: PlannerContextView | None = None,
) -> HarnessPlanningStepResult:
    """Run the Harness gate, optional Planner phase, and next-action decision."""

    planner_result, updated_state = await run_planner_if_needed(
        state,
        user_message,
        candidate_tool_schemas,
        client=client,
        model=model,
        system_policies=system_policies,
        planner_view=planner_view,
    )
    if planner_result is None:
        action = _decide_when_planner_skipped(updated_state)
    else:
        action = decide_after_planning(planner_result)

    return HarnessPlanningStepResult(
        action=action,
        planner_result=planner_result,
        task_state=updated_state,
    )


# ── Harness step driver ─────────────────────────────────────────────────────


def _record_view(
    trace_builder: TraceBuilder | None,
    view_type: str,
    view: Any,
) -> None:
    """Record a ContextView's hash and token estimate in the trace."""
    if trace_builder is None:
        return
    view_hash = getattr(view, "context_hash", None) or "unknown"
    try:
        raw = view.model_dump_json(by_alias=True) if hasattr(view, "model_dump_json") else str(view)
        token_count = len(raw) // 3 + 1
    except Exception:
        token_count = 0
    trace_builder.record_context_view(view_type, view_hash, token_count)


class HarnessPreValidationError(ValueError):
    """Raised when View/State control metadata mismatch — phase/tool/validator
    MUST NOT execute when this is raised."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _validate_view_and_record(
    view: Any,
    state: TaskState,
    trace_builder: TraceBuilder | None,
    phase: str,
    *,
    projector: ContextProjector | None = None,
    plan_id: str | None = None,
    step_id: str | None = None,
    tool_name: str | None = None,
) -> None:
    """Pre-validate view/state metadata and record any boundary mismatch in trace.

    When a mismatch is detected, the failure is recorded in the trace as a
    context_boundary_mismatch BEFORE re-raising — the phase, tool call, and
    Validator counts are all guaranteed to be 0 because the error is raised
    before any of them execute.
    """
    try:
        _validate_view_state_metadata(
            view, state,
            plan_id=plan_id, step_id=step_id, tool_name=tool_name,
        )
        if projector is not None and not projector.verifies(phase, view):
            raise HarnessPreValidationError(
                "view_context_hash_mismatch",
                f"{phase} View payload no longer matches its ContextPack hash",
            )
    except HarnessPreValidationError as e:
        if trace_builder is not None:
            trace_builder.record_context_boundary_mismatch(
                phase=phase,
                error_code=e.code,
                detail=str(e),
                view_phase_revision=getattr(view, "phase_task_revision", None),
                state_revision=state.revision,
            )
        raise


def _validate_view_state_metadata(
    view: Any,
    state: TaskState,
    *,
    plan_id: str | None = None,
    step_id: str | None = None,
    tool_name: str | None = None,
) -> None:
    """Centralized pre-validation: verify that a projected View's identity
    metadata matches the current TaskState BEFORE the phase runs.

    All Views carry task_id and phase_task_revision (the current state.revision
    at projection time).  The frozen task_revision (= baseContextRevision) is
    NOT used for boundary gating — it is a traceability baseline only.

    When plan_id / step_id / tool_name are provided (Executor path), the View
    must also match those.

    Raises HarnessPreValidationError on mismatch — fail-closed: the phase,
    tool call, and business Validator are all blocked.
    """
    view_task_id = getattr(view, "task_id", None)
    if view_task_id is not None and view_task_id != state.task_id:
        raise HarnessPreValidationError(
            "view_task_id_mismatch",
            f"View task_id={view_task_id!r} != state task_id={state.task_id!r}",
        )

    # Compare phase_task_revision (projection-time revision) — NOT the frozen
    # task_revision / baseContextRevision which stays at ContextPack creation
    # time and would block normal OCC advancement (Planner → ++revision → Executor).
    view_phase_revision = getattr(view, "phase_task_revision", None)
    if view_phase_revision is not None and view_phase_revision != state.revision:
        raise HarnessPreValidationError(
            "view_phase_revision_mismatch",
            f"View phase_task_revision={view_phase_revision!r} != state revision={state.revision!r}",
        )

    view_base_revision = getattr(view, "base_context_revision", None)
    if (
        view_base_revision is not None
        and view_phase_revision is not None
        and view_base_revision > view_phase_revision
    ):
        raise HarnessPreValidationError(
            "view_base_revision_from_future",
            "ContextPack base revision cannot be newer than the phase TaskState revision",
        )

    # ValidatorView owns the immutable Plan snapshot consumed by Validator.
    # TaskState is used only to reject a stale or foreign view at the boundary.
    view_plan = getattr(view, "plan", None)
    if view_plan is not None:
        state_plan = state.active_plan
        if state_plan is None or view_plan.plan_id != state_plan.plan_id:
            raise HarnessPreValidationError(
                "view_plan_id_mismatch",
                "ValidatorView plan does not match TaskState.activePlan",
            )
        if view_plan.model_dump(mode="json") != state_plan.model_dump(mode="json"):
            raise HarnessPreValidationError(
                "view_plan_snapshot_mismatch",
                "ValidatorView plan snapshot differs from TaskState.activePlan",
            )

    if plan_id is not None:
        view_plan_id = getattr(view, "plan_id", None)
        if view_plan_id is not None and view_plan_id != plan_id:
            raise HarnessPreValidationError(
                "view_plan_id_mismatch",
                f"View plan_id={view_plan_id!r} != expected plan_id={plan_id!r}",
            )

    if step_id is not None:
        view_step_id = getattr(view, "step_id", None)
        if view_step_id is not None and view_step_id != step_id:
            raise HarnessPreValidationError(
                "view_step_id_mismatch",
                f"View step_id={view_step_id!r} != expected step_id={step_id!r}",
            )

    if tool_name is not None:
        view_tool_name = getattr(view, "tool_name", None)
        if view_tool_name is not None and view_tool_name != tool_name:
            raise HarnessPreValidationError(
                "view_tool_name_mismatch",
                f"View tool_name={view_tool_name!r} != expected tool_name={tool_name!r}",
            )


async def run_harness_step(
    state: TaskState,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    *,
    client: AsyncOpenAI,
    model: str,
    system_policies: dict[str, Any] | None = None,
    tool_caller: ToolCaller = call_tool,
    trace_builder: TraceBuilder | None = None,
    projector: ContextProjector | None = None,
) -> HarnessStepResult:
    """Run one state-driven Planner/Executor/Validator/Replanner transition.

    In context_pack mode (projector is not None), ContextViews are projected
    BEFORE each phase and passed as the phase's authoritative input.

    A centralized pre-validation step verifies that every projected View's
    identity metadata (task_id, plan_id, step_id, tool_name) matches the
    current TaskState BEFORE the phase runs.  Failure here is fail-closed:
    the phase, tool call, and business Validator are all blocked.
    """

    if settings.product_knowledge_enabled:
        system_policies = {**(system_policies or {}), "productKnowledgeUserQuery": user_message}
    # Recover a step abandoned by a crashed worker before making routing
    # decisions. A live, unexpired lease remains fail-closed.
    state = await recover_expired_executor_claim(state)

    # ── Replanner path ─────────────────────────────────────────────────────
    if should_run_replanner(state):
        # Project ReplannerContextView BEFORE running the phase
        replanner_view = None
        if projector is not None:
            raw_failure = state.domain_state.get("validationResult", {})
            max_attempts = int((system_policies or {}).get("maxReplanAttempts", 3))
            replanner_view = projector.replanner_view(
                failed_plan_summary=_make_plan_summary(state),
                failed_plan=state.active_plan,
                failure=raw_failure if isinstance(raw_failure, dict) else {},
                failure_reason="Validator rejected: insufficient evidence",
                remaining_tool_names=_candidate_tool_names(candidate_tool_schemas),
                candidate_tool_schemas=candidate_tool_schemas,
                system_policies=system_policies,
                replan_attempt=int(
                    state.domain_state.get("replanAttemptCount", 0)
                ) + 1,
                max_replan_attempts=max_attempts,
                user_message=user_message,
                reusable_step_outputs=_reusable_step_output_payloads(state),
                phase_task_revision=state.revision,
            )
            _validate_view_and_record(
                replanner_view, state, trace_builder, "replanner",
                projector=projector,
            )
        if trace_builder is not None:
            trace_builder.start_phase("replanner")
            if replanner_view is not None:
                _record_view(trace_builder, "replanner", replanner_view)
                trace_builder.record_phase_task_revision("replanner", replanner_view.phase_task_revision)

        result = await _run_replanning_step(
            state,
            user_message,
            candidate_tool_schemas,
            client=client,
            model=model,
            system_policies=system_policies,
            replanner_view=replanner_view,
        )

        if trace_builder is not None:
            trace_builder.end_phase(
                result.replanner_result.outcome if result.replanner_result else "replanned"
            )
        return result

    # ── Planner phase ──────────────────────────────────────────────────────
    # Project PlannerContextView BEFORE calling the planner
    tool_names = [s.get("function", s).get("name", "") for s in candidate_tool_schemas]
    planner_view = None
    if projector is not None:
        # Flatten tool schemas: unwrap {"function": {...}} → {...} so the
        # Planner can consume them via _planner_tool_spec_from_view_dict.
        flat_tools: list[dict[str, Any]] = []
        for s in candidate_tool_schemas:
            fn = s.get("function", s)
            flat_tools.append(dict(fn) if isinstance(fn, dict) else fn)
        planner_view = projector.planner_view(
            tool_names=tool_names,
            task_status=state.status,
            user_message=user_message,
            candidate_tool_schemas=flat_tools,
            system_policies=system_policies,
            phase_task_revision=state.revision,
        )
        _validate_view_and_record(
            planner_view, state, trace_builder, "planner", projector=projector
        )

    if trace_builder is not None:
        trace_builder.start_phase("planner")
        if planner_view is not None:
            _record_view(trace_builder, "planner", planner_view)
            # Record entry revision BEFORE Planner runs — the value MUST equal
            # planner_view.phaseTaskRevision, NOT the post-persistence revision.
            trace_builder.record_phase_task_revision("planner", planner_view.phase_task_revision)

    planning_step = await run_planning_step(
        state,
        user_message,
        candidate_tool_schemas,
        client=client,
        model=model,
        system_policies=system_policies,
        planner_view=planner_view,
    )

    if trace_builder is not None:
        trace_builder.end_phase(
            planning_step.planner_result.outcome if planning_step.planner_result else "skipped"
        )

    # ── Validator-only path (no executor needed) ───────────────────────────
    if planning_step.action == "ready_for_validation":
        validator_view = None
        if projector is not None:
            validator_view = projector.validator_view(
                plan=planning_step.task_state.active_plan,
                executed_steps=_build_executed_steps(planning_step.task_state),
                phase_task_revision=planning_step.task_state.revision,
            )
            _validate_view_and_record(
                validator_view, planning_step.task_state, trace_builder, "validator",
                projector=projector,
            )
        result = await _run_validation_and_recovery(
            planning_step,
            user_message,
            candidate_tool_schemas,
            client=client,
            model=model,
            system_policies=system_policies,
            validator_view=validator_view,
            projector=projector,
            trace_builder=trace_builder,
        )

        return result

    if planning_step.action != "continue_to_executor":
        return HarnessStepResult(
            action=planning_step.action,
            planner_result=planning_step.planner_result,
            task_state=planning_step.task_state,
        )

    # ── Executor phase ─────────────────────────────────────────────────────
    # Project ExecutorContextView BEFORE calling the executor
    executor_view = None
    if projector is not None:
        active_plan = planning_step.task_state.active_plan
        if active_plan is not None:
            pending = [s for s in active_plan.steps if s.status == "pending"]
            if pending:
                step = pending[0]
                tool_schema_dict = _extract_tool_schema_dict(
                    step.tool_name, candidate_tool_schemas
                )
                state_ref = planning_step.task_state
                prior_outputs = _project_prior_step_outputs_for_step(
                    state_ref,
                    active_plan,
                    step,
                )
                executor_view = projector.executor_view(
                    plan_id=active_plan.plan_id,
                    step_id=step.step_id,
                    step_description=step.description,
                    tool_name=step.tool_name,
                    tool_schema=tool_schema_dict,
                    resolved_arguments=_project_step_arguments(step, prior_outputs),
                    required_fact_keys=_extract_fact_keys_for_step(step),
                    required_constraint_keys=_extract_constraint_keys_for_step(step),
                    prior_step_outputs=prior_outputs,
                    system_policies=system_policies,
                    phase_task_revision=planning_step.task_state.revision,
                )
                _validate_view_and_record(
                    executor_view, planning_step.task_state, trace_builder, "executor",
                    projector=projector,
                    plan_id=active_plan.plan_id,
                )

    if trace_builder is not None:
        trace_builder.start_phase("executor")
        if executor_view is not None:
            _record_view(trace_builder, "executor", executor_view)
            # Record entry revision BEFORE Executor runs.
            trace_builder.record_phase_task_revision("executor", executor_view.phase_task_revision)

    executor_result = await run_executor_step(
        planning_step.task_state,
        candidate_tool_schemas,
        system_policies=system_policies,
        tool_caller=tool_caller,
        executor_view=executor_view,
    )

    # Record tool call in trace
    if trace_builder is not None and executor_result.execution_result is not None:
        exec_res = executor_result.execution_result
        trace = exec_res.tool_trace
        if trace is not None:
            trace_builder.record_tool_call(
                tool_name=exec_res.tool_name,
                ok=trace.ok,
                duration_ms=trace.duration_ms,
                arguments_summary=trace.model_dump_json(
                    include={"tool": True}, exclude_defaults=True
                ),
            )

    if trace_builder is not None:
        trace_builder.end_phase(
            "step_executed" if executor_result.outcome == "step_executed" else executor_result.outcome,
            detail={"tool": executor_result.execution_result.tool_name,
                    "toolOk": executor_result.execution_result.tool_trace.ok if executor_result.execution_result.tool_trace else None}
                if executor_result.execution_result else None,
        )

    # ── Validator after executor ───────────────────────────────────────────
    action = decide_after_execution(executor_result)
    if action == "ready_for_validation":
        validator_view = None
        if projector is not None:
            validator_view = projector.validator_view(
                plan=executor_result.task_state.active_plan,
                executed_steps=_build_executed_steps(executor_result.task_state),
                phase_task_revision=executor_result.task_state.revision,
            )
            _validate_view_and_record(
                validator_view, executor_result.task_state, trace_builder, "validator",
                projector=projector,
            )
        result = await _run_validation_and_recovery(
            planning_step,
            user_message,
            candidate_tool_schemas,
            client=client,
            model=model,
            system_policies=system_policies,
            executor_result=executor_result,
            validator_view=validator_view,
            projector=projector,
            trace_builder=trace_builder,
        )

        return result

    return HarnessStepResult(
        action=action,
        planner_result=planning_step.planner_result,
        executor_result=executor_result,
        task_state=executor_result.task_state,
    )
