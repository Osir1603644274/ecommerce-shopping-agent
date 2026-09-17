import json
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .executor import NormalizedStepOutput
from .model_compat import tool_choice_kwargs
from .planner import (
    PlannerContext,
    PlannerToolSpec,
    accept_planner_model_output,
    build_planner_context,
)
from .planning import (
    PLAN_ARGUMENT_SOURCE_CONTRACT,
    PlannerModelOutput,
    PlanStep,
    PlanStepProposal,
    TaskPlan,
)
from .task_state import (
    TaskConstraint,
    TaskFact,
    TaskState,
    TaskStatePatchRequest,
    TaskStatus,
    update_task_state,
)
from .validator import ValidatorResult


REPLANNER_SUBMISSION_TOOL_NAME = "submit_replanner_output"
DEFAULT_MAX_REPLAN_ATTEMPTS = 1

ReplannerModelOutcome = Literal["replanned", "needs_user_input"]
ReplannerOutcome = Literal[
    "replanned",
    "needs_user_input",
    "replanning_failed",
]

REPLANNER_SYSTEM_PROMPT = (
    "你是北京出行Agent的Replanner，只恢复一个已经失败的Plan，不回答用户问题，也不执行工具。"
    "必须结合failedPlan、failure和当前TaskState事实解释性地选择新路线。"
    "只能选择ReplannerContext.candidateTools中的工具；每个expectedOutput只能使用对应工具"
    "的expectedOutputContracts。每个参数都必须声明argumentSources，不能创造商户、地点、"
    "shopId或用户约束。新路线不能与failedPlan的工具、参数、来源和输出合同完全相同。"
    "reusableStepOutputs只用于理解旧计划已经获得的可靠信息；当前最小版本不能把旧Plan输出"
    "声明为prior_step，新Plan若需要该值，必须在新Plan中安排产生它的步骤。"
    "只有必须由用户提供的信息才返回needs_user_input。不要生成planId、basedOnRevision、"
    "Plan状态、PlanStep状态或replanning_failed。"
    "最后只调用submit_replanner_output一次提交结构化结果。"
)
REPLANNER_SYSTEM_PROMPT += (
    "\n商品导购恢复规则：最多只扩大一次 search_products 召回，不得删除、改写或放宽"
    "任何用户明确的硬约束，也不得把 unknown 当作 pass。扩大后仍无完全匹配时，"
    "返回 needs_user_input，并询问用户愿意调整哪一项明确要求；不要自行替用户选择。"
)


def _argument_source_matrix_text() -> str:
    """Render the shared five-shape source contract without invented keys."""

    lines = [
        "argumentSources 只允许以下五种合法形状（reference 必须由当前上下文支持）："
    ]
    for entry in PLAN_ARGUMENT_SOURCE_CONTRACT:
        shape = json.dumps(entry["shape"], ensure_ascii=False)
        reference = entry["reference"]
        if reference.get("required"):
            detail = reference.get("format")
            if not detail and reference.get("allowed"):
                detail = "允许值：" + "、".join(
                    str(item) for item in reference["allowed"]
                )
            lines.append(
                f'- "{entry["kind"]}"：形状 {shape}；reference 必填'
                f'（{detail or "只能使用当前上下文实际发布的引用"}）。{entry["note"]}'
            )
        else:
            lines.append(
                f'- "{entry["kind"]}"：形状 {shape}；不得包含 reference。'
                f'{entry["note"]}'
            )
    return "\n".join(lines)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("字段不能为空")
    return normalized


class ReplannerSelectionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ReplannerValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        raw_arguments: dict[str, Any] | None = None,
    ):
        self.code = code
        self.errors = list(errors or [])
        self.raw_arguments = raw_arguments
        super().__init__(message)


