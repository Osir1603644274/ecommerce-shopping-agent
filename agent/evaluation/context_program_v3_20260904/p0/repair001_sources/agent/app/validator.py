import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
    USED_PHONE_ATTRIBUTE_REGISTRY,
    USED_PHONE_ATTRIBUTE_RULESET_VERSION,
    observe_used_phone_attributes,
)
from .domains.ecommerce.models import (
    CandidateScope,
    ShoppingGuideState,
    ShoppingRequirement,
    canonicalize_brand,
    compiled_shopping_requirements,
    soft_preference_match_score,
    validate_requirements,
)
from .domains.ecommerce.ranking_contract import (
    TwoStageRankingContractError,
    normalize_persisted_ranking_values,
    normalize_persisted_scope_rerank_values,
)
from .domains.ecommerce.synthetic_prices import canonical_synthetic_price_value
from .domains.ecommerce.shopping_state_update import (
    refresh_shopping_state_v2_after_validation,
)
from .executor import NormalizedStepOutput, StepExecutionResult
from .planning import PlanStep, TaskPlan, transition_plan_status
from .schemas import ToolTrace
from .settings import settings
from .task_state import TaskState, TaskStatePatchRequest, update_task_state
from .validation_contracts import (
    ExpectedOutputContractError,
    RUNTIME_TOOL_CONTRACTS,
    validate_expected_output_declaration,
    validate_normalized_output_values,
)


StepValidationOutcome = Literal[
    "satisfied",
    "insufficient_evidence",
    "invalid_evidence",
]
ValidatorOutcome = Literal[
    "passed",
    "insufficient_evidence",
    "validation_failed",
]


class ValidatorSelectionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ValidatorEvidenceError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ValidatorStepContext(BaseModel):
    """One executed PlanStep plus the evidence persisted by Executor."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    step: PlanStep
    execution_result: StepExecutionResult | None = Field(
        default=None,
        alias="executionResult",
    )
    normalized_output: NormalizedStepOutput | None = Field(
        default=None,
        alias="normalizedOutput",
    )


class ValidatorContext(BaseModel):
    """A revision-bound immutable input for deterministic validation."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    task_id: str = Field(alias="taskId", min_length=1)
    task_revision: int = Field(alias="taskRevision", ge=1)
    plan: TaskPlan
    steps: list[ValidatorStepContext] = Field(min_length=1)


class StepValidationResult(BaseModel):
    """The validation decision for one executed PlanStep."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    step_id: str = Field(alias="stepId", min_length=1)
    outcome: StepValidationOutcome
    expected_output: dict[str, Any] = Field(alias="expectedOutput", min_length=1)
    evidence_summary: dict[str, Any] = Field(
        default_factory=dict,
        alias="evidenceSummary",
    )
    error_code: str | None = Field(default=None, alias="errorCode")
    reason: str | None = None

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "StepValidationResult":
        if self.outcome == "satisfied":
            if self.error_code is not None or self.reason is not None:
                raise ValueError("satisfied步骤不能包含错误信息")
            return self
        if self.error_code is None or self.reason is None:
            raise ValueError("未通过的步骤验证必须包含errorCode和reason")
        return self


class ValidatorResult(BaseModel):
    """The persisted decision for one fully executed active Plan."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    outcome: ValidatorOutcome
    task_id: str = Field(alias="taskId", min_length=1)
    plan_id: str = Field(alias="planId", min_length=1)
    based_on_revision: int = Field(alias="basedOnRevision", ge=1)
    step_results: list[StepValidationResult] = Field(
        default_factory=list,
        alias="stepResults",
    )
    error_code: str | None = Field(default=None, alias="errorCode")
    reason: str | None = None
    validated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="validatedAt",
    )

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "ValidatorResult":
        if self.outcome == "passed":
            if not self.step_results or any(
                item.outcome != "satisfied" for item in self.step_results
            ):
                raise ValueError("passed结果必须包含全部satisfied的步骤结果")
            if self.error_code is not None or self.reason is not None:
                raise ValueError("passed结果不能包含错误信息")
            return self
        if self.error_code is None or self.reason is None:
            raise ValueError("未通过的ValidatorResult必须包含errorCode和reason")
        if self.outcome == "insufficient_evidence" and not any(
            item.outcome == "insufficient_evidence"
            for item in self.step_results
        ):
            raise ValueError("insufficient_evidence结果必须包含证据不足步骤")
        return self


def _validate_selection_boundary(state: TaskState) -> TaskPlan:
    if state.status != "ready":
        raise ValidatorSelectionError(
            "task_not_ready_for_validation",
            f"TaskState状态为{state.status}，不能开始Validator",
        )
    if state.pending_questions:
        raise ValidatorSelectionError(
            "pending_question_exists",
            "TaskState仍有待用户回答的问题，不能开始Validator",
        )
    if state.active_plan is None:
        raise ValidatorSelectionError(
            "active_plan_missing",
            "TaskState中不存在待验证的activePlan",
        )
    if state.active_plan.status != "active":
        raise ValidatorSelectionError(
            "plan_not_active",
            f"Plan状态为{state.active_plan.status}，不能开始Validator",
        )
    non_executed = [
        step.step_id
        for step in state.active_plan.steps
        if step.status != "executed"
    ]
    if non_executed:
        raise ValidatorSelectionError(
            "plan_not_fully_executed",
            "Validator只能检查全部步骤均为executed的Plan："
            + ", ".join(non_executed),
        )
    return state.active_plan


def build_validator_context(
    state: TaskState,
    context_view: Any | None = None,
) -> ValidatorContext:
    """Load the current Plan's latest execution evidence from TaskState.

    When context_view (ValidatorContextView) is provided, it is the
    authoritative source for evidence, constraints, and facts — the Validator
    MUST NOT read from raw TaskState or unbounded history.
    """

    state_plan = _validate_selection_boundary(state)

    if context_view is not None:
        _validate_validator_view_boundary(state, state_plan, context_view)
        return _build_validator_context_from_view(context_view)

    plan = state.active_plan
    assert plan is not None

    raw_history = state.domain_state.get("stepExecutionResults", [])
    if not isinstance(raw_history, list):
        raise ValidatorEvidenceError(
            "invalid_execution_history",
            "TaskState.domainState.stepExecutionResults必须是列表",
        )
    try:
        history = [StepExecutionResult.model_validate(item) for item in raw_history]
    except ValueError as exc:
        raise ValidatorEvidenceError(
            "invalid_execution_history",
            "TaskState中的执行历史无法通过StepExecutionResult校验",
        ) from exc

    raw_outputs = state.domain_state.get("stepOutputs", {})
    if not isinstance(raw_outputs, dict):
        raise ValidatorEvidenceError(
            "invalid_step_outputs",
            "TaskState.domainState.stepOutputs必须是对象",
        )

    steps: list[ValidatorStepContext] = []
    for step in plan.steps:
        execution_result = next(
            (
                item
                for item in reversed(history)
                if item.task_id == state.task_id
                and item.plan_id == plan.plan_id
                and item.step_id == step.step_id
            ),
            None,
        )
        normalized_output = None
        raw_output = raw_outputs.get(step.step_id)
        if raw_output is not None:
            try:
                normalized_output = NormalizedStepOutput.model_validate(raw_output)
            except ValueError as exc:
                raise ValidatorEvidenceError(
                    "invalid_step_output",
                    f"步骤规范化输出无法校验：{step.step_id}",
                ) from exc
            if (
                normalized_output.task_id != state.task_id
                or normalized_output.plan_id != plan.plan_id
                or normalized_output.step_id != step.step_id
            ):
                raise ValidatorEvidenceError(
                    "step_output_mismatch",
                    f"步骤规范化输出不属于当前任务和Plan：{step.step_id}",
                )
            try:
                validate_normalized_output_values(
                    step.tool_name, normalized_output.values
                )
            except ExpectedOutputContractError as exc:
                raise ValidatorEvidenceError(exc.code, str(exc)) from exc
        steps.append(
            ValidatorStepContext(
                step=step.model_copy(deep=True),
                executionResult=execution_result,
                normalizedOutput=normalized_output,
            )
        )

    return ValidatorContext(
        taskId=state.task_id,
        taskRevision=state.revision,
        plan=plan.model_copy(deep=True),
        steps=steps,
    )


def _validate_validator_view_boundary(
    state: TaskState,
    state_plan: TaskPlan,
    view: Any,
) -> None:
    """Use TaskState only as an OCC/identity gate, never as Validator input."""

    if view.task_id != state.task_id:
        raise ValidatorEvidenceError(
            "view_task_id_mismatch",
            "ValidatorView不属于当前TaskState",
        )
    if view.phase_task_revision != state.revision:
        raise ValidatorEvidenceError(
            "view_phase_revision_mismatch",
            "ValidatorView revision与当前TaskState不一致",
        )
    if view.plan is None:
        raise ValidatorEvidenceError(
            "view_plan_missing",
            "ValidatorView必须携带本轮完整Plan快照",
        )
    if view.plan.plan_id != state_plan.plan_id:
        raise ValidatorEvidenceError(
            "view_plan_id_mismatch",
            "ValidatorView Plan不属于当前TaskState",
        )
    if view.plan.model_dump(mode="json") != state_plan.model_dump(mode="json"):
        raise ValidatorEvidenceError(
            "view_plan_snapshot_mismatch",
            "ValidatorView Plan快照与当前TaskState不一致",
        )


