import unittest

from pydantic import ValidationError

from app import task_state
from app.planning import (
    PlanArgumentSource,
    PlanStepStatusTransitionError,
    PlannerModelOutput,
    PlannerResult,
    TaskPlan,
    transition_plan_step_status,
)
from app.task_state import (
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    TaskStateTransitionError,
    create_task_state,
    update_task_state,
)
from tests.fake_redis import FakeRedis


def _merchant_step(
    *,
    step_id: str = "step-1",
    status: str = "pending",
) -> dict:
    return {
        "stepId": step_id,
        "description": "获取目标商户的评论证据",
        "toolName": "search_shop_reviews",
        "arguments": {
            "shopName": "星河咖啡",
            "query": "星河咖啡是否适合安静聊天",
        },
        "argumentSources": {
            "shopName": {
                "kind": "task_state",
                "reference": "facts.shopName",
            },
            "query": {"kind": "task_goal"},
        },
        "expectedOutput": {
            "requiresReviewEvidence": True,
            "requiresCitations": True,
        },
        "status": status,
    }


def _merchant_plan(*, based_on_revision: int = 1, status: str = "active") -> TaskPlan:
    return TaskPlan(
        planId="plan-merchant-1",
        basedOnRevision=based_on_revision,
        status=status,
        steps=[_merchant_step(status="executed" if status == "completed" else "pending")],
    )