class ReplannerContext(BaseModel):
    """A revision-bound recovery view for one failed Plan."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    task_id: str = Field(alias="taskId", min_length=1)
    task_revision: int = Field(alias="taskRevision", ge=1)
    task_status: TaskStatus = Field(alias="taskStatus")
    goal: str = Field(min_length=1, max_length=500)
    facts: tuple[TaskFact, ...] = ()
    constraints: tuple[TaskConstraint, ...] = ()
    unknowns: tuple[str, ...] = ()
    user_message: str = Field(alias="userMessage", min_length=1, max_length=2000)
    failed_plan: TaskPlan = Field(alias="failedPlan")
    failure: ValidatorResult
    reusable_step_outputs: dict[str, NormalizedStepOutput] = Field(
        default_factory=dict,
        alias="reusableStepOutputs",
    )
    candidate_tools: tuple[PlannerToolSpec, ...] = Field(
        default=(),
        alias="candidateTools",
    )
    replan_attempt: int = Field(alias="replanAttempt", ge=1)
    max_replan_attempts: int = Field(alias="maxReplanAttempts", ge=1, le=5)
    system_policies: dict[str, Any] = Field(
        default_factory=dict,
        alias="systemPolicies",
    )
    # Same shopping-guide source contract the Planner uses; the Replanner must
    # reuse the server snapshot verbatim, never a looser set of rules.
    shopping_guide_sources: dict[str, Any] | None = Field(
        default=None,
        alias="shoppingGuideSources",
    )
    long_term_memory: tuple[dict[str, str], ...] = Field(
        default=(), alias="longTermMemory", repr=False, exclude_if=lambda value: value == ()
    )

    @field_validator("task_id", "goal", "user_message")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_required_text(value)

    @model_validator(mode="after")
    def validate_recovery_identity(self) -> "ReplannerContext":
        if self.failed_plan.status != "failed":
            raise ValueError("ReplannerContext.failedPlan必须是failed")
        if self.failure.task_id != self.task_id:
            raise ValueError("ValidatorResult不属于当前Task")
        if self.failure.plan_id != self.failed_plan.plan_id:
            raise ValueError("ValidatorResult不属于failedPlan")
        if self.failure.outcome != "insufficient_evidence":
            raise ValueError("最小Replanner只处理insufficient_evidence")
        return self


class ReplannerModelOutput(BaseModel):
    """The only recovery proposal the model may submit."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    outcome: ReplannerModelOutcome
    steps: list[PlanStepProposal] = Field(default_factory=list)
    question: str | None = Field(default=None, max_length=300)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "ReplannerModelOutput":
        planner_outcome = (
            "planned" if self.outcome == "replanned" else "needs_user_input"
        )
        PlannerModelOutput(
            outcome=planner_outcome,
            steps=self.steps,
            question=self.question,
        )
        return self


