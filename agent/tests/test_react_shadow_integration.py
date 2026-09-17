"""Runtime dispatch tests for the zero-side-effect ReAct V0 shadow."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.control.react_actions import NextAction
from app.control.react_context import build_decision_context_view
from app.control.react_decision import ReactShadowObservation
from app.llm import _run_react_v0_live_agent, _run_unified_harness_agent
from app.schemas import ToolTrace
from app.task_state import TaskState


def _state(revision: int) -> TaskState:
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-shadow-integration",
        taskType="ecommerce_guide",
        sessionId="session-1",
        status="ready",
        revision=revision,
        goal="三千以内的手机",
        domainState={
            "shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "useCases": [],
                "requirements": [{
                    "key": "price_minor",
                    "operator": "lte",
                    "value": 300_000,
                    "unit": "CNY_MINOR",
                    "priority": "hard",
                    "source": "user",
                }],
                "brandAvoidances": [],
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            }
        },
        createdAt=now,
        updatedAt=now,
    )


def _observation(state: TaskState) -> ReactShadowObservation:
    view = build_decision_context_view(
        state,
        user_message="三千以内的手机",
        allowed_tool_names=["search_products"],
    )
    action = NextAction.model_validate({
        "actionId": "action-shadow-1",
        "taskId": state.task_id,
        "basedOnRevision": state.revision,
        "decisionViewHash": view.decision_view_hash,
        "kind": "CALL_TOOL",
        "reasonCode": "need_candidates",
        "toolName": "search_products",
        "argumentRefs": view.allowed_tools[0].argument_refs,
    })
    return ReactShadowObservation(
        status="accepted",
        task_revision=state.revision,
        duration_ms=11.0,
        view=view,
        action=action,
    )


def test_shadow_observes_once_but_fixed_result_remains_authoritative() -> None:
    initial = _state(1)
    updated = _state(2)
    observation = _observation(updated)
    observe = AsyncMock(return_value=observation)
    fixed_result = ("固定路径回答", [], [{"role": "assistant", "content": "固定路径回答"}], "run-fixed", None)
    fixed = AsyncMock(return_value=fixed_result)

    with patch("app.llm.settings.agent_control_runtime", "react_v0_shadow"), patch(
        "app.llm.get_client", return_value=SimpleNamespace()
    ), patch(
        "app.llm._update_task_state_for_unified_harness",
        new=AsyncMock(return_value=updated),
    ), patch(
        "app.llm._explicit_harness_tool_schemas",
        return_value=[{"function": {"name": "search_products"}}],
    ), patch(
        "app.control.react_decision.observe_react_v0_shadow", new=observe
    ), patch(
        "app.llm._should_use_explicit_harness", return_value=True
    ), patch(
        "app.llm._run_explicit_harness_agent", new=fixed
    ):
        result = asyncio.run(_run_unified_harness_agent(
            initial.goal,
            history=None,
            task_state=initial,
            on_answer_delta=None,
            on_task_state=None,
            session_id=initial.session_id,
        ))

    assert result == fixed_result
    observe.assert_awaited_once()
    assert fixed.await_args.kwargs["react_shadow_observation"] is observation
    assert fixed.await_args.kwargs["task_state"] is updated
    assert initial.revision == 1


def test_fixed_v1_never_calls_shadow_decider() -> None:
    initial = _state(1)
    updated = _state(2)
    observe = AsyncMock()
    fixed = AsyncMock(return_value=("ok", [], [], "run-fixed", None))
    with patch("app.llm.settings.agent_control_runtime", "fixed_v1"), patch(
        "app.llm.get_client", return_value=SimpleNamespace()
    ), patch(
        "app.llm._update_task_state_for_unified_harness",
        new=AsyncMock(return_value=updated),
    ), patch(
        "app.control.react_decision.observe_react_v0_shadow", new=observe
    ), patch(
        "app.llm._should_use_explicit_harness", return_value=True
    ), patch(
        "app.llm._run_explicit_harness_agent", new=fixed
    ):
        asyncio.run(_run_unified_harness_agent(
            initial.goal,
            history=None,
            task_state=initial,
            on_answer_delta=None,
            on_task_state=None,
        ))
    observe.assert_not_awaited()
    assert fixed.await_args.kwargs["react_shadow_observation"] is None


def test_react_v0_live_fails_before_state_extraction_or_business_tools() -> None:
    initial = _state(4)
    persist = AsyncMock()
    update = AsyncMock()
    with patch("app.llm.settings.agent_control_runtime", "react_v0"), patch(
        "app.llm.settings.agent_react_live_enabled", False
    ), patch(
        "app.llm.get_client"
    ) as get_client, patch(
        "app.llm._update_task_state_for_unified_harness", new=update
    ), patch(
        "app.llm._persist_trace_safely", new=persist
    ):
        answer, tools, _turns, _run_id, summary = asyncio.run(
            _run_unified_harness_agent(
                initial.goal,
                history=None,
                task_state=initial,
                on_answer_delta=None,
                on_task_state=None,
            )
        )

    assert "当前实验运行时尚未通过验收" in answer
    assert "ReAct" not in answer
    assert "shadow" not in answer
    assert tools == []
    assert summary.failure_code == "react_v0_not_accepted"
    get_client.assert_not_called()
    update.assert_not_awaited()
    trace = persist.await_args.args[0]
    assert trace.entered_runtime == "react_v0"
    assert trace.task_revision_before == trace.task_revision_after == 4


def test_react_v0_live_gate_routes_after_taskstate_update() -> None:
    initial = _state(1)
    updated = _state(2)
    live_result = (
        "ReAct 回答",
        [],
        [{"role": "assistant", "content": "ReAct 回答"}],
        "run-react",
        None,
    )
    live = AsyncMock(return_value=live_result)
    with patch("app.llm.settings.agent_control_runtime", "react_v0"), patch(
        "app.llm.settings.agent_react_live_enabled", True
    ), patch(
        "app.llm.get_client", return_value=SimpleNamespace()
    ), patch(
        "app.llm._update_task_state_for_unified_harness",
        new=AsyncMock(return_value=updated),
    ), patch(
        "app.llm._run_react_v0_live_agent", new=live
    ):
        result = asyncio.run(_run_unified_harness_agent(
            initial.goal,
            history=None,
            task_state=initial,
            on_answer_delta=None,
            on_task_state=None,
            session_id=initial.session_id,
        ))

    assert result == live_result
    live.assert_awaited_once()
    assert live.await_args.kwargs["task_state"] is updated


def test_live_deadline_closes_the_selected_action_outcome() -> None:
    initial = _state(4)
    base = _observation(initial)
    answer_action = NextAction.model_validate({
        "actionId": "action-answer-timeout",
        "taskId": initial.task_id,
        "basedOnRevision": initial.revision,
        "decisionViewHash": base.view.decision_view_hash,
        "kind": "ANSWER",
        "reasonCode": "answer_after_validated_action",
        "answerContextRef": "validated-task:task-shadow-integration:r4",
    })
    observation = ReactShadowObservation(
        status="accepted",
        task_revision=initial.revision,
        duration_ms=1.0,
        view=base.view,
        action=answer_action,
    )
    persisted = AsyncMock()
    completed_trace = ToolTrace(
        tool="compare_products", ok=True, durationMs=3.0, detail={"count": 3}
    )

    async def hanging_loop(**kwargs):
        kwargs["on_tool_trace"](completed_trace)
        kwargs["on_decision"](observation)
        await asyncio.sleep(1)

    async def scenario():
        deadline_at = asyncio.get_running_loop().time() + 0.05
        with patch(
            "app.control.react_runtime.run_react_v0_loop",
            new=hanging_loop,
        ), patch(
            "app.llm.get_task_state", new=AsyncMock(return_value=initial)
        ), patch(
            "app.llm._persist_trace_safely", new=persisted
        ):
            return await _run_react_v0_live_agent(
                initial.goal,
                history=None,
                client=SimpleNamespace(),
                task_state=initial,
                on_answer_delta=None,
                on_task_state=None,
                deadline_at=deadline_at,
                session_id=initial.session_id,
                memory_run_binding=None,
            )

    answer, tools, _turns, _run_id, summary = asyncio.run(scenario())

    assert "超过总执行时间限制" in answer
    assert tools == [completed_trace]
    assert summary.failure_code == "react_v0_deadline_exceeded"
    assert summary.tool_call_count == 1
    trace = persisted.await_args.args[0]
    assert len(trace.react_decisions) == 1
    outcome = trace.react_outcomes[0]
    assert outcome == {
        "actionId": "action-answer-timeout",
        "status": "FAILED",
        "validatorOutcome": "PASSED",
        "stateRevisionAfter": 4,
        "retryable": False,
        "errorCode": "react_v0_deadline_exceeded",
        "observationRefHash": outcome["observationRefHash"],
    }
    assert len(outcome["observationRefHash"]) == 64
