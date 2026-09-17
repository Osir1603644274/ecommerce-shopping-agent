from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import task_state
from app.agent_trace import TraceBuilder
from app.control.react_decision import ReactActionProposal, materialize_next_action
from app.control.react_actions import NextAction
from app.control.react_context import build_decision_context_view
from app.control.react_decision import deterministic_next_action
from app.control.react_runtime import materialize_react_execution_plan
from app.domains.ecommerce.models import CATEGORY_LABELS
from app.graph.nodes.react_policy import (
    ReactPolicyFaultInjected,
    react_policy_node,
    set_react_policy_fault_hook,
)
from app.graph.nodes import entry_node
from app.graph.nodes.clarification import clarification_node
from app.graph.nodes.executor import executor_node
from app.graph.nodes.react_policy import (
    _decision_budget_count,
    _journal_prefix,
    _persist_action,
    _replay_action,
    _reserve_model_decision,
)
from app.graph.nodes.validator import (
    ValidatorFaultInjected,
    set_validator_fault_hook,
)
from app.graph.resume import run_graph_v2_durable
from app.graph.pause_control import (
    public_pause_receipt,
    request_graph_pause,
    read_graph_pause,
)
from app.graph.runtime import GraphV2Runtime
from app.graph.tool_inbox_v2 import InboxResponse, InboxStatus
from app.schemas import ToolTrace
from app.task_state import (
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    get_task_state,
    update_task_state,
)
from tests.test_graph_v2_interrupt_resume import (
    InFileRedis,
    InFileToolInbox,
    _fake_client,
    _search_products_schema,
)
from tests.two_stage_ranking_fixtures import two_stage_search_detail
from tests.test_graph_v2_react_policy import _adaptive_view, _graph_state


def _ready_request(
    task_type: str = "ecommerce_guide",
    category: str = "phone",
) -> TaskStateCreateRequest:
    category_goal = {
        "phone": "三千元以内推荐二手手机",
        "laptop": "三千元以内推荐笔记本",
        "headphones": "三千元以内推荐耳机",
    }.get(category, f"三千元以内推荐{category}")
    return TaskStateCreateRequest(
        goal=category_goal,
        task_type=task_type,
        session_id="session-a",
        status="ready",
        domain_state={
            "shoppingGuide": {
                "mode": "recommend",
                "category": category,
                "useCases": ["续航"],
                "requirements": [
                    {
                        "key": "price_minor",
                        "operator": "lte",
                        "value": 300000,
                        "unit": "CNY_MINOR",
                        "priority": "hard",
                        "source": "user",
                    }
                ],
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            },
            "taskStateExtraction": {"reason": "broad_catalog_discovery"},
        },
    )


@pytest.fixture
def isolated_redis():
    previous = task_state._client
    fake = InFileRedis()
    task_state._client = fake
    task_state._task_locks.clear()
    task_state._session_locks.clear()
    try:
        yield fake
    finally:
        task_state._client = previous
        task_state._task_locks.clear()
        task_state._session_locks.clear()


async def _case_react_v1_full_durable_loop_uses_shared_inbox_and_no_planner(
    isolated_redis,
    category: str,
) -> None:
    state = await create_task_state(_ready_request(category=category))
    state = await update_task_state(
        state.task_id,
        TaskStatePatchRequest(
            expectedRevision=state.revision,
            actor="agent",
            status="ready",
        ),
    )
    calls: list[tuple[str, dict, object]] = []

    async def caller_v2(name: str, arguments: dict, context) -> ToolTrace:
        calls.append((name, arguments, context))
        return ToolTrace(
            tool=name,
            ok=True,
            detail=two_stage_search_detail([101, 102]),
        )

    trace = TraceBuilder("run-react-v1-durable", mode="context_pack")
    result = await run_graph_v2_durable(
        task_id=state.task_id,
        session_id="session-a",
        run_id="run-react-v1-durable",
        user_message=state.goal,
        client=_fake_client(AsyncMock()),
        model="test-model",
        resolve_tool_schemas=lambda _state: [_search_products_schema()],
        tool_caller=AsyncMock(),
        tool_caller_v2=caller_v2,
        tool_inbox=InFileToolInbox(),
        trace_builder=trace,
        max_transitions=12,
        control_policy="react_v1",
    )

    assert result.boundary == "task_completed", (
        result.graph_state,
        trace._trace.react_decisions,
        trace._trace.react_outcomes,
    )
    starts = [
        event["nodeName"]
        for event in result.graph_state["node_events"]
        if event["phase"] == "start"
    ]
    assert starts == [
        "entry",
        "react_policy",
        "executor",
        "validator",
        "react_policy",
    ]
    assert "planner" not in starts
    assert "replanner" not in starts
    assert [name for name, _arguments, _context in calls] == ["search_products"]
    assert calls[0][1]["category"] == CATEGORY_LABELS[category]
    finished = trace.finish()
    assert [item["decisionSource"] for item in finished.react_decisions] == [
        "deterministic_policy",
        "deterministic_policy",
    ]
    assert len(finished.react_outcomes) == 2