def _build_validator_context_from_view(
    view: Any,
) -> ValidatorContext:
    """Build ValidatorContext solely from a ValidatorContextView.

    In context_pack mode the view is authoritative — the Validator MUST NOT
    read state.domain_state for step histories or tool trace bodies.
    """
    if view.plan is None:
        raise ValidatorEvidenceError(
            "view_plan_missing",
            "ValidatorView必须携带本轮完整Plan快照",
        )
    plan = view.plan.model_copy(deep=True)
    if plan.status != "active" or any(step.status != "executed" for step in plan.steps):
        raise ValidatorEvidenceError(
            "view_plan_not_ready",
            "ValidatorView中的Plan必须为active且全部步骤已经executed",
        )
    evidence_ids = [item.step_id for item in view.executed_steps]
    plan_ids = [item.step_id for item in plan.steps]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValidatorEvidenceError(
            "duplicate_view_step",
            "ValidatorView包含重复步骤证据",
        )
    unknown_evidence_ids = sorted(set(evidence_ids) - set(plan_ids))
    if unknown_evidence_ids:
        raise ValidatorEvidenceError(
            "view_step_not_in_plan",
            "ValidatorView包含Plan之外的步骤：" + ", ".join(unknown_evidence_ids),
        )
    if set(evidence_ids) != set(plan_ids):
        raise ValidatorEvidenceError(
            "view_plan_evidence_mismatch",
            "ValidatorView必须为Plan中的每个步骤提供且只提供一份证据",
        )

    steps: list[ValidatorStepContext] = []
    for evidence in view.executed_steps:
        step_id = evidence.step_id
        tool_name = evidence.tool_name
        plan_step = next(
            (s for s in plan.steps if s.step_id == step_id),
            None,
        )
        if plan_step is None:
            raise ValidatorEvidenceError(
                "view_step_not_in_plan",
                f"ValidatorView step_id={step_id} 不在当前Plan中",
            )
        if plan_step.tool_name != tool_name:
            raise ValidatorEvidenceError(
                "view_tool_name_mismatch",
                f"ValidatorView工具名称与Plan不一致：{step_id}",
            )
        # Reconstruct a minimal StepExecutionResult from view data
        from .schemas import ToolTrace

        outcome_label = evidence.outcome
        tool_ok = outcome_label in ("tool_succeeded",)
        now = datetime.now(timezone.utc)
        outcome: StepExecutionOutcome = (
            "tool_succeeded" if tool_ok
            else "tool_failed" if outcome_label == "tool_failed"
            else "tool_error"
        )
        # Build a synthetic ToolTrace for the view-only path
        if outcome in ("tool_succeeded", "tool_failed"):
            synthetic_trace = ToolTrace(
                tool=tool_name,
                ok=(outcome == "tool_succeeded"),
                durationMs=0.0,
                detail=evidence.evidence_values or {},
            )
        else:
            synthetic_trace = None

        execution_result = StepExecutionResult(
            taskId=view.task_id,
            planId=plan.plan_id,
            stepId=step_id,
            toolName=tool_name,
            resolvedArguments=evidence.resolved_arguments,
            outcome=outcome,
            toolTrace=synthetic_trace,
            errorType=("ContextViewToolError" if outcome == "tool_error" else None),
            errorMessage=("Tool execution failed before producing evidence" if outcome == "tool_error" else None),
            startedAt=now,
            finishedAt=now,
            durationMs=0.0,
        )
        normalized_output = None
        if evidence.normalized_output:
            normalized_output = NormalizedStepOutput(
                taskId=view.task_id,
                planId=plan.plan_id,
                stepId=step_id,
                values=evidence.normalized_output,
            )
            try:
                validate_normalized_output_values(
                    tool_name, normalized_output.values
                )
            except ExpectedOutputContractError as exc:
                raise ValidatorEvidenceError(exc.code, str(exc)) from exc
        steps.append(
            ValidatorStepContext(
                step=plan_step.model_copy(deep=True),
                executionResult=execution_result,
                normalizedOutput=normalized_output,
            )
        )

    return ValidatorContext(
        taskId=view.task_id,
        taskRevision=view.phase_task_revision,
        plan=plan.model_copy(deep=True),
        steps=steps,
    )


def _invalid_step(
    step: PlanStep,
    code: str,
    reason: str,
    evidence_summary: dict[str, Any] | None = None,
) -> StepValidationResult:
    return StepValidationResult(
        stepId=step.step_id,
        outcome="invalid_evidence",
        expectedOutput=deepcopy(step.expected_output),
        evidenceSummary=evidence_summary or {},
        errorCode=code,
        reason=reason,
    )


def _insufficient_step(
    step: PlanStep,
    code: str,
    reason: str,
    evidence_summary: dict[str, Any] | None = None,
) -> StepValidationResult:
    return StepValidationResult(
        stepId=step.step_id,
        outcome="insufficient_evidence",
        expectedOutput=deepcopy(step.expected_output),
        evidenceSummary=evidence_summary or {},
        errorCode=code,
        reason=reason,
    )


def _execution_identity_error(
    context: ValidatorContext,
    step_context: ValidatorStepContext,
) -> StepValidationResult | None:
    step = step_context.step
    execution = step_context.execution_result
    if execution is None:
        return _invalid_step(
            step,
            "execution_result_missing",
            f"步骤缺少StepExecutionResult：{step.step_id}",
        )
    if (
        execution.task_id != context.task_id
        or execution.plan_id != context.plan.plan_id
        or execution.step_id != step.step_id
        or execution.tool_name != step.tool_name
    ):
        return _invalid_step(
            step,
            "execution_result_mismatch",
            f"步骤执行记录与PlanStep身份不一致：{step.step_id}",
        )
    if execution.outcome != "tool_succeeded" or execution.tool_trace is None:
        return _invalid_step(
            step,
            "execution_not_successful",
            f"executed步骤没有成功工具结果：{step.step_id}",
            {"executionOutcome": execution.outcome},
        )
    return None


def _validate_shop_id(
    step_context: ValidatorStepContext,
) -> tuple[StepValidationOutcome, str | None, str | None, dict[str, Any]]:
    step = step_context.step
    if step.tool_name != "search_shops":
        return (
            "invalid_evidence",
            "expected_output_tool_mismatch",
            "requiresShopId只能由search_shops步骤提供",
            {},
        )
    output = step_context.normalized_output
    shop_id = output.values.get("shopId") if output is not None else None
    if not isinstance(shop_id, int) or isinstance(shop_id, bool):
        return (
            "insufficient_evidence",
            "shop_id_missing",
            "search_shops没有产生可供后续使用的唯一shopId",
            {},
        )
    return "satisfied", None, None, {"shopId": shop_id}


def _validate_shop_detail(
    step_context: ValidatorStepContext,
) -> tuple[StepValidationOutcome, str | None, str | None, dict[str, Any]]:
    step = step_context.step
    execution = step_context.execution_result
    if step.tool_name != "get_shop_detail":
        return (
            "invalid_evidence",
            "expected_output_tool_mismatch",
            "requiresShopDetail只能由get_shop_detail步骤提供",
            {},
        )
    detail = execution.tool_trace.detail
    if not isinstance(detail, dict):
        return (
            "invalid_evidence",
            "invalid_shop_detail",
            "get_shop_detail没有返回结构化商户详情",
            {},
        )
    shop_id = detail.get("id")
    expected_shop_id = execution.resolved_arguments.get("shopId")
    if not isinstance(shop_id, int) or isinstance(shop_id, bool):
        return (
            "insufficient_evidence",
            "shop_detail_missing",
            "商户详情中缺少合法id",
            {},
        )
    if shop_id != expected_shop_id:
        return (
            "invalid_evidence",
            "shop_detail_identity_mismatch",
            "返回的商户详情id与请求shopId不一致",
            {"requestedShopId": expected_shop_id, "returnedShopId": shop_id},
        )
    return (
        "satisfied",
        None,
        None,
        {"shopId": shop_id, "name": detail.get("name")},
    )


def _review_evidence_count(execution: StepExecutionResult) -> int:
    detail = execution.tool_trace.detail
    counts: list[int] = []
    if isinstance(detail, dict):
        declared_count = detail.get("count")
        if isinstance(declared_count, int) and not isinstance(declared_count, bool):
            counts.append(max(0, declared_count))
        for key in ("reviews", "citations", "chunks"):
            value = detail.get(key)
            if isinstance(value, list):
                counts.append(len(value))
    knowledge_result = execution.tool_trace.knowledge_result
    if isinstance(knowledge_result, dict):
        chunks = knowledge_result.get("chunks")
        if isinstance(chunks, list):
            counts.append(len(chunks))
    return max(counts, default=0)


