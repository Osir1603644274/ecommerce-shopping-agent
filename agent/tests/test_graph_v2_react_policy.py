from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent_trace import TraceBuilder
from app.control.react_actions import ActionOutcome
from app.control.react_context import build_decision_context_view
from app.control.react_decision import deterministic_next_action
from app.graph.nodes.react_policy import react_policy_node
from app.graph.routers import (
    route_after_clarification,
    route_after_entry_durable,
    route_after_react_policy_durable,
    route_after_validator_durable,
)
from app.graph.runtime import GraphV2Runtime
from app.graph.runtime import CONTROL_POLICY_REVISIONS
from tests.test_react_context import _state


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _runtime() -> GraphV2Runtime:
    return GraphV2Runtime(
        user_message="请在当前范围内重新排序",
        client=SimpleNamespace(),
        model="test-model",
        resolve_tool_schemas=lambda _state: [
            _schema("search_products"),
            _schema("rerank_products_in_scope"),
        ],
        tool_caller=AsyncMock(),
        trace_builder=TraceBuilder("run-react-v1", mode="context_pack"),
        projector=None,
        max_transitions=12,
        control_policy="react_v1",
        react_max_model_decisions=2,
        react_decision_timeout_seconds=1.0,
    )


def _graph_state(**updates):
    state = {
        "task_state": _state(),
        "action": "continue_to_executor",
        "transition_count": 0,
        "replan_count": 0,
        "transition_limit_reached": False,
        "terminal_outcome": None,
        "degraded_reason": None,
        "last_route_decision": "react_policy",
        "transitions": [],
        "node_events": [],
        "control_policy": "react_v1",
        "policy_revision": CONTROL_POLICY_REVISIONS["react_v1"],
        "react_model_decision_count": 0,
        "react_action_id": None,
        "react_action_kind": None,
        "react_answer_context_ref": None,
        "react_last_outcome": None,
    }
    state.update(updates)
    return state


def _adaptive_view():
    view = build_decision_context_view(
        _state(),
        user_message="请在当前范围内重新排序",
        allowed_tool_names=["search_products", "rerank_products_in_scope"],
    )
    return view.model_copy(
        update={
            "server_signals": {
                **view.server_signals,
                "adaptiveDecisionRequired": True,
            }
        }
    )


def test_react_v1_routes_inside_durable_graph() -> None:
    assert route_after_entry_durable(
        {"last_route_decision": "react_policy"}
    ) == "react_policy"
    assert route_after_react_policy_durable(
        {"action": "continue_to_executor"}
    ) == "executor"
    assert route_after_validator_durable(
        {"action": "task_completed", "control_policy": "react_v1"}
    ) == "react_policy"
    assert route_after_validator_durable(
        {
            "action": "stop_turn",
            "control_policy": "react_v1",
            "react_last_outcome": {
                "status": "REJECTED",
                "errorCode": "product_candidates_missing",
            },
        }
    ) == "react_policy"
    assert route_after_validator_durable(
        {
            "action": "stop_turn",
            "control_policy": "fixed_v1",
            "react_last_outcome": {
                "status": "REJECTED",
                "errorCode": "product_candidates_missing",
            },
        }
    ) != "react_policy"
    assert route_after_clarification(
        {"action": "continue_to_executor", "control_policy": "react_v1"}
    ) == "react_policy"


def test_deterministic_policy_materializes_plan_without_model_call() -> None:
    current = _state()
    planned = current.model_copy(update={"revision": current.revision + 1})
    runtime = _runtime()
    with patch(
        "app.graph.nodes.react_policy._persist_action",
        new=AsyncMock(return_value=planned),
    ) as persist, patch(
        "app.graph.nodes.react_policy.decide_next_action",
        new=AsyncMock(),
    ) as decide:
        result = asyncio.run(
            react_policy_node(_graph_state(), SimpleNamespace(context=runtime))
        )

    assert result["action"] == "continue_to_executor"
    assert result["react_model_decision_count"] == 0
    assert result["react_action_kind"] == "CALL_TOOL"
    assert persist.await_count == 1
    assert decide.await_count == 0


