"""Minimal, bounded observation view for the decision-driven ReAct V0.

The view is projected from the latest authoritative ``TaskState``.  It carries
only routing facts, normalized shopping requirements and a compact
``CandidateScope`` reference; raw tool payloads and candidate documents never
enter the decider context.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..context_pack import _estimate_dict_tokens
from ..domains.ecommerce.models import (
    CandidateScope,
    ShoppingGuideState,
    compiled_shopping_requirements,
)
from ..task_state import TaskState
from ..settings import settings
from .react_actions import ActionKind, ActionOutcome


DECISION_VIEW_TOKEN_BUDGET = 2_000
REACT_V0_READ_ONLY_TOOLS = frozenset(
    {"search_products", "compare_products", "rerank_products_in_scope"}
)
REACT_V0_ACTIONS = (
    "CALL_TOOL",
    "ANSWER",
    "ASK_CLARIFICATION",
    "NEEDS_REVIEW",
)

_STALE_SCOPE_REFERENCE_CUES = (
    "最开始那两个",
    "最开始的两个",
    "之前那两个",
    "原来那两个",
    "先前那两个",
)
_PRESENTATION_ONLY_CUES = (
    "只展示前三个",
    "只展示前三款",
    "展示前三个",
    "展示前三款",
    "不用把20个都详细",
    "不用把二十个都详细",
)
_SCOPE_ANSWER_CUES = (
    "根据你确实知道的属性",
    "根据已有属性",
    "根据已知属性",
    "怎么选",
    "各自有什么特点",
    "各自适合谁",
)

_ARGUMENT_REFS: dict[str, dict[str, str]] = {
    "search_products": {
        "query": "taskState.goal",
        "category": "shoppingGuide.category",
        "requirements": "shoppingGuide.compiledRequirements",
    },
    "compare_products": {
        "productIds": "shoppingGuide.comparedIds",
        "category": "shoppingGuide.categoryCode",
        "requirements": "shoppingGuide.compiledRequirements",
    },
    "rerank_products_in_scope": {
        "scopeId": "candidateScope.scopeId",
        "productIds": "candidateScope.rankedItemIds",
        "rankingIntent": "scopeRerankRequest.rankingIntent",
        "category": "shoppingGuide.categoryCode",
        "requirements": "shoppingGuide.compiledRequirements",
    },
}


class DecisionViewBudgetExceeded(ValueError):
    pass


class _FrozenView(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)


class DecisionToolOption(_FrozenView):
    name: str
    argument_refs: dict[str, str] = Field(alias="argumentRefs")
    effect_class: Literal["READ_ONLY"] = Field(
        default="READ_ONLY", alias="effectClass"
    )


class DecisionObservationSummary(_FrozenView):
    validation_outcome: str | None = Field(default=None, alias="validationOutcome")
    validator_error_code: str | None = Field(default=None, alias="validatorErrorCode")
    candidate_pool_count: int | None = Field(
        default=None, alias="candidatePoolCount", ge=0
    )
    ranked_item_count: int | None = Field(
        default=None, alias="rankedItemCount", ge=0
    )
    has_complete_match: bool | None = Field(default=None, alias="hasCompleteMatch")
    evidence_gap_keys: list[str] = Field(default_factory=list, alias="evidenceGapKeys")
    stale_candidate_scope: bool = Field(default=False, alias="staleCandidateScope")
    adaptive_trigger: Literal[
        "zero_result", "unsupported_evidence", "stale_reference", "bounded_action_choice"
    ] | None = Field(default=None, alias="adaptiveTrigger")


class DecisionActionOption(_FrozenView):
    """One fully server-owned action that the model may select by ID only."""

    option_id: str = Field(
        alias="optionId",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    kind: ActionKind
    reason_code: str = Field(
        alias="reasonCode",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    tool_name: str | None = Field(default=None, alias="toolName")
    argument_refs: dict[str, str] | None = Field(default=None, alias="argumentRefs")
    answer_context_ref: str | None = Field(default=None, alias="answerContextRef")
    question: str | None = Field(default=None, min_length=1, max_length=512)
    review_code: str | None = Field(default=None, alias="reviewCode")

    @model_validator(mode="after")
    def _validate_payload(self) -> "DecisionActionOption":
        fields = {
            "tool": self.tool_name is not None and self.argument_refs is not None,
            "answer": self.answer_context_ref is not None,
            "question": self.question is not None,
            "review": self.review_code is not None,
        }
        required = {
            "CALL_TOOL": "tool",
            "ANSWER": "answer",
            "ASK_CLARIFICATION": "question",
            "NEEDS_REVIEW": "review",
        }[self.kind]
        if {name for name, present in fields.items() if present} != {required}:
            raise ValueError(f"{self.kind} action option payload mismatch")
        return self


class DecisionCandidateScope(_FrozenView):
    scope_id: str = Field(alias="scopeId")
    status: Literal["active"] = "active"
    candidate_pool_count: int = Field(alias="candidatePoolCount", ge=1)
    ranked_item_count: int = Field(alias="rankedItemCount", ge=1)
    visible_product_ids: list[int] = Field(alias="visibleProductIds", min_length=1)


class DecisionContextView(_FrozenView):
    schema_version: Literal["react-decision-view-v0"] = Field(
        default="react-decision-view-v0", alias="schemaVersion"
    )
    task_id: str = Field(alias="taskId")
    task_revision: int = Field(alias="taskRevision", ge=1)
    task_status: str = Field(alias="taskStatus")
    goal: str
    user_message: str = Field(alias="userMessage")
    shopping_mode: str | None = Field(default=None, alias="shoppingMode")
    category: str | None = None
    use_cases: list[str] = Field(default_factory=list, alias="useCases")
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[dict[str, Any]] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list, alias="pendingQuestions")
    candidate_scope: DecisionCandidateScope | None = Field(
        default=None, alias="candidateScope"
    )
    server_signals: dict[str, bool] = Field(alias="serverSignals")
    allowed_actions: list[str] = Field(alias="allowedActions")
    allowed_tools: list[DecisionToolOption] = Field(alias="allowedTools")
    observation_summary: DecisionObservationSummary = Field(alias="observationSummary")
    allowed_action_options: list[DecisionActionOption] = Field(
        alias="allowedActionOptions", min_length=1
    )
    answer_context_ref: str | None = Field(default=None, alias="answerContextRef")
    last_outcome: dict[str, Any] | None = Field(default=None, alias="lastOutcome")
    long_term_memory: list[dict[str, str]] = Field(
        default_factory=list, alias="longTermMemory", max_length=8)
    decision_view_hash: str = Field(alias="decisionViewHash", min_length=64, max_length=64)


def _plain_unknown(value: object) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        dumped = value.model_dump(by_alias=True, mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if isinstance(value, dict):
        return dict(value)
    return {"value": str(value)}


def _validated_answer_ref(
    state: TaskState,
    *,
    guide: ShoppingGuideState | None,
    scope: CandidateScope | None,
) -> str | None:
    raw = state.domain_state.get("validationResult")
    if isinstance(raw, dict) and raw.get("outcome") == "passed":
        based_on = raw.get("basedOnRevision")
        if type(based_on) is int and based_on + 1 == state.revision:
            return f"validated-task:{state.task_id}:r{state.revision}"
    if (
        isinstance(raw, dict)
        and raw.get("outcome") == "insufficient_evidence"
        and raw.get("errorCode") == "product_candidates_missing"
    ):
        based_on = raw.get("basedOnRevision")
        if type(based_on) is int and based_on + 1 == state.revision:
            return f"zero-result-task:{state.task_id}:r{state.revision}"
    if guide is None or guide.category is None or scope is None:
        return None
    if scope.task_id != state.task_id or scope.status != "active":
        return None
    if not _scope_matches_guide(scope, guide):
        return None
    return f"validated-scope:{scope.scope_id}"


def _scope_matches_guide(
    scope: CandidateScope,
    guide: ShoppingGuideState,
) -> bool:
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
    return (
        scope.category == guide.category
        and scope_requirements == current_requirements
        and scope_avoidances == current_avoidances
    )


def _validated_scope(state: TaskState) -> CandidateScope | None:
    raw = state.domain_state.get("candidateScope")
    if not isinstance(raw, dict):
        return None
    scope = CandidateScope.model_validate(raw)
    if scope.task_id != state.task_id:
        raise ValueError("candidateScope does not belong to current TaskState")
    if scope.status != "active":
        return None
    return scope


def _scope_projection(scope: CandidateScope | None) -> DecisionCandidateScope | None:
    if scope is None:
        return None
    return DecisionCandidateScope(
        scopeId=scope.scope_id,
        candidatePoolCount=len(scope.candidate_pool_ids),
        rankedItemCount=len(scope.ranked_item_ids),
        visibleProductIds=list(scope.visible_product_ids),
    )


def _unsupported_evidence_kind(user_message: str) -> str:
    normalized = user_message.casefold()
    if any(cue in normalized for cue in ("相机", "拍照", "夜景", "摄影")):
        return "camera"
    if any(cue in normalized for cue in ("游戏", "帧率", "散热", "发热")):
        return "gaming"
    return "generic"


def _validation_evidence_summary(state: TaskState) -> dict[str, Any]:
    validation = state.domain_state.get("validationResult")
    if not isinstance(validation, dict):
        return {}
    result: dict[str, Any] = {
        "validationOutcome": validation.get("outcome"),
        "validatorErrorCode": validation.get("errorCode"),
    }
    step_results = validation.get("stepResults")
    if not isinstance(step_results, list):
        return result
    for step in step_results:
        if not isinstance(step, dict):
            continue
        evidence = step.get("evidenceSummary")
        if not isinstance(evidence, dict):
            continue
        candidates = evidence.get("requiresProductCandidates")
        if not isinstance(candidates, dict):
            continue
        for source, target in (
            ("candidatePoolCount", "candidatePoolCount"),
            ("rankedItemCount", "rankedItemCount"),
            ("hasCompleteMatch", "hasCompleteMatch"),
        ):
            if source in candidates:
                result[target] = candidates[source]
        break
    return result


def _explicit_current_search_refresh(user_message: str) -> bool:
    """Narrow positive imperative, never historical/conditional/negated text.

    This selects an existing read-only tool, not product IDs or new filters.
    Uncovered language remains with the existing semantic decision path.
    """
    if not settings.context_history_v1_enabled:
        return False
    for clause in re.split(r"[，,。；;！!？?\n]", user_message):
        text = re.sub(r"\s+", "", clause)
        if re.search(r"不|别|无需|禁止|如果|若|假如|以后|之前|上次|曾经|记录|解释|[‘’“”\"']", text):
            continue
        if re.match(r"^(?:请|麻烦)?(?:现在|本轮)?(?:先)?(?:按[^，。；]{1,40}?)?(?:重新检索|重新搜索|重搜)", text):
            return True
    return False


def build_decision_context_view(
    state: TaskState,
    *,
    user_message: str,
    allowed_tool_names: Iterable[str],
    last_outcome: ActionOutcome | None = None,
    memory_run_binding: Any | None = None,
) -> DecisionContextView:
    """Project one revision-bound, hash-bound observation for the decider."""

    guide_raw = state.domain_state.get("shoppingGuide")
    guide = (
        ShoppingGuideState.model_validate(guide_raw)
        if isinstance(guide_raw, dict)
        else None
    )
    requirements = (
        [
            item.model_dump(by_alias=True, mode="json")
            for item in compiled_shopping_requirements(guide)
        ]
        if guide is not None and guide.category is not None
        else []
    )
    active_scope = _validated_scope(state)
    scope_is_current = bool(
        guide is not None
        and active_scope is not None
        and _scope_matches_guide(active_scope, guide)
    )
    current_scope = active_scope if scope_is_current else None
    scope = _scope_projection(current_scope)
    answer_ref = _validated_answer_ref(
        state,
        guide=guide,
        scope=active_scope,
    )
    compared_ids = guide.compared_ids if guide is not None else []
    comparison_bound = bool(
        current_scope is not None
        and len(compared_ids) >= 2
        and set(compared_ids).issubset(set(current_scope.ranked_item_ids))
    )
    extraction = state.domain_state.get("taskStateExtraction")
    extraction_reason = (
        extraction.get("reason") if isinstance(extraction, dict) else None
    )
    capability_research_requested = (
        extraction_reason == "active_scope_allowlisted_capability_gaps"
    )
    unbound_negative_target = extraction_reason == "unbound_negative_target"
    unsupported_capability_evidence = (
        extraction_reason == "unsupported_game_camera_evidence"
    )
    unsupported_evidence_kind = (
        _unsupported_evidence_kind(user_message)
        if unsupported_capability_evidence
        else None
    )
    boundary_answer_ref = (
        f"evidence-boundary:{unsupported_evidence_kind}:{state.task_id}:r{state.revision}"
        if unsupported_capability_evidence
        else None
    )
    broad_catalog_discovery = extraction_reason in {
        "broad_catalog_discovery",
        "camera_title_claim",
        "gaming_title_claim",
    }
    bound_comparison = (
        extraction_reason == "bound_comparison" and comparison_bound
    )
    compound = state.domain_state.get("compoundComparison")
    if (
        settings.context_history_v1_enabled and comparison_bound and current_scope is not None
        and isinstance(compound, dict) and compound.get("status") == "ready"
        and compound.get("kind") == "compare_first_two" and compound.get("taskId") == state.task_id
        and compound.get("sourcePlanId") == current_scope.source_plan_id
        and compound.get("productIds") == compared_ids
    ):
        bound_comparison = True
        # Search success is only the first half of this server-bound request.
        # Older comparison receipts cannot prematurely authorize its answer.
        answer_ref = None
    presentation_only = bool(
        answer_ref is not None
        and any(cue in user_message for cue in _PRESENTATION_ONLY_CUES)
    )
    scope_answer_requested = bool(
        answer_ref is not None
        and (
            extraction_reason in {"complete_controlled_coverage", "validated_scope_answer"}
            or capability_research_requested
            or any(cue in user_message for cue in _SCOPE_ANSWER_CUES)
        )
    )
    # Retained validation proves old facts, not execution of a new request.
    # Only the first action of this runtime turn is constrained; an actual
    # outcome then takes the normal success/failure/clarification path.
    fresh_search_required = bool(
        last_outcome is None and state.status == "ready"
        and not state.pending_questions and not state.unknowns
        and not unsupported_capability_evidence and not unbound_negative_target
        and _explicit_current_search_refresh(user_message)
    )
    if fresh_search_required:
        bound_comparison = presentation_only = scope_answer_requested = False
    references_old_scope = any(
        cue in user_message for cue in _STALE_SCOPE_REFERENCE_CUES
    )
    compared_ids_outside_current_scope = bool(
        current_scope is not None
        and compared_ids
        and not set(compared_ids).issubset(set(current_scope.ranked_item_ids))
    )
    stale_scope_reference = bool(
        extraction_reason == "stale_candidate_reference"
        or (
            references_old_scope
            and (
                (active_scope is not None and not scope_is_current)
                or compared_ids_outside_current_scope
            )
        )
    )
    validation_summary = _validation_evidence_summary(state)
    zero_result = bool(
        last_outcome is not None
        and last_outcome.error_code == "product_candidates_missing"
        and validation_summary.get("validationOutcome") == "insufficient_evidence"
        and validation_summary.get("rankedItemCount") == 0
    )
    terminal_nonretryable_outcome = bool(
        last_outcome is not None
        and last_outcome.status in {"FAILED", "REJECTED"}
        and not last_outcome.retryable
        and not zero_result
    )
    adaptive_trigger: str | None = None
    if zero_result:
        adaptive_trigger = "zero_result"
    elif unsupported_capability_evidence:
        adaptive_trigger = "unsupported_evidence"
    elif stale_scope_reference:
        adaptive_trigger = "stale_reference"
    unknowns = [_plain_unknown(value) for value in state.unknowns]
    pending_questions = list(state.pending_questions)
    if unbound_negative_target:
        unknowns.append({"kind": "unbound_negative_target", "key": "brand"})
        question = "你说的“那个牌子”具体指哪个品牌？"
        if question not in pending_questions:
            pending_questions.append(question)
    if stale_scope_reference:
        unknowns.append({"kind": "stale_candidate_reference"})
        question = (
            "这两个来自旧筛选范围，可能不满足当前硬条件。"
            "请基于当前候选重新指定序号，或明确提供要比较的商品 ID。"
        )
        if question not in pending_questions:
            pending_questions.append(question)
    if zero_result:
        question = (
            "当前没有商品同时满足全部硬条件。你愿意优先放宽预算，"
            "还是放宽其他某一项条件？"
        )
        if question not in pending_questions:
            pending_questions.append(question)
    rerank_request = state.domain_state.get("scopeRerankRequest")
    eligible_tools = {"search_products"}
    if comparison_bound:
        eligible_tools.add("compare_products")
    if (
        scope is not None
        and current_scope is not None
        and isinstance(rerank_request, dict)
        and rerank_request.get("scopeId") == scope.scope_id
    ):
        eligible_tools.add("rerank_products_in_scope")
    tool_names: list[str] = []
    if fresh_search_required:
        eligible_tools = {"search_products"}
    for raw_name in allowed_tool_names:
        name = str(raw_name)
        if (
            name in REACT_V0_READ_ONLY_TOOLS
            and name in eligible_tools
            and name not in tool_names
        ):
            tool_names.append(name)
    tools = [
        DecisionToolOption(name=name, argumentRefs=dict(_ARGUMENT_REFS[name]))
        for name in tool_names
    ]
    options: list[DecisionActionOption] = []
    # Repeating the exact zero-result search cannot create new evidence and is
    # already blocked by the runtime fingerprint.  Publish only safe terminal
    # choices for that adaptive observation.
    if adaptive_trigger is None and not terminal_nonretryable_outcome:
        for tool in tools:
            tool_reason = {
                "compare_products": "server_bound_comparison",
                "rerank_products_in_scope": "server_rerank_request",
                "search_products": (
                    "server_broad_catalog_discovery"
                    if broad_catalog_discovery
                    else "single_progress_tool"
                ),
            }[tool.name]
            options.append(DecisionActionOption(
                optionId=f"tool.{tool.name}",
                kind="CALL_TOOL",
                reasonCode=tool_reason,
                toolName=tool.name,
                argumentRefs=dict(tool.argument_refs),
            ))
    if (
        (answer_ref is not None or boundary_answer_ref is not None)
        and not fresh_search_required
        and adaptive_trigger != "stale_reference"
        and not terminal_nonretryable_outcome
    ):
        answer_reason = "answer_validated_context"
        if adaptive_trigger == "zero_result":
            answer_reason = "answer_zero_result_boundary"
        elif unsupported_capability_evidence:
            answer_reason = "answer_evidence_boundary"
        elif presentation_only or scope_answer_requested:
            answer_reason = "answer_from_validated_scope"
        elif last_outcome is not None and last_outcome.status == "SUCCEEDED":
            answer_reason = "answer_after_validated_action"
        options.append(DecisionActionOption(
            optionId=(
                "answer.zero_result"
                if adaptive_trigger == "zero_result"
                else "answer.validated_context"
            ),
            kind="ANSWER",
            reasonCode=answer_reason,
            answerContextRef=boundary_answer_ref or answer_ref,
        ))
    published_questions = (
        []
        if adaptive_trigger == "unsupported_evidence"
        or terminal_nonretryable_outcome
        else pending_questions
    )
    for index, question in enumerate(published_questions):
        options.append(DecisionActionOption(
            optionId=f"clarify.pending.{index}",
            kind="ASK_CLARIFICATION",
            reasonCode=(
                "clarify_zero_result_relaxation"
                if adaptive_trigger == "zero_result"
                else (
                    "clarify_stale_candidate_reference"
                    if adaptive_trigger == "stale_reference"
                    else "published_clarification_required"
                )
            ),
            question=question,
        ))
    # Adaptive triggers must expose a real bounded choice.  Every option is
    # still server-authored and side-effect free; the model selects only an
    # option ID and can never invent a question, evidence claim, or tool call.
    if adaptive_trigger == "unsupported_evidence":
        options.append(DecisionActionOption(
            optionId="clarify.unsupported_evidence",
            kind="ASK_CLARIFICATION",
            reasonCode="clarify_unsupported_evidence_boundary",
            question=(
                "当前证据不支持这项实际性能结论。你希望改为比较已核验字段，"
                "还是补充可信的外部证据？"
            ),
        ))
    if adaptive_trigger == "stale_reference":
        options.append(DecisionActionOption(
            optionId="clarify.stale.product_ids",
            kind="ASK_CLARIFICATION",
            reasonCode="clarify_stale_reference_by_product_id",
            question="请明确提供要继续比较的商品 ID。",
        ))
    if not options:
        options.append(DecisionActionOption(
            optionId="review.no_safe_action",
            kind="NEEDS_REVIEW",
            reasonCode=(
                "non_retryable_action_outcome"
                if terminal_nonretryable_outcome
                else "no_safe_action"
            ),
            reviewCode=(
                (last_outcome.error_code or "non_retryable_action_outcome")
                if terminal_nonretryable_outcome and last_outcome is not None
                else "no_safe_action"
            ),
        ))
    # A validated model extraction can leave several safe actions without a
    # deterministic routing signal. Let the decider select a published option;
    # never treat absence of a keyword rule as a reason to stop a ready task.
    # This does not broaden tools/arguments, bypass pending questions, revive
    # stale evidence, or retry a failed action.
    if (
        adaptive_trigger is None
        and last_outcome is None
        and state.status == "ready"
        and not unknowns and not pending_questions
        and current_scope is not None and answer_ref is not None
        and validation_summary.get("validationOutcome") == "passed"
        and isinstance(extraction, dict) and extraction.get("executionKind") == "model"
        and not any((fresh_search_required, bound_comparison, presentation_only, scope_answer_requested,
                     broad_catalog_discovery, "rerank_products_in_scope" in eligible_tools))
        and len(options) > 1
    ):
        adaptive_trigger = "bounded_action_choice"
    observation_summary = DecisionObservationSummary.model_validate({
        **validation_summary,
        "evidenceGapKeys": (
            (
                ["camera_performance", "night_photography"]
                if unsupported_evidence_kind == "camera"
                else ["gaming_fps", "thermal"]
            )
            if unsupported_capability_evidence
            else []
        ),
        "staleCandidateScope": bool(
            stale_scope_reference
            or (active_scope is not None and not scope_is_current)
        ),
        "adaptiveTrigger": adaptive_trigger,
    })
    payload: dict[str, Any] = {
        "schemaVersion": "react-decision-view-v0",
        "taskId": state.task_id,
        "taskRevision": state.revision,
        "taskStatus": state.status,
        "goal": state.goal,
        "userMessage": user_message,
        "shoppingMode": guide.mode if guide is not None else None,
        "category": guide.category if guide is not None else None,
        "useCases": list(guide.use_cases) if guide is not None else [],
        "requirements": requirements,
        "unknowns": unknowns,
        "pendingQuestions": pending_questions,
        "candidateScope": (
            scope.model_dump(by_alias=True, mode="json") if scope is not None else None
        ),
        "serverSignals": {
            "candidateScopeAvailable": scope is not None,
            "staleCandidateScope": bool(
                stale_scope_reference
                or (active_scope is not None and not scope_is_current)
            ),
            "staleScopeReference": stale_scope_reference,
            "comparisonBound": comparison_bound,
            "boundComparison": bound_comparison,
            "freshSearchRequired": fresh_search_required,
            "rerankRequested": "rerank_products_in_scope" in eligible_tools,
            "validatedEvidenceAvailable": answer_ref is not None,
            "unboundNegativeTarget": unbound_negative_target,
            "unsupportedCapabilityEvidence": unsupported_capability_evidence,
            "broadCatalogDiscovery": broad_catalog_discovery,
            "presentationOnly": presentation_only,
            "scopeAnswerRequested": scope_answer_requested,
            "adaptiveDecisionRequired": adaptive_trigger is not None,
        },
        "allowedActions": list(dict.fromkeys(option.kind for option in options)),
        "allowedTools": [tool.model_dump(by_alias=True, mode="json") for tool in tools],
        "observationSummary": observation_summary.model_dump(
            by_alias=True, mode="json"
        ),
        "allowedActionOptions": [
            option.model_dump(by_alias=True, mode="json") for option in options
        ],
        "answerContextRef": boundary_answer_ref or answer_ref,
        "lastOutcome": (
            last_outcome.model_dump(by_alias=True, mode="json")
            if last_outcome is not None
            else None
        ),
    }
    if memory_run_binding is not None:
        from ..memory.v3_runtime import MemoryRunBinding
        from ..domains.ecommerce.shopping_task_state_v2 import canonical_requirement_key
        if type(memory_run_binding) is not MemoryRunBinding:
            raise ValueError("unissued decision memory binding")
        memory = memory_run_binding.payload_for_phase("planner")
        if memory and guide is not None and guide.category == memory_run_binding.category_id:
            overridden = {canonical_requirement_key(item["key"]) for item in requirements
                          if type(item.get("key")) is str}
            if guide.brand_avoidances:
                overridden.add("brand")
            channel = [item for item in memory["preferences"] if item["attributeKey"] not in overridden]
            if channel:
                payload["longTermMemory"] = channel
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    payload["decisionViewHash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    view = DecisionContextView.model_validate(payload)
    tokens = decision_view_token_count(view)
    if tokens > DECISION_VIEW_TOKEN_BUDGET:
        raise DecisionViewBudgetExceeded(
            f"decision ContextView exceeds token budget: "
            f"{tokens} > {DECISION_VIEW_TOKEN_BUDGET}"
        )
    return view


def decision_view_token_count(view: DecisionContextView) -> int:
    return _estimate_dict_tokens(view.model_dump(by_alias=True, mode="json"))


__all__ = [
    "DECISION_VIEW_TOKEN_BUDGET",
    "DecisionActionOption",
    "DecisionCandidateScope",
    "DecisionContextView",
    "DecisionObservationSummary",
    "DecisionToolOption",
    "DecisionViewBudgetExceeded",
    "REACT_V0_ACTIONS",
    "REACT_V0_READ_ONLY_TOOLS",
    "build_decision_context_view",
    "decision_view_token_count",
]