def _validate_review_evidence(
    step_context: ValidatorStepContext,
) -> tuple[StepValidationOutcome, str | None, str | None, dict[str, Any]]:
    step = step_context.step
    execution = step_context.execution_result
    if step.tool_name not in {"search_shop_reviews", "search_knowledge"}:
        return (
            "invalid_evidence",
            "expected_output_tool_mismatch",
            "requiresReviewEvidence只能由评论证据检索步骤提供",
            {},
        )
    count = _review_evidence_count(execution)
    if count == 0:
        return (
            "insufficient_evidence",
            "review_evidence_missing",
            "评论检索成功返回，但没有可用于回答的评论证据",
            {"evidenceCount": 0},
        )
    return "satisfied", None, None, {"evidenceCount": count}


def _validate_product_candidates(step_context: ValidatorStepContext):
    if step_context.step.tool_name != "search_products":
        return "invalid_evidence", "expected_output_tool_mismatch", "候选只能来自商品检索", {}
    output = step_context.normalized_output
    if output is None:
        return "insufficient_evidence", "product_candidates_missing", "没有规范化商品候选", {}
    try:
        ranking_output = normalize_persisted_ranking_values(output.values)
    except TwoStageRankingContractError as exc:
        return "invalid_evidence", exc.code, str(exc), {}
    summary = {
        "candidatePoolCount": len(ranking_output.candidate_pool_ids),
        "rankedItemCount": len(ranking_output.ranked_item_ids),
        "candidatePoolIds": list(ranking_output.candidate_pool_ids),
        "rankedItemIds": list(ranking_output.ranked_item_ids),
        "productIds": list(ranking_output.ranked_item_ids),
        "evidenceRefs": list(ranking_output.evidence_refs),
    }
    summary.update(ranking_output.candidate_support)
    if not ranking_output.ranked_item_ids:
        return (
            "insufficient_evidence",
            "product_candidates_missing",
            "权威候选池存在，但规则重排后没有合法商品候选",
            summary,
        )
    return "satisfied", None, None, summary


def _validate_scope_rerank(step_context: ValidatorStepContext):
    if step_context.step.tool_name != "rerank_products_in_scope":
        return (
            "invalid_evidence",
            "expected_output_tool_mismatch",
            "候选集内重排只能来自范围重排工具",
            {},
        )
    output = step_context.normalized_output
    if output is None:
        return "insufficient_evidence", "scope_rerank_missing", "没有规范化范围重排结果", {}
    try:
        rerank_output = normalize_persisted_scope_rerank_values(output.values)
    except TwoStageRankingContractError as exc:
        return "invalid_evidence", exc.code, str(exc), {}
    if not rerank_output.ranked_item_ids:
        return "insufficient_evidence", "scope_rerank_empty", "范围重排没有合法候选", {}
    execution = step_context.execution_result
    resolved = (
        execution.resolved_arguments
        if execution is not None and execution.resolved_arguments is not None
        else {}
    )
    if (
        not isinstance(resolved, dict)
        or resolved.get("productIds") != list(rerank_output.input_product_ids)
    ):
        return (
            "invalid_evidence",
            "scope_rerank_input_mismatch",
            "范围重排输入与 Executor 解析参数不一致",
            {},
        )
    sources = (
        step_context.step.argument_sources
        if step_context.step.argument_sources is not None
        else {}
    )
    # argument_sources values are PlanArgumentSource models (in-memory PlanStep)
    # or plain dicts (persisted/test fixtures); read `reference` through either.
    product_source = sources.get("productIds") if isinstance(sources, dict) else None
    product_reference = (
        product_source.get("reference")
        if isinstance(product_source, dict)
        else getattr(product_source, "reference", None)
    )
    if (
        not isinstance(sources, dict)
        or product_reference != "scopeRankedItemIds"
    ):
        return (
            "invalid_evidence",
            "scope_rerank_source_mismatch",
            "范围重排必须引用 server-owned CandidateScope 的 rankedItemIds",
            {},
        )
    summary = {
        "scopeId": rerank_output.scope_id,
        "inputCount": len(rerank_output.input_product_ids),
        "outputCount": len(rerank_output.ranked_item_ids),
        "rankedItemIds": list(rerank_output.ranked_item_ids),
        "productIds": list(rerank_output.ranked_item_ids),
        "evidenceRefs": list(rerank_output.evidence_refs),
    }
    summary.update(rerank_output.candidate_support)
    return "satisfied", None, None, summary


def _validate_product_details(step_context: ValidatorStepContext):
    if step_context.step.tool_name != "get_product_details":
        return "invalid_evidence", "expected_output_tool_mismatch", "详情只能来自商品详情工具", {}
    execution = step_context.execution_result
    detail = execution.tool_trace.detail
    products = detail.get("products") if isinstance(detail, dict) else None
    requested = execution.resolved_arguments.get("productIds")
    if not isinstance(products, list) or not products:
        return "insufficient_evidence", "product_details_missing", "商品详情为空", {}
    returned = [item.get("id") for item in products if isinstance(item, dict)]
    if any(item not in requested for item in returned):
        return "invalid_evidence", "product_identity_mismatch", "详情包含未请求的商品 ID", {}
    if set(returned) != set(requested):
        return "insufficient_evidence", "product_details_incomplete", "部分候选缺少权威详情", {
            "requestedProductIds": requested, "returnedProductIds": returned
        }
    return "satisfied", None, None, {"productIds": returned}


_REQUIREMENT_CONTRACT_FIELDS = frozenset(
    {"key", "operator", "value", "unit", "priority", "source"}
)
_CHECK_CONTRACT_FIELDS = frozenset(
    {"key", "operator", "expected", "unit", "priority", "source"}
)


