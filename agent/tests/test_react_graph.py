"""Controlled LangGraph ReAct orchestration tests."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from app.agent_trace import TraceBuilder
from app.harness import HarnessStepResult
from app.react_graph import ReActGraphRuntime, run_controlled_react_graph
from app.task_state import TaskState


def _state(revision: int = 1) -> TaskState:
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="react-task",
        revision=revision,
        status="ready",
        goal="find a product",
        taskType="ecommerce_guide",
        createdAt=now,
        updatedAt=now,
    )


def _runtime(step_runner, *, max_transitions: int, on_transition=None):
    return ReActGraphRuntime(
        user_message="find a product",
        client=MagicMock(),
        model="offline",
        resolve_tool_schemas=lambda _state: [],
        step_runner=step_runner,
        tool_caller=AsyncMock(),
        trace_builder=TraceBuilder("react-test", mode="context_pack"),
        projector=None,
        max_transitions=max_transitions,
        on_transition=on_transition,
    )


def test_graph_loops_reason_act_observe_until_terminal_action() -> None:
    states = [_state(2), _state(3)]
    step_runner = AsyncMock(side_effect=[
        HarnessStepResult(
            action="continue_to_executor",
            task_state=states[0],
        ),
        HarnessStepResult(
            action="task_completed",
            task_state=states[1],
        ),
    ])
    observed = []

    async def on_transition(result):
        observed.append(result.action)

    result = asyncio.run(run_controlled_react_graph(
        _state(),
        _runtime(step_runner, max_transitions=4, on_transition=on_transition),
    ))

    assert result["action"] == "task_completed"
    assert result["transition_count"] == 2
    assert result["transition_limit_reached"] is False
    assert observed == ["continue_to_executor", "task_completed"]
    assert step_runner.await_count == 2


def test_graph_stops_at_transition_limit() -> None:
    async def always_continue(state, *_args, **_kwargs):
        return HarnessStepResult(
            action="continue_to_executor",
            task_state=state.model_copy(update={"revision": state.revision + 1}),
        )

    result = asyncio.run(run_controlled_react_graph(
        _state(),
        _runtime(always_continue, max_transitions=2),
    ))

    assert result["action"] == "continue_to_executor"
    assert result["transition_count"] == 2
    assert result["transition_limit_reached"] is True
