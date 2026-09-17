import json
import uuid
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .context_pack import shopping_guide_argument_sources
from .model_compat import tool_choice_kwargs
from .planning import (
    PLAN_ARGUMENT_SOURCE_CONTRACT,
    PlannerModelOutput,
    PlannerResult,
    PlanStepProposal,
    PlanningFailure,
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
from .validation_contracts import (
    ExpectedOutputContractError,
    expected_output_contracts_for_tool,
    validate_expected_output_declaration,
)


PLANNER_SUBMISSION_TOOL_NAME = "submit_planner_output"


def _argument_source_matrix_text() -> str:
    """Render the five legal argument-source shapes from the contract constant.

    The contract is documentation for the model only — runtime validation stays
    in ``PlanArgumentSource`` / the Planner after-validators.  Rendering it from
    ``PLAN_ARGUMENT_SOURCE_CONTRACT`` keeps prompt and schema in lockstep.
    """
    lines = [
        "argumentSources 只允许以下五种合法形状（kind + 按 kind 条件生效的 reference）："
    ]
    for entry in PLAN_ARGUMENT_SOURCE_CONTRACT:
        shape = json.dumps(entry["shape"], ensure_ascii=False)
        reference = entry["reference"]
        if reference.get("required"):
            detail = reference.get("format")
            if not detail and reference.get("allowed"):
                detail = "允许值：" + "、".join(str(item) for item in reference["allowed"])
            detail = detail or "必填"
            lines.append(
                f'- "{entry["kind"]}"：形状 {shape}；reference 必填（{detail}）。'
                f'{entry["note"]}'
            )
        else:
            lines.append(
                f'- "{entry["kind"]}"：形状 {shape}；不得包含 reference。{entry["note"]}'
            )
    return "\n".join(lines)


PLANNER_SYSTEM_PROMPT = (
    "你是北京出行Agent的Planner，只生成行动计划，不回答用户问题，也不执行任何业务工具。"
    "只能选择PlannerContext.candidateTools中的工具。"
    "每个步骤的expectedOutput只能使用对应candidateTool.expectedOutputContracts中的字段。"
    "每个参数都必须声明argumentSources；不能创造商户、地点、shopId或用户约束。"
    "商品导购任务中，search_products 的 query 必须使用 task_goal 来源且等于完整 goal 原样；"
    "category 与 requirements 必须从 shoppingGuideSources 来源按其固定引用（category/requirements）"
    "原样取值，不得自行改写、推断品牌或增删条目。"
    "可由候选工具获得的信息应规划为步骤，只有必须由用户提供的信息才返回needs_user_input。"
    "不要生成planId、basedOnRevision、Plan状态、PlanStep状态或planning_failed。"
    "最后只调用submit_planner_output一次提交结构化结果。\n\n"
    + _argument_source_matrix_text()
    + "\n\n最小合法电商检索例子（仅演示 argumentSources 形状与 arguments/argumentSources 一一对应；"
    "goal 只是格式占位，query 必须逐字等于 PlannerContext.goal 原样，不得复用本例子）：\n"
    'PlannerContext.goal="想找 iOS 二手机。"；'
    'shoppingGuideSources={"category":"手机","requirements":["os eq ios hard"]}；'
    "candidateTools 含 search_products。规划单个 search_products 步骤：\n"
    'arguments={"query":"想找 iOS 二手机。","category":"手机","requirements":["os eq ios hard"]}\n'
    'argumentSources={"query":{"kind":"task_goal"},'
    '"category":{"kind":"shopping_guide","reference":"category"},'
    '"requirements":{"kind":"shopping_guide","reference":"requirements"}}\n'
    'expectedOutput={"requiresProductCandidates":true}\n'
    "不得在 arguments 中填写 search_products 工具 schema 之外的键；"
    "arguments 与 argumentSources 必须一一对应（每个键只能有一个来源，不能缺、不能多）；"
    "仅当 PlannerContext.systemPolicies 实际发布了某个 policy key 时才允许 "
    '{"kind":"system_policy","reference":"<该发布的key>"}；'
    'prior_step 引用必须指向已经排在前面步骤的 "<stepId>.<outputField>"。'
)

# The source file is UTF-8, but model/tool arguments can still arrive as
# reversible GBK-over-Latin-1 text.  Keep the executable contract compact and
# locale-stable, and require byte-for-byte copies of published source values.
PLANNER_SYSTEM_PROMPT = """You are the Planner for a shopping Agent. Produce a plan; do not answer the user and do not execute tools.
Use only candidateTools and prefer the smallest sufficient plan.
Legal source kinds are "task_goal", "task_state", "shopping_guide", "prior_step", and "system_policy".
For a recommendation, use exactly one search_products step. query must equal PlannerContext.goal byte-for-byte and use "task_goal". category and requirements must equal shoppingGuideSources.category and shoppingGuideSources.requirements and use those "shopping_guide" references.
For comparison, use exactly one compare_products step. productIds must equal shoppingGuideSources.comparedIds and use "shopping_guide"/comparedIds; category must equal shoppingGuideSources.categoryCode; requirements must equal shoppingGuideSources.requirements.
Use "prior_step" only for an earlier step output. Use "task_state" only for a published fact or constraint. Use "system_policy" only for a key actually present in PlannerContext.systemPolicies; never invent one.
最小合法电商检索例子: one search_products step using the exact published query, category, and requirements. arguments 与 argumentSources 必须一一对应. Never place a source object inside arguments. Copy published values exactly; do not translate, re-encode, summarize, or alter them.
Return needs_user_input only when an allowed tool cannot run without new user information. Do not create planId, revisions, statuses, or planning_failed. Call submit_planner_output exactly once."""


def _planner_model_schema() -> dict[str, Any]:
    """Return a compact schema without locale-dependent legacy descriptions."""
    schema = PlannerModelOutput.model_json_schema(by_alias=True)

    def strip_descriptions(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("description", None)
            for nested in value.values():
                strip_descriptions(nested)
        elif isinstance(value, list):
            for nested in value:
                strip_descriptions(nested)

    strip_descriptions(schema)
    schema["description"] = (
        "Submit a minimal plan or one necessary user question. Argument values "
        "must exactly match their declared published sources."
    )
    source_schema = schema.get("$defs", {}).get("PlanArgumentSource")
    if isinstance(source_schema, dict):
        source_schema["description"] = (
            "task_goal has no reference; shopping_guide references category, "
            "requirements, or comparedIds; prior_step references an earlier "
            "<stepId>.<outputField>; system_policy names an actually published key."
        )
        properties = source_schema.get("properties", {})
        if isinstance(properties.get("kind"), dict):
            properties["kind"]["description"] = source_schema["description"]
        if isinstance(properties.get("reference"), dict):
            properties["reference"]["description"] = (
                "Omit for task_goal; otherwise use the exact published reference."
            )
    step_schema = schema.get("$defs", {}).get("PlanStepProposal")
    if isinstance(step_schema, dict):
        argument_sources = step_schema.get("properties", {}).get("argumentSources")
        if isinstance(argument_sources, dict):
            argument_sources["description"] = (
                "arguments 与 argumentSources 必须一一对应; one declared source per argument."
            )
    return schema


def _planner_submission_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": PLANNER_SUBMISSION_TOOL_NAME,
            "description": "提交Planner生成的步骤提案或必要的用户澄清问题。",
            "parameters": _planner_model_schema(),
        },
    }


