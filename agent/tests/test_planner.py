import unittest
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pydantic import ValidationError
from app.settings import settings


@pytest.fixture(autouse=True)
def _legacy_authority_for_pre_migration_planner_fixtures(monkeypatch):
    """Legacy Planner fixtures do not contain a V2/binding dual-write."""

    monkeypatch.setattr(settings, "shopping_state_authority", "legacy")

from app.planner import (
    PLANNER_SYSTEM_PROMPT,
    PLANNER_SUBMISSION_TOOL_NAME,
    PlannerContext,
    accept_planner_model_output,
    build_planner_context,
    build_planner_context_from_view,
    create_plan,
    persist_needs_user_input_result,
    persist_planning_failed_result,
    persist_planner_result,
    persist_planned_result,
    run_planner_phase,
)
from app.planning import (
    PLAN_ARGUMENT_SOURCE_CONTRACT,
    PlanArgumentSource,
    PlannerModelOutput,
    PlannerResult,
)
from app.context_view import ContextProjector, PlannerContextView
from app.context_pack import build_context_pack
from app import task_state
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    TaskStateRevisionConflictError,
    create_task_state,
    update_task_state,
)
from tests.fake_redis import FakeRedis


def _state() -> TaskState:
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-planner-context",
        taskType="local_life",
        status="ready",
        revision=3,
        goal="判断星河咖啡是否适合安静聊天",
        facts=[
            {
                "key": "shopName",
                "value": "星河咖啡",
                "certainty": "confirmed",
                "source": "user",
            }
        ],
        constraints=[
            {
                "key": "evidenceSource",
                "operator": "eq",
                "value": "reviews",
                "source": "user",
            }
        ],
        unknowns=["shopId", "reviewEvidence"],
        pendingQuestions=[],
        createdAt=now,
        updatedAt=now,
    )


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


def _planned_output() -> PlannerModelOutput:
    return PlannerModelOutput(
        outcome="planned",
        steps=[
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
    )


def _planner_reply(payload: dict | str, *, tool_name: str = PLANNER_SUBMISSION_TOOL_NAME):
    arguments = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    call = SimpleNamespace(
        id="planner-call",
        function=SimpleNamespace(name=tool_name, arguments=arguments),
    )
    message = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client(create_mock: AsyncMock):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_mock),
        )
    )


class PlannerContextTests(unittest.TestCase):
    def test_builds_revision_bound_context_from_task_and_candidate_tools(self):
        context = build_planner_context(
            _state(),
            "请根据真实评论判断",
            [_review_tool_schema()],
        )

        payload = context.model_dump(by_alias=True, mode="json")
        self.assertEqual(payload["taskId"], "task-planner-context")
        self.assertEqual(payload["taskRevision"], 3)
        self.assertEqual(payload["facts"][0]["value"], "星河咖啡")
        self.assertEqual(payload["unknowns"], ["shopId", "reviewEvidence"])
        self.assertEqual(
            payload["candidateTools"][0]["name"],
            "search_shop_reviews",
        )
        self.assertEqual(
            payload["candidateTools"][0]["expectedOutputContracts"],
            ["requiresReviewEvidence"],
        )
        self.assertNotIn("activePlan", payload)

    def test_context_is_detached_from_mutable_state_and_schema_lists(self):
        state = _state()
        schema = _review_tool_schema()
        context = build_planner_context(state, "继续判断", [schema])

        state.facts[0].value = "另一家咖啡店"
        state.unknowns.append("newUnknown")
        schema["function"]["parameters"]["required"].clear()

        self.assertEqual(context.facts[0].value, "星河咖啡")
        self.assertEqual(context.unknowns, ("shopId", "reviewEvidence"))
        self.assertEqual(
            context.candidate_tools[0].parameters["required"],
            ["query", "shopName"],
        )

    def test_context_rejects_duplicate_candidate_tool_names(self):
        schema = _review_tool_schema()

        with self.assertRaises(ValidationError) as context:
            build_planner_context(_state(), "继续判断", [schema, schema])

        self.assertIn("候选工具名称不能重复", str(context.exception))

    def test_context_rejects_malformed_tool_schema(self):
        with self.assertRaises(ValueError) as context:
            build_planner_context(
                _state(),
                "继续判断",
                [{"type": "function", "function": {"name": "broken"}}],
            )

        self.assertIn("缺少parameters对象", str(context.exception))

    def test_context_requires_non_empty_current_user_message(self):
        with self.assertRaises(ValidationError):
            PlannerContext(
                taskId="task-1",
                taskRevision=1,
                taskStatus="ready",
                goal="测试",
                userMessage="   ",
            )


class PlannerAcceptanceTests(unittest.TestCase):
    def test_runtime_promotes_valid_model_output_with_server_owned_fields(self):
        context = build_planner_context(
            _state(),
            "请根据真实评论判断",
            [_review_tool_schema()],
        )

        result = accept_planner_model_output(
            context,
            _planned_output(),
            plan_id_factory=lambda: "plan-server-owned",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(result.plan.plan_id, "plan-server-owned")
        self.assertEqual(result.plan.based_on_revision, 3)
        self.assertEqual(result.plan.status, "active")
        self.assertEqual(result.plan.steps[0].status, "pending")

    def test_runtime_rejects_tool_outside_candidate_menu(self):
        context = build_planner_context(
            _state(),
            "请根据真实评论判断",
            [_review_tool_schema()],
        )
        output = _planned_output()
        output.steps[0].tool_name = "get_shop_detail"

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "tool_not_allowed")

    def test_runtime_rejects_missing_required_tool_argument(self):
        context = build_planner_context(
            _state(),
            "请根据真实评论判断",
            [_review_tool_schema()],
        )
        output = _planned_output()
        output.steps[0].arguments.pop("query")
        output.steps[0].argument_sources.pop("query")

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_tool_arguments")
        self.assertIn("query", result.reason)

    def test_runtime_rejects_argument_that_disagrees_with_declared_source(self):
        context = build_planner_context(
            _state(),
            "请根据真实评论判断",
            [_review_tool_schema()],
        )
        output = _planned_output()
        output.steps[0].arguments["shopName"] = "模型虚构的商户"

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")

    def test_runtime_rejects_output_contract_without_validator(self):
        context = build_planner_context(
            _state(),
            "请根据真实评论判断",
            [_review_tool_schema()],
        )
        output = _planned_output()
        output.steps[0].expected_output = {"requiresMagicAnswer": True}

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "unsupported_expected_output")

    def test_runtime_uses_existing_pending_question_instead_of_plan(self):
        state = _state().model_copy(
            update={
                "status": "collecting_information",
                "pending_questions": ["你指的是哪一家门店？"],
            }
        )
        context = build_planner_context(
            state,
            "帮我看看那家店",
            [_review_tool_schema()],
        )

        result = accept_planner_model_output(context, _planned_output())

        self.assertEqual(result.outcome, "needs_user_input")
        self.assertEqual(result.question, "你指的是哪一家门店？")


