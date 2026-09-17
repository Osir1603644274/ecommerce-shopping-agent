import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import task_state
from app.executor import run_executor_step
from app.harness import (
    decide_after_execution,
    decide_after_planning,
    run_harness_step,
    run_planner_if_needed,
    run_planning_step,
    should_run_planner,
)
from app.planner import PLANNER_SUBMISSION_TOOL_NAME
from app.planning import PlannerResult, TaskPlan, transition_plan_step_status
from app.replanner import REPLANNER_SUBMISSION_TOOL_NAME
from app.schemas import ToolTrace
from app.context_pack import build_context_pack
from app.context_view import ContextProjector
from app.tools import TOOL_SCHEMAS
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    update_task_state,
)
from tests.fake_redis import FakeRedis
from tests.two_stage_ranking_fixtures import two_stage_search_detail


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


def _search_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_shops",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def _detail_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "get_shop_detail",
            "parameters": {
                "type": "object",
                "properties": {"shopId": {"type": "integer"}},
                "required": ["shopId"],
            },
        },
    }


def _knowledge_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "从统一知识库检索评论证据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    }


def _planned_reply():
    payload = {
        "outcome": "planned",
        "steps": [
            {
                "stepId": "step-1",
                "description": "获取目标商户的评论证据",
                "toolName": "search_shop_reviews",
                "arguments": {
                    "shopName": "星河咖啡",
                    "query": "判断星河咖啡是否适合安静聊天",
                },
                "argumentSources": {
                    "shopName": {
                        "kind": "task_state",
                        "reference": "facts.shopName",
                    },
                    "query": {"kind": "task_goal"},
                },
                "expectedOutput": {"requiresReviewEvidence": True},
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


def _replanned_reply():
    payload = {
        "outcome": "replanned",
        "steps": [
            {
                "stepId": "recovery-step-1",
                "description": "改用统一知识库检索评论证据",
                "toolName": "search_knowledge",
                "arguments": {
                    "query": "判断星河咖啡是否适合安静聊天",
                },
                "argumentSources": {
                    "query": {"kind": "task_goal"},
                },
                "expectedOutput": {"requiresReviewEvidence": True},
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


def _fake_client(create_mock: AsyncMock):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_mock),
        )
    )


class PlannerHarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _ready_state(self) -> TaskState:
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

    async def test_runs_planner_for_ready_task_without_plan(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planned_reply())

        result, updated = await run_planner_if_needed(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertTrue(should_run_planner(ready))
        self.assertEqual(result.outcome, "planned")
        self.assertIsNotNone(updated.active_plan)
        self.assertEqual(create_mock.await_count, 1)

    async def test_skips_planner_when_active_plan_already_exists(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planned_reply())
        _, planned_state = await run_planner_if_needed(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        result, unchanged = await run_planner_if_needed(
            planned_state,
            "继续",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertFalse(should_run_planner(planned_state))
        self.assertIsNone(result)
        self.assertIs(unchanged, planned_state)
        self.assertEqual(create_mock.await_count, 1)

    async def test_skips_planner_while_waiting_for_user(self):
        ready = await self._ready_state()
        waiting = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                status="collecting_information",
                pendingQuestions=["你指的是哪一家星河咖啡？"],
            ),
        )
        create_mock = AsyncMock()

        result, unchanged = await run_planner_if_needed(
            waiting,
            "还没想好",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertFalse(should_run_planner(waiting))
        self.assertIsNone(result)
        self.assertIs(unchanged, waiting)
        create_mock.assert_not_awaited()

    async def test_skips_planner_when_task_is_paused(self):
        ready = await self._ready_state()
        paused = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="system",
                status="paused",
            ),
        )
        create_mock = AsyncMock()

        result, unchanged = await run_planner_if_needed(
            paused,
            "继续",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertFalse(should_run_planner(paused))
        self.assertIsNone(result)
        self.assertIs(unchanged, paused)
        create_mock.assert_not_awaited()

    async def test_complete_step_returns_executor_action_after_new_plan(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planned_reply())

        step = await run_planning_step(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(step.action, "continue_to_executor")
        self.assertEqual(step.planner_result.outcome, "planned")
        self.assertIsNotNone(step.task_state.active_plan)
        self.assertEqual(create_mock.await_count, 1)

    async def test_complete_step_reuses_existing_plan_without_model_call(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planned_reply())
        first_step = await run_planning_step(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        second_step = await run_planning_step(
            first_step.task_state,
            "继续",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(second_step.action, "continue_to_executor")
        self.assertIsNone(second_step.planner_result)
        self.assertEqual(create_mock.await_count, 1)

    async def test_complete_step_asks_existing_pending_question(self):
        ready = await self._ready_state()
        waiting = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                status="collecting_information",
                pendingQuestions=["你指的是哪一家星河咖啡？"],
            ),
        )
        create_mock = AsyncMock()

        step = await run_planning_step(
            waiting,
            "还没想好",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(step.action, "ask_user")
        self.assertIsNone(step.planner_result)
        self.assertEqual(
            step.task_state.pending_questions,
            ["你指的是哪一家星河咖啡？"],
        )
        create_mock.assert_not_awaited()

    async def test_complete_step_stops_when_task_is_paused(self):
        ready = await self._ready_state()
        paused = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="system",
                status="paused",
            ),
        )
        create_mock = AsyncMock()

        step = await run_planning_step(
            paused,
            "继续",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(step.action, "stop_turn")
        self.assertIsNone(step.planner_result)
        create_mock.assert_not_awaited()

    async def test_complete_step_stops_for_blocked_active_plan(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planned_reply())
        planned_step = await run_planning_step(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )
        blocked_plan = transition_plan_step_status(
            planned_step.task_state.active_plan,
            "step-1",
            "blocked",
        )
        blocked_state = await update_task_state(
            planned_step.task_state.task_id,
            TaskStatePatchRequest(
                expectedRevision=planned_step.task_state.revision,
                actor="agent",
                activePlan=blocked_plan,
            ),
        )

        stopped_step = await run_planning_step(
            blocked_state,
            "继续",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(stopped_step.action, "stop_turn")
        self.assertIsNone(stopped_step.planner_result)
        self.assertEqual(create_mock.await_count, 1)


class PlannerResultDecisionTests(unittest.TestCase):
    def test_planned_result_continues_to_executor(self):
        result = PlannerResult(
            outcome="planned",
            plan={
                "planId": "plan-ready-for-executor",
                "basedOnRevision": 3,
                "steps": [
                    {
                        "stepId": "step-1",
                        "description": "查询商家评论",
                        "toolName": "search_shop_reviews",
                        "arguments": {"query": "星河咖啡"},
                        "argumentSources": {
                            "query": {"kind": "task_goal"},
                        },
                        "expectedOutput": {"requiresReviewEvidence": True},
                    }
                ],
            },
        )

        action = decide_after_planning(result)

        self.assertEqual(action, "continue_to_executor")

    def test_needs_user_input_result_asks_user(self):
        result = PlannerResult(
            outcome="needs_user_input",
            question="你指的是哪一家星河咖啡？",
        )

        action = decide_after_planning(result)

        self.assertEqual(action, "ask_user")

    def test_planning_failure_stops_current_turn(self):
        result = PlannerResult(
            outcome="planning_failed",
            errorCode="invalid_plan",
            reason="模型两次生成的计划都未通过校验",
        )

        action = decide_after_planning(result)

        self.assertEqual(action, "stop_turn")


class HarnessExecutorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _ready_state(self) -> TaskState:
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

    async def test_plans_and_executes_one_terminal_step(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planned_reply())
        tool_caller = AsyncMock(
            return_value=ToolTrace(
                tool="search_shop_reviews",
                ok=True,
                detail={"chunks": [{"reviewId": "review-1"}]},
            )
        )

        result = await run_harness_step(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            tool_caller=tool_caller,
        )

        self.assertEqual(result.action, "task_completed")
        self.assertEqual(result.planner_result.outcome, "planned")
        self.assertEqual(result.executor_result.outcome, "step_executed")
        self.assertEqual(result.validator_result.outcome, "passed")
        self.assertEqual(result.task_state.status, "completed")
        self.assertEqual(result.task_state.active_plan.status, "completed")
        self.assertEqual(result.task_state.active_plan.steps[0].status, "executed")
        self.assertEqual(create_mock.await_count, 1)
        tool_caller.assert_awaited_once_with(
            "search_shop_reviews",
            {
                "shopName": "星河咖啡",
                "query": "判断星河咖啡是否适合安静聊天",
            },
        )

    async def test_returns_continue_to_executor_when_plan_has_another_step(self):
        created = await create_task_state(
            TaskStateCreateRequest(goal="查找星河咖啡并读取商户详情")
        )
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        plan = TaskPlan(
            planId="plan-shop-detail",
            basedOnRevision=ready.revision,
            status="active",
            steps=[
                {
                    "stepId": "step-1",
                    "description": "查找目标商户",
                    "toolName": "search_shops",
                    "arguments": {"query": ready.goal},
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                    },
                    "expectedOutput": {"requiresShopId": True},
                },
                {
                    "stepId": "step-2",
                    "description": "读取目标商户详情",
                    "toolName": "get_shop_detail",
                    "arguments": {"shopId": None},
                    "argumentSources": {
                        "shopId": {
                            "kind": "prior_step",
                            "reference": "step-1.shopId",
                        }
                    },
                    "expectedOutput": {"requiresShopDetail": True},
                },
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
        create_mock = AsyncMock()

        result = await run_harness_step(
            planned,
            "继续",
            [_search_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=True,
                    detail={"shops": [{"id": 321, "name": "星河咖啡"}]},
                )
            ),
        )

        self.assertEqual(result.action, "continue_to_executor")
        self.assertIsNone(result.planner_result)
        self.assertEqual(result.executor_result.outcome, "step_executed")
        self.assertEqual(result.task_state.active_plan.steps[0].status, "executed")
        self.assertEqual(result.task_state.active_plan.steps[1].status, "pending")
        create_mock.assert_not_awaited()

        completed = await run_harness_step(
            result.task_state,
            "继续",
            [_detail_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="get_shop_detail",
                    ok=True,
                    detail={"id": 321, "name": "星河咖啡"},
                )
            ),
        )

        self.assertEqual(completed.action, "task_completed")
        self.assertEqual(completed.validator_result.outcome, "passed")
        self.assertEqual(completed.task_state.status, "completed")
        self.assertEqual(completed.task_state.active_plan.status, "completed")

    async def test_stops_turn_after_tool_failure(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planned_reply())

        result = await run_harness_step(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shop_reviews",
                    ok=False,
                    detail="review backend unavailable",
                )
            ),
        )

        self.assertEqual(result.action, "stop_turn")
        self.assertEqual(result.executor_result.outcome, "step_failed")
        self.assertIsNone(result.validator_result)
        self.assertEqual(result.task_state.active_plan.steps[0].status, "failed")

    async def test_replans_insufficient_evidence_and_executes_new_plan_next_turn(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(
            side_effect=[_planned_reply(), _replanned_reply()]
        )

        result = await run_harness_step(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema(), _knowledge_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shop_reviews",
                    ok=True,
                    detail={"count": 0, "reviews": [], "citations": []},
                )
            ),
        )

        self.assertEqual(result.action, "continue_to_executor")
        self.assertEqual(
            result.validator_result.outcome,
            "insufficient_evidence",
        )
        self.assertEqual(result.replanner_result.outcome, "replanned")
        self.assertEqual(result.task_state.status, "ready")
        self.assertEqual(result.task_state.active_plan.status, "active")
        self.assertEqual(
            result.task_state.active_plan.steps[0].tool_name,
            "search_knowledge",
        )
        self.assertEqual(create_mock.await_count, 2)

        completed = await run_harness_step(
            result.task_state,
            "继续",
            [_review_tool_schema(), _knowledge_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_knowledge",
                    ok=True,
                    detail={
                        "count": 1,
                        "chunks": [{"reviewId": "review-2"}],
                    },
                )
            ),
        )

        self.assertEqual(completed.action, "task_completed")
        self.assertEqual(completed.validator_result.outcome, "passed")
        self.assertEqual(completed.task_state.status, "completed")
        self.assertEqual(completed.task_state.active_plan.status, "completed")
        self.assertEqual(create_mock.await_count, 2)

    async def test_recovers_validation_after_executor_already_persisted(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planned_reply())
        planning = await run_planning_step(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )
        execution = await run_executor_step(
            planning.task_state,
            [_review_tool_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shop_reviews",
                    ok=True,
                    detail={"reviews": [{"reviewId": "review-1"}]},
                )
            ),
        )
        self.assertEqual(execution.task_state.active_plan.status, "active")
        self.assertEqual(execution.task_state.active_plan.steps[0].status, "executed")
        unused_tool_caller = AsyncMock()

        result = await run_harness_step(
            execution.task_state,
            "继续",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            tool_caller=unused_tool_caller,
        )

        self.assertEqual(result.action, "task_completed")
        self.assertIsNone(result.executor_result)
        self.assertEqual(result.validator_result.outcome, "passed")
        self.assertEqual(result.task_state.status, "completed")
        self.assertEqual(create_mock.await_count, 1)
        unused_tool_caller.assert_not_awaited()