def _normalize_required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("字段不能为空")
    return normalized


class PlannerToolSpec(BaseModel):
    """The safe, normalized tool contract exposed to the Planner."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    parameters: dict[str, Any]
    expected_output_contracts: tuple[str, ...] = Field(
        default=(),
        alias="expectedOutputContracts",
    )

    @field_validator("name", "description")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_required_text(value)


class PlannerContext(BaseModel):
    """A revision-bound, consumer-specific read view for initial planning."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    task_id: str = Field(alias="taskId", min_length=1)
    task_revision: int = Field(alias="taskRevision", ge=1)
    task_status: TaskStatus = Field(alias="taskStatus")
    goal: str = Field(min_length=1, max_length=500)
    facts: tuple[TaskFact, ...] = ()
    constraints: tuple[TaskConstraint, ...] = ()
    unknowns: tuple[str, ...] = ()
    pending_questions: tuple[str, ...] = Field(
        default=(),
        alias="pendingQuestions",
    )
    user_message: str = Field(alias="userMessage", min_length=1, max_length=2000)
    candidate_tools: tuple[PlannerToolSpec, ...] = Field(
        default=(),
        alias="candidateTools",
    )
    system_policies: dict[str, Any] = Field(
        default_factory=dict,
        alias="systemPolicies",
    )
    # Server-validated shopping-guide argument sources (category label + exact
    # requirements).  None for non-ecommerce tasks — a plan may never fabricate
    # these references from raw domainState paths.
    shopping_guide_sources: dict[str, Any] | None = Field(
        default=None,
        alias="shoppingGuideSources",
    )
    # Optional B3b channel; it is model input only and is never persisted to
    # TaskState, a checkpoint, or a plan.
    long_term_memory: tuple[dict[str, str], ...] = Field(
        default=(), alias="longTermMemory", repr=False, exclude_if=lambda value: value == ()
    )

    @field_validator("task_id", "goal", "user_message")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_required_text(value)

    @model_validator(mode="after")
    def validate_unique_tool_names(self) -> "PlannerContext":
        names = [tool.name for tool in self.candidate_tools]
        if len(names) != len(set(names)):
            raise ValueError("PlannerContext中的候选工具名称不能重复")
        return self