class PlannerModelCallTests(unittest.IsolatedAsyncioTestCase):
    def _context(self) -> PlannerContext:
        return build_planner_context(
            _state(),
            "请根据真实评论判断",
            [_review_tool_schema()],
        )

    async def test_create_plan_uses_only_structured_submission_tool(self):
        payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(return_value=_planner_reply(payload))

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
            plan_id_factory=lambda: "plan-created-by-runtime",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(result.plan.plan_id, "plan-created-by-runtime")
        request = create_mock.await_args.kwargs
        self.assertEqual(
            [tool["function"]["name"] for tool in request["tools"]],
            [PLANNER_SUBMISSION_TOOL_NAME],
        )
        self.assertNotIn("search_shop_reviews", json.dumps(request["tools"]))
        self.assertEqual(
            request["tool_choice"]["function"]["name"],
            PLANNER_SUBMISSION_TOOL_NAME,
        )

    async def test_deepseek_v4_omits_unsupported_tool_choice(self):
        payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(return_value=_planner_reply(payload))

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="deepseek-v4-flash",
            plan_id_factory=lambda: "plan-v4-compatible",
        )

        self.assertEqual(result.outcome, "planned")
        request = create_mock.await_args.kwargs
        self.assertEqual(
            [tool["function"]["name"] for tool in request["tools"]],
            [PLANNER_SUBMISSION_TOOL_NAME],
        )
        self.assertNotIn("tool_choice", request)

    async def test_create_plan_repairs_invalid_output_once(self):
        valid_payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(
            side_effect=[
                _planner_reply("{not-json"),
                _planner_reply(valid_payload),
            ]
        )

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(create_mock.await_count, 2)
        repair_messages = create_mock.await_args_list[1].kwargs["messages"]
        repair = json.loads(repair_messages[-1]["content"])["plannerRepairRequest"]
        self.assertEqual(repair["attempt"], 1)
        self.assertEqual(
            repair["previousToolCall"]["toolName"],
            PLANNER_SUBMISSION_TOOL_NAME,
        )
        self.assertEqual(
            repair["validationError"]["errorCode"],
            "invalid_model_output",
        )
        self.assertEqual(
            repair["validationError"]["errors"],
            [{"path": "$", "message": "arguments 不是合法 JSON"}],
        )

    async def test_create_plan_returns_failure_after_one_unsuccessful_repair(self):
        create_mock = AsyncMock(return_value=_planner_reply("{not-json"))

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_model_output")
        self.assertEqual(create_mock.await_count, 2)

    async def test_submission_schema_exposes_machine_readable_source_contract(self):
        payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(return_value=_planner_reply(payload))

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        request = create_mock.await_args.kwargs
        source_schema = request["tools"][0]["function"]["parameters"]["$defs"][
            "PlanArgumentSource"
        ]
        self.assertIn("examples", source_schema)
        self.assertEqual(
            {example["kind"] for example in source_schema["examples"]},
            {
                "task_goal",
                "task_state",
                "shopping_guide",
                "prior_step",
                "system_policy",
            },
        )
        self.assertTrue(source_schema["properties"]["kind"]["description"])
        self.assertTrue(source_schema["properties"]["reference"]["description"])
        step_schema = request["tools"][0]["function"]["parameters"]["$defs"][
            "PlanStepProposal"
        ]
        self.assertIn(
            "一一对应",
            step_schema["properties"]["argumentSources"]["description"],
        )

    async def test_initial_prompt_carries_source_matrix_and_minimal_example(self):
        payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(return_value=_planner_reply(payload))

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        prompt = create_mock.await_args.kwargs["messages"][0]["content"]
        self.assertIs(prompt, PLANNER_SYSTEM_PROMPT)
        for kind in (
            "task_goal",
            "task_state",
            "shopping_guide",
            "prior_step",
            "system_policy",
        ):
            self.assertIn(f'"{kind}"', prompt)
        self.assertIn("最小合法电商检索例子", prompt)
        self.assertIn("arguments 与 argumentSources 必须一一对应", prompt)
        for entry in PLAN_ARGUMENT_SOURCE_CONTRACT:
            self.assertIn(f'"{entry["kind"]}"', prompt)

    async def test_repair_context_echoes_invalid_payload_and_structured_errors(self):
        invalid_payload = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。"},
                    "argumentSources": {
                        "query": {"kind": "task_goal", "reference": "goal"}
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }
        valid_payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(
            side_effect=[_planner_reply(invalid_payload), _planner_reply(valid_payload)]
        )

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(create_mock.await_count, 2)
        repair_messages = create_mock.await_args_list[1].kwargs["messages"]
        repair_msg = repair_messages[-1]
        self.assertEqual(repair_msg["role"], "system")
        repair = json.loads(repair_msg["content"])["plannerRepairRequest"]
        self.assertEqual(repair["attempt"], 1)
        self.assertEqual(
            repair["previousToolCall"]["toolName"],
            PLANNER_SUBMISSION_TOOL_NAME,
        )
        self.assertEqual(repair["previousToolCall"]["arguments"], invalid_payload)
        self.assertEqual(
            repair["validationError"]["errorCode"],
            "invalid_model_output",
        )
        error = repair["validationError"]["errors"][0]
        self.assertEqual(error["path"], "steps.0.argumentSources.query")
        self.assertIn("task_goal 参数来源不需要 reference", error["message"])
        self.assertTrue(repair["instruction"])

    async def test_submission_schema_declares_additional_properties_false(self):
        payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(return_value=_planner_reply(payload))

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        parameters = create_mock.await_args.kwargs["tools"][0]["function"][
            "parameters"
        ]
        self.assertIs(
            parameters["$defs"]["PlanArgumentSource"]["additionalProperties"], False
        )
        self.assertIs(
            parameters["$defs"]["PlanStepProposal"]["additionalProperties"], False
        )

    async def test_repair_echoes_unknown_field_error_and_preserves_bogus(self):
        invalid_payload = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。"},
                    "argumentSources": {
                        "query": {"kind": "task_goal", "bogus": "silently-dropped"}
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }
        valid_payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(
            side_effect=[_planner_reply(invalid_payload), _planner_reply(valid_payload)]
        )

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(create_mock.await_count, 2)
        repair_messages = create_mock.await_args_list[1].kwargs["messages"]
        repair = json.loads(repair_messages[-1]["content"])["plannerRepairRequest"]
        self.assertEqual(repair["attempt"], 1)
        self.assertEqual(repair["previousToolCall"]["arguments"], invalid_payload)
        self.assertEqual(
            repair["validationError"]["errorCode"],
            "invalid_model_output",
        )
        target = next(
            error
            for error in repair["validationError"]["errors"]
            if error["path"] == "steps.0.argumentSources.query.bogus"
        )
        self.assertIn("Extra inputs are not permitted", target["message"])
        self.assertTrue(repair["instruction"])

    async def test_create_plan_does_not_call_model_when_task_waits_for_user(self):
        state = _state().model_copy(
            update={
                "status": "collecting_information",
                "pending_questions": ["你指的是哪一家门店？"],
            }
        )
        context = build_planner_context(
            state,
            "帮我看看那家店",
            [_review_tool_schema()],
        )
        create_mock = AsyncMock()

        result = await create_plan(
            context,
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "needs_user_input")
        self.assertEqual(result.question, "你指的是哪一家门店？")
        create_mock.assert_not_awaited()

    async def test_create_plan_converts_model_exception_to_structured_failure(self):
        create_mock = AsyncMock(side_effect=RuntimeError("provider unavailable"))

        result = await create_plan(
            self._context(),
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "model_error")
        self.assertEqual(result.reason, "Planner模型调用失败")