class ReplannerResult(BaseModel):
    """The Runtime-owned result of one recovery attempt."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    outcome: ReplannerOutcome
    task_id: str = Field(alias="taskId", min_length=1)
    previous_plan_id: str = Field(alias="previousPlanId", min_length=1)
    based_on_revision: int = Field(alias="basedOnRevision", ge=1)
    attempt: int = Field(ge=1)
    plan: TaskPlan | None = None
    question: str | None = Field(default=None, max_length=300)
    error_code: str | None = Field(
        default=None,
        alias="errorCode",
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=64,
    )
    reason: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=_utc_now, alias="createdAt")

    @field_validator("question", "error_code", "reason")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "ReplannerResult":
        if self.outcome == "replanned":
            if self.plan is None:
                raise ValueError("replanned结果必须包含plan")
            if self.plan.plan_id == self.previous_plan_id:
                raise ValueError("Replanner必须生成新的planId")
            if self.plan.based_on_revision != self.based_on_revision:
                raise ValueError("新Plan与ReplannerResult必须基于同一revision")
            if any((self.question, self.error_code, self.reason)):
                raise ValueError("replanned结果不能包含question或错误信息")
            return self
        if self.outcome == "needs_user_input":
            if self.question is None:
                raise ValueError("needs_user_input结果必须包含question")
            if self.plan is not None or self.error_code is not None or self.reason is not None:
                raise ValueError("needs_user_input结果只能包含question")
            return self
        if self.plan is not None or self.question is not None:
            raise ValueError("replanning_failed结果不能包含plan或question")
        if self.error_code is None or self.reason is None:
            raise ValueError("replanning_failed结果必须包含errorCode和reason")
        return self


def _replanner_submission_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": REPLANNER_SUBMISSION_TOOL_NAME,
            "description": "提交Replanner生成的新步骤提案或必要的用户澄清问题。",
            "parameters": ReplannerModelOutput.model_json_schema(by_alias=True),
        },
    }


def _validator_result_for_failed_plan(state: TaskState) -> ValidatorResult:
    if state.status != "ready":
        raise ReplannerSelectionError(
            "task_not_ready_for_replanning",
            f"TaskState状态为{state.status}，不能开始Replanner",
        )
    if state.pending_questions:
        raise ReplannerSelectionError(
            "pending_questions_exist",
            "TaskState仍有待用户回答的问题，不能开始Replanner",
        )
    if state.active_plan is None or state.active_plan.status != "failed":
        raise ReplannerSelectionError(
            "failed_plan_missing",
            "Replanner需要一个failed activePlan",
        )

    raw_failure = state.domain_state.get("validationResult")
    if not isinstance(raw_failure, dict):
        raise ReplannerSelectionError(
            "validation_result_missing",
            "TaskState中缺少ValidatorResult",
        )
    try:
        failure = ValidatorResult.model_validate(raw_failure)
    except ValueError as exc:
        raise ReplannerSelectionError(
            "invalid_validation_result",
            "TaskState中的ValidatorResult无法校验",
        ) from exc
    if (
        failure.task_id != state.task_id
        or failure.plan_id != state.active_plan.plan_id
    ):
        raise ReplannerSelectionError(
            "validation_result_mismatch",
            "ValidatorResult与当前failed Plan身份不一致",
        )
    if failure.outcome != "insufficient_evidence":
        raise ReplannerSelectionError(
            "failure_not_replannable",
            "最小Replanner只自动处理insufficient_evidence",
        )
    return failure


def _stored_attempt_count(state: TaskState) -> int:
    raw_count = state.domain_state.get("replanAttemptCount", 0)
    if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < 0:
        raise ReplannerSelectionError(
            "invalid_replan_attempt_count",
            "TaskState.domainState.replanAttemptCount必须是非负整数",
        )
    return raw_count


def _max_attempts(system_policies: dict[str, Any] | None) -> int:
    raw_max = (system_policies or {}).get(
        "maxReplanAttempts",
        DEFAULT_MAX_REPLAN_ATTEMPTS,
    )
    if (
        not isinstance(raw_max, int)
        or isinstance(raw_max, bool)
        or not 1 <= raw_max <= 5
    ):
        raise ReplannerSelectionError(
            "invalid_replan_policy",
            "maxReplanAttempts必须是1到5之间的整数",
        )
    return raw_max


def _reusable_step_outputs(
    state: TaskState,
    failed_plan: TaskPlan,
) -> dict[str, NormalizedStepOutput]:
    raw_outputs = state.domain_state.get("stepOutputs", {})
    if not isinstance(raw_outputs, dict):
        raise ReplannerSelectionError(
            "invalid_step_outputs",
            "TaskState.domainState.stepOutputs必须是对象",
        )

    reusable: dict[str, NormalizedStepOutput] = {}
    for step in failed_plan.steps:
        raw_output = raw_outputs.get(step.step_id)
        if raw_output is None:
            continue
        try:
            output = NormalizedStepOutput.model_validate(raw_output)
        except ValueError as exc:
            raise ReplannerSelectionError(
                "invalid_step_output",
                f"failed Plan的规范化输出无法校验：{step.step_id}",
            ) from exc
        if (
            output.task_id != state.task_id
            or output.plan_id != failed_plan.plan_id
            or output.step_id != step.step_id
        ):
            raise ReplannerSelectionError(
                "step_output_mismatch",
                f"规范化输出不属于当前failed Plan：{step.step_id}",
            )
        reusable[step.step_id] = output.model_copy(deep=True)
    return reusable


def _is_terminal_replanning_failure(state: TaskState) -> bool:
    raw_result = state.domain_state.get("replannerResult")
    if not isinstance(raw_result, dict) or state.active_plan is None:
        return False
    try:
        result = ReplannerResult.model_validate(raw_result)
    except ValueError:
        return True
    return (
        result.previous_plan_id == state.active_plan.plan_id
        and result.outcome == "replanning_failed"
    )


def should_run_replanner(state: TaskState) -> bool:
    """Return whether persisted state represents a recoverable failed Plan."""

    if _is_terminal_replanning_failure(state):
        return False
    try:
        _validator_result_for_failed_plan(state)
        _stored_attempt_count(state)
    except ReplannerSelectionError:
        return False
    return True


def build_replanner_context(
    state: TaskState,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    system_policies: dict[str, Any] | None = None,
) -> ReplannerContext:
    """Build a detached recovery view from trusted persisted evidence."""

    failure = _validator_result_for_failed_plan(state)
    attempt = _stored_attempt_count(state) + 1
    max_attempts = _max_attempts(system_policies)
    if attempt > max_attempts:
        raise ReplannerSelectionError(
            "replan_attempts_exhausted",
            f"已达到最大Replanner次数：{max_attempts}",
        )

    normalized_message = user_message.strip() or "继续恢复当前任务"
    planner_context = build_planner_context(
        state,
        normalized_message,
        candidate_tool_schemas,
        system_policies,
    )
    return ReplannerContext(
        taskId=state.task_id,
        taskRevision=state.revision,
        taskStatus=state.status,
        goal=state.goal,
        facts=planner_context.facts,
        constraints=planner_context.constraints,
        unknowns=planner_context.unknowns,
        userMessage=normalized_message,
        failedPlan=state.active_plan.model_copy(deep=True),
        failure=failure.model_copy(deep=True),
        reusableStepOutputs=_reusable_step_outputs(state, state.active_plan),
        candidateTools=planner_context.candidate_tools,
        replanAttempt=attempt,
        maxReplanAttempts=max_attempts,
        systemPolicies=deepcopy(system_policies or {}),
        shoppingGuideSources=deepcopy(planner_context.shopping_guide_sources),
    )


def build_replanner_context_from_view(
    view: Any,
    *,
    candidate_tool_schemas: list[dict[str, Any]] | None = None,
    system_policies: dict[str, Any] | None = None,
) -> ReplannerContext:
    """Build a ReplannerContext from a ReplannerContextView (context_pack mode).

    In context_pack mode the view is authoritative — the Replanner MUST NOT
    reconstruct context from raw TaskState or full chat history.
    """
    from .task_state import TaskFact, TaskConstraint
    from .planning import TaskPlan

    facts = tuple(
        TaskFact(
            key=f.get("key", ""),
            value=f.get("value", ""),
            certainty=f.get("certainty", "confirmed"),
            source=f.get("source", ""),
        )
        for f in view.confirmed_facts
    )
    constraints = tuple(
        TaskConstraint(
            key=c.get("key", ""),
            operator=c.get("operator", "eq"),
            value=c.get("value", ""),
            source=c.get("source", ""),
        )
        for c in view.constraints
    )

    # Build candidate tools from schemas or remaining_tool_names
    candidate_tools: tuple[PlannerToolSpec, ...] = ()
    view_tool_schemas = view.candidate_tool_schemas
    if view_tool_schemas:
        from .planner import _planner_tool_spec
        candidate_tools = tuple(
            _planner_tool_spec(schema) for schema in view_tool_schemas
            if schema.get("function", schema).get("name") in view.remaining_tool_names
        )

    from .validator import ValidatorResult
    if view.failed_plan is None:
        raise ReplannerSelectionError(
            "view_failed_plan_missing",
            "ReplannerView must contain the exact failed Plan snapshot",
        )
    failed_plan = view.failed_plan.model_copy(deep=True)
    try:
        failure = ValidatorResult.model_validate(view.failure)
    except ValueError as exc:
        raise ReplannerSelectionError(
            "view_failure_invalid",
            "ReplannerView must contain the exact Validator failure",
        ) from exc

    reusable_outputs: dict[str, NormalizedStepOutput] = {}
    try:
        reusable_outputs = {
            step_id: NormalizedStepOutput.model_validate(output)
            for step_id, output in view.reusable_step_outputs.items()
        }
    except ValueError as exc:
        raise ReplannerSelectionError(
            "view_step_outputs_invalid",
            "ReplannerView contains invalid reusable step outputs",
        ) from exc

    extra_policies = deepcopy(view.system_policies)

    return ReplannerContext(
        taskId=view.task_id,
        taskRevision=view.phase_task_revision,
        taskStatus="ready",
        goal=view.goal,
        facts=facts,
        constraints=constraints,
        unknowns=tuple(view.unknowns),
        userMessage=view.user_message or view.goal,
        failedPlan=failed_plan,
        failure=failure,
        reusableStepOutputs=reusable_outputs,
        candidateTools=candidate_tools,
        replanAttempt=view.replan_attempt,
        maxReplanAttempts=view.max_replan_attempts,
        systemPolicies=extra_policies,
        shoppingGuideSources=deepcopy(view.shopping_guide_sources),
        longTermMemory=tuple(deepcopy(view.long_term_memory)),
    )


def _planner_context_from_replanner(context: ReplannerContext) -> PlannerContext:
    return PlannerContext(
        taskId=context.task_id,
        taskRevision=context.task_revision,
        taskStatus=context.task_status,
        goal=context.goal,
        facts=context.facts,
        constraints=context.constraints,
        unknowns=context.unknowns,
        pendingQuestions=(),
        userMessage=context.user_message,
        candidateTools=context.candidate_tools,
        systemPolicies=deepcopy(context.system_policies),
        shoppingGuideSources=deepcopy(context.shopping_guide_sources),
        longTermMemory=tuple(deepcopy(context.long_term_memory)),
    )


def _step_route_signature(step: PlanStep | PlanStepProposal) -> dict[str, Any]:
    return {
        "toolName": step.tool_name,
        "arguments": step.arguments,
        "argumentSources": {
            name: source.model_dump(by_alias=True, mode="json")
            for name, source in step.argument_sources.items()
        },
        "expectedOutput": step.expected_output,
    }


def _route_signature(steps: list[PlanStep] | list[PlanStepProposal]) -> list[dict[str, Any]]:
    return [_step_route_signature(step) for step in steps]


def _failure_result(
    context: ReplannerContext,
    code: str,
    reason: str,
) -> ReplannerResult:
    return ReplannerResult(
        outcome="replanning_failed",
        taskId=context.task_id,
        previousPlanId=context.failed_plan.plan_id,
        basedOnRevision=context.task_revision,
        attempt=context.replan_attempt,
        errorCode=code,
        reason=reason[:500],
    )


def accept_replanner_model_output(
    context: ReplannerContext,
    output: ReplannerModelOutput,
    *,
    plan_id_factory: Callable[[], str] = lambda: f"plan-{uuid.uuid4().hex[:16]}",
) -> ReplannerResult:
    """Promote a recovery proposal through the existing Plan acceptance rules."""

    if output.outcome == "needs_user_input":
        return ReplannerResult(
            outcome="needs_user_input",
            taskId=context.task_id,
            previousPlanId=context.failed_plan.plan_id,
            basedOnRevision=context.task_revision,
            attempt=context.replan_attempt,
            question=output.question,
        )

    planner_output = PlannerModelOutput(
        outcome="planned",
        steps=output.steps,
    )
    planner_result = accept_planner_model_output(
        _planner_context_from_replanner(context),
        planner_output,
        plan_id_factory=plan_id_factory,
    )
    if planner_result.outcome != "planned" or planner_result.plan is None:
        return _failure_result(
            context,
            planner_result.error_code or "invalid_replan",
            planner_result.reason or "新计划没有通过服务器校验",
        )
    if planner_result.plan.plan_id == context.failed_plan.plan_id:
        return _failure_result(
            context,
            "replan_plan_id_conflict",
            "Replanner必须为新执行合同生成不同的planId",
        )
    if _route_signature(planner_result.plan.steps) == _route_signature(
        context.failed_plan.steps
    ):
        return _failure_result(
            context,
            "replan_unchanged",
            "新计划与失败计划的执行路线完全相同",
        )
    return ReplannerResult(
        outcome="replanned",
        taskId=context.task_id,
        previousPlanId=context.failed_plan.plan_id,
        basedOnRevision=context.task_revision,
        attempt=context.replan_attempt,
        plan=planner_result.plan,
    )


def build_replanner_messages(context: ReplannerContext) -> list[dict[str, Any]]:
    from .context_input import is_experimental_context_input, phase_context
    context_payload = context.model_dump(by_alias=True, mode="json")
    available_sources = _available_argument_sources(context)
    policy_keys = available_sources["system_policy"]["keys"]
    dynamic_contract = (
        _argument_source_matrix_text()
        + "\n\n当前 ReplannerContext 实际发布的 systemPolicies keys："
        + json.dumps(policy_keys, ensure_ascii=False)
        + "。kind=system_policy 只能引用该集合中的键，且 arguments 中的值必须与其精确一致。"
        + "如果没有合法来源支持 limit，不得添加 limit；无法形成安全的新路线时返回 "
        "needs_user_input，不得伪造来源。"
    )
    if is_experimental_context_input():
        return [{"role": "system", "content": REPLANNER_SYSTEM_PROMPT + "\n\n" + dynamic_contract},
                {"role": "user", "content": json.dumps(phase_context("replanner", {
                    "context": context_payload, "availableArgumentSources": available_sources}), ensure_ascii=False)}]
    return [
        {
            "role": "system",
            "content": REPLANNER_SYSTEM_PROMPT + "\n\n" + dynamic_contract,
        },
        {
            "role": "user",
            "content": (
                "请根据以下ReplannerContext提交结构化恢复结果：\n"
                + json.dumps(context_payload, ensure_ascii=False)
                + "\n当前可用参数来源：\n"
                + json.dumps(available_sources, ensure_ascii=False)
            ),
        },
    ]


def _available_argument_sources(context: ReplannerContext) -> dict[str, Any]:
    """Describe only source references actually available in this context."""

    task_state_references = {
        **{
            f"facts.{fact.key}": deepcopy(fact.value)
            for fact in context.facts
            if fact.certainty == "confirmed"
        },
        **{
            f"constraints.{constraint.key}": deepcopy(constraint.value)
            for constraint in context.constraints
        },
    }
    shopping_references = deepcopy(context.shopping_guide_sources or {})
    policies = deepcopy(context.system_policies)
    return {
        "task_goal": {
            "shape": {"kind": "task_goal"},
            "value": context.goal,
        },
        "task_state": {
            "shape": {"kind": "task_state", "reference": "facts.<key>"},
            "references": task_state_references,
        },
        "shopping_guide": {
            "shape": {"kind": "shopping_guide", "reference": "category"},
            "references": shopping_references,
        },
        "prior_step": {
            "shape": {
                "kind": "prior_step",
                "reference": "<stepId>.<outputField>",
            },
            "note": (
                "只能引用本次新 Plan 中排在前面的步骤；"
                "reusableStepOutputs 不能直接充当 prior_step"
            ),
        },
        "system_policy": {
            "shape": {
                "kind": "system_policy",
                "reference": "<publishedPolicyKey>",
            },
            "keys": sorted(policies),
            "references": policies,
        },
    }


def _pydantic_error_path(loc: Any) -> str:
    return ".".join(str(part) for part in loc) or "$"


def _parse_replanner_model_output(
    reply: Any,
) -> tuple[ReplannerModelOutput, dict[str, Any], list[dict[str, Any]]]:
    tool_calls = list(getattr(reply, "tool_calls", None) or [])
    matching_calls = [
        call
        for call in tool_calls
        if getattr(getattr(call, "function", None), "name", None)
        == REPLANNER_SUBMISSION_TOOL_NAME
    ]
    if len(matching_calls) != 1:
        raise ReplannerValidationError(
            "missing_structured_output",
            "Replanner模型必须且只能调用一次submit_replanner_output",
        )
    raw_arguments = getattr(matching_calls[0].function, "arguments", "")
    try:
        payload = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReplannerValidationError(
            "invalid_model_output",
            "Replanner模型提交的arguments不是合法JSON",
            errors=[{"path": "$", "message": "arguments 不是合法 JSON"}],
        ) from exc
    if not isinstance(payload, dict):
        raise ReplannerValidationError(
            "invalid_model_output",
            "Replanner模型提交结果必须是JSON对象",
            errors=[{"path": "$", "message": "提交结果必须是 JSON 对象"}],
        )
    try:
        output = ReplannerModelOutput.model_validate(payload)
    except ValidationError as exc:
        errors = [
            {
                "path": _pydantic_error_path(item.get("loc", ())),
                "message": str(item.get("msg", "")),
            }
            for item in exc.errors()
        ]
        raise ReplannerValidationError(
            "invalid_model_output",
            str(exc)[:500],
            errors=errors,
            raw_arguments=payload,
        ) from exc
    return output, payload, []


def _acceptance_errors(
    context: ReplannerContext,
    output: ReplannerModelOutput,
    result: ReplannerResult,
) -> list[dict[str, Any]]:
    """Locate server-side source failures in an otherwise valid payload."""

    if result.error_code == "invalid_argument_source":
        for step_index, step in enumerate(output.steps):
            for argument_name, source in step.argument_sources.items():
                if (
                    source.kind == "system_policy"
                    and source.reference not in context.system_policies
                ):
                    return [{
                        "path": (
                            f"steps.{step_index}.argumentSources."
                            f"{argument_name}.reference"
                        ),
                        "message": f"系统策略中不存在：{source.reference}",
                    }]
                if (
                    source.kind == "system_policy"
                    and step.arguments.get(argument_name)
                    != context.system_policies[source.reference]
                ):
                    return [{
                        "path": f"steps.{step_index}.arguments.{argument_name}",
                        "message": (
                            f"参数 {argument_name} 与声明来源的真实值不一致"
                        ),
                    }]
    return [{
        "path": "$",
        "message": f"{result.error_code or 'replanning_failed'}: {result.reason or ''}",
    }]


def _replanner_repair_message(
    context: ReplannerContext,
    *,
    previous_arguments: dict[str, Any] | None,
    error_code: str,
    reason: str,
    errors: list[dict[str, Any]],
    attempt: int,
) -> dict[str, Any]:
    structured_errors = deepcopy(errors) or [{
        "path": "$",
        "message": f"{error_code}: {reason}",
    }]
    repair_request = {
        "replannerRepairRequest": {
            "attempt": attempt,
            "previousToolCall": {
                "toolName": REPLANNER_SUBMISSION_TOOL_NAME,
                "arguments": deepcopy(previous_arguments or {}),
            },
            "validationError": {
                "errorCode": error_code,
                "reason": reason,
                "errors": structured_errors,
            },
            "availableArgumentSources": _available_argument_sources(context),
            "instruction": (
                "逐项修正 validationError.errors 指向的字段，并重新调用一次 "
                "submit_replanner_output。system_policy 只能使用 "
                "availableArgumentSources.system_policy.keys 中的键且值必须精确一致；"
                "没有合法来源支持的参数必须删除。无法形成安全新路线时返回 "
                "needs_user_input。不要重复原错误。"
            ),
        }
    }
    return {
        "role": "system",
        "content": json.dumps(repair_request, ensure_ascii=False),
    }


async def create_replan(
    context: ReplannerContext,
    *,
    client: AsyncOpenAI,
    model: str,
    plan_id_factory: Callable[[], str] = lambda: f"plan-{uuid.uuid4().hex[:16]}",
) -> ReplannerResult:
    """Call the recovery model and repair an invalid proposal at most once."""

    normalized_model = _normalize_required_text(model)
    messages = build_replanner_messages(context)
    submission_tool = _replanner_submission_tool_schema()
    last_failure = _failure_result(
        context,
        "replanning_failed",
        "Replanner没有生成可接受的恢复计划",
    )
    last_arguments: dict[str, Any] | None = None
    last_errors: list[dict[str, Any]] = []

    for attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model=normalized_model,
                messages=messages,
                tools=[submission_tool],
                **tool_choice_kwargs(
                    normalized_model,
                    {
                        "type": "function",
                        "function": {"name": REPLANNER_SUBMISSION_TOOL_NAME},
                    },
                ),
            )
            output, parsed_arguments, parsed_errors = _parse_replanner_model_output(
                response.choices[0].message
            )
            last_arguments = parsed_arguments
            last_errors = parsed_errors
            result = accept_replanner_model_output(
                context,
                output,
                plan_id_factory=plan_id_factory,
            )
            if result.outcome == "replanning_failed":
                last_errors = _acceptance_errors(context, output, result)
        except ReplannerValidationError as exc:
            if exc.raw_arguments is not None:
                last_arguments = exc.raw_arguments
            if exc.errors:
                last_errors = exc.errors
            result = _failure_result(context, exc.code, str(exc))
        except Exception:
            return _failure_result(
                context,
                "model_error",
                "Replanner模型调用失败",
            )

        if result.outcome != "replanning_failed":
            return result
        last_failure = result
        if attempt == 0:
            messages = [
                *messages,
                _replanner_repair_message(
                    context,
                    previous_arguments=last_arguments,
                    error_code=result.error_code or "replanning_failed",
                    reason=result.reason or "",
                    errors=last_errors,
                    attempt=attempt + 1,
                ),
            ]
    return last_failure


def _selection_failure_result(
    state: TaskState,
    error: ReplannerSelectionError,
) -> ReplannerResult:
    if state.active_plan is None:
        raise error
    return ReplannerResult(
        outcome="replanning_failed",
        taskId=state.task_id,
        previousPlanId=state.active_plan.plan_id,
        basedOnRevision=state.revision,
        attempt=max(1, _stored_attempt_count(state) + 1),
        errorCode=error.code,
        reason=str(error)[:500],
    )


async def persist_replanner_result(
    state: TaskState,
    result: ReplannerResult,
) -> TaskState:
    """Persist one recovery decision against the snapshot it inspected."""

    failure = _validator_result_for_failed_plan(state)
    if (
        result.task_id != state.task_id
        or result.previous_plan_id != failure.plan_id
        or result.based_on_revision != state.revision
    ):
        raise ReplannerValidationError(
            "replanner_result_mismatch",
            "ReplannerResult与待更新TaskState快照不匹配",
        )

    current_attempts = _stored_attempt_count(state)
    domain_patch: dict[str, Any] = {
        "replannerResult": result.model_dump(by_alias=True, mode="json"),
    }
    patch_arguments: dict[str, Any] = {
        "expectedRevision": state.revision,
        "actor": "agent",
        "domainStatePatch": domain_patch,
    }

    if result.outcome == "replanned":
        if result.plan is None:
            raise ReplannerValidationError(
                "replanned_plan_missing",
                "replanned结果缺少新Plan",
            )
        domain_patch["replanAttemptCount"] = current_attempts + 1
        patch_arguments["activePlan"] = result.plan
        patch_arguments["planningFailure"] = None
    elif result.outcome == "needs_user_input":
        domain_patch["replanAttemptCount"] = current_attempts
        patch_arguments["status"] = "collecting_information"
        patch_arguments["pendingQuestions"] = [result.question]
    else:
        domain_patch["replanAttemptCount"] = current_attempts + 1

    patch = TaskStatePatchRequest(**patch_arguments)
    return await update_task_state(state.task_id, patch)


async def run_replanner_phase(
    state: TaskState,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    *,
    client: AsyncOpenAI,
    model: str,
    system_policies: dict[str, Any] | None = None,
    plan_id_factory: Callable[[], str] = lambda: f"plan-{uuid.uuid4().hex[:16]}",
    context_view: Any | None = None,
) -> tuple[ReplannerResult, TaskState]:
    """Build, run, validate, and OCC-persist one recovery decision.

    When context_view (ReplannerContextView) is provided, the Replanner uses it
    as authoritative input — it must not reconstruct its context from raw
    TaskState and full chat history.
    """

    try:
        if context_view is not None:
            context = build_replanner_context_from_view(
                context_view,
                candidate_tool_schemas=candidate_tool_schemas,
                system_policies=system_policies,
            )
        else:
            context = build_replanner_context(
                state,
                user_message,
                candidate_tool_schemas,
                system_policies,
            )
    except ReplannerSelectionError as exc:
        if exc.code != "replan_attempts_exhausted":
            raise
        result = _selection_failure_result(state, exc)
    else:
        result = await create_replan(
            context,
            client=client,
            model=model,
            plan_id_factory=plan_id_factory,
        )
    updated_state = await persist_replanner_result(state, result)
    return result, updated_state
