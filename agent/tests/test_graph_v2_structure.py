"""V2 control-plane graph structure + routing + redaction tests.

DAY-1 AGENT-ADVANCED-ARCH-GRAPH-V2: the V2 control plane must be genuinely
LangGraph-visible — five explicit nodes (entry/planner/executor/validator/
replanner) connected by conditional routers, default-off, with a V1-compatible
boundary contract and a redacted node-event source.

Every graph run below exercises the REAL production phase functions
(``run_planning_step`` / ``run_executor_step`` / ``run_validator_phase`` /
``run_replanner_phase``) through the compiled graph — fake model client, fake
tool caller, FakeRedis task store, no network.
"""
import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.graph import END

from app import task_state
from app.agent_trace import TraceBuilder
from app.context_pack import build_context_pack
from app.context_view import ContextProjector
from app.executor import run_executor_step
from app.graph import (
    CONTROLLED_GRAPH_V2,
    GRAPH_V2_NAME,
    GraphV2Runtime,
    redacted_state_hash,
    redacted_state_snapshot,
    route_after_entry,
    route_after_executor,
    route_after_planner,
    route_after_replanner,
    route_after_validator,
    run_graph_v2,
)
from app.harness import HarnessPreValidationError, _validate_view_and_record
from app.planning import TaskPlan
from app.planner import PLANNER_SUBMISSION_TOOL_NAME
from app.replanner import REPLANNER_SUBMISSION_TOOL_NAME
from app.schemas import ToolTrace
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    update_task_state,
)
from app.tools import TOOL_SCHEMAS
from app.validator import run_validator_phase
from tests.fake_redis import FakeRedis
from tests.two_stage_ranking_fixtures import two_stage_search_detail

_OS_HARD_REQ = {
    "key": "os", "operator": "eq", "value": "ios",
    "unit": "enum", "priority": "hard", "source": "user",
}


def _fake_client(create_mock: AsyncMock):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_mock),
        )
    )


def _search_products_schema() -> dict:
    for spec in TOOL_SCHEMAS:
        if spec["function"]["name"] == "search_products":
            return spec
    raise AssertionError("TOOL_SCHEMAS must ship search_products")


def _get_product_details_schema() -> dict:
    for spec in TOOL_SCHEMAS:
        if spec["function"]["name"] == "get_product_details":
            return spec
    raise AssertionError("TOOL_SCHEMAS must ship get_product_details")


def _review_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_shop_reviews",
            "description": "检索用户明确点名商户的真实评论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "shopName": {"type": "string"},
                },
                "required": ["query", "shopName"],
            },
        },
    }