class PlannerPersistenceTests(unittest.IsolatedAsyncioTestCase):
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

    def _planned_result(self, state: TaskState):
        context = build_planner_context(
            state,
            "请根据真实评论判断",
            [_review_tool_schema()],
        )
        return accept_planner_model_output(
            context,
            _planned_output(),
            plan_id_factory=lambda: "plan-persisted",
        )

    async def test_persists_planned_result_with_occ_revision(self):
        ready = await self._ready_state()
        result = self._planned_result(ready)

        updated = await persist_planned_result(ready, result)

        self.assertEqual(updated.revision, ready.revision + 1)
        self.assertEqual(updated.active_plan.plan_id, "plan-persisted")
        self.assertEqual(
            updated.active_plan.based_on_revision,
            ready.revision,
        )

    async def test_new_plan_clears_previous_plan_scoped_output_receipts(self):
        ready = await self._ready_state()
        stale = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                domainStatePatch={
                    "stepOutputs": {"step-shopping-action": {
                        "taskId": ready.task_id,
                        "planId": "plan-old",
                        "stepId": "step-shopping-action",
                        "values": {"productIds": [1, 2]},
                    }},
                    "executorBlock": {"planId": "plan-old"},
                    "validationResult": {"outcome": "passed"},
                },
            ),
        )

        updated = await persist_planned_result(stale, self._planned_result(stale))

        self.assertEqual(updated.domain_state["stepOutputs"], {})
        self.assertNotIn("executorBlock", updated.domain_state)
        self.assertNotIn("validationResult", updated.domain_state)

    async def test_rejects_plan_when_task_changed_during_planning(self):
        planning_snapshot = await self._ready_state()
        result = self._planned_result(planning_snapshot)
        await update_task_state(
            planning_snapshot.task_id,
            TaskStatePatchRequest(
                expectedRevision=planning_snapshot.revision,
                actor="user",
                domainStatePatch={"changedWhilePlanning": True},
            ),
        )

        with self.assertRaises(TaskStateRevisionConflictError):
            await persist_planned_result(planning_snapshot, result)

    async def test_rejects_non_planned_result_without_mutating_task(self):
        ready = await self._ready_state()

        with self.assertRaises(ValueError):
            await persist_planned_result(
                ready,
                PlannerResult(
                    outcome="needs_user_input",
                    question="请确认门店",
                ),
            )

        latest = await task_state.get_task_state(ready.task_id)
        self.assertEqual(latest.revision, ready.revision)
        self.assertIsNone(latest.active_plan)

    async def test_persists_planner_question_and_collecting_status(self):
        ready = await self._ready_state()
        result = PlannerResult(
            outcome="needs_user_input",
            question="你指的是哪一家海底捞门店？",
        )

        updated = await persist_needs_user_input_result(ready, result)

        self.assertEqual(updated.revision, ready.revision + 1)
        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(
            updated.pending_questions,
            ["你指的是哪一家海底捞门店？"],
        )
        self.assertIsNone(updated.active_plan)

    async def test_rejects_planner_question_when_task_changed_during_planning(self):
        planning_snapshot = await self._ready_state()
        result = PlannerResult(
            outcome="needs_user_input",
            question="请确认具体门店",
        )
        await update_task_state(
            planning_snapshot.task_id,
            TaskStatePatchRequest(
                expectedRevision=planning_snapshot.revision,
                actor="user",
                goal="用户已经改成另一个商家任务",
            ),
        )

        with self.assertRaises(TaskStateRevisionConflictError):
            await persist_needs_user_input_result(planning_snapshot, result)

    async def test_rejects_non_question_result_without_mutating_task(self):
        ready = await self._ready_state()
        planned = self._planned_result(ready)

        with self.assertRaises(ValueError):
            await persist_needs_user_input_result(ready, planned)

        latest = await task_state.get_task_state(ready.task_id)
        self.assertEqual(latest.revision, ready.revision)
        self.assertEqual(latest.status, "ready")
        self.assertEqual(latest.pending_questions, [])

    async def test_persists_planning_failure_without_failing_user_task(self):
        ready = await self._ready_state()
        result = PlannerResult(
            outcome="planning_failed",
            errorCode="invalid_plan",
            reason="模型两次提交的Plan都没有通过校验",
        )

        updated = await persist_planning_failed_result(ready, result)

        self.assertEqual(updated.revision, ready.revision + 1)
        self.assertEqual(updated.status, "ready")
        self.assertIsNone(updated.active_plan)
        self.assertEqual(updated.planning_failure.error_code, "invalid_plan")
        self.assertEqual(
            updated.planning_failure.based_on_revision,
            ready.revision,
        )

    async def test_rejects_planning_failure_when_task_changed(self):
        planning_snapshot = await self._ready_state()
        result = PlannerResult(
            outcome="planning_failed",
            errorCode="invalid_plan",
            reason="旧规划失败",
        )
        await update_task_state(
            planning_snapshot.task_id,
            TaskStatePatchRequest(
                expectedRevision=planning_snapshot.revision,
                actor="user",
                domainStatePatch={"changedWhilePlanning": True},
            ),
        )

        with self.assertRaises(TaskStateRevisionConflictError):
            await persist_planning_failed_result(planning_snapshot, result)

    async def test_successful_plan_clears_previous_planning_failure(self):
        ready = await self._ready_state()
        failed_state = await persist_planning_failed_result(
            ready,
            PlannerResult(
                outcome="planning_failed",
                errorCode="invalid_plan",
                reason="第一次规划失败",
            ),
        )
        result = self._planned_result(failed_state)

        planned_state = await persist_planned_result(failed_state, result)

        self.assertIsNotNone(planned_state.active_plan)
        self.assertIsNone(planned_state.planning_failure)

    async def test_dispatches_planned_result(self):
        ready = await self._ready_state()

        updated = await persist_planner_result(
            ready,
            self._planned_result(ready),
        )

        self.assertIsNotNone(updated.active_plan)
        self.assertEqual(updated.status, "ready")

    async def test_dispatches_needs_user_input_result(self):
        ready = await self._ready_state()

        updated = await persist_planner_result(
            ready,
            PlannerResult(
                outcome="needs_user_input",
                question="你指的是哪一家星河咖啡？",
            ),
        )

        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(
            updated.pending_questions,
            ["你指的是哪一家星河咖啡？"],
        )

    async def test_dispatches_planning_failed_result(self):
        ready = await self._ready_state()

        updated = await persist_planner_result(
            ready,
            PlannerResult(
                outcome="planning_failed",
                errorCode="invalid_plan",
                reason="模型两次提交的Plan都没有通过校验",
            ),
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.planning_failure.error_code, "invalid_plan")


class PlannerPhaseTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_runs_complete_planned_path(self):
        ready = await self._ready_state()
        payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        create_mock = AsyncMock(return_value=_planner_reply(payload))

        result, updated = await run_planner_phase(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
            plan_id_factory=lambda: "plan-from-phase",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(updated.active_plan.plan_id, "plan-from-phase")
        self.assertEqual(updated.revision, ready.revision + 1)

    async def test_runs_complete_needs_user_input_path(self):
        ready = await self._ready_state()
        payload = {
            "outcome": "needs_user_input",
            "question": "你指的是哪一家星河咖啡？",
        }
        create_mock = AsyncMock(return_value=_planner_reply(payload))

        result, updated = await run_planner_phase(
            ready,
            "帮我判断那家咖啡店",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "needs_user_input")
        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(
            updated.pending_questions,
            ["你指的是哪一家星河咖啡？"],
        )

    async def test_runs_complete_planning_failed_path(self):
        ready = await self._ready_state()
        create_mock = AsyncMock(return_value=_planner_reply("{not-json"))

        result, updated = await run_planner_phase(
            ready,
            "请根据真实评论判断",
            [_review_tool_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(create_mock.await_count, 2)
        self.assertEqual(
            updated.planning_failure.error_code,
            "invalid_model_output",
        )

    async def test_rejects_old_plan_when_task_changes_during_model_call(self):
        ready = await self._ready_state()
        payload = _planned_output().model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )

        async def change_task_then_return_plan(**_kwargs):
            await update_task_state(
                ready.task_id,
                TaskStatePatchRequest(
                    expectedRevision=ready.revision,
                    actor="user",
                    goal="用户已经改成另一个商家任务",
                ),
            )
            return _planner_reply(payload)

        create_mock = AsyncMock(side_effect=change_task_then_return_plan)

        with self.assertRaises(TaskStateRevisionConflictError):
            await run_planner_phase(
                ready,
                "请根据真实评论判断",
                [_review_tool_schema()],
                client=_fake_client(create_mock),
                model="test-model",
            )

        latest = await task_state.get_task_state(ready.task_id)
        self.assertEqual(latest.goal, "用户已经改成另一个商家任务")
        self.assertIsNone(latest.active_plan)


_OS_REQ = [
    {
        "key": "os", "operator": "eq", "value": "ios",
        "unit": "enum", "priority": "hard", "source": "user",
    }
]
_TWO_REQ = [
    *_OS_REQ,
    {
        "key": "price_minor", "operator": "lte", "value": 200000,
        "unit": "CNY_MINOR", "priority": "hard", "source": "user",
    },
]


def _ecom_state(*, requirements=None, goal="想找 iOS 二手机。") -> TaskState:
    """Real-shaped used-phone TaskState carrying a validated shoppingGuide."""
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-iphone-guide",
        taskType="ecommerce_guide",
        status="ready",
        revision=3,
        goal=goal,
        facts=[],
        constraints=[
            {
                "key": "os", "operator": "eq", "value": "ios",
                "source": "user",
            }
        ],
        unknowns=[],
        pendingQuestions=[],
        domainState={
            "shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "useCases": [],
                "requirements": requirements if requirements is not None else _OS_REQ,
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            }
        },
        createdAt=now,
        updatedAt=now,
    )


def _non_ecommerce_state() -> TaskState:
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-local-life",
        taskType="local_life",
        status="ready",
        revision=3,
        goal="判断星河咖啡是否适合安静聊天",
        facts=[
            {
                "key": "shopName",
                "value": "星河咖啡",
                "certainty": "confirmed",
                "source": "user",
            }
        ],
        constraints=[],
        unknowns=[],
        pendingQuestions=[],
        createdAt=now,
        updatedAt=now,
    )


def _non_ecommerce_state_with_guide(task_type: str) -> TaskState:
    """A non-ecommerce task that still carries a format-valid shopping guide.

    The guide is complete and would validate as a ShoppingGuideState; the
    production contract must never expose it for any task type other than
    ecommerce_guide.  The goal matches the ecommerce goal so that a claim of
    shopping-guide sources reaches the source-missing check rather than failing
    earlier on the task_goal equality.
    """
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId=f"task-cross-domain-{task_type}",
        taskType=task_type,
        status="ready",
        revision=3,
        goal="想找 iOS 二手机。",
        facts=[],
        constraints=[],
        unknowns=[],
        pendingQuestions=[],
        domainState={
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
        createdAt=now,
        updatedAt=now,
    )


def _search_products_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "检索手机、笔记本或耳机。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string", "enum": ["手机", "笔记本", "耳机"]},
                    "brand": {"type": "string"},
                    "requirements": {"type": "array"},
                },
                "required": ["query", "category"],
            },
        },
    }


def _get_product_details_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "读取商品详情与证据。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "productIds": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["productIds"],
            },
        },
    }


def _compare_products_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "compare_products",
            "description": "比较两个商品并返回字段证据。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "productIds": {"type": "array", "items": {"type": "integer"}},
                    "category": {"type": "string", "enum": ["phone"]},
                    "requirements": {"type": "array"},
                },
                "required": ["productIds", "category", "requirements"],
            },
        },
    }


def _comparison_output() -> PlannerModelOutput:
    return PlannerModelOutput.model_validate({
        "outcome": "planned",
        "steps": [{
            "stepId": "step-compare",
            "description": "比较用户指定的两台手机",
            "toolName": "compare_products",
            "arguments": {
                "productIds": [1105898, 2613960],
                "category": "phone",
                "requirements": _OS_REQ,
            },
            "argumentSources": {
                "productIds": {"kind": "shopping_guide", "reference": "comparedIds"},
                "category": {"kind": "shopping_guide", "reference": "categoryCode"},
                "requirements": {"kind": "shopping_guide", "reference": "requirements"},
            },
            "expectedOutput": {"requiresGuideDecision": True},
        }],
    })


def _shopping_output(
    *,
    query="想找 iOS 二手机。",
    category="手机",
    requirements=None,
    query_source="task_goal",
    category_source="shopping_guide",
    category_reference="category",
    requirements_source="shopping_guide",
    requirements_reference="requirements",
    extra_arguments=None,
    extra_sources=None,
) -> PlannerModelOutput:
    """Build a PlannerModelOutput for a search_products step.

    Legal defaults use the full goal for ``query`` (task_goal) and the
    server-validated shopping-guide source for ``category``/``requirements``.
    """
    arguments = {"query": query, "category": category}
    if requirements is not None:
        arguments["requirements"] = requirements
    if extra_arguments:
        arguments.update(extra_arguments)
    argument_sources = {
        "query": {"kind": query_source},
        "category": {"kind": category_source, "reference": category_reference},
    }
    if requirements is not None:
        argument_sources["requirements"] = {
            "kind": requirements_source,
            "reference": requirements_reference,
        }
    if extra_sources:
        argument_sources.update(extra_sources)
    return PlannerModelOutput(
        outcome="planned",
        steps=[
            {
                "stepId": "step-1",
                "description": "检索iOS二手机",
                "toolName": "search_products",
                "arguments": arguments,
                "argumentSources": argument_sources,
                "expectedOutput": {"requiresProductCandidates": True},
            }
        ],
    )