async def _case_decision_reservations_survive_runtime_reconstruction(
    isolated_redis,
) -> None:
    state = await create_task_state(_ready_request())
    runtime = GraphV2Runtime(
        user_message=state.goal,
        client=SimpleNamespace(),
        model="test-model",
        resolve_tool_schemas=lambda _state: [_search_products_schema()],
        tool_caller=AsyncMock(),
        trace_builder=TraceBuilder("run-budget", mode="context_pack"),
        projector=None,
        max_transitions=12,
        durable=True,
        task_id=state.task_id,
        run_id="run-budget",
        thread_id=f"v2-task:{state.task_id}:run-budget",
        session_owner_hash="0" * 16,
        control_policy="react_v1",
    )
    _, first, first_acquired, first_slot = await _reserve_model_decision(
        state, runtime, view_hash="a" * 64
    )
    _, second, second_acquired, second_slot = await _reserve_model_decision(
        state, runtime, view_hash="b" * 64
    )
    _, third, third_acquired, third_slot = await _reserve_model_decision(
        state, runtime, view_hash="c" * 64
    )
    assert (first, second, third) == (1, 2, 2)
    assert (first_acquired, second_acquired, third_acquired) == (True, True, False)
    assert (first_slot, second_slot, third_slot) == (1, 2, None)
    assert await _decision_budget_count(state, runtime) == 2


async def _case_react_v1_rejects_non_ecommerce_scope_without_model(
    isolated_redis,
) -> None:
    state = await create_task_state(_ready_request(task_type="local_life"))
    runtime = GraphV2Runtime(
        user_message=state.goal,
        client=SimpleNamespace(),
        model="test-model",
        resolve_tool_schemas=lambda _state: [],
        tool_caller=AsyncMock(),
        trace_builder=TraceBuilder("run-ineligible", mode="context_pack"),
        projector=None,
        max_transitions=12,
        control_policy="react_v1",
    )
    result = await entry_node(
        {"task_state": state, "node_events": []},
        SimpleNamespace(context=runtime),
    )
    assert result["last_route_decision"] == "react_scope_ineligible"
    assert result["degraded_reason"] == "react_scope_ineligible"


async def _case_react_v1_rejects_ineligible_pending_clarification(
    isolated_redis,
) -> None:
    request = _ready_request(task_type="local_life").model_copy(
        update={
            "status": "collecting_information",
            "pending_questions": ["请补充地点"],
        }
    )
    state = await create_task_state(request)
    runtime = GraphV2Runtime(
        user_message=state.goal,
        client=SimpleNamespace(),
        model="test-model",
        resolve_tool_schemas=lambda _state: [],
        tool_caller=AsyncMock(),
        trace_builder=TraceBuilder("run-ineligible-pending", mode="context_pack"),
        projector=None,
        max_transitions=12,
        control_policy="react_v1",
    )
    result = await entry_node(
        {"task_state": state, "node_events": []},
        SimpleNamespace(context=runtime),
    )
    assert result["last_route_decision"] == "react_scope_ineligible"


async def _case_react_v1_preserves_server_owned_unsupported_category_answer(
    isolated_redis,
) -> None:
    request = _ready_request().model_copy(
        update={
            "status": "collecting_information",
            "pending_questions": [
                "当前导购运行时尚不支持该品类；目前可处理手机、笔记本和耳机。"
                "你希望改为其中哪一类？"
            ],
            "unknowns": ["当前导购尚不支持平板品类"],
            "domain_state": {
                "shoppingGuide": {
                    "mode": "recommend",
                    "category": None,
                    "requirements": [],
                    "candidateIds": [],
                    "comparedIds": [],
                    "evidenceStatus": "missing",
                },
                "taskStateExtraction": {
                    "route": "deterministic_unsupported_category_boundary",
                    "reason": "unsupported_product_category",
                },
            },
        }
    )
    state = await create_task_state(request)
    runtime = GraphV2Runtime(
        user_message=state.goal,
        client=SimpleNamespace(),
        model="test-model",
        resolve_tool_schemas=lambda _state: [],
        tool_caller=AsyncMock(),
        trace_builder=TraceBuilder("run-server-boundary", mode="context_pack"),
        projector=None,
        max_transitions=12,
        durable=True,
        task_id=state.task_id,
        control_policy="react_v1",
    )
    result = await entry_node(
        {"task_state": state, "task_id": state.task_id, "node_events": []},
        SimpleNamespace(context=runtime),
    )
    assert result["last_route_decision"] == "clarification"
    assert "degraded_reason" not in result


@pytest.mark.parametrize(
    "reason",
    ["unsupported_game_camera_evidence", "stale_candidate_reference"],
)
def test_react_v1_routes_preexecution_adaptive_trigger_before_clarification(
    isolated_redis,
    reason: str,
) -> None:
    async def run_case() -> None:
        request = _ready_request().model_copy(
            update={
                "status": "collecting_information",
                "pending_questions": ["请确认下一步"],
                "domain_state": {
                    **_ready_request().domain_state,
                    "taskStateExtraction": {"reason": reason},
                },
            }
        )
        state = await create_task_state(request)
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-adaptive-entry", mode="context_pack"),
            projector=None,
            max_transitions=12,
            control_policy="react_v1",
            durable=True,
            task_id=state.task_id,
        )
        result = await entry_node(
            {"task_state": state, "node_events": []},
            SimpleNamespace(context=runtime),
        )
        assert result["last_route_decision"] == "react_policy"

    asyncio.run(run_case())


