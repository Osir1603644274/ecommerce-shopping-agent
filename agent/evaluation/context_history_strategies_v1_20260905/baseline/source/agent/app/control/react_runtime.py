"""Minimal executable ReAct V0 loop for read-only shopping actions.

The decision controller owns *which* action runs.  Existing Executor and
Validator infrastructure still owns argument resolution, tool-schema checks,
OCC persistence and evidence validation.  A server-materialized one-step
``TaskPlan`` is only an execution envelope; Planner and Replanner are never
called from this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..context_pack import shopping_guide_argument_sources
from ..executor import ExecutorRunResult, ToolCaller, run_executor_step
from ..planner import persist_planned_result
from ..planning import PlanStep, PlannerResult, TaskPlan
from ..schemas import ToolTrace
from ..task_state import TaskState
from ..tools import call_tool
from ..validator import ValidatorResult, run_validator_phase
from .react_actions import ActionOutcome, NextAction
from .react_context import build_decision_context_view
from .react_decision import (
    ReactShadowObservation,
    decide_react_v0_once,
    validate_next_action,
)


StateCallback = Callable[[TaskState, str], Awaitable[None]]
ToolSchemaProvider = Callable[[TaskState], list[dict[str, Any]]]
AnswerHandler = Callable[[NextAction, TaskState, list[ToolTrace]], Awaitable[str]]
DecisionObserver = Callable[[ReactShadowObservation], None]
OutcomeObserver = Callable[[ActionOutcome], None]
ToolTraceObserver = Callable[[ToolTrace], None]


@dataclass(frozen=True, slots=True)
class ReactToolExecution:
    state: TaskState
    outcome: ActionOutcome
    tool_trace: ToolTrace | None = None
    validator_result: ValidatorResult | None = None


@dataclass(frozen=True, slots=True)
class ReactLoopResult:
    answer: str
    terminal_action: str
    state: TaskState
    actions: tuple[NextAction, ...]
    outcomes: tuple[ActionOutcome, ...]
    tool_traces: tuple[ToolTrace, ...]
    failure_code: str | None = None


def _machine_code(value: object, fallback: str) -> str:
    raw = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if not normalized or not normalized[0].isalpha():
        return fallback
    return normalized[:128]


def _schema_name(schema: dict[str, Any]) -> str | None:
    function = schema.get("function") if schema.get("type") == "function" else None
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _selected_tool_schema(
    tool_name: str,
    allowed_tool_schemas: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    matches = [
        deepcopy(schema)
        for schema in allowed_tool_schemas
        if _schema_name(schema) == tool_name
    ]
    if len(matches) != 1:
        raise ValueError("react_tool_schema_not_uniquely_published")
    return matches[0]


def _shopping_sources(state: TaskState) -> dict[str, Any]:
    if state.task_type != "ecommerce_guide":
        raise ValueError("react_tool_wrong_task_type")
    sources = shopping_guide_argument_sources(
        state.domain_state.get("shoppingGuide"),
        scope=state.domain_state.get("candidateScope"),
        pending_rerank=state.domain_state.get("scopeRerankRequest"),
    )
    if not isinstance(sources, dict):
        raise ValueError("react_shopping_sources_missing")
    return sources


def materialize_react_execution_plan(
    state: TaskState,
    action: NextAction,
) -> TaskPlan:
    """Translate one validated CALL_TOOL action into an execution envelope."""

    if action.kind != "CALL_TOOL" or action.tool_name is None:
        raise ValueError("react_action_not_tool")
    if action.task_id != state.task_id:
        raise ValueError("react_action_task_mismatch")
    if action.based_on_revision != state.revision:
        raise ValueError("react_action_revision_stale")

    sources = _shopping_sources(state)
    requirements = sources.get("requirements")
    if not isinstance(requirements, list):
        raise ValueError("react_requirements_missing")

    tool_name = action.tool_name
    if tool_name == "search_products":
        category = sources.get("category")
        if not isinstance(category, str):
            raise ValueError("react_category_missing")
        arguments = {
            "query": state.goal,
            "category": category,
            "requirements": deepcopy(requirements),
        }
        argument_sources = {
            "query": {"kind": "task_goal"},
            "category": {"kind": "shopping_guide", "reference": "category"},
            "requirements": {
                "kind": "shopping_guide",
                "reference": "requirements",
            },
        }
        expected_output = {"requiresProductCandidates": True}
        description = "按当前已验证导购条件检索商品"
    elif tool_name == "compare_products":
        product_ids = sources.get("comparedIds")
        category = sources.get("categoryCode")
        if not isinstance(product_ids, list) or not isinstance(category, str):
            raise ValueError("react_comparison_binding_missing")
        arguments = {
            "productIds": deepcopy(product_ids),
            "category": category,
            "requirements": deepcopy(requirements),
        }
        argument_sources = {
            "productIds": {
                "kind": "shopping_guide",
                "reference": "comparedIds",
            },
            "category": {
                "kind": "shopping_guide",
                "reference": "categoryCode",
            },
            "requirements": {
                "kind": "shopping_guide",
                "reference": "requirements",
            },
        }
        expected_output = {"requiresGuideDecision": True}
        description = "比较服务端绑定的商品并返回字段级证据"
    elif tool_name == "rerank_products_in_scope":
        scope_id = sources.get("scopeId")
        product_ids = sources.get("scopeRankedItemIds")
        ranking_intent = sources.get("rankingIntent")
        category = sources.get("categoryCode")
        if (
            not isinstance(scope_id, str)
            or not isinstance(product_ids, list)
            or not product_ids
            or not isinstance(ranking_intent, str)
            or not isinstance(category, str)
        ):
            raise ValueError("react_rerank_binding_missing")
        arguments = {
            "scopeId": scope_id,
            "productIds": deepcopy(product_ids),
            "rankingIntent": ranking_intent,
            "category": category,
            "requirements": deepcopy(requirements),
        }
        argument_sources = {
            "scopeId": {"kind": "shopping_guide", "reference": "scopeId"},
            "productIds": {
                "kind": "shopping_guide",
                "reference": "scopeRankedItemIds",
            },
            "rankingIntent": {
                "kind": "shopping_guide",
                "reference": "rankingIntent",
            },
            "category": {
                "kind": "shopping_guide",
                "reference": "categoryCode",
            },
            "requirements": {
                "kind": "shopping_guide",
                "reference": "requirements",
            },
        }
        expected_output = {"requiresScopeRerank": True}
        description = "在当前可信候选范围内重排"
    else:
        raise ValueError("react_tool_not_supported")

    step = PlanStep(
        stepId="step-react-action",
        description=description,
        toolName=tool_name,
        arguments=arguments,
        argumentSources=argument_sources,
        expectedOutput=expected_output,
    )
    plan_id = f"react-{action.action_id}"[:64]
    return TaskPlan(
        planId=plan_id,
        basedOnRevision=state.revision,
        steps=[step],
    )


async def _notify_state(
    callback: StateCallback | None,
    state: TaskState,
    phase: str,
) -> None:
    if callback is not None:
        await callback(state, phase)


def _executor_failure_code(result: ExecutorRunResult) -> str:
    if result.error_code:
        return _machine_code(result.error_code, "react_executor_failed")
    execution = result.execution_result
    if execution is not None and execution.tool_trace is not None:
        detail = execution.tool_trace.detail
        if isinstance(detail, dict):
            return _machine_code(detail.get("code"), "react_tool_failed")
    return "react_tool_failed"


async def execute_react_tool_action(
    *,
    state: TaskState,
    view: Any,
    action: NextAction,
    user_message: str,
    allowed_tool_schemas: list[dict[str, Any]],
    tool_caller: ToolCaller = call_tool,
    on_task_state: StateCallback | None = None,
) -> ReactToolExecution:
    """Execute and validate exactly one revision-bound read-only action."""

    current = state
    tool_trace: ToolTrace | None = None
    try:
        validate_next_action(action, view)
        selected_schema = _selected_tool_schema(
            action.tool_name or "", allowed_tool_schemas
        )
        plan = materialize_react_execution_plan(current, action)
        planned = await persist_planned_result(
            current,
            PlannerResult(outcome="planned", plan=plan),
        )
        current = planned
        await _notify_state(on_task_state, current, "react_plan_committed")

        executor_result = await run_executor_step(
            current,
            [selected_schema],
            tool_caller=tool_caller,
        )
        current = executor_result.task_state
        await _notify_state(on_task_state, current, "react_tool_committed")
        if executor_result.execution_result is not None:
            tool_trace = executor_result.execution_result.tool_trace
        if executor_result.outcome != "step_executed":
            return ReactToolExecution(
                state=current,
                tool_trace=tool_trace,
                outcome=ActionOutcome(
                    actionId=action.action_id,
                    status="FAILED",
                    observationRef=None,
                    validatorOutcome="NOT_RUN",
                    stateRevisionAfter=current.revision,
                    retryable=False,
                    errorCode=_executor_failure_code(executor_result),
                ),
            )

        validator_result, validated_state = await run_validator_phase(current)
        current = validated_state
        await _notify_state(on_task_state, current, "react_validation_committed")
        if validator_result.outcome != "passed":
            return ReactToolExecution(
                state=current,
                tool_trace=tool_trace,
                validator_result=validator_result,
                outcome=ActionOutcome(
                    actionId=action.action_id,
                    status="REJECTED",
                    observationRef=None,
                    validatorOutcome="REJECTED",
                    stateRevisionAfter=current.revision,
                    retryable=False,
                    errorCode=_machine_code(
                        validator_result.error_code,
                        "react_validation_rejected",
                    ),
                ),
            )

        post_view = build_decision_context_view(
            current,
            user_message=user_message,
            allowed_tool_names=[
                name
                for schema in allowed_tool_schemas
                if (name := _schema_name(schema)) is not None
            ],
        )
        if post_view.answer_context_ref is None:
            return ReactToolExecution(
                state=current,
                tool_trace=tool_trace,
                validator_result=validator_result,
                outcome=ActionOutcome(
                    actionId=action.action_id,
                    status="REJECTED",
                    observationRef=None,
                    validatorOutcome="REJECTED",
                    stateRevisionAfter=current.revision,
                    retryable=False,
                    errorCode="validated_answer_context_missing",
                ),
            )
        return ReactToolExecution(
            state=current,
            tool_trace=tool_trace,
            validator_result=validator_result,
            outcome=ActionOutcome(
                actionId=action.action_id,
                status="SUCCEEDED",
                observationRef=post_view.answer_context_ref,
                validatorOutcome="PASSED",
                stateRevisionAfter=current.revision,
                retryable=False,
                errorCode=None,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        code = _machine_code(getattr(exc, "code", None), "react_execution_failed")
        return ReactToolExecution(
            state=current,
            tool_trace=tool_trace,
            outcome=ActionOutcome(
                actionId=action.action_id,
                status="FAILED",
                observationRef=None,
                validatorOutcome="FAILED",
                stateRevisionAfter=current.revision,
                retryable=False,
                errorCode=code,
            ),
        )


def _action_fingerprint(action: NextAction) -> str:
    payload = {
        "kind": action.kind,
        "toolName": action.tool_name,
        "argumentRefs": action.argument_refs,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def run_react_v0_loop(
    *,
    state: TaskState,
    user_message: str,
    client: Any,
    model: str,
    decision_timeout_seconds: float,
    max_iterations: int,
    tool_schema_provider: ToolSchemaProvider,
    answer_handler: AnswerHandler,
    tool_caller: ToolCaller = call_tool,
    on_task_state: StateCallback | None = None,
    on_decision: DecisionObserver | None = None,
    on_outcome: OutcomeObserver | None = None,
    on_tool_trace: ToolTraceObserver | None = None,
) -> ReactLoopResult:
    """Drive bounded Observe -> Decide -> Act transitions to one terminal."""

    current = state
    last_outcome: ActionOutcome | None = None
    actions: list[NextAction] = []
    outcomes: list[ActionOutcome] = []
    traces: list[ToolTrace] = []
    dispatched: set[str] = set()

    def record_outcome(outcome: ActionOutcome) -> None:
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)

    for _iteration in range(max(max_iterations, 1)):
        schemas = tool_schema_provider(current)
        tool_names = [
            name for schema in schemas if (name := _schema_name(schema)) is not None
        ]
        decision = await decide_react_v0_once(
            state=current,
            user_message=user_message,
            allowed_tool_names=tool_names,
            client=client,
            model=model,
            timeout_seconds=decision_timeout_seconds,
            last_outcome=last_outcome,
        )
        if on_decision is not None:
            on_decision(decision)
        if decision.status != "accepted" or decision.action is None or decision.view is None:
            return ReactLoopResult(
                answer="系统决策未通过校验，本轮已安全停止。",
                terminal_action="needs_review",
                state=current,
                actions=tuple(actions),
                outcomes=tuple(outcomes),
                tool_traces=tuple(traces),
                failure_code=decision.error_code or "react_decision_failed",
            )

        action = decision.action
        actions.append(action)
        if action.kind == "CALL_TOOL":
            fingerprint = _action_fingerprint(action)
            if fingerprint in dispatched:
                repeated = ActionOutcome(
                    actionId=action.action_id,
                    status="REJECTED",
                    observationRef=None,
                    validatorOutcome="NOT_RUN",
                    stateRevisionAfter=current.revision,
                    retryable=False,
                    errorCode="repeated_action_blocked",
                )
                record_outcome(repeated)
                return ReactLoopResult(
                    answer="系统检测到重复工具动作，本轮已安全停止。",
                    terminal_action="needs_review",
                    state=current,
                    actions=tuple(actions),
                    outcomes=tuple(outcomes),
                    tool_traces=tuple(traces),
                    failure_code="repeated_action_blocked",
                )
            dispatched.add(fingerprint)
            execution = await execute_react_tool_action(
                state=current,
                view=decision.view,
                action=action,
                user_message=user_message,
                allowed_tool_schemas=schemas,
                tool_caller=tool_caller,
                on_task_state=on_task_state,
            )
            current = execution.state
            if execution.tool_trace is not None:
                traces.append(execution.tool_trace)
                if on_tool_trace is not None:
                    on_tool_trace(execution.tool_trace)
            last_outcome = execution.outcome
            record_outcome(execution.outcome)
            if execution.outcome.status != "SUCCEEDED":
                # A zero-result search is an executed, observed action whose
                # evidence failed the product-candidate gate.  It is the one
                # V0 rejection allowed to re-enter Decide: the next view
                # publishes only safe server-owned clarification/boundary
                # options and never republishes the same search action.
                if execution.outcome.error_code == "product_candidates_missing":
                    continue
                return ReactLoopResult(
                    answer="工具执行或结果校验失败，本轮已安全停止。",
                    terminal_action="needs_review",
                    state=current,
                    actions=tuple(actions),
                    outcomes=tuple(outcomes),
                    tool_traces=tuple(traces),
                    failure_code=execution.outcome.error_code,
                )
            continue

        if action.kind == "ANSWER":
            try:
                answer = await answer_handler(action, current, traces)
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = ActionOutcome(
                    actionId=action.action_id,
                    status="FAILED",
                    observationRef=action.answer_context_ref,
                    validatorOutcome="FAILED",
                    stateRevisionAfter=current.revision,
                    retryable=False,
                    errorCode="answer_generation_failed",
                )
                record_outcome(failed)
                return ReactLoopResult(
                    answer="最终回答生成失败，本轮已安全停止。",
                    terminal_action="needs_review",
                    state=current,
                    actions=tuple(actions),
                    outcomes=tuple(outcomes),
                    tool_traces=tuple(traces),
                    failure_code="answer_generation_failed",
                )
            succeeded = ActionOutcome(
                actionId=action.action_id,
                status="SUCCEEDED",
                observationRef=action.answer_context_ref,
                validatorOutcome="PASSED",
                stateRevisionAfter=current.revision,
                retryable=False,
                errorCode=None,
            )
            record_outcome(succeeded)
            return ReactLoopResult(
                answer=answer,
                terminal_action="answer",
                state=current,
                actions=tuple(actions),
                outcomes=tuple(outcomes),
                tool_traces=tuple(traces),
            )

        if action.kind == "ASK_CLARIFICATION":
            interrupted = ActionOutcome(
                actionId=action.action_id,
                status="INTERRUPTED",
                observationRef=None,
                validatorOutcome="NOT_RUN",
                stateRevisionAfter=current.revision,
                retryable=False,
                errorCode=None,
            )
            record_outcome(interrupted)
            return ReactLoopResult(
                answer=action.question or "请补充缺失信息。",
                terminal_action="ask_clarification",
                state=current,
                actions=tuple(actions),
                outcomes=tuple(outcomes),
                tool_traces=tuple(traces),
            )

        review = ActionOutcome(
            actionId=action.action_id,
            status="REJECTED",
            observationRef=None,
            validatorOutcome="NOT_RUN",
            stateRevisionAfter=current.revision,
            retryable=False,
            errorCode=action.review_code or "needs_review",
        )
        record_outcome(review)
        return ReactLoopResult(
            answer="当前请求需要人工复核，本轮没有继续执行工具。",
            terminal_action="needs_review",
            state=current,
            actions=tuple(actions),
            outcomes=tuple(outcomes),
            tool_traces=tuple(traces),
            failure_code=review.error_code,
        )

    return ReactLoopResult(
        answer="系统执行达到本轮动作上限，已安全停止。",
        terminal_action="needs_review",
        state=current,
        actions=tuple(actions),
        outcomes=tuple(outcomes),
        tool_traces=tuple(traces),
        failure_code="react_iteration_budget_exhausted",
    )


__all__ = [
    "ReactLoopResult",
    "ReactToolExecution",
    "execute_react_tool_action",
    "materialize_react_execution_plan",
    "run_react_v0_loop",
]