class ShoppingGuidePlannerContractTests(unittest.TestCase):
    """E2E-STAGE5-PLANNER-SOURCE-CONTRACT-001: exact repro + legal + attack matrix."""

    def _context(self, *, requirements=None, state=None):
        state = state or _ecom_state(requirements=requirements)
        return build_planner_context(state, state.goal, [_search_products_schema()])

    def test_exact_repro_still_rejected_no_relaxation(self):
        # The real smoke failure: model rewrote the goal into a shortened query
        # while declaring task_goal. Exact-equality contract must not be relaxed.
        context = self._context()
        output = _shopping_output(query="iOS 二手机")

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")
        self.assertIn("query", result.reason)

    def test_legal_corrected_payload_accepted(self):
        # Full goal verbatim + server shopping-guide category/requirements.
        context = self._context()
        output = _shopping_output(requirements=_OS_REQ)

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planned")
        step = result.plan.steps[0]
        self.assertEqual(step.arguments["query"], "想找 iOS 二手机。")
        self.assertEqual(step.arguments["category"], "手机")
        self.assertEqual(step.arguments["requirements"], _OS_REQ)

    def test_internal_category_code_is_not_accepted(self):
        # Model must use the server-mapped tool label, never the internal code.
        context = self._context()
        output = _shopping_output(category="phone")

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")

    def test_category_value_tamper_rejected(self):
        context = self._context()
        output = _shopping_output(category="耳机")

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")
        self.assertIn("category", result.reason)

    def test_requirements_value_tamper_rejected(self):
        context = self._context()
        output = _shopping_output(
            requirements=[dict(_OS_REQ[0], value="android")]
        )

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")
        self.assertIn("requirements", result.reason)

    def test_requirements_field_tamper_rejected(self):
        context = self._context()
        output = _shopping_output(
            requirements=[dict(_OS_REQ[0], unit="GB")]
        )

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")

    def test_extra_requirement_rejected(self):
        extra = [
            dict(_OS_REQ[0]),
            {
                "key": "price_minor", "operator": "lte", "value": 200000,
                "unit": "CNY_MINOR", "priority": "soft",
                "source": "inferred:编造",
            },
        ]
        context = self._context()
        output = _shopping_output(requirements=extra)

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")

    def test_requirements_order_tamper_rejected(self):
        context = self._context(requirements=_TWO_REQ)
        output = _shopping_output(requirements=[_TWO_REQ[1], _TWO_REQ[0]])

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")

    def test_two_requirement_legal_order_accepted(self):
        context = self._context(requirements=_TWO_REQ)
        output = _shopping_output(requirements=_TWO_REQ)

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planned")

    def test_forged_brand_rejected_at_model_boundary(self):
        # shopping_guide/brand is not a fixed reference → the structured output
        # itself fails validation, before any resolver runs.
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。", "category": "手机", "brand": "苹果"},
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "category": {"kind": "shopping_guide", "reference": "category"},
                        "brand": {"kind": "shopping_guide", "reference": "brand"},
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }

        with self.assertRaises(ValidationError):
            PlannerModelOutput.model_validate(raw)

    def test_forged_brand_via_task_state_rejected(self):
        context = self._context()
        output = _shopping_output(
            extra_arguments={"brand": "苹果"},
            extra_sources={"brand": {"kind": "task_state", "reference": "facts.brand"}},
        )

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")
        self.assertIn("brand", result.reason)

    def test_cross_domain_shopping_guide_source_rejected(self):
        # Non-ecommerce task never receives shopping-guide sources. The goal
        # check must pass first so the failure is the missing shopping-guide
        # source, not a query rewrite.
        state = _non_ecommerce_state()
        context = self._context(state=state)
        output = _shopping_output(query=state.goal)

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")
        self.assertIn("购物导购来源中不存在", result.reason)

    def test_local_life_with_valid_guide_never_exposes_sources(self):
        # Codex blocking finding: a format-valid guide on a non-ecommerce task
        # must not become shopping-guide sources in the direct Planner path.
        state = _non_ecommerce_state_with_guide("local_life")
        context = self._context(state=state)

        self.assertIsNone(context.shopping_guide_sources)
        result = accept_planner_model_output(
            context, _shopping_output(requirements=_OS_REQ)
        )
        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")
        self.assertIn("购物导购来源中不存在", result.reason)

    def test_unknown_task_type_with_valid_guide_never_exposes_sources(self):
        state = _non_ecommerce_state_with_guide("custom_domain")
        context = self._context(state=state)

        self.assertIsNone(context.shopping_guide_sources)
        result = accept_planner_model_output(
            context, _shopping_output(requirements=_OS_REQ)
        )
        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")


# Frozen Smoke-003 planner submissions (trace modelCall 1 / 2), byte-for-byte.
# They must keep failing closed at the PlannerModelOutput boundary exactly as
# they did in the real run — the documentation/examples added by this task must
# not relax the after-validator contract.
SMOKE_003_SUBMISSION_1 = {
    "outcome": "planned",
    "steps": [
        {
            "stepId": "step-search-ios-phones",
            "description": "检索 iOS 二手机商品候选",
            "toolName": "search_products",
            "arguments": {
                "query": "想找 iOS 二手机。",
                "category": "手机",
                "requirements": [
                    {
                        "key": "os",
                        "operator": "eq",
                        "value": "ios",
                        "unit": "enum",
                        "priority": "hard",
                        "source": "user",
                    }
                ],
                "limit": 20,
            },
            "argumentSources": {
                "query": {"kind": "task_goal"},
                "category": {"kind": "shopping_guide", "reference": "category"},
                "requirements": {
                    "kind": "shopping_guide",
                    "reference": "requirements",
                },
                "limit": {"kind": "system_policy"},
            },
            "expectedOutput": {"requiresProductCandidates": True},
        },
        {
            "stepId": "step-get-details",
            "description": "读取候选商品的详细规格与证据",
            "toolName": "get_product_details",
            "arguments": {"productIds": None},
            "argumentSources": {
                "productIds": {
                    "kind": "prior_step",
                    "reference": "step-search-ios-phones",
                }
            },
            "expectedOutput": {"requiresProductDetails": True},
        },
        {
            "stepId": "step-compare",
            "description": "对候选 iOS 手机执行确定性硬约束判定与软偏好评分，生成决选",
            "toolName": "compare_products",
            "arguments": {
                "category": "phone",
                "requirements": [
                    {
                        "key": "os",
                        "operator": "eq",
                        "value": "ios",
                        "unit": "enum",
                        "priority": "hard",
                        "source": "user",
                    }
                ],
            },
            "argumentSources": {
                "productIds": {
                    "kind": "prior_step",
                    "reference": "step-search-ios-phones",
                },
                "category": {"kind": "system_policy"},
                "requirements": {
                    "kind": "shopping_guide",
                    "reference": "requirements",
                },
            },
            "expectedOutput": {"requiresGuideDecision": True},
        },
    ],
}