def _planner_tool_spec(schema: dict[str, Any]) -> PlannerToolSpec:
    if schema.get("type") != "function":
        raise ValueError("Planner候选工具必须是function Schema")
    function = schema.get("function")
    if not isinstance(function, dict):
        raise ValueError("工具Schema缺少function对象")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("工具Schema缺少parameters对象")
    return PlannerToolSpec(
        name=function.get("name", ""),
        description=function.get("description", ""),
        parameters=deepcopy(parameters),
        expectedOutputContracts=expected_output_contracts_for_tool(
            function.get("name", "")
        ),
    )


def build_planner_context(
    state: TaskState,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    system_policies: dict[str, Any] | None = None,
) -> PlannerContext:
    """Build a detached Planner view without chat history or other task data."""

    authority_domain = state.domain_state
    authority_selection = None
    if state.task_type == "ecommerce_guide":
        from .domains.ecommerce.shopping_state_authority import select_shopping_state_authority
        from .settings import settings

        authority_selection = select_shopping_state_authority(
            domain_state=state.domain_state,
            task_id=state.task_id,
            task_revision=state.revision,
            goal=state.goal,
            unknowns=state.unknowns,
            pending_questions=state.pending_questions,
            mode=settings.shopping_state_authority,
        )
        authority_domain = authority_selection.domain_state

    return PlannerContext(
        taskId=state.task_id,
        taskRevision=state.revision,
        taskStatus=state.status,
        goal=(authority_selection.goal if authority_selection is not None else state.goal),
        facts=tuple(fact.model_copy(deep=True) for fact in state.facts),
        constraints=tuple(
            constraint.model_copy(deep=True) for constraint in state.constraints
        ),
        unknowns=(
            authority_selection.unknowns
            if authority_selection is not None
            else tuple(state.unknowns)
        ),
        pendingQuestions=(
            authority_selection.pending_questions
            if authority_selection is not None
            else tuple(state.pending_questions)
        ),
        userMessage=user_message,
        candidateTools=tuple(
            _planner_tool_spec(schema) for schema in candidate_tool_schemas
        ),
        systemPolicies=deepcopy(system_policies or {}),
        # Only the ecommerce_guide task type exposes shopping-guide sources.
        # Any other task type must present None even if domainState carries a
        # format-valid shoppingGuide — the Plan may not fabricate these refs.
        shoppingGuideSources=(
            shopping_guide_argument_sources(
                authority_domain.get("shoppingGuide"),
                scope=authority_domain.get("candidateScope"),
                pending_rerank=authority_domain.get("scopeRerankRequest"),
            )
            if state.task_type == "ecommerce_guide"
            else None
        ),
    )


class PlannerValidationError(ValueError):
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


def _new_plan_id() -> str:
    return f"plan-{uuid.uuid4().hex[:16]}"


def _tool_by_name(context: PlannerContext, name: str) -> PlannerToolSpec:
    selected = next(
        (tool for tool in context.candidate_tools if tool.name == name),
        None,
    )
    if selected is None:
        raise PlannerValidationError(
            "tool_not_allowed",
            f"PlanStep使用了候选列表之外的工具：{name}",
        )
    return selected


def _validate_tool_arguments(
    tool: PlannerToolSpec,
    arguments: dict[str, Any],
) -> None:
    properties = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise PlannerValidationError(
            "invalid_tool_schema",
            f"工具 {tool.name} 的参数Schema不合法",
        )

    missing = [name for name in required if name not in arguments]
    if missing:
        raise PlannerValidationError(
            "invalid_tool_arguments",
            f"工具 {tool.name} 缺少必要参数：{', '.join(missing)}",
        )
    unexpected = sorted(set(arguments) - set(properties))
    if unexpected:
        raise PlannerValidationError(
            "invalid_tool_arguments",
            f"工具 {tool.name} 包含未声明参数：{', '.join(unexpected)}",
        )