# ── E2E-STAGE5-PLANNER-SOURCE-CONTRACT-001 ──────────────────────────────────

_OS_HARD_REQ = {
    "key": "os", "operator": "eq", "value": "ios",
    "unit": "enum", "priority": "hard", "source": "user",
}


def _search_products_schema() -> dict:
    """The real production search_products schema (query+category required)."""
    for spec in TOOL_SCHEMAS:
        if spec["function"]["name"] == "search_products":
            return spec
    raise AssertionError("TOOL_SCHEMAS must ship search_products")


def _shopping_planned_reply(goal="想找 iOS 二手机。"):
    """Planner reply: search_products with exact goal + server sources, then details."""
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
                    "requirements": {"kind": "shopping_guide", "reference": "requirements"},
                },
                "expectedOutput": {"requiresProductCandidates": True},
            },
            {
                "stepId": "step-2",
                "description": "读取候选权威详情",
                "toolName": "get_product_details",
                "arguments": {"productIds": [123, 456]},
                "argumentSources": {
                    "productIds": {"kind": "prior_step", "reference": "step-1.productIds"},
                },
                "expectedOutput": {"requiresProductDetails": True},
            },
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


def _get_product_details_schema() -> dict:
    for spec in TOOL_SCHEMAS:
        if spec["function"]["name"] == "get_product_details":
            return spec
    raise AssertionError("TOOL_SCHEMAS must ship get_product_details")


class ShoppingGuideHarnessDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Fake/no-model harness path: real-shaped TaskState → one search_products dispatch.

    Proves the server-derived source contract survives the full
    Planner → Executor flow with zero model, network, or DB calls
    (fake client, AsyncMock tool_caller, FakeRedis).
    """

    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _ready_used_phone_state(self) -> TaskState:
        goal = "想找 iOS 二手机。"
        created = await create_task_state(
            TaskStateCreateRequest(
                goal=goal,
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
        )
        return await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )

    async def test_dispatches_search_products_with_exact_goal_and_server_sources(self):
        ready = await self._ready_used_phone_state()
        create_mock = AsyncMock(return_value=_shopping_planned_reply(goal=ready.goal))

        def _fake_tool(name, arguments):
            if name == "search_products":
                return ToolTrace(
                    tool=name, ok=True, detail=two_stage_search_detail([123, 456])
                )
            if name == "get_product_details":
                return ToolTrace(
                    tool=name, ok=True,
                    detail={
                        "productIds": [123, 456],
                        "products": [{"id": 123}, {"id": 456}],
                    },
                )
            raise AssertionError(f"unexpected tool dispatch: {name}")

        tool_caller = AsyncMock(side_effect=_fake_tool)
        pack = await build_context_pack(
            ready, allowed_tools=["search_products", "get_product_details"]
        )
        projector = ContextProjector(pack)

        result = await run_harness_step(
            ready,
            ready.goal,
            [_search_products_schema(), _get_product_details_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            tool_caller=tool_caller,
            projector=projector,
        )
        # The server-owned ecommerce plan is the smallest sufficient action:
        # one search_products dispatch, whose normalized candidates are
        # persisted and validated as a terminal plan receipt.
        self.assertEqual(result.action, "task_completed")
        search_calls = [
            c for c in tool_caller.await_args_list if c.args[0] == "search_products"
        ]
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(
            search_calls[0].args[1],
            {"query": ready.goal, "category": "手机", "requirements": [_OS_HARD_REQ]},
        )

        self.assertEqual(result.validator_result.outcome, "passed")
        # A completed ecommerce Plan is one finished action receipt.  The
        # durable shopping task stays ready for a subsequent user turn.
        self.assertEqual(result.task_state.status, "ready")
        self.assertEqual(result.task_state.active_plan.status, "completed")
        self.assertEqual(
            len([c for c in tool_caller.await_args_list if c.args[0] == "search_products"]),
            1,
        )
        # Published shopping-guide sources make planning mechanical, so no
        # model call is required (and this test uses FakeRedis/no real DB).
        create_mock.assert_not_awaited()