SMOKE_003_SUBMISSION_2 = {
    "outcome": "planned",
    "steps": [
        {
            "stepId": "step-search-ios-phones",
            "description": "检索iOS二手机商品候选",
            "toolName": "search_products",
            "arguments": {
                "query": "想找 iOS 二手机。",
                "category": "手机",
                "requirements": [
                    {
                        "key": "os",
                        "operator": "eq",
                        "value": "ios",
                        "unit": "enum",
                        "priority": "hard",
                        "source": "user",
                    }
                ],
            },
            "argumentSources": {
                "query": {"kind": "task_goal", "reference": "goal"},
                "category": {"kind": "shopping_guide", "reference": "category"},
                "requirements": {
                    "kind": "shopping_guide",
                    "reference": "requirements",
                },
            },
            "expectedOutput": {
                "requiresProductCandidates": {
                    "productIds": "array of candidate product ids"
                }
            },
        },
        {
            "stepId": "step-get-product-details",
            "description": "获取商品详情",
            "toolName": "get_product_details",
            "arguments": {
                "productIds": "{{step-search-ios-phones.output.productIds}}"
            },
            "argumentSources": {
                "productIds": {
                    "kind": "prior_step",
                    "reference": "step-search-ios-phones.productIds",
                }
            },
            "expectedOutput": {
                "requiresProductDetails": {
                    "productDetails": "detail snapshot with evidence"
                }
            },
        },
    ],
}