def _guide_requirement_identity(
    value: object, *, expected_field: str
) -> tuple[str, str, str, str, str, str] | None:
    if not isinstance(value, dict):
        return None
    required = (
        _REQUIREMENT_CONTRACT_FIELDS
        if expected_field == "value"
        else _CHECK_CONTRACT_FIELDS
    )
    if not required.issubset(value):
        return None
    try:
        canonical_value = json.dumps(
            value[expected_field],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None
    scalar_fields = ("key", "operator", "unit", "priority", "source")
    if not all(isinstance(value.get(field), str) for field in scalar_fields):
        return None
    return (
        value["key"],
        value["operator"],
        canonical_value,
        value["unit"],
        value["priority"],
        value["source"],
    )


def _validated_phone_field_evidence(
    product: dict[str, Any],
    evidence_by_ref: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None, list[dict[str, Any]], list[str]]:
    """Validate seven-field facts before they enter FinalAnswerContextView."""

    product_id = product.get("id")
    attributes = product.get("attributes")
    if type(product_id) is not int or not isinstance(attributes, list):
        return "controlled_evidence_mismatch", "商品快照缺少受控属性集合", [], []
    fields: list[dict[str, Any]] = []
    refs: list[str] = []
    for key in USED_PHONE_ATTRIBUTE_REGISTRY:
        matches = [
            item for item in attributes
            if isinstance(item, dict) and item.get("key") == key
        ]
        if len(matches) > 1:
            return "controlled_evidence_mismatch", "商品快照包含重复受控属性", [], []
        if not matches:
            fields.append({
                "key": key, "status": "unknown", "actual": None,
                "evidenceRef": None,
            })
            continue
        attribute = matches[0]
        if (
            attribute.get("evidenceField") != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
            or attribute.get("extractionMethod") != USED_PHONE_ATTRIBUTE_RULESET_VERSION
            or attribute.get("normalizedNumber") is not None
            or attribute.get("normalizedBoolean") is not None
        ):
            return "controlled_evidence_mismatch", "受控属性快照缺少可信原始证据", [], []
        actual = attribute.get("normalizedText")
        ref = f"product:{product_id}:attribute:{key}"
        citation = evidence_by_ref.get(ref)
        raw_value = (
            citation.get("rawValue")
            if actual is not None and isinstance(citation, dict)
            else attribute.get("rawValue")
        )
        if not isinstance(raw_value, str):
            return "controlled_evidence_mismatch", "受控属性快照缺少可信原始证据", [], []
        observation = observe_used_phone_attributes(raw_value)[key]
        if observation.status == "known" and observation.fact is not None:
            if (
                actual != observation.fact.value
                or citation is None
                or citation.get("field") != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
                or citation.get("method") != USED_PHONE_ATTRIBUTE_RULESET_VERSION
                or citation.get("rawValue") != raw_value
            ):
                return "controlled_evidence_mismatch", "受控属性事实与当前商品证据不一致", [], []
            fields.append({
                "key": key, "status": "known", "actual": actual,
                "evidenceRef": ref,
            })
            refs.append(ref)
            continue
        if actual is not None or observation.status not in {"unknown", "conflict"}:
            return "controlled_evidence_mismatch", "未知或冲突属性被提升为确定事实", [], []
        fields.append({
            "key": key, "status": observation.status, "actual": None,
            "evidenceRef": None,
        })
    return None, None, fields, refs


def _validate_guide_decision(step_context: ValidatorStepContext):
    if step_context.step.tool_name != "compare_products":
        return "invalid_evidence", "expected_output_tool_mismatch", "决选只能来自比较工具", {}
    execution = step_context.execution_result
    detail = execution.tool_trace.detail
    if not isinstance(detail, dict):
        return "invalid_evidence", "invalid_guide_decision", "决选结果不是对象", {}
    finalists = detail.get("products")
    evidence = detail.get("evidence")
    requested = execution.resolved_arguments.get("productIds", [])
    resolved_category = execution.resolved_arguments.get("category")
    resolved_requirements = execution.resolved_arguments.get("requirements")
    detail_requirements = detail.get("requirements")
    if (
        resolved_category not in {"phone", "laptop", "headphones"}
        or detail.get("category") != resolved_category
        or not isinstance(resolved_requirements, list)
        or not isinstance(detail_requirements, list)
        or detail_requirements != resolved_requirements
    ):
        return "invalid_evidence", "requirement_check_mismatch", "比较要求与已解析工具参数不一致", {}
    requirement_identities = []
    parsed_requirements = []
    for requirement in resolved_requirements:
        if (
            not isinstance(requirement, dict)
            or set(requirement) != _REQUIREMENT_CONTRACT_FIELDS
        ):
            return "invalid_evidence", "requirement_check_mismatch", "比较要求结构不完整或包含额外字段", {}
        identity = _guide_requirement_identity(requirement, expected_field="value")
        if identity is None:
            return "invalid_evidence", "requirement_check_mismatch", "比较要求结构无效", {}
        try:
            parsed_requirements.append(ShoppingRequirement.model_validate(requirement))
        except ValueError:
            return "invalid_evidence", "requirement_check_mismatch", "比较要求无法通过运行时合同", {}
        requirement_identities.append(identity)
    if len(set(requirement_identities)) != len(requirement_identities):
        return "invalid_evidence", "requirement_check_mismatch", "比较要求包含重复项", {}
    try:
        validate_requirements(resolved_category, parsed_requirements)
    except ValueError:
        return "invalid_evidence", "requirement_check_mismatch", "比较要求不属于当前品类合同", {}
    required_counter = Counter(requirement_identities)
    requirement_by_identity = dict(zip(requirement_identities, parsed_requirements))
    hard_max_budget_active = any(
        requirement.key == "price_minor"
        and requirement.operator == "lte"
        and requirement.priority == "hard"
        for requirement in parsed_requirements
    )
    if not isinstance(finalists, list) or len(finalists) > 3:
        return "invalid_evidence", "invalid_finalist_count", "决选商品数量必须不超过 3", {}
    if not finalists:
        return "insufficient_evidence", "no_product_match", "没有可用于决选的商品", {}
    expanded_evidence: list[dict[str, Any]] = []
    for item in evidence or []:
        if isinstance(item, dict) and isinstance(item.get("refs"), list):
            body = {key: value for key, value in item.items() if key != "refs"}
            expanded_evidence.extend(
                {"ref": ref, **body}
                for ref in item["refs"]
                if isinstance(ref, str)
            )
        else:
            expanded_evidence.append(item)
    evidence_by_ref: dict[str, dict[str, Any]] = {}
    for item in expanded_evidence:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            continue
        ref = item["ref"]
        previous = evidence_by_ref.setdefault(ref, item)
        if previous != item:
            return "invalid_evidence", "evidence_reference_collision", "同一证据引用包含冲突内容", {}
    evidence_refs = set(evidence_by_ref)
    validated_matrix: list[dict[str, Any]] = []
    validated_field_evidence: dict[int, list[dict[str, Any]]] = {}
    validated_field_refs: dict[int, list[str]] = {}
    fully_matched_values: list[bool] = []
    for row in finalists:
        product = row.get("product") if isinstance(row, dict) else None
        if not isinstance(product, dict) or product.get("id") not in requested:
            return "invalid_evidence", "product_identity_mismatch", "决选引用了召回范围外的商品", {}
        checks = row.get("checks")
        if not isinstance(checks, list):
            return "invalid_evidence", "requirement_check_mismatch", "决选缺少完整约束检查", {}
        check_identities = []
        for check in checks:
            identity = _guide_requirement_identity(check, expected_field="expected")
            if identity is None:
                return "invalid_evidence", "requirement_check_mismatch", "约束检查结构不完整", {}
            check_identities.append(identity)
        if Counter(check_identities) != required_counter:
            return "invalid_evidence", "requirement_check_mismatch", "决选约束检查与已解析要求不是完整同一集合", {}
        hard_failures = sum(
            check.get("priority") == "hard" and check.get("status") == "fail"
            for check in checks
        )
        hard_unknowns = sum(
            check.get("priority") == "hard" and check.get("status") == "unknown"
            for check in checks
        )
        soft_passes = sum(
            check.get("priority") == "soft" and check.get("status") == "pass"
            for check in checks
        )
        soft_count = sum(
            check.get("priority") == "soft" for check in checks
        )
        soft_preference_points = sum(
            soft_preference_match_score(
                check.get("actual"),
                requirement_by_identity[identity],
                str(check.get("status")),
            )
            for check, identity in zip(checks, check_identities)
        )
        expected_soft_preference_score = (
            soft_preference_points / soft_count if soft_count else 0.0
        )
        budget_check = next((
            check for check in checks
            if check.get("key") == "price_minor"
            and check.get("operator") == "lte"
            and check.get("priority") == "hard"
        ), None)
        expected_budget_sort_price = (
            int(budget_check["actual"])
            if isinstance(budget_check, dict)
            and type(budget_check.get("actual")) in {int, float}
            else None
        )
        fully_matched = hard_failures == 0 and hard_unknowns == 0
        if (
            type(row.get("hardFailures")) is not int
            or row.get("hardFailures") != hard_failures
            or type(row.get("hardUnknowns")) is not int
            or row.get("hardUnknowns") != hard_unknowns
            or type(row.get("softScore")) is not int
            or row.get("softScore") != soft_passes
            or type(row.get("softPreferenceScore")) not in {int, float}
            or abs(
                float(row.get("softPreferenceScore"))
                - expected_soft_preference_score
            ) > 1e-7
            or row.get("budgetSortPriceMinor") != expected_budget_sort_price
            or type(row.get("fullyMatched")) is not bool
            or row.get("fullyMatched") is not fully_matched
        ):
            return "invalid_evidence", "requirement_count_mismatch", "决选约束计数与逐项检查不一致", {}
        if hard_failures != 0:
            return "invalid_evidence", "hard_constraint_violation", "明确违反硬约束的商品未被淘汰", {}
        validated_matrix.append({"productId": product["id"], "checks": checks})
        fully_matched_values.append(fully_matched)
        for check in checks:
            if check.get("status") not in {"pass", "fail", "unknown"}:
                return "invalid_evidence", "requirement_check_mismatch", "约束检查状态无效", {}
            ref = check.get("evidenceRef")
            if ref is not None and ref not in evidence_refs:
                return "invalid_evidence", "evidence_reference_out_of_bounds", "存在越界证据引用", {}
            if check.get("status") == "unknown" and check.get("actual") is not None:
                return "invalid_evidence", "unknown_status_inconsistent", "unknown 状态与实际值冲突", {}
            if check.get("actual") is None and check.get("status") != "unknown":
                return "invalid_evidence", "unknown_status_inconsistent", "缺失实际值却被判定为已知", {}
            controlled_key = check.get("key")
            if controlled_key in USED_PHONE_ATTRIBUTE_REGISTRY:
                actual = check.get("actual")
                ref = check.get("evidenceRef")
                snapshot_attributes = [
                    item
                    for item in product.get("attributes") or []
                    if isinstance(item, dict) and item.get("key") == controlled_key
                ]
                if actual is None:
                    if ref is not None:
                        return "invalid_evidence", "controlled_evidence_mismatch", "未知受控属性不能引用确定性证据", {}
                    if len(snapshot_attributes) > 1:
                        return "invalid_evidence", "controlled_evidence_mismatch", "未知受控属性包含重复商品快照记录", {}
                    if any(
                        item.get("normalizedNumber") is not None
                        or item.get("normalizedBoolean") is not None
                        or item.get("normalizedText") is not None
                        for item in snapshot_attributes
                    ):
                        return "invalid_evidence", "controlled_evidence_mismatch", "商品快照含有受控属性但检查结果标为未知", {}
                    if snapshot_attributes:
                        snapshot_attribute = snapshot_attributes[0]
                        raw_value = snapshot_attribute.get("rawValue")
                        if (
                            not isinstance(raw_value, str)
                            or snapshot_attribute.get("evidenceField")
                            != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
                            or snapshot_attribute.get("extractionMethod")
                            != USED_PHONE_ATTRIBUTE_RULESET_VERSION
                        ):
                            return "invalid_evidence", "controlled_evidence_mismatch", "未知受控属性快照缺少可信原始证据", {}
                        observation = observe_used_phone_attributes(raw_value)[controlled_key]
                        if observation.status not in {"unknown", "conflict"} or observation.fact is not None:
                            return "invalid_evidence", "controlled_evidence_mismatch", "已知原始证据不能降级伪装为 unknown", {}
                else:
                    expected_ref = (
                        f"product:{int(product['id'])}:attribute:{controlled_key}"
                    )
                    citation = evidence_by_ref.get(ref)
                    if ref != expected_ref or citation is None:
                        return "invalid_evidence", "controlled_evidence_mismatch", "受控属性证据未绑定当前商品与字段", {}
                    raw_value = citation.get("rawValue")
                    if (
                        citation.get("field") != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
                        or citation.get("method")
                        != USED_PHONE_ATTRIBUTE_RULESET_VERSION
                        or not isinstance(raw_value, str)
                    ):
                        return "invalid_evidence", "controlled_evidence_mismatch", "受控属性证据来源或归一化方法不可信", {}
                    if len(snapshot_attributes) != 1:
                        return "invalid_evidence", "controlled_evidence_mismatch", "商品快照缺少唯一的受控属性记录", {}
                    snapshot_attribute = snapshot_attributes[0]
                    snapshot_actual = snapshot_attribute.get("normalizedNumber")
                    if snapshot_actual is None:
                        snapshot_actual = snapshot_attribute.get("normalizedBoolean")
                    if snapshot_actual is None:
                        snapshot_actual = snapshot_attribute.get("normalizedText")
                    if (
                        snapshot_actual != actual
                        or (
                            snapshot_attribute.get("rawValue") is not None
                            and snapshot_attribute.get("rawValue") != raw_value
                        )
                        or snapshot_attribute.get("evidenceField")
                        != citation.get("field")
                        or snapshot_attribute.get("extractionMethod")
                        != citation.get("method")
                    ):
                        return "invalid_evidence", "controlled_evidence_mismatch", "证据与当前商品快照属性不一致", {}
                    observation = observe_used_phone_attributes(raw_value)[controlled_key]
                    if (
                        observation.status != "known"
                        or observation.fact is None
                        or observation.fact.value != actual
                    ):
                        return "invalid_evidence", "controlled_evidence_mismatch", "原始证据不能推出声明的受控属性值", {}
            if (
                check.get("key") == "price_minor"
                and check.get("status") in {"pass", "fail"}
            ):
                if product.get("priceStatus") == "verified":
                    expected_price_ref = (
                        f"product:{int(product['id'])}:snapshotPriceMinor"
                    )
                    if check.get("evidenceRef") != expected_price_ref:
                        return "invalid_evidence", "price_evidence_mismatch", "真实价格证据未绑定当前商品", {}
                else:
                    synthetic_error = None
                    try:
                        synthetic_value, synthetic = canonical_synthetic_price_value(
                            int(product["id"]),
                            directory=settings.used_phone_synthetic_price_dir,
                            policy=settings.used_phone_synthetic_price_policy,
                            allow_budget=True,
                        )
                    except Exception as exc:
                        synthetic_value, synthetic = None, None
                        synthetic_error = f"{type(exc).__name__}: {exc}"
                    expected_price_ref = (
                        f"product:{int(product['id'])}:syntheticReferencePriceMinor"
                    )
                    citation = evidence_by_ref.get(check.get("evidenceRef"))
                    synthetic_checks = {
                        "runtime_value": synthetic_value is not None,
                        "runtime_metadata": synthetic is not None,
                        "actual": check.get("actual") == synthetic_value,
                        "evidence_ref": check.get("evidenceRef") == expected_price_ref,
                        "citation": isinstance(citation, dict),
                        "field": isinstance(citation, dict)
                        and citation.get("field") == "syntheticReferencePriceMinor",
                        "raw_value": isinstance(citation, dict)
                        and citation.get("rawValue") == synthetic_value,
                        "price_status": isinstance(citation, dict)
                        and citation.get("priceStatus") == "synthetic",
                        "data_nature": isinstance(citation, dict)
                        and citation.get("dataNature") == "synthetic",
                        "price_policy": isinstance(citation, dict)
                        and citation.get("pricePolicy") == "budget_and_ranking",
                        "ruleset_version": synthetic is not None
                        and isinstance(citation, dict)
                        and citation.get("rulesetVersion") == synthetic["rulesetVersion"],
                        "ruleset_sha256": synthetic is not None
                        and isinstance(citation, dict)
                        and citation.get("rulesetSha256") == synthetic["rulesetSha256"],
                        "source_catalog_sha256": synthetic is not None
                        and isinstance(citation, dict)
                        and citation.get("sourceCatalogSha256")
                        == synthetic["sourceCatalogSha256"],
                        "seed": synthetic is not None
                        and isinstance(citation, dict)
                        and citation.get("seed") == synthetic["seed"],
                        "disclosure": synthetic is not None
                        and isinstance(citation, dict)
                        and citation.get("disclosureZh") == synthetic["disclosureZh"],
                    }
                    failed_synthetic_checks = [
                        name for name, passed in synthetic_checks.items() if not passed
                    ]
                    if failed_synthetic_checks:
                        return (
                            "invalid_evidence",
                            "synthetic_price_evidence_mismatch",
                            "合成参考价未通过显式预算策略校验",
                            {
                                "productId": product.get("id"),
                                "failedChecks": failed_synthetic_checks,
                                "runtimeError": synthetic_error,
                            },
                        )
            actual, expected, operator = (
                check.get("actual"), check.get("expected"), check.get("operator")
            )
            if actual is not None and operator in {"eq", "lte", "gte", "in", "not_in"}:
                comparison_actual = actual
                comparison_expected = expected
                if check.get("key") == "brand" and isinstance(actual, str):
                    comparison_actual = canonicalize_brand(actual)
                    if isinstance(expected, str):
                        comparison_expected = canonicalize_brand(expected)
                    elif isinstance(expected, list):
                        comparison_expected = [
                            canonicalize_brand(item)
                            if isinstance(item, str) else item
                            for item in expected
                        ]
                expected_status = {
                    "eq": (
                        comparison_actual.casefold() == comparison_expected.casefold()
                        if isinstance(comparison_actual, str)
                        and isinstance(comparison_expected, str)
                        else comparison_actual == comparison_expected
                    ),
                    "lte": lambda: float(comparison_actual) <= float(comparison_expected),
                    "gte": lambda: float(comparison_actual) >= float(comparison_expected),
                    "in": lambda: comparison_actual in comparison_expected,
                    "not_in": lambda: comparison_actual not in comparison_expected,
                }[operator]
                if callable(expected_status):
                    expected_status = expected_status()
                if check.get("status") != ("pass" if expected_status else "fail"):
                    return "invalid_evidence", "constraint_calculation_mismatch", "硬约束计算不一致", {}
            if (
                check.get("priority") == "hard"
                and str(check.get("source", "")).startswith("inferred:")
            ):
                return "invalid_evidence", "inferred_hard_constraint", "推断要求不能作为硬约束", {}
        expected_selection = (
            "full_match"
            if row.get("hardFailures") == 0 and row.get("hardUnknowns") == 0
            else "closest_alternative"
        )
        if row.get("selectionType") != expected_selection:
            return "invalid_evidence", "selection_label_mismatch", "决选标签与约束状态不一致", {}
        breakdown = row.get("scoreBreakdown")
        if not isinstance(breakdown, dict):
            return "invalid_evidence", "score_breakdown_missing", "决选缺少可审计分数组成", {}
        if abs(
            float(breakdown.get("softRequirementMatch", -1))
            - expected_soft_preference_score
        ) > 1e-7:
            return "invalid_evidence", "score_calculation_mismatch", "软偏好分数与逐项检查不一致", {}
        calculated = (
            0.55 * float(breakdown.get("normalizedRecall", 0))
            + 0.30 * float(breakdown.get("softRequirementMatch", 0))
            + 0.15 * float(breakdown.get("evidenceCompleteness", 0))
        )
        if abs(calculated - float(breakdown.get("final", -1))) > 1e-7:
            return "invalid_evidence", "score_calculation_mismatch", "重排分数与固定公式不一致", {}
        if resolved_category == "phone":
            error_code, error_reason, fields, field_refs = (
                _validated_phone_field_evidence(product, evidence_by_ref)
            )
            if error_code is not None:
                return "invalid_evidence", error_code, error_reason, {}
            validated_field_evidence[product["id"]] = fields
            validated_field_refs[product["id"]] = field_refs
    if detail.get("comparisonMatrix") != validated_matrix:
        return "invalid_evidence", "requirement_check_mismatch", "比较矩阵与决选逐项检查不一致", {}
    if detail.get("hasCompleteMatch") is not any(fully_matched_values):
        return "invalid_evidence", "requirement_count_mismatch", "完整匹配汇总与决选检查不一致", {}
    ordering = [
        (
            int(row.get("hardUnknowns", 0)) > 0,
            -float(row.get("softPreferenceScore", 0)),
            -int(row["budgetSortPriceMinor"])
            if hard_max_budget_active
            and type(row.get("budgetSortPriceMinor")) is int
            else 0,
            -float(row["scoreBreakdown"]["final"]),
            int(row["product"]["id"]),
        )
        for row in finalists
    ]
    if ordering != sorted(ordering):
        return "invalid_evidence", "ranking_mismatch", "最终排序与比较分数不一致", {}
    ranked_finalists = []
    evidence_ref_order: list[str] = []
    for row in finalists:
        product = row["product"]
        product_id = int(product["id"])
        title = product.get("title")
        brand = product.get("brand")
        title_ref = f"product:{product_id}:title"
        brand_ref = f"product:{product_id}:brand"
        title_citation = evidence_by_ref.get(title_ref)
        brand_citation = evidence_by_ref.get(brand_ref)
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(title_citation, dict)
            or title_citation.get("field") != "title"
            or title_citation.get("rawValue") != title
        ):
            return "invalid_evidence", "product_identity_evidence_mismatch", "商品标题缺少可信快照证据", {}
        if brand is not None and (
            not isinstance(brand, str)
            or not isinstance(brand_citation, dict)
            or brand_citation.get("field") != "brand"
            or brand_citation.get("rawValue") != brand
        ):
            return "invalid_evidence", "product_identity_evidence_mismatch", "商品品牌缺少可信快照证据", {}
        compact_checks = []
        for check in row["checks"]:
            compact = {
                key: check.get(key)
                for key in ("key", "status", "actual", "evidenceRef")
                if key in check
            }
            compact_checks.append(compact)
            ref = compact.get("evidenceRef")
            if isinstance(ref, str) and ref not in evidence_ref_order:
                evidence_ref_order.append(ref)
        ranked_finalists.append({
            "productId": product_id,
            "title": title,
            "brand": brand,
            "titleEvidenceRef": title_ref,
            "brandEvidenceRef": brand_ref if isinstance(brand, str) else None,
            "selectionType": row["selectionType"],
            "hardUnknowns": row["hardUnknowns"],
            "softScore": row["softScore"],
            "finalScore": row["scoreBreakdown"]["final"],
            "checks": compact_checks,
            "fieldEvidence": validated_field_evidence.get(row["product"]["id"], []),
        })
        for ref in (title_ref, brand_ref if isinstance(brand, str) else None):
            if isinstance(ref, str) and ref not in evidence_ref_order:
                evidence_ref_order.append(ref)
        for ref in validated_field_refs.get(row["product"]["id"], []):
            if ref not in evidence_ref_order:
                evidence_ref_order.append(ref)
    return "satisfied", None, None, {
        "finalistIds": [row["product"]["id"] for row in finalists],
        "rankedFinalists": ranked_finalists,
        "evidenceRefs": evidence_ref_order,
        "hasCompleteMatch": detail.get("hasCompleteMatch"),
    }