def _planner_search_reply(goal: str):
    """Planner reply: one search_products step with server sources.

    NOTE: for ecommerce_guide the server published the deterministic plan, so
    this reply is never consumed — it documents the shape the V2 planner node
    accepts when the LLM path is used (local-life tasks).
    """
    payload = {
        "outcome": "planned",
        "steps": [
            {
                "stepId": "step-1",
                "description": "检索iOS二手机",
                "toolName": "search_products",
                "arguments": {
                    "query": goal,
                    "category": "手机",
                    "requirements": [_OS_HARD_REQ],
                },
                "argumentSources": {
                    "query": {"kind": "task_goal"},
                    "category": {"kind": "shopping_guide", "reference": "category"},
                    "requirements": {
                        "kind": "shopping_guide", "reference": "requirements",
                    },
                },
                "expectedOutput": {"requiresProductCandidates": True},
            }
        ],
    }
    call = SimpleNamespace(
        id="planner-call",
        function=SimpleNamespace(
            name=PLANNER_SUBMISSION_TOOL_NAME,
            arguments=json.dumps(payload, ensure_ascii=False),
        ),
    )
    message = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _planner_ask_user_reply():
    """Planner reply: needs_user_input → ask_user boundary (local-life path)."""
    payload = {
        "outcome": "needs_user_input",
        "question": "你指的是哪一家星河咖啡？",
    }
    call = SimpleNamespace(
        id="planner-call",
        function=SimpleNamespace(
            name=PLANNER_SUBMISSION_TOOL_NAME,
            arguments=json.dumps(payload, ensure_ascii=False),
        ),
    )
    message = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _replanner_search_reply(goal: str):
    """Replanner reply: a recovered search_products plan.

    The recovered route MUST differ from the failed deterministic plan (which
    ran the exact-goal search) or the Replanner fails closed with
    ``replan_unchanged``.  With ``searchResultLimit`` in system_policies the
    model can legitimately widen recall via the ``limit`` argument.
    """
    payload = {
        "outcome": "replanned",
        "steps": [
            {
                "stepId": "step-recovery-1",
                "description": "扩大召回再次搜索 iOS 二手机",
                "toolName": "search_products",
                "arguments": {
                    "query": goal,
                    "category": "手机",
                    "requirements": [_OS_HARD_REQ],
                    "limit": 20,
                },
                "argumentSources": {
                    "query": {"kind": "task_goal"},
                    "category": {"kind": "shopping_guide", "reference": "category"},
                    "requirements": {
                        "kind": "shopping_guide", "reference": "requirements",
                    },
                    "limit": {"kind": "system_policy", "reference": "searchResultLimit"},
                },
                "expectedOutput": {"requiresProductCandidates": True},
            }
        ],
    }
    call = SimpleNamespace(
        id="replanner-call",
        function=SimpleNamespace(
            name=REPLANNER_SUBMISSION_TOOL_NAME,
            arguments=json.dumps(payload, ensure_ascii=False),
        ),
    )
    message = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_tool(name: str, arguments: dict):
    if name == "search_products":
        return ToolTrace(
            tool=name, ok=True, detail=two_stage_search_detail([101, 102])
        )
    if name == "get_product_details":
        return ToolTrace(
            tool=name, ok=True,
            detail={"productIds": [101, 102], "products": [{"id": 101}, {"id": 102}]},
        )
    raise AssertionError(f"unexpected tool dispatch: {name}")


def _used_phone_create_request() -> TaskStateCreateRequest:
    return TaskStateCreateRequest(
        goal="想找 iOS 二手机。",
        task_type="ecommerce_guide",
        domain_state={
            "shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "useCases": [],
                "requirements": [_OS_HARD_REQ],
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            }
        },
    )


# ── Part A: pure structure (no DB, no graph run) ────────────────────────────


def test_graph_exposes_five_explicit_nodes_no_react_cycle() -> None:
    nodes = set(CONTROLLED_GRAPH_V2.get_graph().nodes)
    assert {"entry", "planner", "executor", "validator", "replanner"} <= nodes
    assert "react_cycle" not in nodes
    assert CONTROLLED_GRAPH_V2.name == GRAPH_V2_NAME
    assert GRAPH_V2_NAME == "ecommerce-guide-v2-control-plane"


def test_graph_edges_connect_each_phase_explicitly() -> None:
    edges = {(e.source, e.target) for e in CONTROLLED_GRAPH_V2.get_graph().edges}
    assert ("__start__", "entry") in edges
    assert ("entry", "planner") in edges
    assert ("entry", "replanner") in edges
    assert ("planner", "executor") in edges
    assert ("planner", "validator") in edges
    assert ("executor", "executor") in edges
    assert ("executor", "validator") in edges
    assert ("validator", "replanner") in edges
    assert ("replanner", "executor") in edges
    assert ("replanner", "__end__") in edges
    assert ("validator", "__end__") in edges
    assert ("planner", "__end__") in edges
    assert ("executor", "__end__") in edges
    assert ("entry", "__end__") in edges


def test_route_after_entry_picks_planner_replanner_or_end() -> None:
    assert route_after_entry({"last_route_decision": "planner"}) == "planner"
    assert route_after_entry({"last_route_decision": "replanner"}) == "replanner"
    # Illegal state (missing route decision) must not loop the graph.
    assert route_after_entry({}) == END
    # Budget exhausted always wins over the natural route.
    assert (
        route_after_entry(
            {"transition_limit_reached": True, "last_route_decision": "planner"}
        )
        == END
    )