def _task_state_source_value(context: PlannerContext, reference: str) -> Any:
    scope, separator, key = reference.partition(".")
    if not separator or not key:
        raise PlannerValidationError(
            "invalid_argument_source",
            f"TaskState参数来源格式无效：{reference}",
        )
    if scope == "facts":
        fact = next((item for item in context.facts if item.key == key), None)
        if fact is None:
            raise PlannerValidationError(
                "invalid_argument_source",
                f"TaskState中不存在fact：{key}",
            )
        if fact.certainty != "confirmed":
            raise PlannerValidationError(
                "invalid_argument_source",
                f"Plan参数只能引用confirmed fact：{key}",
            )
        return fact.value
    if scope == "constraints":
        constraint = next(
            (item for item in context.constraints if item.key == key),
            None,
        )
        if constraint is None:
            raise PlannerValidationError(
                "invalid_argument_source",
                f"TaskState中不存在constraint：{key}",
            )
        return constraint.value
    raise PlannerValidationError(
        "invalid_argument_source",
        f"不支持的TaskState参数来源：{reference}",
    )


def _shopping_guide_source_value(
    context: PlannerContext,
    reference: str,
) -> Any:
    sources = context.shopping_guide_sources
    if not sources or reference not in sources:
        raise PlannerValidationError(
            "invalid_argument_source",
            f"购物导购来源中不存在：{reference}",
        )
    return sources[reference]


def _validate_argument_sources(
    context: PlannerContext,
    proposal: PlanStepProposal,
    *,
    path_prefix: str = "$.steps[*]",
) -> None:
    for argument_name, source in proposal.argument_sources.items():
        argument_value = proposal.arguments[argument_name]
        if source.kind == "prior_step":
            # The Executor resolves this value from the persisted earlier result.
            continue
        if source.kind == "task_goal":
            expected_value = context.goal
        elif source.kind == "task_state":
            expected_value = _task_state_source_value(context, source.reference)
        elif source.kind == "shopping_guide":
            expected_value = _shopping_guide_source_value(context, source.reference)
        else:
            if source.reference not in context.system_policies:
                raise PlannerValidationError(
                    "invalid_argument_source",
                    f"系统策略中不存在：{source.reference}",
                    errors=[{
                        "path": f"{path_prefix}.argumentSources.{argument_name}.reference",
                        "message": (
                            f"system_policy key {source.reference!r} is not published; "
                            f"available keys: {sorted(context.system_policies)}"
                        ),
                    }],
                )
            expected_value = context.system_policies[source.reference]
        if argument_value != expected_value:
            raise PlannerValidationError(
                "invalid_argument_source",
                f"参数 {argument_name} 与声明来源的真实值不一致",
                errors=[{
                    "path": f"{path_prefix}.arguments.{argument_name}",
                    "message": (
                        f"value does not exactly match declared {source.kind} source; "
                        f"required value: {json.dumps(expected_value, ensure_ascii=False, sort_keys=True)}"
                    ),
                }],
            )


def _validate_shopping_guide_plan_shape(
    context: PlannerContext,
    output: PlannerModelOutput,
) -> None:
    """Require the one production action implied by server-owned guide state.

    Prompt guidance is not a runtime contract.  In particular, a comparison
    plan containing search/details/compare can be individually well typed but
    still exhaust the Harness transition budget before ``compare_products`` is
    reached.  Once a validated shopping guide is published, accept only the
    smallest sufficient action: one compare for an explicit comparison pair,
    otherwise one search.
    """

    sources = context.shopping_guide_sources
    if not sources:
        return
    compared_ids = sources.get("comparedIds")
    if not isinstance(compared_ids, list) or not compared_ids:
        # Preserve the generic/prior-step Planner contract for recommendation
        # tasks.  The strict shape is required here specifically because an
        # explicit comparison pair is already sufficient for the terminal
        # compare action.
        return
    expected_tool = "compare_products"
    actual_tools = [step.tool_name for step in output.steps]
    if len(actual_tools) == 1 and actual_tools[0] == expected_tool:
        return
    raise PlannerValidationError(
        "invalid_plan_shape",
        f"购物导购计划必须只包含一个 {expected_tool} 步骤",
        errors=[{
            "path": "$.steps",
            "message": (
                f"required exactly one {expected_tool} step from published "
                f"shoppingGuideSources; received: {actual_tools}"
            ),
        }],
    )


