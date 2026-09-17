"""Typed, domain-neutral Planner/Executor data contracts."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PlanStatus = Literal["active", "stale", "completed", "failed"]
PlanStepStatus = Literal[
    "pending",
    "executing",
    "executed",
    "failed",
    "blocked",
]
PlanDecision = Literal["execute_plan"]
PlannerOutcome = Literal[
    "planned",
    "needs_user_input",
    "planning_failed",
]
PlannerModelOutcome = Literal["planned", "needs_user_input"]
PlanArgumentSourceKind = Literal[
    "task_state",
    "task_goal",
    "prior_step",
    "system_policy",
    "shopping_guide",
]

# The only references a plan may declare against the server-validated shopping
# guide.  Anything else is a fabricated domainState path and fails closed at the
# PlanArgumentSource boundary (not just at resolution time).  ``scopeId`` /
# ``scopeRankedItemIds`` / ``rankingIntent`` are published only when a valid
# active CandidateScope AND a same-scope one-turn ScopeRerankRequest pass the
# strong-typed boundary; the Planner copies them verbatim, never invents them.
SHOPPING_GUIDE_SOURCE_REFERENCES = frozenset(
    {"category", "categoryCode", "requirements", "comparedIds",
     "scopeId", "scopeRankedItemIds", "scopeSourceQuery", "rankingIntent"}
)

# The five legal argument-source shapes, as machine-readable contract data.
# This is emitted into the Planner submission tool schema and the Planner
# prompt so the model sees the exact conditional rules instead of guessing from
# the flat `{kind, reference}` field pair.  Runtime validation never relies on
# this description — it is documentation only; `PlanArgumentSource` and the
# Planner after-validators stay the single source of truth.
PLAN_ARGUMENT_SOURCE_CONTRACT: tuple[dict[str, Any], ...] = (
    {
        "kind": "task_goal",
        "shape": {"kind": "task_goal"},
        "reference": {"required": False},
        "note": "引用完整 goal；不得包含 reference。",
    },
    {
        "kind": "task_state",
        "shape": {"kind": "task_state", "reference": "facts.<key>"},
        "reference": {"required": True, "format": "facts.<key> 或 constraints.<key>"},
        "note": "必须引用 PlannerContext 中已存在且已确认的 fact/constraint。",
    },
    {
        "kind": "shopping_guide",
        "shape": {"kind": "shopping_guide", "reference": "category"},
        "reference": {
            "required": True,
            "allowed": sorted(SHOPPING_GUIDE_SOURCE_REFERENCES),
        },
        "note": "必须引用 shoppingGuideSources 实际发布的固定字段并原样取值。",
    },
    {
        "kind": "prior_step",
        "shape": {"kind": "prior_step", "reference": "<stepId>.<outputField>"},
        "reference": {"required": True, "format": "<stepId>.<outputField>"},
        "note": "必须引用已经排在前面的 step，且格式必须是 stepId.outputField。",
    },
    {
        "kind": "system_policy",
        "shape": {"kind": "system_policy", "reference": "<publishedPolicyKey>"},
        "reference": {"required": True},
        "note": "必须引用 PlannerContext.systemPolicies 中实际发布的 policy key，不得虚构。",
    },
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("字段不能为空")
    return normalized


class PlanArgumentSource(BaseModel):
    """Explain where one concrete tool argument came from.

    The after-validator rules are the single source of truth.  The schema
    descriptions/examples below make those conditional rules machine-readable
    to the Planner model (which only sees the JSON Schema, never the Python
    validator), but they are documentation, never a substitute for validation.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"kind": "task_goal"},
                {"kind": "task_state", "reference": "facts.shopName"},
                {"kind": "shopping_guide", "reference": "category"},
                {"kind": "shopping_guide", "reference": "categoryCode"},
                {"kind": "shopping_guide", "reference": "requirements"},
                {"kind": "shopping_guide", "reference": "comparedIds"},
                {"kind": "shopping_guide", "reference": "scopeId"},
                {"kind": "shopping_guide", "reference": "scopeRankedItemIds"},
                {"kind": "shopping_guide", "reference": "rankingIntent"},
                {"kind": "prior_step", "reference": "step-search.productIds"},
                {
                    "kind": "system_policy",
                    "reference": "<publishedPolicyKey>",
                },
            ]
        },
    )

    kind: PlanArgumentSourceKind = Field(
        description=(
            "参数来源类型。按 kind 条件生效的合法 JSON 形状（只允许以下五种）：\n"
            '1. "task_goal" —— {"kind":"task_goal"}，引用完整 goal；不得包含 reference。\n'
            '2. "task_state" —— {"kind":"task_state","reference":"facts.<key>"} 或 '
            '{"kind":"task_state","reference":"constraints.<key>"}。\n'
            '3. "shopping_guide" —— reference 必须是 shoppingGuideSources 实际发布的 '
            'category、categoryCode、requirements、comparedIds、scopeId、'
            'scopeRankedItemIds 或 rankingIntent；'
            "必须从 shoppingGuideSources 原样取值。\n"
            '4. "prior_step" —— {"kind":"prior_step","reference":"<stepId>.<outputField>"}；'
            "必须引用已经排在前面的 step，且格式必须是 stepId.outputField。\n"
            '5. "system_policy" —— {"kind":"system_policy","reference":"<publishedPolicyKey>"}；'
            "必须引用 PlannerContext.systemPolicies 中实际发布的 policy key，不得虚构。"
        ),
    )
    reference: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "来源引用。task_goal 必须省略；task_state 为 facts.<key> 或 constraints.<key>；"
            "shopping_guide 仅可引用 shoppingGuideSources 发布字段；prior_step 为 <stepId>.<outputField>；"
            "system_policy 为 PlannerContext.systemPolicies 中实际发布的 key。"
        ),
    )

    @field_validator("reference")
    @classmethod
    def normalize_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_reference(self) -> "PlanArgumentSource":
        if self.kind == "task_goal":
            if self.reference is not None:
                raise ValueError("task_goal 参数来源不需要 reference")
            return self
        if self.reference is None:
            raise ValueError(f"{self.kind} 参数来源必须提供 reference")
        if self.kind == "prior_step" and "." not in self.reference:
            raise ValueError("prior_step reference 必须使用 stepId.outputField 格式")
        if self.kind == "shopping_guide" and self.reference not in SHOPPING_GUIDE_SOURCE_REFERENCES:
            raise ValueError(
                "shopping_guide 参数来源只允许固定引用："
                + ", ".join(sorted(SHOPPING_GUIDE_SOURCE_REFERENCES))
            )
        return self


class PlanStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    step_id: str = Field(alias="stepId", min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    tool_name: str = Field(alias="toolName", min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    argument_sources: dict[str, PlanArgumentSource] = Field(
        default_factory=dict,
        alias="argumentSources",
    )
    expected_output: dict[str, Any] = Field(
        alias="expectedOutput",
        min_length=1,
    )
    status: PlanStepStatus = "pending"

    @field_validator("step_id", "description", "tool_name")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_required_text(value)

    @model_validator(mode="after")
    def validate_argument_sources(self) -> "PlanStep":
        argument_names = set(self.arguments)
        source_names = set(self.argument_sources)
        if argument_names != source_names:
            missing = sorted(argument_names - source_names)
            extra = sorted(source_names - argument_names)
            details: list[str] = []
            if missing:
                details.append(f"缺少来源：{', '.join(missing)}")
            if extra:
                details.append(f"多余来源：{', '.join(extra)}")
            raise ValueError("arguments 与 argumentSources 必须一一对应；" + "；".join(details))
        return self


class PlanStepProposal(BaseModel):
    """Model-owned step content before Runtime adds execution state."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    step_id: str = Field(alias="stepId", min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    tool_name: str = Field(alias="toolName", min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    argument_sources: dict[str, PlanArgumentSource] = Field(
        default_factory=dict,
        alias="argumentSources",
        description=(
            "每个 arguments 键必须且只能在这里有一个来源声明；arguments 与 "
            "argumentSources 必须一一对应（不能缺、不能多）。只使用"
            "argumentSources 五种合法形状（见 PlanArgumentSource）。"
        ),
    )
    expected_output: dict[str, Any] = Field(
        alias="expectedOutput",
        min_length=1,
    )

    @field_validator("step_id", "description", "tool_name")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_required_text(value)

    @model_validator(mode="after")
    def validate_argument_sources(self) -> "PlanStepProposal":
        argument_names = set(self.arguments)
        source_names = set(self.argument_sources)
        if argument_names != source_names:
            missing = sorted(argument_names - source_names)
            extra = sorted(source_names - argument_names)
            details: list[str] = []
            if missing:
                details.append(f"缺少来源：{', '.join(missing)}")
            if extra:
                details.append(f"多余来源：{', '.join(extra)}")
            raise ValueError("arguments 与 argumentSources 必须一一对应；" + "；".join(details))
        return self

    def accept(self) -> PlanStep:
        return PlanStep.model_validate(self.model_dump(mode="python"))


class PlannerModelOutput(BaseModel):
    """The only payload the planning model may propose.

    Legal argument-source shapes (machine-readable summary; see
    ``PlanArgumentSource`` for per-field rules and ``PLAN_ARGUMENT_SOURCE_CONTRACT``
    for the exact five forms):

    - ``{"kind":"task_goal"}``
    - ``{"kind":"task_state","reference":"facts.<key>"}`` / ``"constraints.<key>"``
    - ``{"kind":"shopping_guide","reference":"category"}`` / ``"requirements"``
    - ``{"kind":"prior_step","reference":"<stepId>.<outputField>"}``
    - ``{"kind":"system_policy","reference":"<publishedPolicyKey>"}``
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    outcome: PlannerModelOutcome
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
    def validate_outcome_payload(self) -> "PlannerModelOutput":
        if self.outcome == "planned":
            if not self.steps:
                raise ValueError("planned 模型输出必须包含至少一个步骤")
            if self.question is not None:
                raise ValueError("planned 模型输出不能包含question")
        else:
            if self.question is None:
                raise ValueError("needs_user_input 模型输出必须包含question")
            if self.steps:
                raise ValueError("needs_user_input 模型输出不能包含步骤")

        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("模型输出中的stepId不能重复")
        prior_step_ids: set[str] = set()
        for step in self.steps:
            for source in step.argument_sources.values():
                if source.kind != "prior_step":
                    continue
                referenced_step_id = source.reference.split(".", 1)[0]
                if referenced_step_id not in prior_step_ids:
                    raise ValueError(
                        f"{step.step_id} 只能引用已经排在前面的步骤，"
                        f"当前引用：{source.reference}"
                    )
            prior_step_ids.add(step.step_id)
        return self


class TaskPlan(BaseModel):
    """A persisted action contract, not a model chain-of-thought transcript."""

    model_config = ConfigDict(populate_by_name=True)

    plan_id: str = Field(alias="planId", min_length=1, max_length=64)
    based_on_revision: int = Field(alias="basedOnRevision", ge=1)
    decision: PlanDecision = "execute_plan"
    status: PlanStatus = "active"
    steps: list[PlanStep] = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utc_now, alias="createdAt")
    updated_at: datetime = Field(default_factory=_utc_now, alias="updatedAt")

    @field_validator("plan_id")
    @classmethod
    def normalize_plan_id(cls, value: str) -> str:
        return _normalize_required_text(value)

    @model_validator(mode="after")
    def validate_plan(self) -> "TaskPlan":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Plan 中的 stepId 不能重复")

        prior_step_ids: set[str] = set()
        for step in self.steps:
            for source in step.argument_sources.values():
                if source.kind != "prior_step":
                    continue
                referenced_step_id = source.reference.split(".", 1)[0]
                if referenced_step_id not in prior_step_ids:
                    raise ValueError(
                        f"{step.step_id} 只能引用已经排在前面的步骤，"
                        f"当前引用：{source.reference}"
                    )
            prior_step_ids.add(step.step_id)

        if self.status == "completed" and any(
            step.status != "executed" for step in self.steps
        ):
            raise ValueError("completed Plan 的所有步骤都必须是 executed")
        return self


class PlannerResult(BaseModel):
    """The Planner boundary: a Plan, a user question, or a planning failure."""

    model_config = ConfigDict(populate_by_name=True)

    outcome: PlannerOutcome
    plan: TaskPlan | None = None
    question: str | None = Field(default=None, max_length=300)
    error_code: str | None = Field(
        default=None,
        alias="errorCode",
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=64,
    )
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("question", "error_code", "reason")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "PlannerResult":
        if self.outcome == "planned":
            if self.plan is None:
                raise ValueError("planned 结果必须包含plan")
            if any((self.question, self.error_code, self.reason)):
                raise ValueError("planned 结果不能包含question或错误信息")
            return self

        if self.outcome == "needs_user_input":
            if self.question is None:
                raise ValueError("needs_user_input 结果必须包含question")
            if self.plan is not None or self.error_code is not None or self.reason is not None:
                raise ValueError("needs_user_input 结果只能包含question")
            return self

        if self.plan is not None or self.question is not None:
            raise ValueError("planning_failed 结果不能包含plan或question")
        if self.error_code is None or self.reason is None:
            raise ValueError("planning_failed 结果必须包含errorCode和reason")
        return self


class PlanningFailure(BaseModel):
    """A persisted record of one unsuccessful planning attempt."""

    model_config = ConfigDict(populate_by_name=True)

    error_code: str = Field(
        alias="errorCode",
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=64,
    )
    reason: str = Field(min_length=1, max_length=500)
    based_on_revision: int = Field(alias="basedOnRevision", ge=1)
    failed_at: datetime = Field(default_factory=_utc_now, alias="failedAt")

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return _normalize_required_text(value)


class PlanStatusTransitionError(ValueError):
    pass


class PlanStepStatusTransitionError(ValueError):
    pass


_ALLOWED_PLAN_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "active": {"stale", "completed", "failed"},
    "stale": set(),
    "completed": set(),
    "failed": set(),
}