def _validate_place_candidates(step_context: ValidatorStepContext):
    if step_context.step.tool_name != "search_places":
        return "invalid_evidence", "expected_output_tool_mismatch", "Place candidates must come from search_places", {}
    detail = step_context.execution_result.tool_trace.detail
    items = detail.get("items") if isinstance(detail, dict) else None
    if not isinstance(items, list) or not items:
        return "insufficient_evidence", "place_candidates_missing", "No place candidates were returned", {}
    ids = [item.get("id") for item in items if isinstance(item, dict)]
    if len(ids) != len(items) or not all(isinstance(item, str) and item for item in ids):
        return "invalid_evidence", "invalid_place_candidates", "A place candidate is missing its stable id", {}
    return "satisfied", None, None, {"candidateCount": len(ids), "placeIds": ids}


def _validate_place_detail(step_context: ValidatorStepContext):
    if step_context.step.tool_name != "get_place_detail":
        return "invalid_evidence", "expected_output_tool_mismatch", "Place detail must come from get_place_detail", {}
    execution = step_context.execution_result
    detail = execution.tool_trace.detail
    place = detail.get("place") if isinstance(detail, dict) else None
    if not isinstance(place, dict) or not isinstance(place.get("id"), str):
        return "insufficient_evidence", "place_detail_missing", "The place detail is missing", {}
    requested = execution.resolved_arguments.get("placeId")
    if place["id"] != requested:
        return "invalid_evidence", "place_detail_identity_mismatch", "Returned place id does not match the requested placeId", {}
    return "satisfied", None, None, {"placeId": place["id"], "name": place.get("name")}