def accept_planner_model_output(
    context: PlannerContext,
    output: PlannerModelOutput,
    *,
    plan_id_factory: Callable[[], str] = _new_plan_id,
    raise_validation_error: bool = False,
) -> PlannerResult:
    """Promote a model proposal into a server-owned PlannerResult."""

    if output.outcome == "needs_user_input":
        return PlannerResult(
            outcome="needs_user_input",
            question=output.question,
        )
    if context.pending_questions:
        return PlannerResult(
            outcome="needs_user_input",
            question=context.pending_questions[0],
        )
    if context.task_status != "ready":
        return PlannerResult(
            outcome="planning_failed",
            errorCode="task_not_ready",
            reason=f"TaskState状态为{context.task_status}，不能创建Plan",
        )

    try:
        _validate_shopping_guide_plan_shape(context, output)
        accepted_proposals: list[PlanStepProposal] = []
        for step_index, proposal in enumerate(output.steps):
            tool = _tool_by_name(context, proposal.tool_name)
            _validate_tool_arguments(tool, proposal.arguments)
            _validate_argument_sources(
                context,
                proposal,
                path_prefix=f"$.steps[{step_index}]",
            )
            if context.shopping_guide_sources is not None:
                # For the ecommerce Harness, expectedOutput describes a
                # server-owned Validator contract, not model intent.  Derive
                # the one exact declaration from the registered tool instead
                # of allowing the Planner to invent or omit evidence fields.
                expected_output = {
                    name: True for name in tool.expected_output_contracts
                }
                if not expected_output:
                    raise PlannerValidationError(
                        "unsupported_expected_output",
                        f"工具尚未注册Validator输出契约：{proposal.tool_name}",
                    )
                accepted_proposals.append(
                    proposal.model_copy(update={"expected_output": expected_output})
                )
            else:
                try:
                    validate_expected_output_declaration(
                        proposal.tool_name,
                        proposal.expected_output,
                    )
                except ExpectedOutputContractError as exc:
                    raise PlannerValidationError(exc.code, str(exc)) from exc
                accepted_proposals.append(proposal)
        plan = TaskPlan(
            planId=plan_id_factory(),
            basedOnRevision=context.task_revision,
            steps=[proposal.accept() for proposal in accepted_proposals],
        )
    except (PlannerValidationError, ValueError) as exc:
        if raise_validation_error and isinstance(exc, PlannerValidationError):
            raise
        error_code = (
            exc.code if isinstance(exc, PlannerValidationError) else "invalid_plan"
        )
        return PlannerResult(
            outcome="planning_failed",
            errorCode=error_code,
            reason=str(exc),
        )
    return PlannerResult(outcome="planned", plan=plan)


def build_planner_messages(context: PlannerContext) -> list[dict[str, Any]]:
    context_payload = context.model_dump(by_alias=True, mode="json")
    return [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "请根据以下PlannerContext提交结构化规划结果：\n"
                + json.dumps(context_payload, ensure_ascii=False)
            ),
        },
    ]


def _pydantic_error_path(loc: Any) -> str:
    """Render a pydantic ``loc`` tuple as a stable dotted path."""
    return ".".join(str(part) for part in loc)


def _parse_planner_model_output(
    reply: Any,
) -> tuple[PlannerModelOutput, dict[str, Any], list[dict[str, Any]]]:
    """Parse the single submission tool call into a validated model output.

    Returns ``(output, raw_payload, structured_errors)``.  On a schema-level
    rejection the raised ``PlannerValidationError`` carries the same structured
    errors plus the raw payload so the repair request can echo the exact
    invalid tool call back to the model.
    """
    tool_calls = list(getattr(reply, "tool_calls", None) or [])
    matching_calls = [
        call
        for call in tool_calls
        if getattr(getattr(call, "function", None), "name", None)
        == PLANNER_SUBMISSION_TOOL_NAME
    ]
    if len(matching_calls) != 1:
        raise PlannerValidationError(
            "missing_structured_output",
            "Planner模型必须且只能调用一次submit_planner_output",
        )
    raw_arguments = getattr(matching_calls[0].function, "arguments", "")
    try:
        payload = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlannerValidationError(
            "invalid_model_output",
            "Planner模型提交的arguments不是合法JSON",
            errors=[{"path": "$", "message": "arguments 不是合法 JSON"}],
        ) from exc
    if not isinstance(payload, dict):
        raise PlannerValidationError(
            "invalid_model_output",
            "Planner模型提交结果必须是JSON对象",
            errors=[{"path": "$", "message": "提交结果必须是 JSON 对象"}],
        )
    try:
        output = PlannerModelOutput.model_validate(payload)
    except ValidationError as exc:
        errors = [
            {
                "path": _pydantic_error_path(item.get("loc", ())),
                "message": str(item.get("msg", "")),
            }
            for item in exc.errors()
        ]
        raise PlannerValidationError(
            "invalid_model_output",
            str(exc)[:500],
            errors=errors,
            raw_arguments=payload,
        ) from exc
    return output, payload, []