def test_react_v1_keeps_ordinary_pending_question_on_clarification(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        request = _ready_request().model_copy(
            update={
                "status": "collecting_information",
                "pending_questions": ["请补充预算"],
            }
        )
        state = await create_task_state(request)
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-ordinary-clarification", mode="context_pack"),
            projector=None,
            max_transitions=12,
            control_policy="react_v1",
            durable=True,
            task_id=state.task_id,
        )
        result = await entry_node(
            {"task_state": state, "node_events": []},
            SimpleNamespace(context=runtime),
        )
        assert result["last_route_decision"] == "clarification"

    asyncio.run(run_case())


@pytest.mark.parametrize("category", ["phone", "laptop", "headphones"])
def test_react_v1_full_durable_loop_uses_shared_inbox_and_no_planner(
    isolated_redis,
    category: str,
) -> None:
    asyncio.run(
        _case_react_v1_full_durable_loop_uses_shared_inbox_and_no_planner(
            isolated_redis,
            category,
        )
    )


def test_react_v1_pause_after_tool_resumes_before_validator_without_redispatch(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                status="ready",
            ),
        )
        run_id = "run-react-pause-after-tool"
        thread_id = f"v2-task:{state.task_id}:{run_id}"
        calls = 0

        async def caller_v2(name: str, arguments: dict, _context) -> ToolTrace:
            nonlocal calls
            calls += 1
            await request_graph_pause(
                task_id=state.task_id,
                session_id="session-a",
                run_id=run_id,
                thread_id=thread_id,
                control_policy="react_v1",
            )
            return ToolTrace(
                tool=name,
                ok=True,
                detail=two_stage_search_detail([101, 102]),
            )

        common = dict(
            task_id=state.task_id,
            session_id="session-a",
            user_message=state.goal,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            tool_caller_v2=caller_v2,
            tool_inbox=InFileToolInbox(),
            max_transitions=12,
            control_policy="react_v1",
        )
        paused = await run_graph_v2_durable(
            **common,
            run_id=run_id,
            thread_id=thread_id,
            trace_builder=TraceBuilder(run_id, mode="context_pack"),
        )
        assert paused.boundary == "operator_paused"
        assert paused.pause_receipt["pausedBeforeNode"] == "validator"
        assert calls == 1

        resumed = await run_graph_v2_durable(
            **common,
            restart=True,
            pause_resume=public_pause_receipt(paused.pause_receipt),
            trace_builder=TraceBuilder(run_id, mode="context_pack"),
        )
        assert resumed.boundary == "task_completed"
        assert calls == 1
        assert await read_graph_pause(state.task_id) is None

    asyncio.run(run_case())


def test_decision_reservations_survive_runtime_reconstruction(
    isolated_redis,
) -> None:
    asyncio.run(
        _case_decision_reservations_survive_runtime_reconstruction(
            isolated_redis
        )
    )


def test_react_v1_rejects_non_ecommerce_scope_without_model(
    isolated_redis,
) -> None:
    asyncio.run(
        _case_react_v1_rejects_non_ecommerce_scope_without_model(
            isolated_redis
        )
    )


def test_react_v1_rejects_ineligible_pending_clarification(
    isolated_redis,
) -> None:
    asyncio.run(
        _case_react_v1_rejects_ineligible_pending_clarification(isolated_redis)
    )


def test_react_v1_preserves_server_owned_unsupported_category_answer(
    isolated_redis,
) -> None:
    asyncio.run(
        _case_react_v1_preserves_server_owned_unsupported_category_answer(
            isolated_redis
        )
    )