def _validate_shop_types(step_context: ValidatorStepContext):
    if step_context.step.tool_name != "list_shop_types":
        return "invalid_evidence", "expected_output_tool_mismatch", "Shop types must come from list_shop_types", {}
    detail = step_context.execution_result.tool_trace.detail
    names = detail.get("names") if isinstance(detail, dict) else None
    if not isinstance(names, list) or not names or not all(isinstance(item, str) for item in names):
        return "insufficient_evidence", "shop_types_missing", "No usable shop types were returned", {}
    return "satisfied", None, None, {"count": len(names), "names": names}


def _validate_shop_recommendations(step_context: ValidatorStepContext):
    if step_context.step.tool_name != "recommend_shops":
        return "invalid_evidence", "expected_output_tool_mismatch", "Recommendations must come from recommend_shops", {}
    detail = step_context.execution_result.tool_trace.detail
    shops = detail.get("shops") if isinstance(detail, dict) else None
    if not isinstance(shops, list) or not shops:
        return "insufficient_evidence", "shop_recommendations_missing", "No shop recommendations were returned", {}
    ids = [item.get("shopId") for item in shops if isinstance(item, dict)]
    if len(ids) != len(shops) or not all(type(item) is int for item in ids):
        return "invalid_evidence", "invalid_shop_recommendations", "A recommendation is missing a valid shopId", {}
    return "satisfied", None, None, {"count": len(ids), "shopIds": ids}


_EXPECTED_OUTPUT_VALIDATORS = {
    "requiresShopId": _validate_shop_id,
    "requiresShopDetail": _validate_shop_detail,
    "requiresReviewEvidence": _validate_review_evidence,
    "requiresProductCandidates": _validate_product_candidates,
    "requiresProductDetails": _validate_product_details,
    "requiresGuideDecision": _validate_guide_decision,
    "requiresScopeRerank": _validate_scope_rerank,
    "requiresPlaceCandidates": _validate_place_candidates,
    "requiresPlaceDetail": _validate_place_detail,
    "requiresShopTypes": _validate_shop_types,
    "requiresShopRecommendations": _validate_shop_recommendations,
}

_REGISTERED_EXPECTED_OUTPUTS = frozenset(
    output_name
    for contract in RUNTIME_TOOL_CONTRACTS.values()
    for output_name in contract.expected_outputs
)
if frozenset(_EXPECTED_OUTPUT_VALIDATORS) != _REGISTERED_EXPECTED_OUTPUTS:
    raise RuntimeError(
        "Validator implementations do not match the central runtime tool contracts"
    )


def validate_step_result(
    context: ValidatorContext,
    step_context: ValidatorStepContext,
) -> StepValidationResult:
    """Validate one step against the server-owned expected-output registry."""

    identity_error = _execution_identity_error(context, step_context)
    if identity_error is not None:
        return identity_error

    try:
        validate_expected_output_declaration(
            step_context.step.tool_name,
            step_context.step.expected_output,
        )
    except ExpectedOutputContractError as exc:
        return _invalid_step(
            step_context.step,
            exc.code,
            str(exc),
        )

    outcomes: list[StepValidationOutcome] = []
    summaries: dict[str, Any] = {}
    first_code = None
    first_reason = None
    for contract_name, required in step_context.step.expected_output.items():
        if contract_name == "requiresGuideDecision":
            execution = step_context.execution_result
            requested = set(execution.resolved_arguments.get("productIds", []))
            candidate_ids: set[int] = set()
            for prior in context.steps:
                if prior.step.tool_name != "search_products" or prior.normalized_output is None:
                    continue
                candidate_ids.update(prior.normalized_output.values.get("productIds", []))
            # A direct comparison has no search step by design.  In that shape
            # the only other trusted identity source is the server-published
            # shoppingGuide.comparedIds reference that Executor already
            # resolved and equality-checked against the accepted Plan.
            direct_compare_source = step_context.step.argument_sources.get(
                "productIds"
            )
            has_trusted_direct_selection = (
                direct_compare_source is not None
                and direct_compare_source.kind == "shopping_guide"
                and direct_compare_source.reference == "comparedIds"
                and len(requested) in {2, 3}
            )
            if (
                not requested
                or (
                    not requested.issubset(candidate_ids)
                    and not has_trusted_direct_selection
                )
            ):
                return _invalid_step(
                    step_context.step,
                    "product_provenance_mismatch",
                    "比较商品 ID 必须来自当前计划的 search_products 候选或服务端绑定的 2 至 3 件已展示商品",
                    {"requestedProductIds": sorted(requested),
                     "candidateProductIds": sorted(candidate_ids)},
                )
        validator = _EXPECTED_OUTPUT_VALIDATORS.get(contract_name)
        if required is not True or validator is None:
            return _invalid_step(
                step_context.step,
                "unsupported_expected_output",
                f"Validator不支持expectedOutput契约：{contract_name}",
            )
        outcome, code, reason, summary = validator(step_context)
        outcomes.append(outcome)
        summaries[contract_name] = summary
        if first_code is None and code is not None:
            first_code = code
            first_reason = reason

    if "invalid_evidence" in outcomes:
        return _invalid_step(
            step_context.step,
            first_code or "invalid_evidence",
            first_reason or "步骤证据无法通过校验",
            summaries,
        )
    if "insufficient_evidence" in outcomes:
        return _insufficient_step(
            step_context.step,
            first_code or "insufficient_evidence",
            first_reason or "步骤证据不足",
            summaries,
        )
    return StepValidationResult(
        stepId=step_context.step.step_id,
        outcome="satisfied",
        expectedOutput=deepcopy(step_context.step.expected_output),
        evidenceSummary=summaries,
    )