def _planner_repair_message(
    *,
    previous_arguments: dict[str, Any] | None,
    error_code: str,
    reason: str,
    errors: list[dict[str, Any]],
    attempt: int,
) -> dict[str, Any]:
    """Build the structured repair request appended after the first rejection.

    The model sees its own previous tool call verbatim plus precise, testable
    validation errors — not an unverifiable generic hint.  The failed record is
    never modified; the repair is a fresh system turn on the next attempt.
    """
    structured_errors = list(errors)
    if not structured_errors and reason:
        structured_errors = [{"path": "$", "message": f"{error_code}: {reason}"}]
    repair_request = {
        "plannerRepairRequest": {
            "attempt": attempt,
            "previousToolCall": {
                "toolName": PLANNER_SUBMISSION_TOOL_NAME,
                "arguments": previous_arguments if previous_arguments is not None else {},
            },
            "validationError": {
                "errorCode": error_code,
                "reason": reason,
                "errors": structured_errors,
            },
            "instruction": (
                "你上一轮调用 submit_planner_output 提交的规划结果未通过校验。"
                "请严格根据 validationError.errors 中列出的每一条错误修正 "
                "previousToolCall.arguments，然后重新调用 submit_planner_output；"
                "本次只允许调用一次，不要重复原错误，也不要引入与校验错误无关的新错误。"
            ),
        }
    }
    return {
        "role": "system",
        "content": json.dumps(repair_request, ensure_ascii=False),
    }


def _planning_failure(code: str, reason: str) -> PlannerResult:
    return PlannerResult(
        outcome="planning_failed",
        errorCode=code,
        reason=reason[:500],
    )


def _deterministic_shopping_guide_plan(
    context: PlannerContext,
    *,
    plan_id_factory: Callable[[], str],
) -> PlannerResult | None:
    """Build the only legal ecommerce step from published server sources.

    Once ``shoppingGuideSources`` exists, the model has no remaining planning
    choice: an in-scope rerank for an active server-owned rerank request, one
    comparison for a bound pair, otherwise one search.  Constructing that
    mechanical plan locally avoids schema-copy failures and keeps the model for
    genuinely open-ended planning only.  Runtime validation still checks tool
    schemas and every source/value binding below.
    """

    sources = context.shopping_guide_sources
    if not sources:
        return None
    requirements = sources.get("requirements")
    if not isinstance(requirements, list):
        return None
    scope_id = sources.get("scopeId")
    scope_ranked_ids = sources.get("scopeRankedItemIds")
    ranking_intent = sources.get("rankingIntent")
    if (
        isinstance(scope_id, str)
        and isinstance(scope_ranked_ids, list)
        and scope_ranked_ids
        and isinstance(ranking_intent, str)
    ):
        # Server-owned in-scope rerank: the CandidateScope materialized from the
        # current successful search is the only legal product universe.  The
        # Planner copies the exact published references; it never invents IDs or
        # triggers a new full-catalog search.
        category_code = sources.get("categoryCode")
        if not isinstance(category_code, str):
            return None
        tool_name = "rerank_products_in_scope"
        arguments = {
            "scopeId": scope_id,
            "productIds": deepcopy(scope_ranked_ids),
            "rankingIntent": ranking_intent,
            "category": category_code,
            "requirements": deepcopy(requirements),
        }
        argument_sources = {
            "scopeId": {"kind": "shopping_guide", "reference": "scopeId"},
            "productIds": {
                "kind": "shopping_guide",
                "reference": "scopeRankedItemIds",
            },
            "rankingIntent": {"kind": "shopping_guide", "reference": "rankingIntent"},
            "category": {"kind": "shopping_guide", "reference": "categoryCode"},
            "requirements": {"kind": "shopping_guide", "reference": "requirements"},
        }
        description = "在上一轮可信候选范围内按标题文本相关性重排"
        expected_output = {"requiresScopeRerank": True}
    else:
        compared_ids = sources.get("comparedIds")
        if isinstance(compared_ids, list):
            category_code = sources.get("categoryCode")
            if not isinstance(category_code, str):
                return None
            tool_name = "compare_products"
            arguments = {
                "productIds": deepcopy(compared_ids),
                "category": category_code,
                "requirements": deepcopy(requirements),
            }
            argument_sources = {
                "productIds": {"kind": "shopping_guide", "reference": "comparedIds"},
                "category": {"kind": "shopping_guide", "reference": "categoryCode"},
                "requirements": {"kind": "shopping_guide", "reference": "requirements"},
            }
            description = "比较已绑定的商品并返回字段级证据"
            expected_output = {"requiresGuideDecision": True}
        else:
            category = sources.get("category")
            if not isinstance(category, str):
                return None
            tool_name = "search_products"
            arguments = {
                "query": context.goal,
                "category": category,
                "requirements": deepcopy(requirements),
            }
            argument_sources = {
                "query": {"kind": "task_goal"},
                "category": {"kind": "shopping_guide", "reference": "category"},
                "requirements": {"kind": "shopping_guide", "reference": "requirements"},
            }
            description = "按已验证的导购条件检索商品"
            expected_output = {"requiresProductCandidates": True}

    try:
        output = PlannerModelOutput.model_validate({
            "outcome": "planned",
            "steps": [{
                "stepId": "step-shopping-action",
                "description": description,
                "toolName": tool_name,
                "arguments": arguments,
                "argumentSources": argument_sources,
                "expectedOutput": expected_output,
            }],
        })
        return accept_planner_model_output(
            context,
            output,
            plan_id_factory=plan_id_factory,
            raise_validation_error=True,
        )
    except (PlannerValidationError, ValidationError, ValueError) as exc:
        # A malformed/published source must fail closed; do not ask the model to
        # reinterpret a server-owned value after deterministic construction.
        code = exc.code if isinstance(exc, PlannerValidationError) else "invalid_plan"
        return _planning_failure(code, str(exc))