def test_model_decision_budget_fails_closed_before_third_call() -> None:
    runtime = _runtime()
    with patch(
        "app.graph.nodes.react_policy.build_decision_context_view",
        return_value=_adaptive_view(),
    ), patch(
        "app.graph.nodes.react_policy.deterministic_next_action",
        return_value=None,
    ), patch(
        "app.graph.nodes.react_policy.decide_next_action",
        new=AsyncMock(),
    ) as decide:
        result = asyncio.run(
            react_policy_node(
                _graph_state(react_model_decision_count=2),
                SimpleNamespace(context=runtime),
            )
        )

    assert result["action"] == "stop_turn"
    assert result["degraded_reason"] == "react_model_decision_budget_exhausted"
    assert decide.await_count == 0


def test_failed_model_attempt_still_consumes_budget_and_is_counted() -> None:
    observed: list[tuple[str, float, bool]] = []
    runtime = _runtime()
    runtime = GraphV2Runtime(
        **{
            **runtime.__dict__,
            "on_model_call": lambda stage, duration, *, failed: observed.append(
                (stage, duration, failed)
            ),
        }
    )
    with patch(
        "app.graph.nodes.react_policy.build_decision_context_view",
        return_value=_adaptive_view(),
    ), patch(
        "app.graph.nodes.react_policy.deterministic_next_action",
        return_value=None,
    ), patch(
        "app.graph.nodes.react_policy._reserve_model_decision",
        new=AsyncMock(return_value=(_state(), 1, True, 1)),
    ), patch(
        "app.graph.nodes.react_policy.decide_next_action",
        new=AsyncMock(side_effect=RuntimeError("provider failed")),
    ):
        result = asyncio.run(
            react_policy_node(_graph_state(), SimpleNamespace(context=runtime))
        )

    assert result["action"] == "stop_turn"
    assert result["react_model_decision_count"] == 1
    assert result["degraded_reason"] == "decision_model_failed"
    assert [item[0] for item in observed] == ["react_decision"]
    assert observed[0][2] is True
    decision = runtime.trace_builder.finish().react_decisions[-1]
    end_event = result["node_events"][-1]
    assert decision["modelName"] == "test-model"
    assert decision["modelCallId"].startswith("rmc-")
    assert len(decision["decisionBindingHash"]) == 64
    assert end_event["nodeName"] == "react_policy"
    assert end_event["modelCallId"] == decision["modelCallId"]
    assert end_event["decisionBindingHash"] == decision["decisionBindingHash"]


def test_failed_reservation_never_calls_decision_provider() -> None:
    decide = AsyncMock()
    with patch(
        "app.graph.nodes.react_policy.build_decision_context_view",
        return_value=_adaptive_view(),
    ), patch(
        "app.graph.nodes.react_policy.deterministic_next_action",
        return_value=None,
    ), patch(
        "app.graph.nodes.react_policy._reserve_model_decision",
        new=AsyncMock(return_value=(_state(), 1, False, None)),
    ), patch(
        "app.graph.nodes.react_policy.decide_next_action",
        new=decide,
    ):
        result = asyncio.run(
            react_policy_node(_graph_state(), SimpleNamespace(context=_runtime()))
        )

    assert result["action"] == "stop_turn"
    assert result["degraded_reason"] == "react_model_decision_reservation_failed"
    decide.assert_not_awaited()


def test_nonretryable_validator_rejection_cannot_repeat_tool() -> None:
    current = _state()
    view = build_decision_context_view(
        current,
        user_message="请继续推荐",
        allowed_tool_names=["search_products"],
        last_outcome=ActionOutcome(
            actionId="action-rejected",
            status="REJECTED",
            observationRef=None,
            validatorOutcome="REJECTED",
            stateRevisionAfter=current.revision,
            retryable=False,
            errorCode="evidence_contract_rejected",
        ),
    )
    assert all(option.kind != "CALL_TOOL" for option in view.allowed_action_options)
    action = deterministic_next_action(view)
    assert action is not None
    assert action.kind == "NEEDS_REVIEW"