_ALLOWED_STEP_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"executing", "blocked"},
    # executing -> pending is reserved for Runtime lease-expiry recovery.
    "executing": {"pending", "executed", "failed", "blocked"},
    "executed": set(),
    "failed": {"pending"},
    "blocked": {"pending", "failed"},
}


def transition_plan_status(plan: TaskPlan, target: PlanStatus) -> TaskPlan:
    if plan.status == target:
        return plan
    if target not in _ALLOWED_PLAN_STATUS_TRANSITIONS[plan.status]:
        raise PlanStatusTransitionError(
            f"不允许Plan从 {plan.status} 转换到 {target}"
        )
    payload = plan.model_dump(mode="python")
    payload["status"] = target
    payload["updated_at"] = _utc_now()
    return TaskPlan.model_validate(payload)


def transition_plan_step_status(
    plan: TaskPlan,
    step_id: str,
    target: PlanStepStatus,
) -> TaskPlan:
    selected = next((step for step in plan.steps if step.step_id == step_id), None)
    if selected is None:
        raise KeyError(f"PlanStep不存在：{step_id}")
    if selected.status == target:
        return plan
    if target not in _ALLOWED_STEP_STATUS_TRANSITIONS[selected.status]:
        raise PlanStepStatusTransitionError(
            f"不允许PlanStep从 {selected.status} 转换到 {target}"
        )

    steps = [
        step.model_copy(update={"status": target})
        if step.step_id == step_id
        else step
        for step in plan.steps
    ]
    payload = plan.model_dump(mode="python")
    payload["steps"] = steps
    payload["updated_at"] = _utc_now()
    return TaskPlan.model_validate(payload)


