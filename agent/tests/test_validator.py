import unittest
from unittest.mock import AsyncMock

from app import task_state
from app.executor import run_executor_step
from app.planning import TaskPlan
from app.schemas import ToolTrace
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    TaskStateRevisionConflictError,
    create_task_state,
    update_task_state,
)
from app.validator import (
    ValidatorResult,
    ValidatorSelectionError,
    build_validator_context,
    persist_validator_result,
    run_validator_phase,
    validate_task_result,
)
from tests.fake_redis import FakeRedis


def _review_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_shop_reviews",
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


def _search_schema() -> dict:
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


def _detail_schema() -> dict:
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


class ValidatorPhaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _ready_state(
        self,
        goal: str = "判断星河咖啡是否适合安静聊天",
    ) -> TaskState:
        created = await create_task_state(
            TaskStateCreateRequest(
                goal=goal,
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

    async def _review_executed_state(
        self,
        *,
        detail: dict,
        expected_output: dict | None = None,
    ) -> TaskState:
        ready = await self._ready_state()
        plan = TaskPlan(
            planId="plan-review-evidence",
            basedOnRevision=ready.revision,
            status="active",
            steps=[
                {
                    "stepId": "step-1",
                    "description": "检索目标商户评论证据",
                    "toolName": "search_shop_reviews",
                    "arguments": {
                        "query": ready.goal,
                        "shopName": "星河咖啡",
                    },
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "shopName": {
                            "kind": "task_state",
                            "reference": "facts.shopName",
                        },
                    },
                    "expectedOutput": expected_output
                    or {"requiresReviewEvidence": True},
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
        execution = await run_executor_step(
            planned,
            [_review_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shop_reviews",
                    ok=True,
                    detail=detail,
                )
            ),
        )
        self.assertEqual(execution.outcome, "step_executed")
        return execution.task_state

    async def _shop_detail_executed_state(
        self,
        *,
        returned_shop_id: int = 321,
    ) -> TaskState:
        ready = await self._ready_state(
            "查找星河咖啡并读取商户详情"
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
        first = await run_executor_step(
            planned,
            [_search_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=True,
                    detail={"shops": [{"id": 321, "name": "星河咖啡"}]},
                )
            ),
        )
        second = await run_executor_step(
            first.task_state,
            [_detail_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="get_shop_detail",
                    ok=True,
                    detail={
                        "id": returned_shop_id,
                        "name": "星河咖啡",
                        "phone": "123456",
                    },
                )
            ),
        )
        self.assertEqual(second.outcome, "step_executed")
        return second.task_state

    async def test_builds_context_from_latest_execution_evidence(self):
        executed = await self._review_executed_state(
            detail={"chunks": [{"reviewId": "review-1"}]}
        )

        context = build_validator_context(executed)

        self.assertEqual(context.task_revision, executed.revision)
        self.assertEqual(context.plan.plan_id, "plan-review-evidence")
        self.assertEqual(
            context.steps[0].execution_result.outcome,
            "tool_succeeded",
        )

    async def test_passes_review_evidence_and_completes_plan_and_task(self):
        executed = await self._review_executed_state(
            detail={
                "count": 1,
                "reviews": [{"reviewId": "review-1"}],
            }
        )

        result, completed = await run_validator_phase(executed)

        self.assertEqual(result.outcome, "passed")
        self.assertEqual(result.step_results[0].outcome, "satisfied")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.active_plan.status, "completed")
        persisted = ValidatorResult.model_validate(
            completed.domain_state["validationResult"]
        )
        self.assertEqual(persisted.based_on_revision, executed.revision)

    async def test_marks_empty_review_result_as_insufficient_evidence(self):
        executed = await self._review_executed_state(
            detail={"count": 0, "reviews": [], "citations": []}
        )

        result, updated = await run_validator_phase(executed)

        self.assertEqual(result.outcome, "insufficient_evidence")
        self.assertEqual(result.error_code, "review_evidence_missing")
        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.active_plan.status, "failed")

    async def test_rejects_unknown_expected_output_contract(self):
        executed = await self._review_executed_state(
            detail={"reviews": [{"reviewId": "review-1"}]},
            expected_output={"requiresMagicAnswer": True},
        )

        result, updated = await run_validator_phase(executed)

        self.assertEqual(result.outcome, "validation_failed")
        self.assertEqual(result.error_code, "unsupported_expected_output")
        self.assertEqual(updated.active_plan.status, "failed")

    async def test_missing_execution_record_is_validation_failure(self):
        executed = await self._review_executed_state(
            detail={"reviews": [{"reviewId": "review-1"}]}
        )
        damaged = await update_task_state(
            executed.task_id,
            TaskStatePatchRequest(
                expectedRevision=executed.revision,
                actor="system",
                domainStatePatch={"stepExecutionResults": None},
            ),
        )

        result, updated = await run_validator_phase(damaged)

        self.assertEqual(result.outcome, "validation_failed")
        self.assertEqual(result.error_code, "execution_result_missing")
        self.assertEqual(updated.active_plan.status, "failed")

    async def test_malformed_execution_history_is_persisted_as_failure(self):
        executed = await self._review_executed_state(
            detail={"reviews": [{"reviewId": "review-1"}]}
        )
        damaged = await update_task_state(
            executed.task_id,
            TaskStatePatchRequest(
                expectedRevision=executed.revision,
                actor="system",
                domainStatePatch={"stepExecutionResults": "broken"},
            ),
        )

        result, updated = await run_validator_phase(damaged)

        self.assertEqual(result.outcome, "validation_failed")
        self.assertEqual(result.error_code, "invalid_execution_history")
        self.assertEqual(updated.active_plan.status, "failed")

    async def test_passes_two_step_shop_detail_plan(self):
        executed = await self._shop_detail_executed_state()

        result, completed = await run_validator_phase(executed)

        self.assertEqual(result.outcome, "passed")
        self.assertEqual(
            [item.outcome for item in result.step_results],
            ["satisfied", "satisfied"],
        )
        self.assertEqual(completed.status, "completed")

    async def test_detects_shop_detail_identity_mismatch(self):
        executed = await self._shop_detail_executed_state(
            returned_shop_id=999
        )

        result, updated = await run_validator_phase(executed)

        self.assertEqual(result.outcome, "validation_failed")
        self.assertEqual(result.error_code, "shop_detail_identity_mismatch")
        self.assertEqual(updated.active_plan.status, "failed")

    async def test_occ_rejects_stale_validation_result(self):
        executed = await self._review_executed_state(
            detail={"reviews": [{"reviewId": "review-1"}]}
        )
        context = build_validator_context(executed)
        result = validate_task_result(context)
        changed = await update_task_state(
            executed.task_id,
            TaskStatePatchRequest(
                expectedRevision=executed.revision,
                actor="user",
                domainStatePatch={"userChangedTask": True},
            ),
        )

        with self.assertRaises(TaskStateRevisionConflictError):
            await persist_validator_result(executed, result)

        latest = await task_state.get_task_state(executed.task_id)
        self.assertEqual(latest.revision, changed.revision)
        self.assertEqual(latest.active_plan.status, "active")

    async def test_rejects_validation_before_all_steps_execute(self):
        ready = await self._ready_state()
        plan = TaskPlan(
            planId="plan-still-pending",
            basedOnRevision=ready.revision,
            steps=[
                {
                    "stepId": "step-1",
                    "description": "检索评论",
                    "toolName": "search_shop_reviews",
                    "arguments": {
                        "query": ready.goal,
                        "shopName": "星河咖啡",
                    },
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "shopName": {
                            "kind": "task_state",
                            "reference": "facts.shopName",
                        },
                    },
                    "expectedOutput": {"requiresReviewEvidence": True},
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

        with self.assertRaises(ValidatorSelectionError) as raised:
            await run_validator_phase(planned)

        self.assertEqual(raised.exception.code, "plan_not_fully_executed")
        latest = await task_state.get_task_state(planned.task_id)
        self.assertEqual(latest.revision, planned.revision)