def build_planner_context_from_view(view: Any) -> PlannerContext:
    """Build a PlannerContext from a PlannerContextView (context_pack mode).

    The view carries the minimal, deterministic projection — the Planner MUST
    NOT pull extra facts, constraints or tools from outside the View.
    """
    from .task_state import TaskFact, TaskConstraint

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
        for c in view.hard_constraints
    )

    return PlannerContext(
        taskId=view.task_id,
        taskRevision=view.base_context_revision,
        taskStatus=view.task_status,
        goal=view.goal,
        facts=facts,
        constraints=constraints,
        unknowns=tuple(view.unknowns),
        pendingQuestions=tuple(view.pending_questions),
        userMessage=view.user_message,
        candidateTools=tuple(
            _planner_tool_spec_from_view_dict(tool_dict)
            for tool_dict in view.candidate_tools
        ),
        systemPolicies=deepcopy(view.system_policies),
        shoppingGuideSources=deepcopy(view.shopping_guide_sources),
        longTermMemory=tuple(deepcopy(view.long_term_memory)),
    )


def _planner_tool_spec_from_view_dict(tool_dict: dict[str, Any]) -> PlannerToolSpec:
    """Build a PlannerToolSpec from a serialised tool dict stored in a View.

    The dict may be a full OpenAI function schema (with ``name``, ``description``,
    ``parameters``) or a pre-flattened PlannerToolSpec dict.
    """
    name = tool_dict.get("name", "")
    description = tool_dict.get("description", "")
    parameters = tool_dict.get("parameters", {})
    if isinstance(parameters, str):
        import json as _json
        try:
            parameters = _json.loads(parameters)
        except (TypeError, ValueError):
            parameters = {"type": "object", "properties": {}}
    if not isinstance(parameters, dict) or "type" not in parameters:
        parameters = {"type": "object", "properties": parameters if isinstance(parameters, dict) else {}}
    return PlannerToolSpec(
        name=name,
        description=description,
        parameters=deepcopy(parameters),
        expectedOutputContracts=expected_output_contracts_for_tool(name),
    )


async def create_plan(
    context: PlannerContext,
    *,
    client: AsyncOpenAI,
    model: str,
    plan_id_factory: Callable[[], str] = _new_plan_id,
) -> PlannerResult:
    """Call the planning model, validate its proposal, and repair at most once."""

    if context.pending_questions:
        return PlannerResult(
            outcome="needs_user_input",
            question=context.pending_questions[0],
        )
    if context.task_status != "ready":
        return _planning_failure(
            "task_not_ready",
            f"TaskState状态为{context.task_status}，不能调用Planner",
        )

    deterministic = _deterministic_shopping_guide_plan(
        context,
        plan_id_factory=plan_id_factory,
    )
    if deterministic is not None:
        return deterministic

    normalized_model = _normalize_required_text(model)
    messages = build_planner_messages(context)
    submission_tool = _planner_submission_tool_schema()
    last_failure = _planning_failure(
        "planning_failed",
        "Planner没有生成可接受的计划",
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
                        "function": {"name": PLANNER_SUBMISSION_TOOL_NAME},
                    },
                ),
            )
            reply = response.choices[0].message
            output, parsed_arguments, parsed_errors = _parse_planner_model_output(
                reply
            )
            last_arguments = parsed_arguments
            last_errors = parsed_errors
            result = accept_planner_model_output(
                context,
                output,
                plan_id_factory=plan_id_factory,
                raise_validation_error=True,
            )
        except PlannerValidationError as exc:
            if exc.raw_arguments is not None:
                last_arguments = exc.raw_arguments
            if exc.errors:
                last_errors = exc.errors
            result = _planning_failure(exc.code, str(exc))
        except Exception:
            return _planning_failure(
                "model_error",
                "Planner模型调用失败",
            )

        if result.outcome != "planning_failed":
            return result
        last_failure = result
        if attempt == 0:
            messages = [
                *messages,
                _planner_repair_message(
                    previous_arguments=last_arguments,
                    error_code=result.error_code or "planning_failed",
                    reason=result.reason or "",
                    errors=last_errors,
                    attempt=attempt + 1,
                ),
            ]

    return last_failure