def validate_existing_plan_update(current: TaskPlan, proposed: TaskPlan) -> None:
    """Reject attempts to rewrite an accepted plan instead of progressing it."""

    if current.plan_id != proposed.plan_id:
        raise ValueError("只能校验同一个planId的Plan更新")
    if (
        current.based_on_revision != proposed.based_on_revision
        or current.decision != proposed.decision
        or current.created_at != proposed.created_at
    ):
        raise PlanStatusTransitionError("已接受Plan的生成依据和创建信息不能修改")

    if current.status != proposed.status:
        if proposed.status not in _ALLOWED_PLAN_STATUS_TRANSITIONS[current.status]:
            raise PlanStatusTransitionError(
                f"不允许Plan从 {current.status} 转换到 {proposed.status}"
            )

    current_steps = {step.step_id: step for step in current.steps}
    proposed_steps = {step.step_id: step for step in proposed.steps}
    if current_steps.keys() != proposed_steps.keys():
        raise PlanStatusTransitionError("已接受Plan不能增删或替换PlanStep")

    for step_id, current_step in current_steps.items():
        proposed_step = proposed_steps[step_id]
        current_contract = current_step.model_dump(exclude={"status"}, mode="python")
        proposed_contract = proposed_step.model_dump(exclude={"status"}, mode="python")
        if current_contract != proposed_contract:
            raise PlanStatusTransitionError(
                f"已接受PlanStep的执行契约不能修改：{step_id}"
            )
        if current_step.status == proposed_step.status:
            continue
        if proposed_step.status not in _ALLOWED_STEP_STATUS_TRANSITIONS[
            current_step.status
        ]:
            raise PlanStepStatusTransitionError(
                f"不允许PlanStep从 {current_step.status} 转换到 "
                f"{proposed_step.status}"
            )