def test_route_after_planner_covers_continue_validate_and_terminal() -> None:
    assert route_after_planner({"action": "continue_to_executor"}) == "executor"
    assert route_after_planner({"action": "ready_for_validation"}) == "validator"
    assert route_after_planner({"action": "ask_user"}) == END
    assert route_after_planner({"action": "task_completed"}) == END
    assert route_after_planner({"action": "stop_turn"}) == END
    # Illegal action and exhausted budget both fail closed to END.
    assert route_after_planner({"action": "not-a-real-action"}) == END
    assert (
        route_after_planner(
            {"transition_limit_reached": True, "action": "continue_to_executor"}
        )
        == END
    )


def test_route_after_executor_covers_loop_validate_and_terminal() -> None:
    assert route_after_executor({"action": "continue_to_executor"}) == "executor"
    assert route_after_executor({"action": "ready_for_validation"}) == "validator"
    assert route_after_executor({"action": "ask_user"}) == END
    assert route_after_executor({"action": "stop_turn"}) == END
    assert (
        route_after_executor(
            {"transition_limit_reached": True, "action": "continue_to_executor"}
        )
        == END
    )


def test_route_after_validator_routes_replan_or_completed() -> None:
    assert route_after_validator({"action": "ready_for_replanning"}) == "replanner"
    assert route_after_validator({"action": "task_completed"}) == END
    assert route_after_validator({"action": "stop_turn"}) == END
    assert (
        route_after_validator(
            {"transition_limit_reached": True, "action": "ready_for_replanning"}
        )
        == END
    )


def test_route_after_replanner_returns_to_executor_or_end() -> None:
    assert route_after_replanner({"action": "continue_to_executor"}) == "executor"
    assert route_after_replanner({"action": "ask_user"}) == END
    assert route_after_replanner({"action": "stop_turn"}) == END
    assert (
        route_after_replanner(
            {"transition_limit_reached": True, "action": "continue_to_executor"}
        )
        == END
    )


def test_redacted_state_snapshot_is_identity_only() -> None:
    now = datetime.now(timezone.utc)
    state = TaskState(
        taskId="t-redacted",
        revision=3,
        status="ready",
        goal="用户不能看到的秘密目标文本",
        taskType="ecommerce_guide",
        createdAt=now,
        updatedAt=now,
        domainState={
            "stepOutputs": {"step-1": {"huge": "raw-payload"}},
            "validationResult": {"outcome": "passed"},
            "compoundComparison": {"status": "ready"},
        },
    )
    snapshot = redacted_state_snapshot(state)
    serialized = json.dumps(snapshot, ensure_ascii=False)
    # User goal, raw outputs and raw evidence never enter the event projection.
    assert "秘密目标文本" not in serialized
    assert "raw-payload" not in serialized
    assert snapshot["taskId"] == "t-redacted"
    assert snapshot["revision"] == 3
    assert snapshot["validationOutcome"] == "passed"
    digest = redacted_state_hash(state)
    assert isinstance(digest, str) and len(digest) == 16


# ── Part B: full graph runs against the real production phases ──────────────