async def persist_planned_result(
    state: TaskState,
    result: PlannerResult,
) -> TaskState:
    """Persist one accepted Plan without retrying a stale planning snapshot."""

    if result.outcome != "planned" or result.plan is None:
        raise ValueError("只有planned PlannerResult可以写入activePlan")
    if result.plan.based_on_revision != state.revision:
        raise ValueError("Plan的basedOnRevision与待更新TaskState revision不一致")

    patch = TaskStatePatchRequest(
        expectedRevision=state.revision,
        actor="agent",
        activePlan=result.plan,
        planningFailure=None,
        # These receipts are scoped to the previous Plan. A new accepted Plan
        # must not reinterpret a reused stepId under a different tool contract.
        domainStatePatch={
            "stepOutputs": {},
            "executorBlock": None,
            "validationResult": None,
        },
    )
    return await update_task_state(state.task_id, patch)


async def persist_needs_user_input_result(
    state: TaskState,
    result: PlannerResult,
) -> TaskState:
    """Persist one Planner clarification question without retrying stale state."""

    if result.outcome != "needs_user_input" or result.question is None:
        raise ValueError(
            "只有needs_user_input PlannerResult可以写入pendingQuestions"
        )

    patch = TaskStatePatchRequest(
        expectedRevision=state.revision,
        actor="agent",
        status="collecting_information",
        pendingQuestions=[result.question],
        planningFailure=None,
    )
    return await update_task_state(state.task_id, patch)


async def persist_planning_failed_result(
    state: TaskState,
    result: PlannerResult,
) -> TaskState:
    """Record one planning failure while leaving the user task retryable."""

    if (
        result.outcome != "planning_failed"
        or result.error_code is None
        or result.reason is None
    ):
        raise ValueError(
            "只有planning_failed PlannerResult可以写入planningFailure"
        )

    failure = PlanningFailure(
        errorCode=result.error_code,
        reason=result.reason,
        basedOnRevision=state.revision,
    )
    patch = TaskStatePatchRequest(
        expectedRevision=state.revision,
        actor="agent",
        planningFailure=failure,
    )
    return await update_task_state(state.task_id, patch)


async def persist_planner_result(
    state: TaskState,
    result: PlannerResult,
) -> TaskState:
    """Route one PlannerResult to its matching TaskState persistence path."""

    if result.outcome == "planned":
        return await persist_planned_result(state, result)
    if result.outcome == "needs_user_input":
        return await persist_needs_user_input_result(state, result)
    return await persist_planning_failed_result(state, result)


async def run_planner_phase(
    state: TaskState,
    user_message: str,
    candidate_tool_schemas: list[dict[str, Any]],
    *,
    client: AsyncOpenAI,
    model: str,
    system_policies: dict[str, Any] | None = None,
    plan_id_factory: Callable[[], str] = _new_plan_id,
    context_view: Any | None = None,
) -> tuple[PlannerResult, TaskState]:
    """Build context, create one plan result, and persist it with OCC.

    When context_view (PlannerContextView) is provided, the Planner MUST use it
    as its authoritative input instead of reconstructing from raw TaskState.
    """

    if context_view is not None:
        # context_pack mode: use the pre-projected PlannerContextView
        context = build_planner_context_from_view(context_view)
    else:
        context = build_planner_context(
            state,
            user_message,
            candidate_tool_schemas,
            system_policies,
        )
    result = await create_plan(
        context,
        client=client,
        model=model,
        plan_id_factory=plan_id_factory,
    )
    updated_state = await persist_planner_result(state, result)
    return result, updated_state
