import asyncio
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app import task_state
from app.executor import (
    ExecutorArgumentResolutionError,
    ExecutorBlockRecord,
    ExecutorClaimError,
    ExecutorOutputExtractionError,
    ExecutorSelectionError,
    ExecutorStepContext,
    ExecutorResultPersistenceError,
    ExecutorToolDispatchError,
    ExecutorToolValidationError,
    ResolvedExecutorArguments,
    NormalizedStepOutput,
    StepExecutionResult,
    ValidatedExecutorToolCall,
    build_executor_step_context,
    block_executor_step,
    claim_executor_step,
    execute_validated_tool_call,
    execute_and_record_tool_call,
    extract_normalized_step_output,
    persist_step_execution_result,
    recover_expired_executor_claim,
    run_executor_step,
    resolve_executor_arguments,
    select_next_plan_step,
    validate_executor_tool_call,
)
from app.planning import TaskPlan, transition_plan_step_status
from app.schemas import ToolTrace
from app.context_view import ExecutorContextView
from app.domains.ecommerce.ranking_contract import (
    RANKING_FORMULA,
    RANKING_TIE_BREAK,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
)
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskFact,
    TaskStatePatchRequest,
    TaskStateRevisionConflictError,
    create_task_state,
    update_task_state,
)
from tests.fake_redis import FakeRedis


def _shop_search_step(*, status: str = "pending") -> dict:
    return {
        "stepId": "step-1",
        "description": "根据店铺名称查找商户",
        "toolName": "search_shops",
        "arguments": {"query": "查找星河咖啡并读取商户详情"},
        "argumentSources": {
            "query": {"kind": "task_goal"},
        },
        "expectedOutput": {"requiresShopId": True},
        "status": status,
    }


def _shop_detail_step(*, status: str = "pending") -> dict:
    return {
        "stepId": "step-2",
        "description": "读取目标商户详情",
        "toolName": "get_shop_detail",
        "arguments": {"shopId": None},
        "argumentSources": {
            "shopId": {
                "kind": "prior_step",
                "reference": "step-1.shopId",
            },
        },
        "expectedOutput": {"requiresShopDetail": True},
        "status": status,
    }


def _plan(
    *,
    based_on_revision: int = 6,
    plan_status: str = "active",
    first_status: str = "pending",
    second_status: str = "pending",
) -> TaskPlan:
    return TaskPlan(
        planId="plan-shop-lookup",
        basedOnRevision=based_on_revision,
        status=plan_status,
        steps=[
            _shop_search_step(status=first_status),
            _shop_detail_step(status=second_status),
        ],
    )


def _state(
    *,
    status: str = "ready",
    plan: TaskPlan | None = None,
) -> TaskState:
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-shop-lookup",
        taskType="local_life",
        status=status,
        revision=7,
        goal="查找星河咖啡并读取商户详情",
        activePlan=plan,
        createdAt=now,
        updatedAt=now,
    )


class ExecutorStepSelectionTests(unittest.TestCase):
    def test_builds_revision_bound_context_for_first_pending_step(self):
        context = build_executor_step_context(_state(plan=_plan()))

        self.assertEqual(context.task_id, "task-shop-lookup")
        self.assertEqual(context.expected_revision, 7)
        self.assertEqual(context.plan_id, "plan-shop-lookup")
        self.assertEqual(context.step.step_id, "step-1")
        self.assertEqual(context.step.status, "pending")

    def test_selects_second_step_after_first_step_executed(self):
        context = build_executor_step_context(
            _state(
                status="executing",
                plan=_plan(first_status="executed"),
            )
        )

        self.assertEqual(context.step.step_id, "step-2")

    def test_rejects_task_without_active_plan(self):
        with self.assertRaises(ExecutorSelectionError) as raised:
            build_executor_step_context(_state())

        self.assertEqual(raised.exception.code, "active_plan_missing")

    def test_rejects_paused_task(self):
        with self.assertRaises(ExecutorSelectionError) as raised:
            build_executor_step_context(_state(status="paused", plan=_plan()))

        self.assertEqual(raised.exception.code, "task_not_executable")

    def test_rejects_stale_plan(self):
        with self.assertRaises(ExecutorSelectionError) as raised:
            build_executor_step_context(
                _state(plan=_plan(plan_status="stale"))
            )

        self.assertEqual(raised.exception.code, "plan_not_active")

    def test_does_not_select_an_already_executing_step_twice(self):
        with self.assertRaises(ExecutorSelectionError) as raised:
            build_executor_step_context(
                _state(status="executing", plan=_plan(first_status="executing"))
            )

        self.assertEqual(raised.exception.code, "step_already_executing")

    def test_does_not_skip_failed_or_blocked_step(self):
        for status in ("failed", "blocked"):
            with self.subTest(status=status):
                with self.assertRaises(ExecutorSelectionError) as raised:
                    select_next_plan_step(_plan(first_status=status))

                self.assertEqual(raised.exception.code, "step_not_runnable")

    def test_rejects_invalid_linear_step_order(self):
        with self.assertRaises(ExecutorSelectionError) as raised:
            select_next_plan_step(
                _plan(first_status="pending", second_status="executing")
            )

        self.assertEqual(raised.exception.code, "invalid_step_order")

    def test_reports_when_active_plan_has_no_pending_step(self):
        with self.assertRaises(ExecutorSelectionError) as raised:
            select_next_plan_step(
                _plan(first_status="executed", second_status="executed")
            )

        self.assertEqual(raised.exception.code, "plan_has_no_pending_step")


class ExecutorStepClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _state_with_plan(self) -> TaskState:
        created = await create_task_state(
            TaskStateCreateRequest(
                goal="查找星河咖啡并读取商户详情",
            )
        )
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        return await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                activePlan=_plan(based_on_revision=ready.revision),
            ),
        )

    async def test_claims_pending_step_and_task_in_one_revision(self):
        planned_state = await self._state_with_plan()
        context = build_executor_step_context(planned_state)

        claimed_state = await claim_executor_step(planned_state, context)

        self.assertEqual(claimed_state.revision, planned_state.revision + 1)
        self.assertEqual(claimed_state.status, "executing")
        self.assertEqual(
            claimed_state.active_plan.steps[0].status,
            "executing",
        )
        self.assertEqual(
            claimed_state.active_plan.steps[1].status,
            "pending",
        )

    async def test_occ_rejects_claim_after_task_changes(self):
        planned_state = await self._state_with_plan()
        context = build_executor_step_context(planned_state)
        changed_state = await update_task_state(
            planned_state.task_id,
            TaskStatePatchRequest(
                expectedRevision=planned_state.revision,
                actor="user",
                domainStatePatch={"userChangedTask": True},
            ),
        )

        with self.assertRaises(TaskStateRevisionConflictError):
            await claim_executor_step(planned_state, context)

        latest = await task_state.get_task_state(planned_state.task_id)
        self.assertEqual(latest.revision, changed_state.revision)
        self.assertEqual(latest.active_plan.steps[0].status, "pending")

    async def test_blocks_claimed_step_after_argument_resolution_failure(self):
        planned_state = await self._state_with_plan()
        context = build_executor_step_context(planned_state)
        claimed_state = await claim_executor_step(planned_state, context)
        error = ExecutorArgumentResolutionError(
            "argument_source_missing",
            "TaskState中不存在fact：shopName",
        )

        blocked_state = await block_executor_step(claimed_state, context, error)

        self.assertEqual(blocked_state.revision, claimed_state.revision + 1)
        self.assertEqual(blocked_state.status, "ready")
        self.assertEqual(blocked_state.active_plan.steps[0].status, "blocked")
        record = ExecutorBlockRecord.model_validate(
            blocked_state.domain_state["executorBlock"]
        )
        self.assertEqual(record.plan_id, blocked_state.active_plan.plan_id)
        self.assertEqual(record.step_id, "step-1")
        self.assertEqual(record.error_code, "argument_source_missing")

    async def test_occ_rejects_block_write_after_claimed_state_changes(self):
        planned_state = await self._state_with_plan()
        context = build_executor_step_context(planned_state)
        claimed_state = await claim_executor_step(planned_state, context)
        changed_state = await update_task_state(
            claimed_state.task_id,
            TaskStatePatchRequest(
                expectedRevision=claimed_state.revision,
                actor="user",
                domainStatePatch={"userChangedTask": True},
            ),
        )
        error = ExecutorArgumentResolutionError(
            "argument_source_changed",
            "参数来源已变化，不能沿用旧Plan：query",
        )

        with self.assertRaises(TaskStateRevisionConflictError):
            await block_executor_step(claimed_state, context, error)

        latest = await task_state.get_task_state(claimed_state.task_id)
        self.assertEqual(latest.revision, changed_state.revision)
        self.assertEqual(latest.active_plan.steps[0].status, "executing")
        self.assertNotIn("executorBlock", latest.domain_state)

    async def test_same_context_cannot_claim_step_twice(self):
        planned_state = await self._state_with_plan()
        context = build_executor_step_context(planned_state)
        await claim_executor_step(planned_state, context)

        with self.assertRaises(TaskStateRevisionConflictError):
            await claim_executor_step(planned_state, context)

    async def test_expired_claim_is_marked_unknown_and_never_requeued(self):
        planned_state = await self._state_with_plan()
        context = build_executor_step_context(planned_state)
        claimed = await claim_executor_step(planned_state, context)
        expired_lease = dict(claimed.domain_state["executorLease"])
        expired_lease["expiresAt"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        expired = await update_task_state(
            claimed.task_id,
            TaskStatePatchRequest(
                expectedRevision=claimed.revision,
                actor="system",
                domainStatePatch={"executorLease": expired_lease},
            ),
        )

        recovered = await recover_expired_executor_claim(expired)

        self.assertEqual(recovered.status, "ready")
        self.assertEqual(recovered.active_plan.status, "stale")
        self.assertEqual(recovered.active_plan.steps[0].status, "failed")
        self.assertNotIn("executorLease", recovered.domain_state)
        self.assertEqual(
            recovered.domain_state["executorRecovery"]["status"],
            "UNKNOWN",
        )
        self.assertEqual(
            recovered.domain_state["executorRecovery"]["reason"],
            "lease_expired",
        )
        with self.assertRaises(ExecutorSelectionError) as raised:
            select_next_plan_step(recovered.active_plan)
        self.assertEqual(raised.exception.code, "plan_not_active")

    async def test_durable_task_claim_before_inbox_is_safe_to_requeue(self):
        planned_state = await self._state_with_plan()
        context = build_executor_step_context(planned_state)
        claimed = await claim_executor_step(planned_state, context, durable=True)
        expired_lease = dict(claimed.domain_state["executorLease"])
        self.assertEqual(expired_lease["inboxStatus"], "ABSENT")
        expired_lease["expiresAt"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        expired = await update_task_state(
            claimed.task_id,
            TaskStatePatchRequest(
                expectedRevision=claimed.revision,
                actor="system",
                domainStatePatch={"executorLease": expired_lease},
            ),
        )

        recovered = await recover_expired_executor_claim(expired)

        self.assertEqual(recovered.status, "ready")
        self.assertEqual(recovered.active_plan.status, "active")
        self.assertEqual(recovered.active_plan.steps[0].status, "pending")
        self.assertEqual(
            recovered.domain_state["executorRecovery"]["status"], "SAFE_RETRY",
        )

    async def test_rejects_context_for_another_plan_without_writing(self):
        planned_state = await self._state_with_plan()
        context = build_executor_step_context(planned_state).model_copy(
            update={"plan_id": "plan-from-another-task"}
        )

        with self.assertRaises(ExecutorClaimError) as raised:
            await claim_executor_step(planned_state, context)

        self.assertEqual(raised.exception.code, "plan_mismatch")
        latest = await task_state.get_task_state(planned_state.task_id)
        self.assertEqual(latest.revision, planned_state.revision)
        self.assertEqual(latest.active_plan.steps[0].status, "pending")


class ExecutorArgumentResolutionTests(unittest.TestCase):
    def _claimed_state_and_context(
        self,
        state: TaskState,
    ) -> tuple[TaskState, ExecutorStepContext]:
        context = build_executor_step_context(state)
        claimed_plan = transition_plan_step_status(
            state.active_plan,
            context.step.step_id,
            "executing",
        )
        claimed_state = state.model_copy(
            update={
                "status": "executing",
                "revision": state.revision + 1,
                "active_plan": claimed_plan,
            }
        )
        return claimed_state, context

    def test_resolves_task_goal_argument(self):
        claimed_state, context = self._claimed_state_and_context(
            _state(plan=_plan())
        )

        result = resolve_executor_arguments(claimed_state, context)

        self.assertIsInstance(result, ResolvedExecutorArguments)
        self.assertEqual(
            result.resolved_arguments,
            {"query": "查找星河咖啡并读取商户详情"},
        )

    def test_resolves_task_state_and_system_policy_arguments(self):
        plan = TaskPlan(
            planId="plan-review-search",
            basedOnRevision=6,
            steps=[
                {
                    "stepId": "step-review",
                    "description": "查询目标商户评论",
                    "toolName": "search_shop_reviews",
                    "arguments": {
                        "shopName": "星河咖啡",
                        "source": "reviews",
                    },
                    "argumentSources": {
                        "shopName": {
                            "kind": "task_state",
                            "reference": "facts.shopName",
                        },
                        "source": {
                            "kind": "system_policy",
                            "reference": "reviewSource",
                        },
                    },
                    "expectedOutput": {"requiresReviewEvidence": True},
                }
            ],
        )
        state = _state(plan=plan).model_copy(
            update={
                "facts": [
                    TaskFact(
                        key="shopName",
                        value="星河咖啡",
                        certainty="confirmed",
                        source="user",
                    )
                ]
            }
        )
        claimed_state, context = self._claimed_state_and_context(state)

        result = resolve_executor_arguments(
            claimed_state,
            context,
            system_policies={"reviewSource": "reviews"},
        )

        self.assertEqual(
            result.resolved_arguments,
            {"shopName": "星河咖啡", "source": "reviews"},
        )

    def test_resolves_prior_step_output(self):
        selected_state = _state(
            status="executing",
            plan=_plan(first_status="executed"),
        )
        claimed_state, context = self._claimed_state_and_context(selected_state)

        result = resolve_executor_arguments(
            claimed_state,
            context,
            prior_step_outputs={"step-1": {"shopId": "shop-321"}},
        )

        self.assertEqual(
            result.resolved_arguments,
            {"shopId": "shop-321"},
        )

    def test_rejects_missing_prior_step_output(self):
        selected_state = _state(
            status="executing",
            plan=_plan(first_status="executed"),
        )
        claimed_state, context = self._claimed_state_and_context(selected_state)

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(claimed_state, context)

        self.assertEqual(raised.exception.code, "prior_step_output_missing")

    def test_rejects_changed_authoritative_source(self):
        selected_state = _state(plan=_plan())
        claimed_state, context = self._claimed_state_and_context(selected_state)
        changed_state = claimed_state.model_copy(
            update={"goal": "用户已经改成另一个商家任务"}
        )

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(changed_state, context)

        self.assertEqual(raised.exception.code, "argument_source_changed")

    def test_rejects_argument_resolution_before_step_is_claimed(self):
        selected_state = _state(plan=_plan())
        context = build_executor_step_context(selected_state)

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(selected_state, context)

        self.assertEqual(raised.exception.code, "task_not_executing")


class ExecutorToolValidationTests(unittest.TestCase):
    def _context_and_resolved(
        self,
    ) -> tuple[ExecutorStepContext, ResolvedExecutorArguments]:
        selected_state = _state(plan=_plan())
        context = build_executor_step_context(selected_state)
        claimed_plan = transition_plan_step_status(
            selected_state.active_plan,
            context.step.step_id,
            "executing",
        )
        claimed_state = selected_state.model_copy(
            update={
                "status": "executing",
                "revision": selected_state.revision + 1,
                "active_plan": claimed_plan,
            }
        )
        return context, resolve_executor_arguments(claimed_state, context)

    def _search_shops_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "search_shops",
                "description": "根据查询文本查找商户。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }

    def test_accepts_whitelisted_tool_with_schema_valid_arguments(self):
        context, resolved = self._context_and_resolved()

        tool_call = validate_executor_tool_call(
            context,
            resolved,
            [self._search_shops_schema()],
        )

        self.assertIsInstance(tool_call, ValidatedExecutorToolCall)
        self.assertEqual(tool_call.tool_name, "search_shops")
        self.assertEqual(
            tool_call.arguments,
            {"query": "查找星河咖啡并读取商户详情"},
        )

    def test_rejects_tool_outside_allowlist(self):
        context, resolved = self._context_and_resolved()
        schema = self._search_shops_schema()
        schema["function"]["name"] = "search_shop_reviews"

        with self.assertRaises(ExecutorToolValidationError) as raised:
            validate_executor_tool_call(context, resolved, [schema])

        self.assertEqual(raised.exception.code, "tool_not_allowed")

    def test_rejects_missing_required_argument(self):
        context, resolved = self._context_and_resolved()
        missing = resolved.model_copy(update={"resolved_arguments": {}})

        with self.assertRaises(ExecutorToolValidationError) as raised:
            validate_executor_tool_call(
                context,
                missing,
                [self._search_shops_schema()],
            )

        self.assertEqual(raised.exception.code, "invalid_tool_arguments")

    def test_rejects_undeclared_argument(self):
        context, resolved = self._context_and_resolved()
        extra = resolved.model_copy(
            update={
                "resolved_arguments": {
                    **resolved.resolved_arguments,
                    "admin": True,
                }
            }
        )

        with self.assertRaises(ExecutorToolValidationError) as raised:
            validate_executor_tool_call(
                context,
                extra,
                [self._search_shops_schema()],
            )

        self.assertEqual(raised.exception.code, "invalid_tool_arguments")

    def test_rejects_argument_with_wrong_json_type(self):
        context, resolved = self._context_and_resolved()
        wrong_type = resolved.model_copy(
            update={"resolved_arguments": {"query": 123}}
        )

        with self.assertRaises(ExecutorToolValidationError) as raised:
            validate_executor_tool_call(
                context,
                wrong_type,
                [self._search_shops_schema()],
            )

        self.assertEqual(raised.exception.code, "invalid_tool_arguments")

    def test_rejects_duplicate_tool_names_in_allowlist(self):
        context, resolved = self._context_and_resolved()
        schema = self._search_shops_schema()

        with self.assertRaises(ExecutorToolValidationError) as raised:
            validate_executor_tool_call(context, resolved, [schema, schema])

        self.assertEqual(raised.exception.code, "duplicate_allowed_tool")


class ExecutorToolDispatchTests(unittest.IsolatedAsyncioTestCase):
    def _tool_call(self) -> ValidatedExecutorToolCall:
        return ValidatedExecutorToolCall(
            taskId="task-shop-lookup",
            planId="plan-shop-lookup",
            stepId="step-1",
            toolName="search_shops",
            arguments={"query": "星河咖啡"},
        )

    async def test_dispatches_exactly_one_validated_tool_call(self):
        expected = ToolTrace(
            tool="search_shops",
            ok=True,
            detail={"shops": [{"id": 321, "name": "星河咖啡"}]},
        )
        tool_caller = AsyncMock(return_value=expected)

        trace = await execute_validated_tool_call(
            self._tool_call(),
            tool_caller=tool_caller,
        )

        self.assertIs(trace, expected)
        tool_caller.assert_awaited_once_with(
            "search_shops",
            {"query": "星河咖啡"},
        )

    async def test_returns_failed_tool_trace_without_reclassifying_it(self):
        expected = ToolTrace(
            tool="search_shops",
            ok=False,
            detail="backend unavailable",
        )

        trace = await execute_validated_tool_call(
            self._tool_call(),
            tool_caller=AsyncMock(return_value=expected),
        )

        self.assertIs(trace, expected)
        self.assertFalse(trace.ok)

    async def test_propagates_tool_exception_for_later_failure_persistence(self):
        tool_caller = AsyncMock(side_effect=RuntimeError("network timeout"))

        with self.assertRaisesRegex(RuntimeError, "network timeout"):
            await execute_validated_tool_call(
                self._tool_call(),
                tool_caller=tool_caller,
            )

        self.assertEqual(tool_caller.await_count, 1)

    async def test_rejects_trace_for_a_different_tool(self):
        wrong_trace = ToolTrace(
            tool="search_shop_reviews",
            ok=True,
            detail={"chunks": []},
        )

        with self.assertRaises(ExecutorToolDispatchError):
            await execute_validated_tool_call(
                self._tool_call(),
                tool_caller=AsyncMock(return_value=wrong_trace),
            )

    async def test_isolates_validated_arguments_from_tool_mutation(self):
        tool_call = self._tool_call()

        async def mutating_tool(name: str, arguments: dict) -> ToolTrace:
            arguments["query"] = "被工具修改"
            return ToolTrace(tool=name, ok=True, detail={"shops": []})

        await execute_validated_tool_call(
            tool_call,
            tool_caller=mutating_tool,
        )

        self.assertEqual(tool_call.arguments, {"query": "星河咖啡"})


class StepExecutionResultTests(unittest.IsolatedAsyncioTestCase):
    def _tool_call(self) -> ValidatedExecutorToolCall:
        return ValidatedExecutorToolCall(
            taskId="task-shop-lookup",
            planId="plan-shop-lookup",
            stepId="step-1",
            toolName="search_shops",
            arguments={"query": "星河咖啡"},
        )

    async def test_records_successful_tool_trace(self):
        trace = ToolTrace(
            tool="search_shops",
            ok=True,
            detail={"shops": [{"id": 321, "name": "星河咖啡"}]},
        )

        result = await execute_and_record_tool_call(
            self._tool_call(),
            tool_caller=AsyncMock(return_value=trace),
        )

        self.assertEqual(result.outcome, "tool_succeeded")
        self.assertIs(result.tool_trace, trace)
        self.assertIsNone(result.error_type)
        self.assertGreaterEqual(result.duration_ms, 0)

    async def test_records_failed_tool_trace(self):
        trace = ToolTrace(
            tool="search_shops",
            ok=False,
            detail="backend unavailable",
        )

        result = await execute_and_record_tool_call(
            self._tool_call(),
            tool_caller=AsyncMock(return_value=trace),
        )

        self.assertEqual(result.outcome, "tool_failed")
        self.assertIs(result.tool_trace, trace)
        self.assertIsNone(result.error_message)

    async def test_records_tool_exception_without_a_trace(self):
        result = await execute_and_record_tool_call(
            self._tool_call(),
            tool_caller=AsyncMock(side_effect=TimeoutError("tool timed out")),
        )

        self.assertEqual(result.outcome, "tool_error")
        self.assertIsNone(result.tool_trace)
        self.assertEqual(result.error_type, "TimeoutError")
        self.assertEqual(result.error_message, "tool timed out")

    def test_rejects_inconsistent_outcome_and_trace(self):
        now = datetime.now(timezone.utc)

        with self.assertRaises(ValidationError):
            StepExecutionResult(
                taskId="task-shop-lookup",
                planId="plan-shop-lookup",
                stepId="step-1",
                toolName="search_shops",
                resolvedArguments={"query": "星河咖啡"},
                outcome="tool_succeeded",
                toolTrace=ToolTrace(
                    tool="search_shops",
                    ok=False,
                    detail="backend unavailable",
                ),
                startedAt=now,
                finishedAt=now,
                durationMs=0,
            )


class StepOutputExtractionTests(unittest.IsolatedAsyncioTestCase):
    def _context(self) -> ExecutorStepContext:
        return build_executor_step_context(_state(plan=_plan()))

    async def _result_with_shops(self, shops: list[dict]) -> StepExecutionResult:
        context = self._context()
        tool_call = ValidatedExecutorToolCall(
            taskId=context.task_id,
            planId=context.plan_id,
            stepId=context.step.step_id,
            toolName=context.step.tool_name,
            arguments=context.step.arguments,
        )
        return await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=True,
                    detail={"shops": shops},
                )
            ),
        )

    async def test_extracts_shop_id_from_one_unique_candidate(self):
        context = self._context()
        result = await self._result_with_shops(
            [{"id": 321, "name": "星河咖啡"}]
        )

        output = extract_normalized_step_output(context, result)

        self.assertEqual(output.values, {"shopId": 321})
        self.assertEqual(output.step_id, "step-1")

    async def test_rejects_zero_shop_candidates(self):
        context = self._context()
        result = await self._result_with_shops([])

        with self.assertRaises(ExecutorOutputExtractionError) as raised:
            extract_normalized_step_output(context, result)

        self.assertEqual(raised.exception.code, "shop_not_found")

    async def test_rejects_multiple_shop_candidates(self):
        context = self._context()
        result = await self._result_with_shops(
            [
                {"id": 321, "name": "星河咖啡一店"},
                {"id": 654, "name": "星河咖啡二店"},
            ]
        )

        with self.assertRaises(ExecutorOutputExtractionError) as raised:
            extract_normalized_step_output(context, result)

        self.assertEqual(raised.exception.code, "shop_ambiguous")

    async def test_rejects_failed_execution_result(self):
        context = self._context()
        tool_call = ValidatedExecutorToolCall(
            taskId=context.task_id,
            planId=context.plan_id,
            stepId=context.step.step_id,
            toolName=context.step.tool_name,
            arguments=context.step.arguments,
        )
        result = await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=False,
                    detail="backend unavailable",
                )
            ),
        )

        with self.assertRaises(ExecutorOutputExtractionError) as raised:
            extract_normalized_step_output(context, result)

        self.assertEqual(raised.exception.code, "execution_not_successful")


class StepExecutionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _claimed_execution(
        self,
        *,
        durable: bool = False,
    ) -> tuple[TaskState, ExecutorStepContext, ValidatedExecutorToolCall]:
        created = await create_task_state(
            TaskStateCreateRequest(
                goal="查找星河咖啡并读取商户详情",
            )
        )
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        planned = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                activePlan=_plan(based_on_revision=ready.revision),
            ),
        )
        context = build_executor_step_context(planned)
        claimed = await claim_executor_step(planned, context, durable=durable)
        tool_call = ValidatedExecutorToolCall(
            taskId=context.task_id,
            planId=context.plan_id,
            stepId=context.step.step_id,
            toolName=context.step.tool_name,
            arguments=context.step.arguments,
        )
        return claimed, context, tool_call

    async def test_persists_success_and_marks_step_executed(self):
        claimed, context, tool_call = await self._claimed_execution()
        result = await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=True,
                    detail={"shops": [{"id": 321, "name": "星河咖啡"}]},
                )
            ),
        )
        step_output = extract_normalized_step_output(context, result)

        updated = await persist_step_execution_result(
            claimed,
            context,
            tool_call,
            result,
            step_output,
        )

        self.assertEqual(updated.revision, claimed.revision + 1)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.active_plan.status, "active")
        self.assertEqual(updated.active_plan.steps[0].status, "executed")
        self.assertEqual(updated.active_plan.steps[1].status, "pending")
        history = updated.domain_state["stepExecutionResults"]
        self.assertEqual(len(history), 1)
        persisted = StepExecutionResult.model_validate(history[0])
        self.assertEqual(persisted.outcome, "tool_succeeded")
        self.assertEqual(persisted.tool_trace.detail["shops"][0]["id"], 321)
        output = NormalizedStepOutput.model_validate(
            updated.domain_state["stepOutputs"]["step-1"]
        )
        self.assertEqual(output.values, {"shopId": 321})

        next_context = build_executor_step_context(updated)
        next_claimed = await claim_executor_step(updated, next_context)
        resolved = resolve_executor_arguments(next_claimed, next_context)
        self.assertEqual(resolved.resolved_arguments, {"shopId": 321})

    async def test_persists_failed_trace_and_marks_step_failed(self):
        claimed, context, tool_call = await self._claimed_execution()
        result = await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=False,
                    detail="backend unavailable",
                )
            ),
        )

        updated = await persist_step_execution_result(
            claimed,
            context,
            tool_call,
            result,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.active_plan.steps[0].status, "failed")
        persisted = StepExecutionResult.model_validate(
            updated.domain_state["stepExecutionResults"][0]
        )
        self.assertEqual(persisted.outcome, "tool_failed")

    async def test_persists_durable_failed_trace_without_normalizing_output(self):
        claimed, context, tool_call = await self._claimed_execution(durable=True)
        lease = dict(claimed.domain_state["executorLease"])
        input_hash = hashlib.sha256(json.dumps(
            tool_call.arguments, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        lease.update({"inboxStatus": "IN_FLIGHT", "canonicalArgsSha256": input_hash})
        claimed = await update_task_state(
            claimed.task_id,
            TaskStatePatchRequest(
                expectedRevision=claimed.revision, actor="agent",
                domainStatePatch={"executorLease": lease},
            ),
        )
        result = await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(return_value=ToolTrace(
                tool="search_shops", ok=False, detail="backend unavailable",
            )),
        )
        trace_hash = hashlib.sha256(json.dumps(
            result.tool_trace.model_dump(by_alias=True, mode="json"),
            ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        receipt = {
            "taskId": context.task_id, "planId": context.plan_id,
            "stepId": context.step.step_id, "toolName": context.step.tool_name,
            "stateRevision": context.expected_revision, "inputHash": input_hash,
            "resultHash": trace_hash, "inboxStatus": "SUCCEEDED",
            "toolOutcome": "tool_failed", "executionId": "a" * 64,
            "logicalSlotKey": "b" * 64, "fence": 1,
        }

        updated = await persist_step_execution_result(
            claimed, context, tool_call, result, durable_tool_receipt=receipt,
        )

        self.assertEqual(updated.active_plan.steps[0].status, "failed")
        self.assertEqual(
            StepExecutionResult.model_validate(
                updated.domain_state["stepExecutionResults"][0]
            ).outcome,
            "tool_failed",
        )
        self.assertNotIn("stepOutputs", updated.domain_state)

        bad_receipt = dict(receipt, toolOutcome="tool_succeeded")
        with self.assertRaises(ExecutorResultPersistenceError) as raised:
            await persist_step_execution_result(
                claimed, context, tool_call, result, durable_tool_receipt=bad_receipt,
            )
        self.assertEqual(raised.exception.code, "durable_inbox_receipt_mismatch")

    async def test_persists_exception_and_marks_step_failed(self):
        claimed, context, tool_call = await self._claimed_execution()
        result = await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(side_effect=TimeoutError("tool timed out")),
        )

        updated = await persist_step_execution_result(
            claimed,
            context,
            tool_call,
            result,
        )

        self.assertEqual(updated.active_plan.steps[0].status, "failed")
        persisted = StepExecutionResult.model_validate(
            updated.domain_state["stepExecutionResults"][0]
        )
        self.assertEqual(persisted.outcome, "tool_error")
        self.assertEqual(persisted.error_type, "TimeoutError")

    async def test_persists_output_extraction_failure_as_blocked(self):
        cases = [
            ([], "shop_not_found"),
            (
                [
                    {"id": 321, "name": "星河咖啡一店"},
                    {"id": 654, "name": "星河咖啡二店"},
                ],
                "shop_ambiguous",
            ),
        ]
        for shops, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                claimed, context, tool_call = await self._claimed_execution()
                result = await execute_and_record_tool_call(
                    tool_call,
                    tool_caller=AsyncMock(
                        return_value=ToolTrace(
                            tool="search_shops",
                            ok=True,
                            detail={"shops": shops},
                        )
                    ),
                )
                with self.assertRaises(ExecutorOutputExtractionError) as raised:
                    extract_normalized_step_output(context, result)

                updated = await persist_step_execution_result(
                    claimed,
                    context,
                    tool_call,
                    result,
                    output_error=raised.exception,
                )

                self.assertEqual(updated.status, "ready")
                self.assertEqual(updated.active_plan.status, "active")
                self.assertEqual(updated.active_plan.steps[0].status, "blocked")
                execution = StepExecutionResult.model_validate(
                    updated.domain_state["stepExecutionResults"][0]
                )
                self.assertEqual(execution.outcome, "tool_succeeded")
                block = ExecutorBlockRecord.model_validate(
                    updated.domain_state["executorBlock"]
                )
                self.assertEqual(block.stage, "output_extraction")
                self.assertEqual(block.error_code, expected_code)
                self.assertNotIn(
                    "step-1",
                    updated.domain_state.get("stepOutputs", {}),
                )

    async def test_rejects_output_and_output_error_together(self):
        claimed, context, tool_call = await self._claimed_execution()
        result = await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=True,
                    detail={"shops": [{"id": 321, "name": "星河咖啡"}]},
                )
            ),
        )
        output = extract_normalized_step_output(context, result)
        error = ExecutorOutputExtractionError(
            "shop_ambiguous",
            "不能擅自选择shopId",
        )

        with self.assertRaises(ExecutorResultPersistenceError) as raised:
            await persist_step_execution_result(
                claimed,
                context,
                tool_call,
                result,
                output,
                output_error=error,
            )

        self.assertEqual(raised.exception.code, "conflicting_output_result")
        latest = await task_state.get_task_state(claimed.task_id)
        self.assertEqual(latest.revision, claimed.revision)
        self.assertEqual(latest.active_plan.steps[0].status, "executing")

    async def test_occ_rejects_result_write_after_claimed_state_changes(self):
        claimed, context, tool_call = await self._claimed_execution()
        result = await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=True,
                    detail={"shops": []},
                )
            ),
        )
        changed = await update_task_state(
            claimed.task_id,
            TaskStatePatchRequest(
                expectedRevision=claimed.revision,
                actor="user",
                domainStatePatch={"userChangedTask": True},
            ),
        )

        with self.assertRaises(TaskStateRevisionConflictError):
            await persist_step_execution_result(
                claimed,
                context,
                tool_call,
                result,
            )

        latest = await task_state.get_task_state(claimed.task_id)
        self.assertEqual(latest.revision, changed.revision)
        self.assertEqual(latest.active_plan.steps[0].status, "executing")
        self.assertNotIn("stepExecutionResults", latest.domain_state)

    async def test_rejects_result_from_another_execution_without_writing(self):
        claimed, context, tool_call = await self._claimed_execution()
        result = await execute_and_record_tool_call(
            tool_call,
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=True,
                    detail={"shops": []},
                )
            ),
        )
        mismatched = result.model_copy(update={"plan_id": "another-plan"})

        with self.assertRaises(ExecutorResultPersistenceError) as raised:
            await persist_step_execution_result(
                claimed,
                context,
                tool_call,
                mismatched,
            )

        self.assertEqual(raised.exception.code, "execution_result_mismatch")
        latest = await task_state.get_task_state(claimed.task_id)
        self.assertEqual(latest.revision, claimed.revision)
        self.assertEqual(latest.active_plan.steps[0].status, "executing")


class ExecutorRunStepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    def _search_schema(self) -> dict:
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

    def _detail_schema(self) -> dict:
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

    async def _planned_state(self) -> TaskState:
        created = await create_task_state(
            TaskStateCreateRequest(
                goal="查找星河咖啡并读取商户详情",
            )
        )
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        return await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                activePlan=_plan(based_on_revision=ready.revision),
            ),
        )

    async def test_runs_two_plan_steps_across_two_separate_calls(self):
        planned = await self._planned_state()
        search_caller = AsyncMock(
            return_value=ToolTrace(
                tool="search_shops",
                ok=True,
                detail={"shops": [{"id": 321, "name": "星河咖啡"}]},
            )
        )

        first = await run_executor_step(
            planned,
            [self._search_schema()],
            tool_caller=search_caller,
        )

        self.assertEqual(first.outcome, "step_executed")
        self.assertEqual(first.task_state.status, "ready")
        self.assertEqual(first.task_state.active_plan.steps[0].status, "executed")
        self.assertEqual(first.task_state.active_plan.steps[1].status, "pending")
        self.assertEqual(first.step_output.values, {"shopId": 321})
        search_caller.assert_awaited_once()

        detail_caller = AsyncMock(
            return_value=ToolTrace(
                tool="get_shop_detail",
                ok=True,
                detail={"id": 321, "name": "星河咖啡", "phone": "123"},
            )
        )
        second = await run_executor_step(
            first.task_state,
            [self._detail_schema()],
            tool_caller=detail_caller,
        )

        self.assertEqual(second.outcome, "step_executed")
        self.assertIsNone(second.step_output)
        self.assertTrue(
            all(
                step.status == "executed"
                for step in second.task_state.active_plan.steps
            )
        )
        detail_caller.assert_awaited_once_with(
            "get_shop_detail",
            {"shopId": 321},
        )

    async def test_blocks_before_dispatch_when_tool_is_not_allowed(self):
        planned = await self._planned_state()
        tool_caller = AsyncMock()

        result = await run_executor_step(
            planned,
            [],
            tool_caller=tool_caller,
        )

        self.assertEqual(result.outcome, "step_blocked")
        self.assertEqual(result.error_code, "tool_not_allowed")
        self.assertEqual(result.task_state.status, "ready")
        self.assertEqual(result.task_state.active_plan.steps[0].status, "blocked")
        block = ExecutorBlockRecord.model_validate(
            result.task_state.domain_state["executorBlock"]
        )
        self.assertEqual(block.stage, "tool_validation")
        tool_caller.assert_not_awaited()

    async def test_blocks_before_validation_when_argument_source_is_missing(self):
        created = await create_task_state(
            TaskStateCreateRequest(goal="读取目标商户详情")
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
            planId="plan-missing-shop-id",
            basedOnRevision=ready.revision,
            status="active",
            steps=[
                {
                    "stepId": "step-1",
                    "description": "读取目标商户详情",
                    "toolName": "get_shop_detail",
                    "arguments": {"shopId": 321},
                    "argumentSources": {
                        "shopId": {
                            "kind": "task_state",
                            "reference": "facts.shopId",
                        }
                    },
                    "expectedOutput": {"requiresShopDetail": True},
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
        tool_caller = AsyncMock()

        result = await run_executor_step(
            planned,
            [self._detail_schema()],
            tool_caller=tool_caller,
        )

        self.assertEqual(result.outcome, "step_blocked")
        self.assertEqual(result.error_code, "argument_source_missing")
        block = ExecutorBlockRecord.model_validate(
            result.task_state.domain_state["executorBlock"]
        )
        self.assertEqual(block.stage, "argument_resolution")
        tool_caller.assert_not_awaited()

    async def test_marks_step_failed_when_tool_reports_failure(self):
        planned = await self._planned_state()

        result = await run_executor_step(
            planned,
            [self._search_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=False,
                    detail="backend unavailable",
                )
            ),
        )

        self.assertEqual(result.outcome, "step_failed")
        self.assertEqual(result.execution_result.outcome, "tool_failed")
        self.assertEqual(result.task_state.active_plan.steps[0].status, "failed")

    async def test_blocks_when_successful_search_has_ambiguous_output(self):
        planned = await self._planned_state()

        result = await run_executor_step(
            planned,
            [self._search_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shops",
                    ok=True,
                    detail={
                        "shops": [
                            {"id": 321, "name": "星河咖啡一店"},
                            {"id": 654, "name": "星河咖啡二店"},
                        ]
                    },
                )
            ),
        )

        self.assertEqual(result.outcome, "step_blocked")
        self.assertEqual(result.error_code, "shop_ambiguous")
        self.assertEqual(result.execution_result.outcome, "tool_succeeded")
        self.assertEqual(result.task_state.active_plan.steps[0].status, "blocked")
        block = ExecutorBlockRecord.model_validate(
            result.task_state.domain_state["executorBlock"]
        )
        self.assertEqual(block.stage, "output_extraction")


_OS_REQ = [
    {
        "key": "os", "operator": "eq", "value": "ios",
        "unit": "enum", "priority": "hard", "source": "user",
    }
]


def _ecom_state(*, category: str = "手机", requirements=None) -> TaskState:
    """Real-shaped used-phone TaskState with an accepted search_products plan."""
    now = datetime.now(timezone.utc)
    requirements = requirements if requirements is not None else _OS_REQ
    step = {
        "stepId": "step-1",
        "description": "检索iOS二手机",
        "toolName": "search_products",
        "arguments": {
            "query": "想找 iOS 二手机。",
            "category": category,
            "requirements": requirements,
        },
        "argumentSources": {
            "query": {"kind": "task_goal"},
            "category": {"kind": "shopping_guide", "reference": "category"},
            "requirements": {"kind": "shopping_guide", "reference": "requirements"},
        },
        "expectedOutput": {"requiresProductCandidates": True},
    }
    return TaskState(
        taskId="task-iphone-guide",
        taskType="ecommerce_guide",
        status="ready",
        revision=3,
        goal="想找 iOS 二手机。",
        facts=[],
        constraints=[{"key": "os", "operator": "eq", "value": "ios", "source": "user"}],
        unknowns=[],
        pendingQuestions=[],
        activePlan=TaskPlan(
            planId="plan-iphone-guide",
            basedOnRevision=3,
            steps=[step],
        ),
        domainState={
            "shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "useCases": [],
                "requirements": requirements,
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            }
        },
        createdAt=now,
        updatedAt=now,
    )