def test_react_v1_rechecks_scope_after_clarification_application(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        request = _ready_request().model_copy(
            update={
                "status": "collecting_information",
                "pending_questions": ["请补充关键条件"],
            }
        )
        state = await create_task_state(request)

        async def invalidate_scope(updated, _answer):
            return await update_task_state(
                updated.task_id,
                TaskStatePatchRequest(
                    expectedRevision=updated.revision,
                    actor="agent",
                    status="collecting_information",
                    pendingQuestions=["当前品类不受支持，请重新选择品类。"],
                    domainStatePatch={
                        "shoppingGuide": {
                            "mode": "recommend",
                            "category": None,
                            "useCases": [],
                            "requirements": [],
                            "brandAvoidances": [],
                            "candidateIds": [],
                            "comparedIds": [],
                            "evidenceStatus": "missing",
                        },
                        "candidateScope": None,
                        "scopeRerankRequest": None,
                    },
                ),
            )

        runtime = GraphV2Runtime(
            user_message="补充条件",
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-scope-after-clarification", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=state.task_id,
            run_id="run-scope-after-clarification",
            thread_id=f"v2-task:{state.task_id}:run-scope-after-clarification",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
            clarification_answer_applier=invalidate_scope,
        )
        graph_state = _graph_state(
            task_state=state,
            task_id=state.task_id,
            thread_id=runtime.thread_id,
            session_owner_hash=runtime.session_owner_hash,
            graph_revision=state.revision,
            action="ask_user",
        )
        with patch("app.graph.nodes.clarification.interrupt", return_value="新要求"):
            result = await clarification_node(
                graph_state, SimpleNamespace(context=runtime)
            )
        assert result["action"] == "stop_turn"
        assert result["degraded_reason"] == "react_scope_ineligible"
        assert result["task_state"].revision == state.revision + 2

    asyncio.run(run_case())


def test_concurrent_decision_reservations_never_grant_more_than_two(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-budget-concurrent", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=state.task_id,
            run_id="run-budget-concurrent",
            thread_id=f"v2-task:{state.task_id}:run-budget-concurrent",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        results = await asyncio.gather(*[
            _reserve_model_decision(state, runtime, view_hash=str(index) * 64)
            for index in range(1, 4)
        ])
        assert sum(
            int(acquired)
            for _state_after, _count, acquired, _slot in results
        ) == 2
        assert await _decision_budget_count(state, runtime) == 2

    asyncio.run(run_case())


def test_resume_rejects_control_policy_switch(isolated_redis) -> None:
    async def run_case() -> None:
        state = await create_task_state(
            TaskStateCreateRequest(
                goal="推荐二手手机",
                task_type="ecommerce_guide",
                session_id="session-a",
                pending_questions=["预算是多少？"],
                domain_state=_ready_request().domain_state,
            )
        )
        inbox = InFileToolInbox()
        common = dict(
            task_id=state.task_id,
            session_id="session-a",
            user_message=state.goal,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            tool_caller_v2=AsyncMock(),
            tool_inbox=inbox,
            max_transitions=12,
        )
        parked = await run_graph_v2_durable(
            **common,
            trace_builder=TraceBuilder("run-policy-fixed", mode="context_pack"),
            run_id="run-policy-fixed",
            control_policy="fixed_v1",
        )
        payload = {
            "taskId": state.task_id,
            "runId": parked.run_id,
            "threadId": parked.thread_id,
            "revision": parked.interrupt_payload["revision"],
            "proposalHash": parked.interrupt_payload["proposalHash"],
            "answer": "2000元",
        }
        rejected = await run_graph_v2_durable(
            **common,
            trace_builder=TraceBuilder("run-policy-react", mode="context_pack"),
            resume=payload,
            control_policy="react_v1",
        )
        assert rejected.boundary == "resume_rejected"
        assert rejected.rejected_reason == "run_control_policy_mismatch"

    asyncio.run(run_case())


def test_model_answer_rejects_concurrent_taskstate_revision_drift(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        current = _graph_state()["task_state"]
        drifted = current.model_copy(update={"revision": current.revision + 1})
        view = _adaptive_view()
        answer_option = next(
            item
            for item in view.allowed_action_options
            if item.kind == "ANSWER"
        )
        action = materialize_next_action(
            ReactActionProposal(
                taskId=view.task_id,
                basedOnRevision=view.task_revision,
                decisionViewHash=view.decision_view_hash,
                optionId=answer_option.option_id,
            ),
            view,
        )
        runtime = GraphV2Runtime(
            user_message="请根据证据回答",
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-revision-drift", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=current.task_id,
            run_id="run-revision-drift",
            thread_id=f"v2-task:{current.task_id}:run-revision-drift",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        graph_state = _graph_state(
            task_id=current.task_id,
            thread_id=runtime.thread_id,
            session_owner_hash=runtime.session_owner_hash,
            graph_revision=current.revision,
        )
        with patch(
            "app.graph.nodes.react_policy._hydrate_task_state",
            new=AsyncMock(side_effect=[current, drifted]),
        ), patch(
            "app.graph.nodes.react_policy.build_decision_context_view",
            return_value=view,
        ), patch(
            "app.graph.nodes.react_policy.deterministic_next_action",
            return_value=None,
        ), patch(
            "app.graph.nodes.react_policy._reserve_model_decision",
            new=AsyncMock(return_value=(current, 1, True, 1)),
        ), patch(
            "app.graph.nodes.react_policy.decide_next_action",
            new=AsyncMock(return_value=action),
        ):
            result = await react_policy_node(
                graph_state, SimpleNamespace(context=runtime)
            )
        assert result["terminal_outcome"] == "STATE_DIVERGED"
        assert result["react_last_outcome"]["errorCode"] == "stale_task_revision"
        decision = runtime.trace_builder.finish().react_decisions[-1]
        end_event = result["node_events"][-1]
        assert decision["modelName"] == "test-model"
        assert decision["modelCallId"].startswith("rmc-")
        assert len(decision["decisionBindingHash"]) == 64
        assert decision["taskRevision"] == view.task_revision
        assert end_event["routeDecision"] == "STATE_DIVERGED"
        assert end_event["modelName"] == decision["modelName"]
        assert end_event["modelCallId"] == decision["modelCallId"]
        assert (
            end_event["decisionBindingHash"]
            == decision["decisionBindingHash"]
        )

    asyncio.run(run_case())


def test_model_call_identity_binds_trace_to_exact_react_policy_event(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        current = _graph_state()["task_state"]
        view = _adaptive_view()
        answer_option = next(
            item for item in view.allowed_action_options if item.kind == "ANSWER"
        )
        action = materialize_next_action(
            ReactActionProposal(
                taskId=view.task_id,
                basedOnRevision=view.task_revision,
                decisionViewHash=view.decision_view_hash,
                optionId=answer_option.option_id,
            ),
            view,
        )
        trace = TraceBuilder("run-model-binding", mode="context_pack")
        runtime = GraphV2Runtime(
            user_message="请根据证据回答",
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=trace,
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=current.task_id,
            run_id="run-model-binding",
            thread_id=f"v2-task:{current.task_id}:run-model-binding",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        graph_state = _graph_state(
            task_id=current.task_id,
            thread_id=runtime.thread_id,
            session_owner_hash=runtime.session_owner_hash,
            graph_revision=current.revision,
        )
        with patch(
            "app.graph.nodes.react_policy._hydrate_task_state",
            new=AsyncMock(side_effect=[current, current]),
        ), patch(
            "app.graph.nodes.react_policy.build_decision_context_view",
            return_value=view,
        ), patch(
            "app.graph.nodes.react_policy.deterministic_next_action",
            return_value=None,
        ), patch(
            "app.graph.nodes.react_policy.decide_next_action",
            new=AsyncMock(return_value=action),
        ):
            result = await react_policy_node(
                graph_state, SimpleNamespace(context=runtime)
            )

        decision = trace.finish().react_decisions[-1]
        end_event = result["node_events"][-1]
        assert decision["decisionSource"] == "model"
        assert decision["modelName"] == "test-model"
        assert decision["modelCallId"].startswith("rmc-")
        assert end_event["nodeName"] == "react_policy"
        assert end_event["phase"] == "end"
        assert end_event["modelName"] == "test-model"
        assert end_event["modelCallId"] == decision["modelCallId"]
        assert (
            end_event["decisionBindingHash"]
            == decision["decisionBindingHash"]
        )

    asyncio.run(run_case())


def test_model_crash_window_consumes_durable_reservations(isolated_redis) -> None:
    async def run_case() -> None:
        current = _graph_state()["task_state"]
        view = _adaptive_view()
        answer_option = next(
            item for item in view.allowed_action_options if item.kind == "ANSWER"
        )
        action = materialize_next_action(
            ReactActionProposal(
                taskId=view.task_id,
                basedOnRevision=view.task_revision,
                decisionViewHash=view.decision_view_hash,
                optionId=answer_option.option_id,
            ),
            view,
        )
        decide = AsyncMock(return_value=action)
        runtime = GraphV2Runtime(
            user_message="请根据证据回答",
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-model-crash", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=current.task_id,
            run_id="run-model-crash",
            thread_id=f"v2-task:{current.task_id}:run-model-crash",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        graph_state = _graph_state(
            task_id=current.task_id,
            thread_id=runtime.thread_id,
            session_owner_hash=runtime.session_owner_hash,
            graph_revision=current.revision,
        )

        def fault(point: str) -> None:
            if point == "after_model_decision":
                raise ReactPolicyFaultInjected(point)

        set_react_policy_fault_hook(fault)
        try:
            with patch(
                "app.graph.nodes.react_policy._hydrate_task_state",
                new=AsyncMock(return_value=current),
            ), patch(
                "app.graph.nodes.react_policy.build_decision_context_view",
                return_value=view,
            ), patch(
                "app.graph.nodes.react_policy.deterministic_next_action",
                return_value=None,
            ), patch(
                "app.graph.nodes.react_policy.decide_next_action",
                new=decide,
            ):
                for _ in range(2):
                    with pytest.raises(ReactPolicyFaultInjected):
                        await react_policy_node(
                            graph_state, SimpleNamespace(context=runtime)
                        )
                set_react_policy_fault_hook(None)
                stopped = await react_policy_node(
                    graph_state, SimpleNamespace(context=runtime)
                )
        finally:
            set_react_policy_fault_hook(None)
        assert decide.await_count == 2
        assert stopped["degraded_reason"] == "react_model_decision_budget_exhausted"

    asyncio.run(run_case())


@pytest.mark.parametrize("kind", ["CALL_TOOL", "ASK_CLARIFICATION"])
def test_action_persist_crash_window_replays_without_new_decision(
    isolated_redis,
    kind: str,
) -> None:
    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                status="ready",
            ),
        )
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder(f"run-action-{kind}", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=state.task_id,
            run_id=f"run-action-{kind}",
            thread_id=f"v2-task:{state.task_id}:run-action-{kind}",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        graph_state = _graph_state(
            task_state=state,
            task_id=state.task_id,
            thread_id=runtime.thread_id,
            session_owner_hash=runtime.session_owner_hash,
            graph_revision=state.revision,
        )
        view = build_decision_context_view(
            state,
            user_message=state.goal,
            allowed_tool_names=["search_products"],
        )
        if kind == "CALL_TOOL":
            action = deterministic_next_action(view)
            assert action is not None and action.kind == "CALL_TOOL"
        else:
            action = NextAction(
                actionId="action-ask-crash",
                taskId=state.task_id,
                basedOnRevision=state.revision,
                decisionViewHash=view.decision_view_hash,
                kind="ASK_CLARIFICATION",
                reasonCode="published_clarification_required",
                question="预算是多少？",
            )

        def fault(point: str) -> None:
            if point == "after_action_persist":
                raise ReactPolicyFaultInjected(point)

        set_react_policy_fault_hook(fault)
        try:
            with patch(
                "app.graph.nodes.react_policy.deterministic_next_action",
                return_value=action,
            ):
                with pytest.raises(ReactPolicyFaultInjected):
                    await react_policy_node(
                        graph_state, SimpleNamespace(context=runtime)
                    )
        finally:
            set_react_policy_fault_hook(None)

        replayed = await react_policy_node(
            graph_state, SimpleNamespace(context=runtime)
        )
        assert replayed["action"] == (
            "continue_to_executor" if kind == "CALL_TOOL" else "ask_user"
        )
        assert replayed["react_action_id"] == action.action_id

    asyncio.run(run_case())


def test_action_atomic_commit_response_loss_replays_without_new_decision(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                status="ready",
            ),
        )
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-anchor-crash", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=state.task_id,
            run_id="run-anchor-crash",
            thread_id=f"v2-task:{state.task_id}:run-anchor-crash",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        view = build_decision_context_view(
            state,
            user_message=state.goal,
            allowed_tool_names=["search_products"],
        )
        action = deterministic_next_action(view)
        assert action is not None and action.kind == "CALL_TOOL"
        plan = materialize_react_execution_plan(state, action)

        def fault(point: str) -> None:
            if point == "after_action_atomic_commit":
                raise ReactPolicyFaultInjected(point)

        set_react_policy_fault_hook(fault)
        try:
            with pytest.raises(ReactPolicyFaultInjected):
                await _persist_action(state, runtime, action, plan=plan)
        finally:
            set_react_policy_fault_hook(None)

        committed = await get_task_state(state.task_id)
        assert committed is not None
        assert committed.revision == state.revision + 1
        replayed = await _replay_action(committed, runtime)
        assert replayed == action

    asyncio.run(run_case())


@pytest.mark.parametrize(
    ("inbox_status", "inbox_error", "expected_error"),
    [
        (InboxStatus.UNKNOWN, None, "tool_inbox_unknown"),
        (InboxStatus.CONFLICT, None, "tool_inbox_conflict"),
        (
            InboxStatus.UNAVAILABLE,
            "redis_claim_unavailable",
            "redis_claim_unavailable",
        ),
    ],
)
def test_react_v1_inbox_rejection_persists_outcome_without_live_call(
    isolated_redis,
    inbox_status: InboxStatus,
    inbox_error: str | None,
    expected_error: str,
) -> None:
    class RejectingInbox(InFileToolInbox):
        async def claim(self, slot, *, run_id: str, thread_id: str) -> InboxResponse:
            return InboxResponse(inbox_status, error_code=inbox_error)

    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                status="ready",
            ),
        )
        live_caller = AsyncMock()
        trace = TraceBuilder(
            f"run-inbox-{inbox_status.value.lower()}", mode="context_pack"
        )
        result = await run_graph_v2_durable(
            task_id=state.task_id,
            session_id="session-a",
            run_id=f"run-inbox-{inbox_status.value.lower()}",
            user_message=state.goal,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            tool_caller_v2=live_caller,
            tool_inbox=RejectingInbox(),
            trace_builder=trace,
            max_transitions=12,
            control_policy="react_v1",
        )

        assert result.boundary == "stop_turn"
        live_caller.assert_not_awaited()
        outcome = result.graph_state["react_last_outcome"]
        assert outcome["status"] == "FAILED"
        assert outcome["errorCode"] == expected_error
        receipt = (result.task_state.domain_state or {})["reactV1OutcomeReceipt"]
        assert receipt["outcome"] == outcome
        finished = trace.finish()
        assert len(finished.react_outcomes) == 1
        assert finished.react_outcomes[0]["errorCode"] == expected_error

    asyncio.run(run_case())


def test_executor_failure_rehydrate_rejects_foreign_action_drift(
    isolated_redis,
) -> None:
    class DriftingRejectingInbox(InFileToolInbox):
        async def claim(self, slot, *, run_id: str, thread_id: str) -> InboxResponse:
            live = await get_task_state(slot.task_id)
            assert live is not None
            await update_task_state(
                live.task_id,
                TaskStatePatchRequest(
                    expectedRevision=live.revision,
                    actor="user",
                    domainStatePatch={"reactV1ActionReceipt": None},
                ),
            )
            return InboxResponse(InboxStatus.UNKNOWN)

    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                status="ready",
            ),
        )
        live_caller = AsyncMock()
        result = await run_graph_v2_durable(
            task_id=state.task_id,
            session_id="session-a",
            run_id="run-inbox-foreign-drift",
            user_message=state.goal,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            tool_caller_v2=live_caller,
            tool_inbox=DriftingRejectingInbox(),
            trace_builder=TraceBuilder("run-inbox-foreign-drift", mode="context_pack"),
            max_transitions=12,
            control_policy="react_v1",
        )
        assert result.boundary == "state_diverged"
        live_caller.assert_not_awaited()
        assert "reactV1OutcomeReceipt" not in (result.task_state.domain_state or {})

    asyncio.run(run_case())


def test_executor_failure_rehydrate_rejects_external_revision_drift(
    isolated_redis,
) -> None:
    class RevisionDriftingInbox(InFileToolInbox):
        async def claim(self, slot, *, run_id: str, thread_id: str) -> InboxResponse:
            live = await get_task_state(slot.task_id)
            assert live is not None
            await update_task_state(
                live.task_id,
                TaskStatePatchRequest(
                    expectedRevision=live.revision,
                    actor="user",
                    domainStatePatch={"externalAuditMarker": "foreign-write"},
                ),
            )
            return InboxResponse(InboxStatus.UNKNOWN)

    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                status="ready",
            ),
        )
        live_caller = AsyncMock()
        result = await run_graph_v2_durable(
            task_id=state.task_id,
            session_id="session-a",
            run_id="run-inbox-revision-drift",
            user_message=state.goal,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            tool_caller_v2=live_caller,
            tool_inbox=RevisionDriftingInbox(),
            trace_builder=TraceBuilder("run-inbox-revision-drift", mode="context_pack"),
            max_transitions=12,
            control_policy="react_v1",
        )
        assert result.boundary == "state_diverged"
        live_caller.assert_not_awaited()
        assert "reactV1OutcomeReceipt" not in (result.task_state.domain_state or {})

    asyncio.run(run_case())