def validate_task_result(context: ValidatorContext) -> ValidatorResult:
    """Apply every deterministic expected-output contract to the Plan."""

    step_results = [
        validate_step_result(context, step_context)
        for step_context in context.steps
    ]
    invalid = next(
        (item for item in step_results if item.outcome == "invalid_evidence"),
        None,
    )
    if invalid is not None:
        return ValidatorResult(
            outcome="validation_failed",
            taskId=context.task_id,
            planId=context.plan.plan_id,
            basedOnRevision=context.task_revision,
            stepResults=step_results,
            errorCode=invalid.error_code,
            reason=invalid.reason,
        )
    insufficient = next(
        (
            item
            for item in step_results
            if item.outcome == "insufficient_evidence"
        ),
        None,
    )
    if insufficient is not None:
        return ValidatorResult(
            outcome="insufficient_evidence",
            taskId=context.task_id,
            planId=context.plan.plan_id,
            basedOnRevision=context.task_revision,
            stepResults=step_results,
            errorCode=insufficient.error_code,
            reason=insufficient.reason,
        )
    return ValidatorResult(
        outcome="passed",
        taskId=context.task_id,
        planId=context.plan.plan_id,
        basedOnRevision=context.task_revision,
        stepResults=step_results,
    )


def _evidence_failure_result(
    state: TaskState,
    error: ValidatorEvidenceError,
) -> ValidatorResult:
    return ValidatorResult(
        outcome="validation_failed",
        taskId=state.task_id,
        planId=state.active_plan.plan_id,
        basedOnRevision=state.revision,
        errorCode=error.code,
        reason=str(error),
    )


def _materialize_candidate_scope(
    state: TaskState,
    plan: TaskPlan,
    step: PlanStep,
    ranking_output: Any,
    displayed_ids: list[int],
    guide: ShoppingGuideState,
) -> CandidateScope:
    """Freeze a server-owned CandidateScope from the passed search output.

    ``scopeId`` is generated by the server from the task and the revision the
    Validator actually inspected.  The requirements/brand-avoidance snapshots
    come from the same compiled guide contract the Executor used, so a later
    scope-preserving turn can prove the base conditions did not drift.
    """

    if guide.category is None:
        raise ValueError("candidate_scope_category_missing")
    scope_id = f"scope-{state.task_id}-{state.revision}"
    return CandidateScope(
        scope_id=scope_id,
        task_id=state.task_id,
        source_revision=state.revision,
        source_plan_id=plan.plan_id,
        source_step_id=step.step_id,
        category=guide.category,
        candidate_pool_ids=list(ranking_output.candidate_pool_ids),
        ranked_item_ids=list(ranking_output.ranked_item_ids),
        visible_product_ids=displayed_ids,
        requirements_snapshot=[
            item.model_dump(mode="json")
            for item in compiled_shopping_requirements(guide)
        ],
        brand_avoidances_snapshot=[
            item.model_dump(mode="json") for item in guide.brand_avoidances
        ],
        evidence_refs=list(ranking_output.evidence_refs),
        created_at=datetime.now(timezone.utc).isoformat(),
        status="active",
    )


def _scope_validator_presentations(summary: object) -> list[int]:
    presentations = (
        summary.get("productPresentations")
        if isinstance(summary, dict) else None
    )
    if not isinstance(presentations, list):
        return []
    return [
        item.get("productId")
        for item in presentations[:3]
        if isinstance(item, dict) and type(item.get("productId")) is int
    ]