def _ecom_executor_view(
    state: TaskState,
    *,
    resolved_arguments=None,
    shopping_guide_sources=None,
) -> ExecutorContextView:
    step = state.active_plan.steps[0]
    return ExecutorContextView(
        runId="run-1",
        taskId=state.task_id,
        baseContextRevision=state.revision,
        phaseTaskRevision=state.revision + 1,
        contextHash="",
        planId=state.active_plan.plan_id,
        stepId=step.step_id,
        stepDescription=step.description,
        toolName=step.tool_name,
        taskGoal=state.goal,
        resolvedArguments=(
            dict(step.arguments) if resolved_arguments is None else resolved_arguments
        ),
        shoppingGuideSources=(
            {"category": "手机", "requirements": _OS_REQ}
            if shopping_guide_sources is None
            else shopping_guide_sources
        ),
    )


class SingleStepProductOutputPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Smoke-005 shape: terminal search output is also Validator evidence."""

    _candidate_ids = [
        4346166, 2157503, 1239068, 117916, 3934984,
        5989522, 634577, 3470593, 4524730, 1912721,
        2965840, 3956695, 3600996, 1967528, 5304970,
        5286377, 1092202, 2640402, 351899, 4244556,
    ]

    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    @classmethod
    def _search_detail(cls, candidate_ids=None):
        ranked = cls._candidate_ids if candidate_ids is None else candidate_ids
        valid_ranked = [item for item in ranked if type(item) is int and item > 0]
        raw = "iOS"
        evidence = [
            {
                "ref": f"product:{item}:attribute:os",
                "field": "relevance.attr_value", "rawValue": raw,
                "method": "used-phone-exact-token-seven-field-v2",
            }
            for item in valid_ranked
        ]
        return {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "candidatePoolIds": list(ranked),
            "rankedItemIds": list(ranked),
            "candidateIds": list(ranked),
            "candidates": [
                {
                    "id": item,
                    "attributes": [{
                        "key": "os", "normalizedNumber": None,
                        "normalizedBoolean": None, "normalizedText": "ios",
                        "rawValue": raw, "evidenceField": "relevance.attr_value",
                        "extractionMethod": "used-phone-exact-token-seven-field-v2",
                    }],
                    "checks": [{
                        "key": "os", "operator": "eq", "expected": "ios",
                        "unit": "enum", "priority": "hard", "source": "user",
                        "actual": "ios", "status": "pass",
                        "evidenceRef": f"product:{item}:attribute:os",
                    }],
                    "selectionType": "full_match",
                    "evidenceRefs": [f"product:{item}:attribute:os"],
                }
                for item in valid_ranked
            ],
            "retrievalTrace": {
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "candidatePoolCount": len(ranked),
                "authoritativeFactCount": len(ranked),
            },
            "rankingTrace": {
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "inputCandidateCount": len(ranked),
                "rankedItemCount": len(ranked),
                "tieBreak": RANKING_TIE_BREAK,
                "formula": RANKING_FORMULA,
            },
            "citationTrace": {
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "sourceTool": "search_products",
                "rankedItemIds": list(ranked),
                "evidenceRefCount": len(evidence),
                "binding": "current_successful_tool_call_ranked_items_only",
            },
            "evidenceRefs": [item["ref"] for item in evidence],
            "evidence": evidence,
            "eliminated": [],
        }

    @staticmethod
    def _schema() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "search_products",
                "description": "检索商品",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string"},
                        "category": {"type": "string"},
                        "requirements": {"type": "array"},
                    },
                    "required": ["query", "category", "requirements"],
                },
            },
        }

    async def _planned_state(self) -> TaskState:
        created = await create_task_state(
            TaskStateCreateRequest(
                goal="想找 iOS 二手机。",
                task_type="ecommerce_guide",
                domain_state={
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "useCases": [],
                        "requirements": _OS_REQ,
                        "candidateIds": [],
                        "comparedIds": [],
                        "evidenceStatus": "missing",
                    }
                },
            )
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
            planId="plan-smoke-005-shape",
            basedOnRevision=ready.revision,
            steps=[{
                "stepId": "step-search",
                "description": "搜索 iOS 二手机",
                "toolName": "search_products",
                "arguments": {
                    "query": ready.goal,
                    "category": "手机",
                    "requirements": _OS_REQ,
                },
                "argumentSources": {
                    "query": {"kind": "task_goal"},
                    "category": {
                        "kind": "shopping_guide",
                        "reference": "category",
                    },
                    "requirements": {
                        "kind": "shopping_guide",
                        "reference": "requirements",
                    },
                },
                "expectedOutput": {"requiresProductCandidates": True},
            }],
        )
        return await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                activePlan=plan,
            ),
        )

    async def test_terminal_search_persists_registered_candidate_output(self):
        planned = await self._planned_state()

        result = await run_executor_step(
            planned,
            [self._schema()],
            tool_caller=AsyncMock(return_value=ToolTrace(
                tool="search_products",
                ok=True,
                detail=self._search_detail(),
            )),
        )

        self.assertEqual(result.outcome, "step_executed")
        self.assertEqual(result.step_output.values["candidatePoolIds"], self._candidate_ids)
        self.assertEqual(result.step_output.values["rankedItemIds"], self._candidate_ids)
        self.assertEqual(result.step_output.values["productIds"], self._candidate_ids)
        persisted = NormalizedStepOutput.model_validate(
            result.task_state.domain_state["stepOutputs"]["step-search"]
        )
        self.assertEqual(persisted.values["productIds"], self._candidate_ids)
        inbox = result.task_state.domain_state["executorToolInbox"]
        self.assertEqual(len(inbox), 1)
        record = next(iter(inbox.values()))
        self.assertEqual(record["status"], "COMPLETED")
        self.assertEqual(record["toolName"], "search_products")
        self.assertEqual(len(record["resultDigest"]), 64)

    async def test_cancellation_stales_plan_and_clears_executor_lease(self):
        planned = await self._planned_state()
        entered_tool = asyncio.Event()

        async def never_returns(_name, _arguments):
            entered_tool.set()
            await asyncio.Future()

        execution = asyncio.create_task(run_executor_step(
            planned,
            [self._schema()],
            tool_caller=never_returns,
        ))
        await entered_tool.wait()
        execution.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await execution

        latest = await task_state.get_task_state(planned.task_id)
        self.assertEqual(latest.status, "ready")
        self.assertEqual(latest.active_plan.status, "stale")
        self.assertEqual(latest.active_plan.steps[0].status, "failed")
        self.assertNotIn("executorLease", latest.domain_state)
        self.assertEqual(
            latest.domain_state["executorRecovery"]["reason"],
            "execution_cancelled",
        )

    async def test_empty_or_invalid_candidates_fail_closed(self):
        for candidate_ids in ([], [4346166, "2157503"], [True]):
            with self.subTest(candidate_ids=candidate_ids):
                planned = await self._planned_state()
                result = await run_executor_step(
                    planned,
                    [self._schema()],
                    tool_caller=AsyncMock(return_value=ToolTrace(
                        tool="search_products",
                        ok=True,
                        detail=self._search_detail(candidate_ids),
                    )),
                )

                self.assertEqual(result.outcome, "step_blocked")
                self.assertIn(
                    result.error_code,
                    {"ranking_ids_missing", "invalid_ranking_id"},
                )
                self.assertNotIn(
                    "step-search",
                    result.task_state.domain_state.get("stepOutputs", {}),
                )

    async def test_tool_failure_never_persists_normalized_output(self):
        planned = await self._planned_state()

        result = await run_executor_step(
            planned,
            [self._schema()],
            tool_caller=AsyncMock(return_value=ToolTrace(
                tool="search_products",
                ok=False,
                detail=self._search_detail(),
            )),
        )

        self.assertEqual(result.outcome, "step_failed")
        self.assertIsNone(result.step_output)
        self.assertEqual(
            result.task_state.domain_state.get("stepOutputs", {}), {}
        )
        inbox = result.task_state.domain_state["executorToolInbox"]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(next(iter(inbox.values()))["status"], "FAILED")

    async def test_raw_candidate_alias_cannot_impersonate_two_stage_output(self):
        planned = await self._planned_state()

        result = await run_executor_step(
            planned,
            [self._schema()],
            tool_caller=AsyncMock(return_value=ToolTrace(
                tool="search_products",
                ok=True,
                detail={"candidateIds": self._candidate_ids},
            )),
        )

        self.assertEqual(result.outcome, "step_blocked")
        self.assertEqual(
            result.error_code, "unsupported_ranking_contract_version"
        )
        self.assertEqual(
            result.task_state.domain_state.get("stepOutputs", {}), {}
        )


class ShoppingGuideExecutorContractTests(unittest.TestCase):
    """E2E-STAGE5-PLANNER-SOURCE-CONTRACT-001: Executor re-derives the same source."""

    def _claim_with_view(self, state, view):
        context = build_executor_step_context(state, executor_view=view)
        claimed_plan = transition_plan_step_status(
            state.active_plan,
            context.step.step_id,
            "executing",
        )
        claimed_state = state.model_copy(
            update={
                "status": "executing",
                "revision": state.revision + 1,
                "active_plan": claimed_plan,
            }
        )
        return claimed_state, context

    def test_resolves_shopping_guide_sources_from_executor_view(self):
        state = _ecom_state()
        view = _ecom_executor_view(state)
        claimed_state, context = self._claim_with_view(state, view)

        result = resolve_executor_arguments(claimed_state, context)

        self.assertEqual(result.resolved_arguments["query"], "想找 iOS 二手机。")
        self.assertEqual(result.resolved_arguments["category"], "手机")
        self.assertEqual(result.resolved_arguments["requirements"], _OS_REQ)

    def test_rejects_tampered_plan_shopping_guide_argument(self):
        state = _ecom_state(category="耳机")
        view = _ecom_executor_view(state)
        claimed_state, context = self._claim_with_view(state, view)

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(claimed_state, context)

        self.assertEqual(raised.exception.code, "argument_source_changed")
        self.assertIn("category", str(raised.exception))

    def test_rejects_tampered_view_resolved_arguments(self):
        # Plan args correct, but the ExecutorView's frozen expectation disagrees
        # with the server recomputation → fail closed.
        state = _ecom_state()
        view = _ecom_executor_view(
            state,
            resolved_arguments={
                "query": "想找 iOS 二手机。",
                "category": "耳机",
                "requirements": _OS_REQ,
            },
        )
        claimed_state, context = self._claim_with_view(state, view)

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(claimed_state, context)

        self.assertEqual(raised.exception.code, "view_resolved_arguments_mismatch")

    def test_legacy_path_resolves_from_state_shopping_guide(self):
        state = _ecom_state()
        claimed_state, context = self._claim_with_view(state, None)

        result = resolve_executor_arguments(claimed_state, context)

        self.assertEqual(result.resolved_arguments["category"], "手机")
        self.assertEqual(result.resolved_arguments["requirements"], _OS_REQ)

    def test_rejects_shopping_guide_source_when_state_has_no_guide(self):
        # Cross-domain task: no shoppingGuide in domain_state → fail closed.
        state = _ecom_state()
        state = state.model_copy(
            update={"domain_state": dict(state.domain_state, shoppingGuide=None)}
        )
        claimed_state, context = self._claim_with_view(state, None)

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(claimed_state, context)

        self.assertEqual(raised.exception.code, "argument_source_missing")

    def test_local_life_with_valid_guide_fails_closed_legacy_path(self):
        # Codex blocking finding: a format-valid guide on a non-ecommerce task
        # must not resolve through the legacy TaskState derivation.
        state = _ecom_state().model_copy(update={"task_type": "local_life"})
        claimed_state, context = self._claim_with_view(state, None)

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(claimed_state, context)

        self.assertEqual(raised.exception.code, "argument_source_missing")

    def test_unknown_task_type_with_valid_guide_fails_closed_legacy_path(self):
        state = _ecom_state().model_copy(update={"task_type": "custom_domain"})
        claimed_state, context = self._claim_with_view(state, None)

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(claimed_state, context)

        self.assertEqual(raised.exception.code, "argument_source_missing")

    def test_local_life_with_valid_guide_fails_closed_even_with_view_sources(self):
        # The Executor gate is at the boundary: even a view that (incorrectly)
        # carried sources must not let a non-ecommerce task resolve them.
        state = _ecom_state().model_copy(update={"task_type": "local_life"})
        view = _ecom_executor_view(state)  # would carry shopping-guide sources
        claimed_state, context = self._claim_with_view(state, view)

        with self.assertRaises(ExecutorArgumentResolutionError) as raised:
            resolve_executor_arguments(claimed_state, context)

        self.assertEqual(raised.exception.code, "argument_source_missing")