def test_terminal_action_journal_corruption_fails_closed(isolated_redis) -> None:
    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-terminal-journal", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=state.task_id,
            run_id="run-terminal-journal",
            thread_id=f"v2-task:{state.task_id}:run-terminal-journal",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        action = NextAction(
            actionId="action-terminal-journal",
            taskId=state.task_id,
            basedOnRevision=state.revision,
            decisionViewHash="a" * 64,
            kind="ANSWER",
            reasonCode="answer_validated_context",
            answerContextRef=f"validated-task:{state.task_id}:r{state.revision}",
        )
        await _persist_action(state, runtime, action)
        isolated_redis.strings[f"{_journal_prefix(runtime)}:latest-action"] = "{bad"
        with pytest.raises(Exception) as exc:
            await _replay_action(state, runtime)
        assert getattr(exc.value, "code", None) == "react_action_journal_invalid"

        malformed = {
            "taskId": runtime.task_id,
            "runId": runtime.run_id,
            "threadId": runtime.thread_id,
            "sessionOwnerHash": runtime.session_owner_hash,
            "controlPolicy": "react_v1",
            "policyRevision": "react-v1-2026-08-27",
            "taskRevision": state.revision,
            "action": None,
            "actionSha256": "0" * 64,
        }
        isolated_redis.strings[f"{_journal_prefix(runtime)}:latest-action"] = (
            json.dumps(malformed)
        )
        with pytest.raises(Exception) as legal_json_exc:
            await _replay_action(state, runtime)
        assert getattr(legal_json_exc.value, "code", None) == (
            "react_action_journal_invalid"
        )

    asyncio.run(run_case())