async def persist_validator_result(
    state: TaskState,
    result: ValidatorResult,
    *,
    extra_domain_state_patch: dict[str, Any] | None = None,
) -> TaskState:
    """Persist the Validator decision with the revision it actually inspected."""

    plan = _validate_selection_boundary(state)
    if (
        result.task_id != state.task_id
        or result.plan_id != plan.plan_id
        or result.based_on_revision != state.revision
    ):
        raise ValidatorEvidenceError(
            "validation_result_mismatch",
            "ValidatorResult与待更新的TaskState快照不匹配",
        )
    target_plan_status = "completed" if result.outcome == "passed" else "failed"
    updated_plan = transition_plan_status(plan, target_plan_status)
    domain_patch: dict[str, Any] = {
        "validationResult": result.model_dump(by_alias=True, mode="json")
    }
    if extra_domain_state_patch:
        overlap = set(domain_patch).intersection(extra_domain_state_patch)
        if overlap:
            raise ValueError("extra validator domain patch overlaps owned keys")
        domain_patch.update(extra_domain_state_patch)
    active_plan: TaskPlan | None = updated_plan
    compound = state.domain_state.get("compoundComparison")
    if (
        result.outcome == "passed"
        and isinstance(compound, dict)
        and compound.get("status") == "awaiting_search_validation"
        and compound.get("kind") == "compare_first_two"
        and compound.get("taskId") == state.task_id
    ):
        if len(plan.steps) != 1 or plan.steps[0].tool_name != "search_products":
            raise ValidatorEvidenceError(
                "compound_comparison_source_mismatch",
                "复合比较只能绑定当前已通过校验的唯一搜索步骤",
            )
        satisfied = [
            item for item in result.step_results
            if item.step_id == plan.steps[0].step_id and item.outcome == "satisfied"
        ]
        summary = (
            satisfied[0].evidence_summary.get("requiresProductCandidates")
            if len(satisfied) == 1 else None
        )
        presentations = (
            summary.get("productPresentations")
            if isinstance(summary, dict) else None
        )
        raw_outputs = state.domain_state.get("stepOutputs")
        raw_output = (
            raw_outputs.get(plan.steps[0].step_id)
            if isinstance(raw_outputs, dict) else None
        )
        try:
            normalized_output = NormalizedStepOutput.model_validate(raw_output)
            validate_normalized_output_values(
                "search_products", normalized_output.values
            )
        except (ValueError, ExpectedOutputContractError) as exc:
            raise ValidatorEvidenceError(
                "compound_comparison_output_invalid",
                "复合比较的搜索输出不满足运行时合同",
            ) from exc
        normalized_presentations = normalized_output.values[
            "candidateSupport"
        ].get("productPresentations")
        if (
            normalized_output.task_id != state.task_id
            or normalized_output.plan_id != plan.plan_id
            or normalized_output.step_id != plan.steps[0].step_id
            or presentations != normalized_presentations
        ):
            raise ValidatorEvidenceError(
                "compound_comparison_output_identity_mismatch",
                "复合比较展示与当前 Task/Plan/Step 的规范化输出不一致",
            )
        displayed_ids = [
            item.get("productId")
            for item in presentations[:3]
            if isinstance(item, dict) and type(item.get("productId")) is int
        ] if isinstance(presentations, list) else []
        if len(displayed_ids) < 2 or len(displayed_ids) != len(set(displayed_ids)):
            raise ValidatorEvidenceError(
                "compound_comparison_candidates_missing",
                "Validator 放行的有序展示结果不足两件，不能绑定前两个",
            )
        try:
            ranking_output = normalize_persisted_ranking_values(
                normalized_output.values
            )
        except TwoStageRankingContractError as exc:
            raise ValidatorEvidenceError(
                "compound_comparison_ranking_invalid", str(exc)
            ) from exc
        guide = ShoppingGuideState.model_validate(
            state.domain_state.get("shoppingGuide")
        )
        new_scope = _materialize_candidate_scope(
            state,
            plan,
            plan.steps[0],
            ranking_output,
            displayed_ids,
            guide,
        )
        previous_scope_raw = state.domain_state.get("candidateScope")
        if isinstance(previous_scope_raw, dict):
            try:
                previous_scope = CandidateScope.model_validate(previous_scope_raw)
            except ValueError:
                previous_scope = None
            if (
                previous_scope is not None
                and previous_scope.task_id == state.task_id
                and previous_scope.status == "active"
                and previous_scope.scope_id != new_scope.scope_id
            ):
                domain_patch["candidateScopeInvalidation"] = {
                    "scopeId": previous_scope.scope_id,
                    "status": "invalidated",
                    "invalidationReason": "compound_search_replaced_scope",
                    "replacedByScopeId": new_scope.scope_id,
                    "invalidatedAt": datetime.now(timezone.utc).isoformat(),
                }
        domain_patch.update({
            "validationResult": None,
            "candidateScope": new_scope.model_dump(by_alias=True, mode="json"),
            "shoppingGuide": guide.model_copy(update={
                "mode": "compare",
                "candidate_ids": displayed_ids,
                "compared_ids": displayed_ids[:2],
            }).model_dump(by_alias=True, mode="json"),
            "scopeRerankRequest": None,
            "compoundComparison": {
                "status": "ready",
                "kind": "compare_first_two",
                "taskId": state.task_id,
                "sourcePlanId": plan.plan_id,
                "sourceStepId": plan.steps[0].step_id,
                "productIds": displayed_ids[:2],
            },
        })
        active_plan = None
    elif (
        result.outcome == "passed"
        and isinstance(compound, dict)
        and compound.get("status") == "ready"
        and len(plan.steps) == 1
        and plan.steps[0].tool_name == "compare_products"
    ):
        domain_patch["compoundComparison"] = None
    elif (
        result.outcome == "passed"
        and state.task_type == "ecommerce_guide"
        and len(plan.steps) == 1
        and plan.steps[0].tool_name == "rerank_products_in_scope"
    ):
        # A passed in-scope rerank updates ranked/visible order inside the SAME
        # scopeId.  The request is one-turn: it is consumed by this update.
        step = plan.steps[0]
        satisfied = [
            item for item in result.step_results
            if item.step_id == step.step_id and item.outcome == "satisfied"
        ]
        summary = (
            satisfied[0].evidence_summary.get("requiresScopeRerank")
            if len(satisfied) == 1 else None
        )
        raw_outputs = state.domain_state.get("stepOutputs")
        raw_output = (
            raw_outputs.get(step.step_id)
            if isinstance(raw_outputs, dict) else None
        )
        try:
            normalized_output = NormalizedStepOutput.model_validate(raw_output)
            validate_normalized_output_values(
                "rerank_products_in_scope", normalized_output.values
            )
        except (ValueError, ExpectedOutputContractError) as exc:
            raise ValidatorEvidenceError(
                "scope_rerank_output_invalid",
                "范围重排输出不满足运行时合同",
            ) from exc
        if (
            normalized_output.task_id != state.task_id
            or normalized_output.plan_id != plan.plan_id
            or normalized_output.step_id != step.step_id
        ):
            raise ValidatorEvidenceError(
                "scope_rerank_output_identity_mismatch",
                "范围重排输出与当前 Task/Plan/Step 不一致",
            )
        try:
            rerank_output = normalize_persisted_scope_rerank_values(
                normalized_output.values
            )
        except TwoStageRankingContractError as exc:
            raise ValidatorEvidenceError(
                "scope_rerank_output_invalid", str(exc)
            ) from exc
        try:
            current_scope = CandidateScope.model_validate(
                state.domain_state.get("candidateScope")
            )
        except ValueError as exc:
            raise ValidatorEvidenceError(
                "scope_rerank_scope_missing",
                "范围重排必须绑定有效的 server-owned CandidateScope",
            ) from exc
        if (
            current_scope.status != "active"
            or current_scope.task_id != state.task_id
            or current_scope.scope_id != rerank_output.scope_id
        ):
            raise ValidatorEvidenceError(
                "scope_rerank_scope_invalid",
                "范围重排引用的 scope 已失效或跨任务",
            )
        if not set(rerank_output.ranked_item_ids).issubset(
            current_scope.ranked_item_ids
        ):
            raise ValidatorEvidenceError(
                "scope_rerank_output_outside_scope",
                "范围重排输出必须属于当前 scope 的 rankedItemIds",
            )
        displayed_ids = _scope_validator_presentations(summary)
        if not displayed_ids or len(displayed_ids) != len(set(displayed_ids)):
            raise ValidatorEvidenceError(
                "scope_rerank_visible_products_missing",
                "Validator 放行的有序展示结果无效，不能更新 scope 展示顺序",
            )
        updated_scope = current_scope.model_copy(update={
            "ranked_item_ids": list(rerank_output.ranked_item_ids),
            "visible_product_ids": displayed_ids,
        })
        domain_patch["candidateScope"] = updated_scope.model_dump(
            by_alias=True, mode="json"
        )
        domain_patch["scopeRerankRequest"] = None
    elif (
        result.outcome == "passed"
        and state.task_type == "ecommerce_guide"
        and len(plan.steps) == 1
        and plan.steps[0].tool_name == "search_products"
    ):
        # A passed full search freezes the new trusted range and invalidates any
        # prior scope it replaces, recording the reason for the audit trail.
        step = plan.steps[0]
        satisfied = [
            item for item in result.step_results
            if item.step_id == step.step_id and item.outcome == "satisfied"
        ]
        summary = (
            satisfied[0].evidence_summary.get("requiresProductCandidates")
            if len(satisfied) == 1 else None
        )
        raw_outputs = state.domain_state.get("stepOutputs")
        raw_output = (
            raw_outputs.get(step.step_id)
            if isinstance(raw_outputs, dict) else None
        )
        try:
            normalized_output = NormalizedStepOutput.model_validate(raw_output)
            validate_normalized_output_values(
                "search_products", normalized_output.values
            )
        except (ValueError, ExpectedOutputContractError) as exc:
            raise ValidatorEvidenceError(
                "scope_source_output_invalid",
                "范围物化依赖的搜索输出不满足运行时合同",
            ) from exc
        if (
            normalized_output.task_id != state.task_id
            or normalized_output.plan_id != plan.plan_id
            or normalized_output.step_id != step.step_id
        ):
            raise ValidatorEvidenceError(
                "scope_source_identity_mismatch",
                "范围物化依赖与当前 Task/Plan/Step 不一致",
            )
        try:
            ranking_output = normalize_persisted_ranking_values(
                normalized_output.values
            )
        except TwoStageRankingContractError as exc:
            raise ValidatorEvidenceError(
                "scope_source_ranking_invalid", str(exc)
            ) from exc
        try:
            guide = ShoppingGuideState.model_validate(
                state.domain_state.get("shoppingGuide")
            )
        except ValueError as exc:
            raise ValidatorEvidenceError(
                "scope_source_guide_missing",
                "范围物化依赖的导购状态缺失",
            ) from exc
        displayed_ids = _scope_validator_presentations(summary)
        if (
            not displayed_ids
            or len(displayed_ids) != len(set(displayed_ids))
            or not set(displayed_ids).issubset(ranking_output.ranked_item_ids)
        ):
            raise ValidatorEvidenceError(
                "scope_visible_products_invalid",
                "Validator 放行的有序展示结果无效，不能物化 scope",
            )
        new_scope = _materialize_candidate_scope(
            state, plan, step, ranking_output, displayed_ids, guide
        )
        previous_scope_raw = state.domain_state.get("candidateScope")
        if isinstance(previous_scope_raw, dict):
            try:
                previous_scope = CandidateScope.model_validate(previous_scope_raw)
            except ValueError:
                previous_scope = None
            if (
                previous_scope is not None
                and previous_scope.task_id == state.task_id
                and previous_scope.status == "active"
                and previous_scope.scope_id != new_scope.scope_id
            ):
                domain_patch["candidateScopeInvalidation"] = {
                    "scopeId": previous_scope.scope_id,
                    "status": "invalidated",
                    "invalidationReason": "new_full_catalog_search_replaced_scope",
                    "replacedByScopeId": new_scope.scope_id,
                    "invalidatedAt": datetime.now(timezone.utc).isoformat(),
                }
        domain_patch["candidateScope"] = new_scope.model_dump(
            by_alias=True, mode="json"
        )
        domain_patch["shoppingGuide"] = guide.model_copy(update={
            "candidate_ids": list(new_scope.visible_product_ids),
            "evidence_status": "complete",
        }).model_dump(by_alias=True, mode="json")
        domain_patch["scopeRerankRequest"] = None
    keep_ready_for_followup = state.task_type == "ecommerce_guide"
    if keep_ready_for_followup:
        refreshed_v2 = refresh_shopping_state_v2_after_validation(
            state,
            domain_patch,
        )
        if refreshed_v2 is not None:
            domain_patch["shoppingTaskStateV2"] = refreshed_v2
    patch = TaskStatePatchRequest(
        expectedRevision=state.revision,
        actor="agent",
        # A validated Plan completes this turn's action, not the user's durable
        # shopping task.  Keep the task ready so a later user message can
        # incrementally revise constraints and produce a fresh Plan.
        status="ready" if keep_ready_for_followup else (
            "completed" if result.outcome == "passed" else "ready"
        ),
        activePlan=active_plan,
        domainStatePatch=domain_patch,
    )
    return await update_task_state(state.task_id, patch)


async def run_validator_phase(
    state: TaskState,
    context_view: Any | None = None,
    *,
    extra_domain_state_patch_factory: (
        Callable[[ValidatorResult, int], dict[str, Any]] | None
    ) = None,
) -> tuple[ValidatorResult, TaskState]:
    """Build, run, and OCC-persist one deterministic Validator decision.

    When context_view (ValidatorContextView) is provided, the Validator uses it
    as an authoritative boundary: it must not read evidence or constraints from
    full chat history or ToolTrace bodies outside the View.
    """

    _validate_selection_boundary(state)
    try:
        context = build_validator_context(state, context_view=context_view)
        result = validate_task_result(context)
    except ValidatorEvidenceError as exc:
        result = _evidence_failure_result(state, exc)
    extra_patch = (
        extra_domain_state_patch_factory(result, state.revision + 1)
        if extra_domain_state_patch_factory is not None
        else None
    )
    updated_state = await persist_validator_result(
        state, result, extra_domain_state_patch=extra_patch
    )
    return result, updated_state