class PlannerArgumentSourceReferenceContractTests(unittest.TestCase):
    """E2E-STAGE5-PLANNER-SOURCE-REFERENCE-003.

    Offline contract tests: the two real Smoke-003 submissions keep failing
    closed at the PlannerModelOutput boundary, the five legal argument-source
    shapes are each exercised, every illegal form is rejected, and the
    corrected payloads pass both ``model_validate`` and
    ``accept_planner_model_output``.
    """

    def _context(self, *, requirements=None, state=None):
        state = state or _ecom_state(requirements=requirements)
        return build_planner_context(state, state.goal, [_search_products_schema()])

    def _policy_context(self, policies=None):
        state = _ecom_state()
        return build_planner_context(
            state,
            state.goal,
            [_search_products_schema()],
            system_policies=policies or {"appleBrandAllowlist": "苹果"},
        )

    @staticmethod
    def _error_paths(exc: ValidationError) -> list[str]:
        return [
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors()
        ]

    # -- Smoke-003 frozen submissions: exact-shape schema rejection ----------

    def test_smoke003_submission_1_still_rejected_with_exact_four_errors(self):
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(SMOKE_003_SUBMISSION_1)
        self.assertEqual(
            sorted(self._error_paths(ctx.exception)),
            sorted(
                [
                    "steps.0.argumentSources.limit",
                    "steps.1.argumentSources.productIds",
                    "steps.2.argumentSources.productIds",
                    "steps.2.argumentSources.category",
                ]
            ),
        )

    def test_smoke003_submission_2_still_rejected_with_exact_single_error(self):
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(SMOKE_003_SUBMISSION_2)
        self.assertEqual(
            self._error_paths(ctx.exception),
            ["steps.0.argumentSources.query"],
        )
        self.assertIn(
            "task_goal 参数来源不需要 reference",
            ctx.exception.errors()[0]["msg"],
        )

    # -- Legal corrected versions: schema + business accept both pass ---------

    def test_legal_single_step_search_products_passes_schema_and_accept(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-search",
                    "description": "检索iOS二手机商品候选",
                    "toolName": "search_products",
                    "arguments": {
                        "query": "想找 iOS 二手机。",
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
                }
            ],
        }
        output = PlannerModelOutput.model_validate(raw)
        result = accept_planner_model_output(
            self._context(requirements=_OS_REQ), output
        )
        self.assertEqual(result.outcome, "planned")
        step = result.plan.steps[0]
        self.assertEqual(step.arguments["query"], "想找 iOS 二手机。")
        self.assertEqual(step.arguments["category"], "手机")
        self.assertEqual(step.arguments["requirements"], _OS_REQ)

    def test_legal_multi_step_prior_step_reference_passes_schema_and_accept(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-search",
                    "description": "检索iOS二手机商品候选",
                    "toolName": "search_products",
                    "arguments": {
                        "query": "想找 iOS 二手机。",
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
                },
                {
                    "stepId": "step-details",
                    "description": "读取候选商品详情",
                    "toolName": "get_product_details",
                    "arguments": {"productIds": None},
                    "argumentSources": {
                        "productIds": {
                            "kind": "prior_step",
                            "reference": "step-search.productIds",
                        }
                    },
                    "expectedOutput": {"requiresProductDetails": True},
                },
            ],
        }
        state = _ecom_state()
        context = build_planner_context(
            state,
            state.goal,
            [_search_products_schema(), _get_product_details_schema()],
        )
        output = PlannerModelOutput.model_validate(raw)
        result = accept_planner_model_output(context, output)
        self.assertEqual(result.outcome, "planned")
        self.assertEqual(result.plan.steps[1].arguments["productIds"], None)

    def test_system_policy_published_key_accepted(self):
        context = self._policy_context()
        output = _shopping_output(
            extra_arguments={"brand": "苹果"},
            extra_sources={
                "brand": {
                    "kind": "system_policy",
                    "reference": "appleBrandAllowlist",
                }
            },
        )
        result = accept_planner_model_output(context, output)
        self.assertEqual(result.outcome, "planned")

    # -- Illegal forms rejected at the schema boundary -------------------------

    def test_task_goal_with_reference_rejected_at_schema(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。"},
                    "argumentSources": {
                        "query": {"kind": "task_goal", "reference": "goal"}
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(raw)
        self.assertIn("steps.0.argumentSources.query", self._error_paths(ctx.exception))

    def test_system_policy_missing_reference_rejected_at_schema(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。", "brand": "苹果"},
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "brand": {"kind": "system_policy"},
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(raw)
        self.assertIn(
            "steps.0.argumentSources.brand", self._error_paths(ctx.exception)
        )

    def test_prior_step_reference_missing_output_field_rejected(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。"},
                    "argumentSources": {"query": {"kind": "task_goal"}},
                    "expectedOutput": {"requiresProductCandidates": True},
                },
                {
                    "stepId": "step-2",
                    "description": "读取详情",
                    "toolName": "get_product_details",
                    "arguments": {"productIds": None},
                    "argumentSources": {
                        "productIds": {"kind": "prior_step", "reference": "step-1"}
                    },
                    "expectedOutput": {"requiresProductDetails": True},
                },
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(raw)
        self.assertIn(
            "steps.1.argumentSources.productIds", self._error_paths(ctx.exception)
        )

    def test_prior_step_reference_to_future_step_rejected(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-a",
                    "description": "先读取详情",
                    "toolName": "get_product_details",
                    "arguments": {"productIds": None},
                    "argumentSources": {
                        "productIds": {
                            "kind": "prior_step",
                            "reference": "step-b.productIds",
                        }
                    },
                    "expectedOutput": {"requiresProductDetails": True},
                },
                {
                    "stepId": "step-b",
                    "description": "后检索候选",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。", "category": "手机"},
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "category": {
                            "kind": "shopping_guide",
                            "reference": "category",
                        },
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                },
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(raw)
        messages = " | ".join(
            str(error["msg"]) for error in ctx.exception.errors()
        )
        self.assertIn("只能引用已经排在前面的步骤", messages)

    def test_prior_step_nonexistent_output_field_deferred_to_executor(self):
        # Format-valid, earlier-step prior_step reference passes the Planner
        # boundary; the Executor gates output-field existence
        # (prior_step_output_missing) when the plan actually runs.
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-search",
                    "description": "检索iOS二手机商品候选",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。", "category": "手机"},
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "category": {
                            "kind": "shopping_guide",
                            "reference": "category",
                        },
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                },
                {
                    "stepId": "step-details",
                    "description": "读取详情",
                    "toolName": "get_product_details",
                    "arguments": {"productIds": None},
                    "argumentSources": {
                        "productIds": {
                            "kind": "prior_step",
                            "reference": "step-search.nonexistent",
                        }
                    },
                    "expectedOutput": {"requiresProductDetails": True},
                },
            ],
        }
        state = _ecom_state()
        context = build_planner_context(
            state,
            state.goal,
            [_search_products_schema(), _get_product_details_schema()],
        )
        output = PlannerModelOutput.model_validate(raw)
        result = accept_planner_model_output(context, output)
        self.assertEqual(result.outcome, "planned")

    def test_argument_without_source_rejected_at_schema(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。", "category": "手机"},
                    "argumentSources": {"query": {"kind": "task_goal"}},
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(raw)
        messages = " | ".join(
            str(error["msg"]) for error in ctx.exception.errors()
        )
        self.assertIn("arguments 与 argumentSources 必须一一对应", messages)

    def test_extra_source_without_argument_rejected_at_schema(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。"},
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "category": {
                            "kind": "shopping_guide",
                            "reference": "category",
                        },
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(raw)
        messages = " | ".join(
            str(error["msg"]) for error in ctx.exception.errors()
        )
        self.assertIn("arguments 与 argumentSources 必须一一对应", messages)

    # -- Illegal forms rejected at the business-accept boundary ----------------

    def test_system_policy_fabricated_key_rejected(self):
        context = self._policy_context()
        output = _shopping_output(
            extra_arguments={"brand": "苹果"},
            extra_sources={
                "brand": {"kind": "system_policy", "reference": "notPublishedKey"}
            },
        )
        result = accept_planner_model_output(context, output)
        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")
        self.assertIn("notPublishedKey", result.reason)


class PlannerUnknownFieldContractTests(unittest.TestCase):
    """E2E-STAGE5-PLANNER-SOURCE-REFERENCE-004.

    ``PlanArgumentSource`` is now ``extra="forbid"``: unknown keys are rejected
    with ``extra_forbidden`` instead of being silently dropped, both when the
    source is validated directly and when it appears nested inside a
    ``PlannerModelOutput``.  The five legal shapes and the frozen Smoke-003
    fixtures keep their prior behavior.
    """

    def _context(self, *, requirements=None, state=None):
        state = state or _ecom_state(requirements=requirements)
        return build_planner_context(state, state.goal, [_search_products_schema()])

    @staticmethod
    def _error_paths(exc: ValidationError) -> list[str]:
        return [
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors()
        ]

    def test_direct_task_goal_unknown_key_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PlanArgumentSource.model_validate(
                {"kind": "task_goal", "bogus": "silently-dropped"}
            )
        errors = ctx.exception.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "extra_forbidden")
        self.assertEqual(errors[0]["loc"], ("bogus",))
        self.assertIn("Extra inputs are not permitted", errors[0]["msg"])

    def test_direct_system_policy_unknown_key_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PlanArgumentSource.model_validate(
                {
                    "kind": "system_policy",
                    "reference": "searchResultLimit",
                    "bogus": 1,
                }
            )
        errors = ctx.exception.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "extra_forbidden")
        self.assertEqual(errors[0]["loc"], ("bogus",))

    def test_nested_task_goal_unknown_key_rejected_in_planner_output(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。", "category": "手机"},
                    "argumentSources": {
                        "query": {"kind": "task_goal", "bogus": "silently-dropped"},
                        "category": {
                            "kind": "shopping_guide",
                            "reference": "category",
                        },
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(raw)
        self.assertIn(
            "steps.0.argumentSources.query.bogus", self._error_paths(ctx.exception)
        )
        target = next(
            error
            for error in ctx.exception.errors()
            if error["loc"] == ("steps", 0, "argumentSources", "query", "bogus")
        )
        self.assertEqual(target["type"], "extra_forbidden")

    def test_nested_system_policy_unknown_key_rejected_in_planner_output(self):
        raw = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "检索iOS二手机",
                    "toolName": "search_products",
                    "arguments": {"query": "想找 iOS 二手机。", "brand": "苹果"},
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "brand": {
                            "kind": "system_policy",
                            "reference": "appleBrandAllowlist",
                            "bogus": 1,
                        },
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            PlannerModelOutput.model_validate(raw)
        self.assertIn(
            "steps.0.argumentSources.brand.bogus",
            self._error_paths(ctx.exception),
        )

    def test_five_legal_sources_still_validate(self):
        for source in [
            {"kind": "task_goal"},
            {"kind": "task_state", "reference": "facts.shopName"},
            {"kind": "shopping_guide", "reference": "category"},
            {"kind": "prior_step", "reference": "step-search.productIds"},
            {"kind": "system_policy", "reference": "searchResultLimit"},
        ]:
            with self.subTest(source=source):
                self.assertIsNotNone(PlanArgumentSource.model_validate(source))


class ShoppingGuidePlannerViewTests(unittest.IsolatedAsyncioTestCase):
    """E2E-STAGE5-PLANNER-SOURCE-CONTRACT-001: view → PlannerContext source flow."""

    async def test_build_planner_context_from_view_preserves_same_sources(self):
        state = _ecom_state()
        pack = await build_context_pack(
            state, allowed_tools=["search_products"]
        )
        projector = ContextProjector(pack)
        view = projector.planner_view(
            tool_names=["search_products"],
            task_status="ready",
            user_message=state.goal,
            candidate_tool_schemas=[_search_products_schema()["function"]],
        )

        context = build_planner_context_from_view(view)
        self.assertEqual(
            context.shopping_guide_sources,
            {"category": "手机", "requirements": _OS_REQ},
        )

        result = accept_planner_model_output(context, _shopping_output())
        self.assertEqual(result.outcome, "planned")
        self.assertEqual(
            result.plan.steps[0].arguments["category"],
            "手机",
        )

    async def test_published_shopping_sources_use_deterministic_plan_without_model(self):
        state = _ecom_state()
        context = build_planner_context(
            state, state.goal, [_search_products_schema()]
        )
        create_mock = AsyncMock()

        result = await create_plan(
            context,
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(
            result.plan.steps[0].arguments,
            {"query": state.goal, "category": "手机", "requirements": _OS_REQ},
        )
        self.assertEqual(
            result.plan.steps[0].argument_sources["category"].reference,
            "category",
        )

    async def test_compare_ids_are_published_as_server_owned_source(self):
        state = _ecom_state()
        guide = state.domain_state["shoppingGuide"]
        guide["mode"] = "compare"
        guide["comparedIds"] = [1105898, 2613960]
        pack = await build_context_pack(state, allowed_tools=["compare_products"])
        projector = ContextProjector(pack)
        view = projector.planner_view(
            tool_names=["compare_products"],
            task_status="ready",
            user_message=state.goal,
            candidate_tool_schemas=[],
        )
        self.assertEqual(view.shopping_guide_sources["comparedIds"], [1105898, 2613960])
        self.assertEqual(view.shopping_guide_sources["categoryCode"], "phone")

    async def test_brand_avoidance_compiles_into_deterministic_search_plan(self):
        state = _ecom_state(requirements=[], goal="不喜欢苹果的")
        guide = state.domain_state["shoppingGuide"]
        guide["requirements"] = []
        guide["brandAvoidances"] = [{
            "values": ["apple"], "strength": "hard", "source": "user",
        }]
        context = build_planner_context(state, state.goal, [_search_products_schema()])
        create_mock = AsyncMock()

        result = await create_plan(
            context, client=_fake_client(create_mock), model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(result.plan.steps[0].arguments, {
            "query": "不喜欢苹果的",
            "category": "手机",
            "requirements": [{
                "key": "brand", "operator": "not_in", "value": ["apple"],
                "unit": "text", "priority": "hard", "source": "user",
            }],
        })
        self.assertEqual(
            result.plan.steps[0].argument_sources["requirements"].kind,
            "shopping_guide",
        )
        self.assertEqual(
            result.plan.steps[0].argument_sources["requirements"].reference,
            "requirements",
        )

    async def test_comparison_builds_one_bound_compare_step_without_model(self):
        state = _ecom_state()
        guide = state.domain_state["shoppingGuide"]
        guide["mode"] = "compare"
        guide["comparedIds"] = [1105898, 2613960]
        context = build_planner_context(
            state,
            state.goal,
            [_search_products_schema(), _compare_products_schema()],
        )
        create_mock = AsyncMock()

        result = await create_plan(
            context, client=_fake_client(create_mock), model="test-model",
        )

        self.assertEqual(result.outcome, "planned")
        self.assertEqual([step.tool_name for step in result.plan.steps], ["compare_products"])
        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(result.plan.steps[0].arguments, {
            "productIds": [1105898, 2613960],
            "category": "phone",
            "requirements": _OS_REQ,
        })

    def test_ecommerce_expected_output_is_derived_from_runtime_registry(self):
        state = _ecom_state()
        guide = state.domain_state["shoppingGuide"]
        guide["mode"] = "compare"
        guide["comparedIds"] = [1105898, 2613960]
        context = build_planner_context(
            state,
            state.goal,
            [_compare_products_schema()],
        )
        proposal = _comparison_output().model_copy(deep=True)
        proposal.steps[0].expected_output = {
            "comparisonDecision": True,
            "inventedEvidence": True,
        }

        result = accept_planner_model_output(context, proposal)

        self.assertEqual(result.outcome, "planned")
        self.assertEqual(
            result.plan.steps[0].expected_output,
            {"requiresGuideDecision": True},
        )

    def test_non_ecommerce_expected_output_remains_fail_closed(self):
        context = build_planner_context(
            _state(),
            _state().goal,
            [_review_tool_schema()],
        )
        output = PlannerModelOutput.model_validate({
            "outcome": "planned",
            "steps": [{
                "stepId": "step-review",
                "description": "查评论",
                "toolName": "search_shop_reviews",
                "arguments": {"query": context.goal, "shopName": "星河咖啡"},
                "argumentSources": {
                    "query": {"kind": "task_goal"},
                    "shopName": {"kind": "task_state", "reference": "facts.shopName"},
                },
                "expectedOutput": {"inventedEvidence": True},
            }],
        })

        result = accept_planner_model_output(context, output)

        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "unsupported_expected_output")

    async def test_view_contract_rejects_tampered_category(self):
        state = _ecom_state()
        pack = await build_context_pack(
            state, allowed_tools=["search_products"]
        )
        projector = ContextProjector(pack)
        view = projector.planner_view(
            tool_names=["search_products"],
            task_status="ready",
            user_message=state.goal,
            candidate_tool_schemas=[_search_products_schema()["function"]],
        )
        context = build_planner_context_from_view(view)

        result = accept_planner_model_output(
            context, _shopping_output(category="笔记本")
        )
        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")

    def test_planner_context_view_exposes_sources(self):
        view = PlannerContextView(
            runId="run-1",
            taskId="task-iphone-guide",
            baseContextRevision=3,
            phaseTaskRevision=4,
            contextHash="",
            goal="想找 iOS 二手机。",
            userMessage="想找 iOS 二手机。",
            taskStatus="ready",
            candidateTools=[_search_products_schema()["function"]],
            shoppingGuideSources={"category": "手机", "requirements": _OS_REQ},
        )
        self.assertEqual(view.shopping_guide_sources["category"], "手机")

    async def test_local_life_with_valid_guide_view_path_never_exposes_sources(self):
        # ContextPack gates the guide; PlannerView and PlannerContext inherit None.
        state = _non_ecommerce_state_with_guide("local_life")
        pack = await build_context_pack(
            state, allowed_tools=["search_products"]
        )
        projector = ContextProjector(pack)
        view = projector.planner_view(
            tool_names=["search_products"],
            task_status="ready",
            user_message=state.goal,
            candidate_tool_schemas=[_search_products_schema()["function"]],
        )
        self.assertIsNone(pack.shopping_guide_state)
        self.assertIsNone(view.shopping_guide_sources)

        context = build_planner_context_from_view(view)
        self.assertIsNone(context.shopping_guide_sources)
        result = accept_planner_model_output(
            context, _shopping_output(requirements=_OS_REQ)
        )
        self.assertEqual(result.outcome, "planning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")

    async def test_unknown_task_type_with_valid_guide_view_path_never_exposes_sources(self):
        state = _non_ecommerce_state_with_guide("custom_domain")
        pack = await build_context_pack(
            state, allowed_tools=["search_products"]
        )
        projector = ContextProjector(pack)
        view = projector.planner_view(
            tool_names=["search_products"],
            task_status="ready",
            user_message=state.goal,
            candidate_tool_schemas=[_search_products_schema()["function"]],
        )
        self.assertIsNone(pack.shopping_guide_state)
        self.assertIsNone(view.shopping_guide_sources)
        self.assertIsNone(build_planner_context_from_view(view).shopping_guide_sources)