def test_call_tool_receipt_binds_complete_immutable_plan_contract(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-plan-contract", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=state.task_id,
            run_id="run-plan-contract",
            thread_id=f"v2-task:{state.task_id}:run-plan-contract",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        view = build_decision_context_view(
            state,
            user_message=state.goal,
            allowed_tool_names=["search_products"],
        )
        action = deterministic_next_action(view)
        assert action is not None and action.kind == "CALL_TOOL"
        plan = materialize_react_execution_plan(state, action)
        persisted = await _persist_action(state, runtime, action, plan=plan)

        tampered_step = persisted.active_plan.steps[0].model_copy(
            update={"tool_name": "compare_products"}
        )
        tampered_plan = persisted.active_plan.model_copy(
            update={"steps": [tampered_step]}
        )
        tampered = persisted.model_copy(update={"active_plan": tampered_plan})
        with pytest.raises(Exception) as exc:
            await _replay_action(tampered, runtime)
        assert getattr(exc.value, "code", None) == "react_action_journal_invalid"

        anchor_key = f"{_journal_prefix(runtime)}:action:{action.action_id}"
        isolated_redis.strings[anchor_key] = "{bad"
        with pytest.raises(Exception) as anchor_exc:
            await _replay_action(persisted, runtime)
        assert getattr(anchor_exc.value, "code", None) == (
            "react_action_journal_invalid"
        )

    asyncio.run(run_case())


def test_normal_executor_rejects_missing_action_anchor_before_inbox(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        live_caller = AsyncMock()
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=SimpleNamespace(),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=live_caller,
            trace_builder=TraceBuilder("run-normal-anchor", mode="context_pack"),
            projector=None,
            max_transitions=12,
            durable=True,
            task_id=state.task_id,
            run_id="run-normal-anchor",
            thread_id=f"v2-task:{state.task_id}:run-normal-anchor",
            session_owner_hash="0" * 16,
            control_policy="react_v1",
        )
        view = build_decision_context_view(
            state,
            user_message=state.goal,
            allowed_tool_names=["search_products"],
        )
        action = deterministic_next_action(view)
        assert action is not None and action.kind == "CALL_TOOL"
        plan = materialize_react_execution_plan(state, action)
        persisted = await _persist_action(state, runtime, action, plan=plan)
        isolated_redis.strings.pop(
            f"{_journal_prefix(runtime)}:action:{action.action_id}"
        )
        result = await executor_node(
            _graph_state(
                task_state=persisted,
                task_id=persisted.task_id,
                thread_id=runtime.thread_id,
                session_owner_hash=runtime.session_owner_hash,
                graph_revision=persisted.revision,
                react_action_id=action.action_id,
                react_action_kind="CALL_TOOL",
            ),
            SimpleNamespace(context=runtime),
        )
        assert result["terminal_outcome"] == "STATE_DIVERGED"
        live_caller.assert_not_awaited()

    asyncio.run(run_case())


def test_restart_after_caller_before_inbox_complete_persists_unknown_once(
    isolated_redis,
) -> None:
    class ProcessCrash(BaseException):
        pass

    class CrashBeforeCompleteInbox(InFileToolInbox):
        crash = True

        async def complete(self, slot, **kwargs) -> InboxResponse:
            if self.crash:
                raise ProcessCrash()
            return await super().complete(slot, **kwargs)

    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                status="ready",
            ),
        )
        inbox = CrashBeforeCompleteInbox()
        live_caller = AsyncMock(
            return_value=ToolTrace(
                tool="search_products",
                ok=True,
                detail=two_stage_search_detail([101, 102]),
            )
        )
        common = dict(
            task_id=state.task_id,
            session_id="session-a",
            user_message=state.goal,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            tool_caller_v2=live_caller,
            tool_inbox=inbox,
            max_transitions=12,
            control_policy="react_v1",
        )
        with pytest.raises(ProcessCrash):
            await run_graph_v2_durable(
                **common,
                run_id="run-crash-before-complete",
                trace_builder=TraceBuilder(
                    "run-crash-before-complete", mode="context_pack"
                ),
            )
        assert live_caller.await_count == 1
        inbox.crash = False
        restarted = await run_graph_v2_durable(
            **common,
            restart=True,
            trace_builder=TraceBuilder("run-crash-restart", mode="context_pack"),
        )
        assert restarted.boundary == "stop_turn"
        assert live_caller.await_count == 1
        assert restarted.graph_state["react_last_outcome"]["errorCode"] == (
            "tool_inbox_unknown"
        )

    asyncio.run(run_case())