class GraphV2StructureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _ready_used_phone_state(self) -> TaskState:
        created = await create_task_state(_used_phone_create_request())
        return await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )

    async def _ready_local_life_state(self) -> TaskState:
        created = await create_task_state(
            TaskStateCreateRequest(
                goal="判断星河咖啡是否适合安静聊天",
                facts=[
                    {
                        "key": "shopName",
                        "value": "星河咖啡",
                        "certainty": "confirmed",
                        "source": "user",
                    }
                ],
            )
        )
        return await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )

    async def _failed_used_phone_state(self) -> TaskState:
        """Reconstruct Smoke-005's historical executed-without-output state."""
        created = await create_task_state(_used_phone_create_request())
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        plan = TaskPlan(
            planId="plan-without-candidates",
            basedOnRevision=ready.revision,
            steps=[
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {
                        "query": ready.goal,
                        "category": "手机",
                        "requirements": [_OS_HARD_REQ],
                    },
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "category": {
                            "kind": "shopping_guide", "reference": "category",
                        },
                        "requirements": {
                            "kind": "shopping_guide", "reference": "requirements",
                        },
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        )
        planned = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                activePlan=plan,
            ),
        )
        executed = await run_executor_step(
            planned,
            [_search_products_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_products",
                    ok=True,
                    detail=two_stage_search_detail([101]),
                )
            ),
        )
        historical_gap = await update_task_state(
            executed.task_state.task_id,
            TaskStatePatchRequest(
                expectedRevision=executed.task_state.revision,
                actor="system",
                domainStatePatch={"stepOutputs": {}},
            ),
        )
        result, failed = await run_validator_phase(historical_gap)
        self.assertEqual(result.outcome, "insufficient_evidence")
        return failed

    def _runtime(
        self,
        *,
        state: TaskState,
        client,
        tool_caller,
        max_transitions: int,
        trace_builder: TraceBuilder,
        projector=None,
        system_policies=None,
        max_replans: int = 3,
    ) -> GraphV2Runtime:
        return GraphV2Runtime(
            user_message=state.goal,
            client=client,
            model="test-model",
            resolve_tool_schemas=lambda s: [
                _search_products_schema(), _get_product_details_schema(),
            ],
            tool_caller=tool_caller,
            trace_builder=trace_builder,
            projector=projector,
            max_transitions=max_transitions,
            system_policies=system_policies,
            max_replans=max_replans,
        )

    async def test_search_task_completed_full_graph_run(self):
        ready = await self._ready_used_phone_state()
        create_mock = AsyncMock(return_value=_planner_search_reply(goal=ready.goal))
        tool_caller = AsyncMock(side_effect=_fake_tool)
        pack = await build_context_pack(
            ready, allowed_tools=["search_products", "get_product_details"]
        )
        projector = ContextProjector(pack)
        trace = TraceBuilder("run-graph-v2-search", mode="context_pack")
        runtime = self._runtime(
            state=ready,
            client=_fake_client(create_mock),
            tool_caller=tool_caller,
            max_transitions=8,
            trace_builder=trace,
            projector=projector,
        )

        graph_state = await run_graph_v2(ready, runtime)

        # Boundary contract identical to V1: transitions[-1].action drives the
        # surrounding harness block; budget flag untouched.
        self.assertEqual(graph_state["action"], "task_completed")
        self.assertEqual(graph_state["terminal_outcome"], "task_completed")
        self.assertIs(graph_state["transition_limit_reached"], False)
        self.assertEqual(graph_state["transitions"][-1].action, "task_completed")

        # One explicit node per phase (entry is control, not a phase).
        node_sequence = [
            event["nodeName"]
            for event in graph_state["node_events"]
            if event["phase"] == "start"
        ]
        self.assertEqual(node_sequence, ["entry", "planner", "executor", "validator"])
        self.assertEqual(graph_state["transition_count"], 3)

        # The deterministic ecommerce plan needs no model call; one search
        # dispatch is the smallest sufficient action.
        create_mock.assert_not_awaited()
        search_calls = [
            c for c in tool_caller.await_args_list if c.args[0] == "search_products"
        ]
        self.assertEqual(len(search_calls), 1)

        # Per-node event pairs exist for every executed node.
        end_events = [e for e in graph_state["node_events"] if e["phase"] == "end"]
        self.assertEqual(
            [e["nodeName"] for e in end_events],
            ["entry", "planner", "executor", "validator"],
        )

    async def test_ask_user_planner_decision_routes_to_end(self):
        ready = await self._ready_local_life_state()
        create_mock = AsyncMock(return_value=_planner_ask_user_reply())
        runtime = GraphV2Runtime(
            user_message="请根据真实评论判断",
            client=_fake_client(create_mock),
            model="test-model",
            resolve_tool_schemas=lambda s: [_review_tool_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-graph-ask-user"),
            projector=None,
            max_transitions=4,
        )

        graph_state = await run_graph_v2(ready, runtime)

        # ask_user is a terminal boundary: no executor, graph routes to END.
        self.assertEqual(graph_state["action"], "ask_user")
        self.assertEqual(graph_state["terminal_outcome"], "ask_user")
        self.assertEqual(graph_state["transition_count"], 1)
        self.assertEqual(
            graph_state["task_state"].status, "collecting_information"
        )
        self.assertEqual(
            graph_state["task_state"].pending_questions,
            ["你指的是哪一家星河咖啡？"],
        )
        node_sequence = [
            event["nodeName"]
            for event in graph_state["node_events"]
            if event["phase"] == "start"
        ]
        self.assertEqual(node_sequence, ["entry", "planner"])

    async def test_max_transitions_budget_fail_closed(self):
        ready = await self._ready_used_phone_state()
        create_mock = AsyncMock(return_value=_planner_search_reply(goal=ready.goal))
        tool_caller = AsyncMock(side_effect=_fake_tool)
        runtime = self._runtime(
            state=ready,
            client=_fake_client(create_mock),
            tool_caller=tool_caller,
            max_transitions=2,
            trace_builder=TraceBuilder("run-graph-limit"),
        )

        graph_state = await run_graph_v2(ready, runtime)

        # Budget exhausted mid-continue: planner consumed one business node, the
        # first executor node consumed the second — normalised to the exact V1
        # limit contract (continue_to_executor + flag) for the harness boundary.
        self.assertEqual(graph_state["action"], "continue_to_executor")
        self.assertIs(graph_state["transition_limit_reached"], True)
        self.assertEqual(graph_state["terminal_outcome"], "max_transitions_exceeded")
        self.assertEqual(graph_state["degraded_reason"], "max_transitions_exceeded")
        self.assertEqual(graph_state["transitions"][-1].action, "continue_to_executor")
        node_sequence = [
            event["nodeName"]
            for event in graph_state["node_events"]
            if event["phase"] == "start"
        ]
        self.assertEqual(node_sequence, ["entry", "planner", "executor"])
        # Only one tool dispatch ran before the budget cut the turn (the second
        # plan step never executed).
        self.assertEqual(
            len([e for e in graph_state["node_events"] if e.get("toolName")]), 1
        )

    async def test_validator_fail_then_replan_full_loop(self):
        failed = await self._failed_used_phone_state()
        create_mock = AsyncMock(return_value=_replanner_search_reply(goal=failed.goal))
        tool_caller = AsyncMock(side_effect=_fake_tool)
        runtime = GraphV2Runtime(
            user_message=failed.goal,
            client=_fake_client(create_mock),
            model="test-model",
            resolve_tool_schemas=lambda s: [_search_products_schema()],
            tool_caller=tool_caller,
            trace_builder=TraceBuilder("run-graph-replan"),
            projector=None,
            max_transitions=10,
            system_policies={"searchResultLimit": 20},
            max_replans=2,
        )

        graph_state = await run_graph_v2(failed, runtime)

        # entry → replanner → executor → validator → task_completed.
        node_sequence = [
            event["nodeName"]
            for event in graph_state["node_events"]
            if event["phase"] == "start"
        ]
        self.assertEqual(node_sequence, ["entry", "replanner", "executor", "validator"])
        self.assertEqual(graph_state["action"], "task_completed")
        self.assertEqual(graph_state["terminal_outcome"], "task_completed")
        self.assertEqual(graph_state["replan_count"], 1)
        self.assertEqual(graph_state["transition_count"], 3)

    async def test_replanner_backstop_max_replans_fail_closed(self):
        failed = await self._failed_used_phone_state()
        create_mock = AsyncMock()
        runtime = GraphV2Runtime(
            user_message=failed.goal,
            client=_fake_client(create_mock),
            model="test-model",
            resolve_tool_schemas=lambda s: [_search_products_schema()],
            tool_caller=AsyncMock(),
            trace_builder=TraceBuilder("run-graph-replan-backstop"),
            projector=None,
            max_transitions=10,
            max_replans=0,
        )

        graph_state = await run_graph_v2(failed, runtime)

        self.assertEqual(graph_state["action"], "stop_turn")
        self.assertEqual(graph_state["terminal_outcome"], "stop_turn")
        self.assertEqual(graph_state["degraded_reason"], "max_replans_exceeded")
        self.assertEqual(graph_state["replan_count"], 1)
        # The backstop fires BEFORE any model call / tool dispatch.
        create_mock.assert_not_awaited()
        node_sequence = [
            event["nodeName"]
            for event in graph_state["node_events"]
            if event["phase"] == "start"
        ]
        self.assertEqual(node_sequence, ["entry", "replanner"])

    async def test_node_events_redacted_and_bounded(self):
        ready = await self._ready_used_phone_state()
        create_mock = AsyncMock(return_value=_planner_search_reply(goal=ready.goal))
        tool_caller = AsyncMock(side_effect=_fake_tool)
        pack = await build_context_pack(
            ready, allowed_tools=["search_products", "get_product_details"]
        )
        projector = ContextProjector(pack)
        runtime = self._runtime(
            state=ready,
            client=_fake_client(create_mock),
            tool_caller=tool_caller,
            max_transitions=8,
            trace_builder=TraceBuilder("run-graph-redact", mode="context_pack"),
            projector=projector,
        )

        graph_state = await run_graph_v2(ready, runtime)
        events = graph_state["node_events"]

        # 8 events = one start/end pair per node (entry/planner/executor/validator).
        self.assertEqual(len(events), 8)
        allowed_keys = {
            "nodeName", "phase", "codeSource", "enteredBecause", "routeDecision",
            "revisionBefore", "revisionAfter", "durationMs", "toolName",
            "modelName", "modelCallId", "decisionBindingHash",
            "decisionViewHash", "decisionTaskRevision", "decisionActionId",
            "decisionActionKind", "decisionErrorCode",
            "redactedStateHash", "errorCode",
            # Day-2 server-bound identity (requirement #9): the durable runner
            # stamps thread/task identity and the latest checkpoint hash onto
            # every redacted node event.  They stay None on the non-durable
            # path, but the keys must not be treated as unexpected.
            "threadId", "taskId", "checkpointHash",
        }
        joined = json.dumps(events, ensure_ascii=False)
        for event in events:
            self.assertTrue(set(event).issubset(allowed_keys), event)
            # No user message / goal / tool arguments / raw payloads leak.
            self.assertNotIn(ready.goal, json.dumps(event, ensure_ascii=False))
            # Every event stays far under the redaction budget.
            self.assertLess(len(json.dumps(event, ensure_ascii=False)), 2048)
            digest = event.get("redactedStateHash")
            self.assertIsNotNone(digest)
            self.assertRegex(digest, r"^[0-9a-f]{16}$")
        # The whole run's node-event source stays under 8KB (6KB is ~real budget).
        self.assertLess(len(joined), 8192)
        # enteredBecause chains the previous node's routeDecision.
        starts = [e for e in events if e["phase"] == "start"]
        self.assertEqual(starts[1]["enteredBecause"], "planner")
        self.assertEqual(starts[2]["enteredBecause"], "continue_to_executor")
        self.assertEqual(starts[3]["enteredBecause"], "ready_for_validation")

    async def test_cross_task_view_rejected_fail_closed(self):
        state_a = await self._ready_used_phone_state()
        state_b = await self._ready_used_phone_state()  # different task id
        pack = await build_context_pack(
            state_a, allowed_tools=["search_products", "get_product_details"]
        )
        projector = ContextProjector(pack)
        view = projector.planner_view(
            tool_names=["search_products", "get_product_details"],
            task_status=state_a.status,
            user_message=state_a.goal,
            candidate_tool_schemas=[
                _search_products_schema(), _get_product_details_schema(),
            ],
            phase_task_revision=state_a.revision,
        )
        trace = TraceBuilder("run-cross-task")
        with self.assertRaises(HarnessPreValidationError) as cm:
            _validate_view_and_record(
                view, state_b, trace, "planner", projector=projector
            )
        self.assertEqual(cm.exception.code, "view_task_id_mismatch")
        # The gate recorded the boundary mismatch before raising — phase,
        # tool call and Validator counts are all guaranteed zero.
        self.assertTrue(trace._trace.context_boundary_mismatches)
        self.assertTrue(trace._trace.degraded)