class PlanningModelTests(unittest.TestCase):
    def test_plan_defaults_and_serializes_public_aliases(self):
        plan = _merchant_plan()

        payload = plan.model_dump(by_alias=True, mode="json")

        self.assertEqual(plan.status, "active")
        self.assertEqual(plan.steps[0].status, "pending")
        self.assertEqual(payload["planId"], "plan-merchant-1")
        self.assertEqual(payload["basedOnRevision"], 1)
        self.assertIn("argumentSources", payload["steps"][0])

    def test_step_requires_one_source_for_every_argument(self):
        step = _merchant_step()
        step["argumentSources"].pop("query")

        with self.assertRaises(ValidationError) as context:
            TaskPlan(
                planId="plan-missing-source",
                basedOnRevision=1,
                steps=[step],
            )

        self.assertIn("缺少来源：query", str(context.exception))

    def test_plan_rejects_duplicate_step_ids(self):
        with self.assertRaises(ValidationError) as context:
            TaskPlan(
                planId="plan-duplicate",
                basedOnRevision=1,
                steps=[_merchant_step(), _merchant_step()],
            )

        self.assertIn("stepId 不能重复", str(context.exception))

    def test_step_can_only_reference_an_earlier_step(self):
        detail_step = {
            "stepId": "step-2",
            "description": "读取商户详情",
            "toolName": "get_shop_detail",
            "arguments": {"shopId": None},
            "argumentSources": {
                "shopId": {
                    "kind": "prior_step",
                    "reference": "step-3.shopId",
                }
            },
            "expectedOutput": {"requiresShopDetail": True},
        }

        with self.assertRaises(ValidationError) as context:
            TaskPlan(
                planId="plan-forward-reference",
                basedOnRevision=1,
                steps=[_merchant_step(), detail_step],
            )

        self.assertIn("只能引用已经排在前面的步骤", str(context.exception))

    def test_completed_plan_requires_all_steps_executed(self):
        with self.assertRaises(ValidationError) as context:
            TaskPlan(
                planId="plan-not-finished",
                basedOnRevision=1,
                status="completed",
                steps=[_merchant_step(status="pending")],
            )

        self.assertIn("所有步骤都必须是 executed", str(context.exception))

    def test_step_status_must_follow_runtime_transition(self):
        plan = _merchant_plan()

        with self.assertRaises(PlanStepStatusTransitionError):
            transition_plan_step_status(plan, "step-1", "executed")

        executing = transition_plan_step_status(plan, "step-1", "executing")
        executed = transition_plan_step_status(executing, "step-1", "executed")
        self.assertEqual(executed.steps[0].status, "executed")

    def test_planner_result_accepts_exactly_one_planned_payload(self):
        result = PlannerResult(outcome="planned", plan=_merchant_plan())

        self.assertEqual(result.outcome, "planned")
        self.assertIsNotNone(result.plan)
        self.assertIsNone(result.question)
        self.assertIsNone(result.error_code)

    def test_planned_result_requires_plan_and_rejects_error_fields(self):
        with self.assertRaises(ValidationError) as missing_plan:
            PlannerResult(outcome="planned")
        self.assertIn("必须包含plan", str(missing_plan.exception))

        with self.assertRaises(ValidationError) as mixed_payload:
            PlannerResult(
                outcome="planned",
                plan=_merchant_plan(),
                reason="不应与Plan同时出现",
            )
        self.assertIn("不能包含question或错误信息", str(mixed_payload.exception))

    def test_needs_user_input_requires_only_question(self):
        result = PlannerResult(
            outcome="needs_user_input",
            question="你指的是哪一家海底捞门店？",
        )
        self.assertEqual(result.question, "你指的是哪一家海底捞门店？")

        with self.assertRaises(ValidationError) as mixed_payload:
            PlannerResult(
                outcome="needs_user_input",
                question="请确认门店",
                plan=_merchant_plan(),
            )
        self.assertIn("只能包含question", str(mixed_payload.exception))

    def test_planning_failed_requires_structured_error_without_plan(self):
        result = PlannerResult(
            outcome="planning_failed",
            errorCode="invalid_plan",
            reason="PlanStep引用了不存在的工具",
        )

        payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
        self.assertEqual(payload["errorCode"], "invalid_plan")
        self.assertNotIn("plan", payload)

        with self.assertRaises(ValidationError) as missing_reason:
            PlannerResult(
                outcome="planning_failed",
                errorCode="invalid_plan",
            )
        self.assertIn("必须包含errorCode和reason", str(missing_reason.exception))

    def test_model_output_contains_only_route_content(self):
        step = _merchant_step()
        step.pop("status")
        output = PlannerModelOutput(outcome="planned", steps=[step])

        payload = output.model_dump(by_alias=True, mode="json")
        self.assertEqual(payload["outcome"], "planned")
        self.assertNotIn("status", payload["steps"][0])
        self.assertNotIn("planId", payload)
        self.assertNotIn("basedOnRevision", payload)
        self.assertEqual(output.steps[0].accept().status, "pending")

    def test_model_output_rejects_runtime_owned_fields(self):
        step = _merchant_step()
        with self.assertRaises(ValidationError) as step_status:
            PlannerModelOutput(outcome="planned", steps=[step])
        self.assertIn("status", str(step_status.exception))

        valid_step = _merchant_step()
        valid_step.pop("status")
        with self.assertRaises(ValidationError) as plan_identity:
            PlannerModelOutput(
                outcome="planned",
                steps=[valid_step],
                planId="model-invented-plan",
            )
        self.assertIn("planId", str(plan_identity.exception))

    def test_model_output_needs_user_input_is_not_a_fake_plan(self):
        output = PlannerModelOutput(
            outcome="needs_user_input",
            question="你指的是哪一家海底捞门店？",
        )
        self.assertEqual(output.steps, [])

        step = _merchant_step()
        step.pop("status")
        with self.assertRaises(ValidationError) as mixed_payload:
            PlannerModelOutput(
                outcome="needs_user_input",
                question="请确认门店",
                steps=[step],
            )
        self.assertIn("不能包含步骤", str(mixed_payload.exception))

    def test_model_cannot_declare_runtime_planning_failure(self):
        with self.assertRaises(ValidationError):
            PlannerModelOutput(
                outcome="planning_failed",
                question="模型不能宣布运行时失败",
            )


class PlanningTaskStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def test_agent_can_persist_plan_with_same_occ_revision(self):
        created = await create_task_state(
            TaskStateCreateRequest(goal="判断星河咖啡是否适合安静聊天")
        )
        plan = _merchant_plan(based_on_revision=created.revision)

        updated = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                activePlan=plan,
            ),
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.active_plan.plan_id, "plan-merchant-1")
        self.assertEqual(updated.active_plan.based_on_revision, 1)

    async def test_new_plan_rejects_old_generation_revision_even_when_occ_matches(self):
        created = await create_task_state(TaskStateCreateRequest(goal="测试Plan版本"))
        latest = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                addUnknowns=["shopId"],
            ),
        )

        with self.assertRaises(TaskStateTransitionError) as context:
            await update_task_state(
                latest.task_id,
                TaskStatePatchRequest(
                    expectedRevision=latest.revision,
                    actor="agent",
                    activePlan=_merchant_plan(based_on_revision=created.revision),
                ),
            )

        self.assertIn("basedOnRevision", str(context.exception))

    async def test_new_plan_must_start_active_with_pending_steps(self):
        created = await create_task_state(TaskStateCreateRequest(goal="测试新Plan状态"))

        with self.assertRaises(TaskStateTransitionError) as context:
            await update_task_state(
                created.task_id,
                TaskStatePatchRequest(
                    expectedRevision=created.revision,
                    actor="agent",
                    activePlan=_merchant_plan(status="completed"),
                ),
            )

        self.assertIn("所有PlanStep必须是pending", str(context.exception))

    async def test_existing_plan_cannot_skip_step_status_transition(self):
        created = await create_task_state(TaskStateCreateRequest(goal="测试步骤状态推进"))
        persisted = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                activePlan=_merchant_plan(based_on_revision=created.revision),
            ),
        )
        proposed = persisted.active_plan.model_copy(
            update={
                "steps": [
                    persisted.active_plan.steps[0].model_copy(
                        update={"status": "executed"}
                    )
                ]
            }
        )

        with self.assertRaises(TaskStateTransitionError) as context:
            await update_task_state(
                persisted.task_id,
                TaskStatePatchRequest(
                    expectedRevision=persisted.revision,
                    actor="agent",
                    activePlan=proposed,
                ),
            )

        self.assertIn("pending 转换到 executed", str(context.exception))

    def test_user_actor_cannot_replace_active_plan(self):
        with self.assertRaises(ValidationError) as context:
            TaskStatePatchRequest(
                expectedRevision=1,
                actor="user",
                activePlan=_merchant_plan(),
            )

        self.assertIn("只能由 agent 或 system 更新", str(context.exception))


class ShoppingGuideArgumentSourceTests(unittest.TestCase):
    """E2E-STAGE5-PLANNER-SOURCE-CONTRACT-001: explicit shopping-guide sources."""

    def test_allows_only_fixed_category_and_requirements_references(self):
        for reference in ("category", "requirements"):
            source = PlanArgumentSource(kind="shopping_guide", reference=reference)
            self.assertEqual(source.kind, "shopping_guide")
            self.assertEqual(source.reference, reference)

    def test_rejects_any_fabricated_shopping_guide_reference(self):
        # A whole-object or nested path must fail at the model boundary, before
        # any resolver runs — never an open domainState path.
        for reference in (
            "brand",
            "useCases",
            "shoppingGuide",
            "domainState.shoppingGuide.category",
            "requirements.0.key",
            "candidateIds",
        ):
            with self.assertRaises(ValidationError) as context:
                PlanArgumentSource(kind="shopping_guide", reference=reference)
            self.assertIn(
                "shopping_guide 参数来源只允许固定引用",
                str(context.exception),
            )

    def test_shopping_guide_source_requires_a_reference(self):
        with self.assertRaises(ValidationError):
            PlanArgumentSource(kind="shopping_guide", reference=None)

    def test_schema_examples_do_not_publish_a_fabricated_policy_key(self):
        schema = PlanArgumentSource.model_json_schema()
        serialized = str(schema)

        self.assertNotIn("searchResultLimit", serialized)
        examples = schema.get("examples", [])
        policy_example = next(
            example
            for example in examples
            if example.get("kind") == "system_policy"
        )
        self.assertEqual(policy_example["reference"], "<publishedPolicyKey>")