def test_validator_receipt_crash_restarts_without_revalidation_or_tool(
    isolated_redis,
) -> None:
    async def run_case() -> None:
        state = await create_task_state(_ready_request())
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                status="ready",
            ),
        )
        inbox = InFileToolInbox()
        live_caller = AsyncMock(
            return_value=ToolTrace(
                tool="search_products",
                ok=True,
                detail=two_stage_search_detail([101, 102]),
            )
        )

        def fault() -> None:
            raise ValidatorFaultInjected()

        set_validator_fault_hook(fault)
        try:
            faulted = await run_graph_v2_durable(
                task_id=state.task_id,
                session_id="session-a",
                run_id="run-validator-crash",
                user_message=state.goal,
                client=_fake_client(AsyncMock()),
                model="test-model",
                resolve_tool_schemas=lambda _state: [_search_products_schema()],
                tool_caller=AsyncMock(),
                tool_caller_v2=live_caller,
                tool_inbox=inbox,
                trace_builder=TraceBuilder("run-validator-crash", mode="context_pack"),
                max_transitions=12,
                control_policy="react_v1",
            )
        finally:
            set_validator_fault_hook(None)
        assert faulted.boundary == "fault_injected"
        revision_after_fault = faulted.task_state.revision
        restarted = await run_graph_v2_durable(
            task_id=state.task_id,
            session_id="session-a",
            restart=True,
            user_message=state.goal,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda _state: [_search_products_schema()],
            tool_caller=AsyncMock(),
            tool_caller_v2=live_caller,
            tool_inbox=inbox,
            trace_builder=TraceBuilder("run-validator-restart", mode="context_pack"),
            max_transitions=12,
            control_policy="react_v1",
        )
        assert restarted.boundary == "task_completed"
        assert live_caller.await_count == 1
        assert restarted.task_state.revision == revision_after_fault
        starts = [
            event["nodeName"]
            for event in restarted.graph_state["node_events"]
            if event["phase"] == "start"
        ]
        assert starts[-2:] == ["validator", "react_policy"]

    asyncio.run(run_case())
