import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import task_state
from app.executor import run_executor_step
from app.planning import TaskPlan
from app.replanner import (
    REPLANNER_SUBMISSION_TOOL_NAME,
    ReplannerModelOutput,
    accept_replanner_model_output,
    build_replanner_context,
    build_replanner_context_from_view,
    build_replanner_messages,
    create_replan,
    persist_replanner_result,
    run_replanner_phase,
    should_run_replanner,
)
from app.schemas import ToolTrace
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    TaskStateRevisionConflictError,
    create_task_state,
    update_task_state,
)
from app.validator import run_validator_phase
from tests.two_stage_ranking_fixtures import two_stage_search_detail
from tests.fake_redis import FakeRedis


def _review_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_shop_reviews",
            "description": "检索指定商户的评论证据。",
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


def _knowledge_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "从统一知识库检索评论证据。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def _replanned_output() -> ReplannerModelOutput:
    return ReplannerModelOutput(
        outcome="replanned",
        steps=[
            {
                "stepId": "recovery-step-1",
                "description": "改用统一知识库检索",
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
    )


def _replanner_reply():
    payload = _replanned_output().model_dump(by_alias=True, mode="json")
    call = SimpleNamespace(
        id="replanner-call",
        function=SimpleNamespace(
            name=REPLANNER_SUBMISSION_TOOL_NAME,
            arguments=json.dumps(payload, ensure_ascii=False),
        ),
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[call])
            )
        ]
    )


def _fake_client(create_mock: AsyncMock):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_mock),
        )
    )


class ReplannerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _insufficient_state(self, *, task_type="local_life", domain_state=None):
        created = await create_task_state(
            TaskStateCreateRequest(
                goal="判断星河咖啡是否适合安静聊天",
                task_type=task_type,
                domain_state=domain_state or {},
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
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        plan = TaskPlan(
            planId="plan-without-evidence",
            basedOnRevision=ready.revision,
            steps=[
                {
                    "stepId": "step-1",
                    "description": "检索指定商户评论",
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
        executed = await run_executor_step(
            planned,
            [_review_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shop_reviews",
                    ok=True,
                    detail={"count": 0, "reviews": [], "citations": []},
                )
            ),
        )
        result, failed = await run_validator_phase(executed.task_state)
        self.assertEqual(result.outcome, "insufficient_evidence")
        return failed

    async def test_builds_revision_bound_context_from_failed_plan(self):
        failed = await self._insufficient_state()

        context = build_replanner_context(
            failed,
            "继续寻找其他证据",
            [_review_schema(), _knowledge_schema()],
        )

        self.assertTrue(should_run_replanner(failed))
        self.assertEqual(context.task_revision, failed.revision)
        self.assertEqual(context.failed_plan.plan_id, "plan-without-evidence")
        self.assertEqual(context.failure.outcome, "insufficient_evidence")
        self.assertEqual(context.replan_attempt, 1)
        self.assertEqual(
            [tool.name for tool in context.candidate_tools],
            ["search_shop_reviews", "search_knowledge"],
        )

    async def test_accepts_a_different_server_validated_plan(self):
        failed = await self._insufficient_state()
        context = build_replanner_context(
            failed,
            "继续寻找其他证据",
            [_review_schema(), _knowledge_schema()],
        )

        result = accept_replanner_model_output(
            context,
            _replanned_output(),
            plan_id_factory=lambda: "plan-recovered",
        )

        self.assertEqual(result.outcome, "replanned")
        self.assertEqual(result.previous_plan_id, "plan-without-evidence")
        self.assertEqual(result.plan.plan_id, "plan-recovered")
        self.assertEqual(result.plan.based_on_revision, failed.revision)
        self.assertEqual(result.plan.steps[0].tool_name, "search_knowledge")

    async def test_rejects_a_route_that_only_changes_step_id(self):
        failed = await self._insufficient_state()
        context = build_replanner_context(
            failed,
            "再试一次",
            [_review_schema()],
        )
        unchanged = ReplannerModelOutput(
            outcome="replanned",
            steps=[
                {
                    "stepId": "renamed-step",
                    "description": "换了描述但路线未变",
                    "toolName": "search_shop_reviews",
                    "arguments": {
                        "query": failed.goal,
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

        result = accept_replanner_model_output(
            context,
            unchanged,
            plan_id_factory=lambda: "plan-renamed-only",
        )

        self.assertEqual(result.outcome, "replanning_failed")
        self.assertEqual(result.error_code, "replan_unchanged")

    async def test_persists_new_plan_and_attempt_count_with_occ(self):
        failed = await self._insufficient_state()
        context = build_replanner_context(
            failed,
            "继续寻找其他证据",
            [_knowledge_schema()],
        )
        result = accept_replanner_model_output(
            context,
            _replanned_output(),
            plan_id_factory=lambda: "plan-recovered",
        )

        updated = await persist_replanner_result(failed, result)

        self.assertEqual(updated.active_plan.plan_id, "plan-recovered")
        self.assertEqual(updated.active_plan.status, "active")
        self.assertEqual(updated.domain_state["replanAttemptCount"], 1)
        self.assertEqual(
            updated.domain_state["replannerResult"]["outcome"],
            "replanned",
        )
        self.assertFalse(should_run_replanner(updated))

    async def test_user_question_does_not_consume_replan_attempt(self):
        failed = await self._insufficient_state()
        context = build_replanner_context(
            failed,
            "继续寻找其他证据",
            [_knowledge_schema()],
        )
        result = accept_replanner_model_output(
            context,
            ReplannerModelOutput(
                outcome="needs_user_input",
                question="是否允许改用更广泛的评论知识库？",
            ),
        )

        waiting = await persist_replanner_result(failed, result)

        self.assertEqual(waiting.status, "collecting_information")
        self.assertEqual(
            waiting.pending_questions,
            ["是否允许改用更广泛的评论知识库？"],
        )
        self.assertEqual(waiting.domain_state["replanAttemptCount"], 0)

        resumed = await update_task_state(
            waiting.task_id,
            TaskStatePatchRequest(
                expectedRevision=waiting.revision,
                actor="user",
                status="ready",
                pendingQuestions=[],
            ),
        )
        self.assertTrue(should_run_replanner(resumed))

    async def test_attempt_limit_becomes_a_persisted_terminal_failure(self):
        failed = await self._insufficient_state()
        exhausted = await update_task_state(
            failed.task_id,
            TaskStatePatchRequest(
                expectedRevision=failed.revision,
                actor="system",
                domainStatePatch={"replanAttemptCount": 1},
            ),
        )
        create_mock = AsyncMock()

        result, stopped = await run_replanner_phase(
            exhausted,
            "继续",
            [_knowledge_schema()],
            client=_fake_client(create_mock),
            model="test-model",
        )

        self.assertEqual(result.outcome, "replanning_failed")
        self.assertEqual(result.error_code, "replan_attempts_exhausted")
        self.assertEqual(
            stopped.domain_state["replannerResult"]["outcome"],
            "replanning_failed",
        )
        self.assertFalse(should_run_replanner(stopped))
        create_mock.assert_not_awaited()

    async def test_replanner_result_cannot_overwrite_a_newer_revision(self):
        failed = await self._insufficient_state()
        context = build_replanner_context(
            failed,
            "继续寻找其他证据",
            [_knowledge_schema()],
        )
        result = accept_replanner_model_output(
            context,
            _replanned_output(),
            plan_id_factory=lambda: "plan-recovered",
        )
        await update_task_state(
            failed.task_id,
            TaskStatePatchRequest(
                expectedRevision=failed.revision,
                actor="system",
                domainStatePatch={"unrelatedUpdate": True},
            ),
        )

        with self.assertRaises(TaskStateRevisionConflictError):
            await persist_replanner_result(failed, result)

    async def test_calls_model_with_structured_output_and_persists_plan(self):
        failed = await self._insufficient_state()
        create_mock = AsyncMock(return_value=_replanner_reply())
        context = build_replanner_context(
            failed,
            "继续寻找其他证据",
            [_knowledge_schema()],
        )

        result = await create_replan(
            context,
            client=_fake_client(create_mock),
            model="test-model",
            plan_id_factory=lambda: "plan-recovered",
        )

        self.assertEqual(result.outcome, "replanned")
        self.assertEqual(result.plan.plan_id, "plan-recovered")
        request = create_mock.await_args.kwargs
        self.assertEqual(
            request["tool_choice"]["function"]["name"],
            REPLANNER_SUBMISSION_TOOL_NAME,
        )

    async def test_deepseek_v4_omits_unsupported_tool_choice(self):
        failed = await self._insufficient_state()
        create_mock = AsyncMock(return_value=_replanner_reply())
        context = build_replanner_context(
            failed,
            "continue with other evidence",
            [_knowledge_schema()],
        )

        result = await create_replan(
            context,
            client=_fake_client(create_mock),
            model="deepseek-v4-pro",
            plan_id_factory=lambda: "plan-v4-recovered",
        )

        self.assertEqual(result.outcome, "replanned")
        self.assertNotIn("tool_choice", create_mock.await_args.kwargs)


# ── E2E-STAGE5-PLANNER-SOURCE-CONTRACT-001 ──────────────────────────────────

_OS_HARD_REQ = {
    "key": "os", "operator": "eq", "value": "ios",
    "unit": "enum", "priority": "hard", "source": "user",
}

_GOAL = "想找 iOS 二手机。"


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
                    "requirements": {"type": "array"},
                },
                "required": ["query", "category"],
            },
        },
    }


def _search_products_schema_with_limit() -> dict:
    schema = _search_products_schema()
    schema["function"]["parameters"]["properties"]["limit"] = {
        "type": "integer",
        "minimum": 1,
    }
    return schema


_SMOKE_005_REPLANNER_PAYLOADS = [
    {
        "outcome": "replanned",
        "steps": [{
            "stepId": "step-search",
            "description": "搜索 iOS 二手机（扩大召回）",
            "toolName": "search_products",
            "arguments": {
                "query": _GOAL,
                "category": "手机",
                "requirements": [_OS_HARD_REQ],
                "limit": 20,
            },
            "argumentSources": {
                "query": {"kind": "task_goal", "reference": None},
                "category": {"kind": "shopping_guide", "reference": "category"},
                "requirements": {
                    "kind": "shopping_guide",
                    "reference": "requirements",
                },
                "limit": {
                    "kind": "system_policy",
                    "reference": "searchResultLimit",
                },
            },
            "expectedOutput": {"requiresProductCandidates": True},
        }],
    },
    {
        "outcome": "replanned",
        "steps": [{
            "stepId": "step-search-expanded",
            "description": "扩大召回再次搜索 iOS 二手机",
            "toolName": "search_products",
            "arguments": {
                "query": _GOAL,
                "category": "手机",
                "limit": 20,
                "requirements": [_OS_HARD_REQ],
            },
            "argumentSources": {
                "query": {"kind": "task_goal", "reference": None},
                "category": {"kind": "shopping_guide", "reference": "category"},
                "limit": {
                    "kind": "system_policy",
                    "reference": "searchResultLimit",
                },
                "requirements": {
                    "kind": "shopping_guide",
                    "reference": "requirements",
                },
            },
            "expectedOutput": {"requiresProductCandidates": True},
        }],
    },
]


def _replanner_payload_reply(payload: dict):
    call = SimpleNamespace(
        id="replanner-call",
        function=SimpleNamespace(
            name=REPLANNER_SUBMISSION_TOOL_NAME,
            arguments=json.dumps(payload, ensure_ascii=False),
        ),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=None, tool_calls=[call])
        )]
    )


def _used_phone_create_request() -> TaskStateCreateRequest:
    return TaskStateCreateRequest(
        goal=_GOAL,
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


class ShoppingGuideReplannerContractTests(unittest.IsolatedAsyncioTestCase):
    """The same server source contract must survive the Replanner path."""

    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def _insufficient_state(self, *, task_type="local_life", domain_state=None):
        """A local-life review task that fails the Executor+Validator gate."""
        created = await create_task_state(
            TaskStateCreateRequest(
                goal="判断星河咖啡是否适合安静聊天",
                task_type=task_type,
                domain_state=domain_state or {},
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
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        plan = TaskPlan(
            planId="plan-without-evidence",
            basedOnRevision=ready.revision,
            steps=[
                {
                    "stepId": "step-1",
                    "description": "检索指定商户评论",
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
        executed = await run_executor_step(
            planned,
            [_review_schema()],
            tool_caller=AsyncMock(
                return_value=ToolTrace(
                    tool="search_shop_reviews",
                    ok=True,
                    detail={"count": 0, "reviews": [], "citations": []},
                )
            ),
        )
        result, failed = await run_validator_phase(executed.task_state)
        self.assertEqual(result.outcome, "insufficient_evidence")
        return failed

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
                        "query": _GOAL,
                        "category": "手机",
                        "requirements": [_OS_HARD_REQ],
                    },
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "category": {"kind": "shopping_guide", "reference": "category"},
                        "requirements": {"kind": "shopping_guide", "reference": "requirements"},
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

    async def test_build_replanner_context_carries_server_sources(self):
        failed = await self._failed_used_phone_state()

        context = build_replanner_context(
            failed,
            "继续",
            [_search_products_schema()],
        )

        self.assertTrue(should_run_replanner(failed))
        self.assertEqual(context.shopping_guide_sources["category"], "手机")
        self.assertEqual(
            context.shopping_guide_sources["requirements"], [_OS_HARD_REQ]
        )

    async def test_replanner_context_feeds_planner_context_with_same_sources(self):
        failed = await self._failed_used_phone_state()
        context = build_replanner_context(
            failed,
            "继续",
            [_search_products_schema()],
        )

        from app.replanner import _planner_context_from_replanner

        planner_context = _planner_context_from_replanner(context)

        self.assertEqual(
            planner_context.shopping_guide_sources, context.shopping_guide_sources
        )
        self.assertIsNot(
            planner_context.shopping_guide_sources,
            context.shopping_guide_sources,
        )  # deepcopy, never shared mutable state

    async def test_build_replanner_context_from_view_carries_sources(self):
        failed = await self._failed_used_phone_state()
        from app.context_pack import build_context_pack
        from app.context_view import ContextProjector

        pack = await build_context_pack(failed, allowed_tools=["search_products"])
        projector = ContextProjector(pack)
        failure = failed.domain_state.get("validationResult")
        view = projector.replanner_view(
            failed_plan_summary={"planId": failed.active_plan.plan_id, "steps": [], "status": "failed"},
            failed_plan=failed.active_plan,
            failure=failure,
            failure_reason="Validator rejected: no product candidates",
            remaining_tool_names=["search_products"],
            candidate_tool_schemas=[_search_products_schema()],
            phase_task_revision=failed.revision,
        )

        context = build_replanner_context_from_view(
            view,
            candidate_tool_schemas=[_search_products_schema()],
        )

        self.assertEqual(context.task_id, failed.task_id)
        self.assertEqual(context.shopping_guide_sources["category"], "手机")
        self.assertEqual(
            context.shopping_guide_sources["requirements"], [_OS_HARD_REQ]
        )

    async def test_local_life_with_valid_guide_never_carries_sources(self):
        # Codex blocking finding: a format-valid guide on a non-ecommerce task
        # must not surface through the direct Replanner construction path.
        failed = await self._insufficient_state(
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
        context = build_replanner_context(
            failed, "继续", [_review_schema(), _knowledge_schema()]
        )

        self.assertIsNone(context.shopping_guide_sources)

    async def test_unknown_task_type_with_valid_guide_never_carries_sources(self):
        failed = await self._insufficient_state(
            task_type="custom_domain",
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
        context = build_replanner_context(
            failed, "继续", [_review_schema(), _knowledge_schema()]
        )

        self.assertIsNone(context.shopping_guide_sources)

    async def test_local_life_with_valid_guide_replanner_view_never_carries_sources(self):
        failed = await self._insufficient_state(
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
        from app.context_pack import build_context_pack
        from app.context_view import ContextProjector

        pack = await build_context_pack(
            failed, allowed_tools=["search_shop_reviews"]
        )
        projector = ContextProjector(pack)
        view = projector.replanner_view(
            failed_plan_summary={
                "planId": failed.active_plan.plan_id,
                "steps": [],
                "status": "failed",
            },
            failed_plan=failed.active_plan,
            failure=failed.domain_state.get("validationResult"),
            failure_reason="Validator rejected: no product candidates",
            remaining_tool_names=["search_shop_reviews"],
            candidate_tool_schemas=[_review_schema()],
            phase_task_revision=failed.revision,
        )
        self.assertIsNone(pack.shopping_guide_state)
        self.assertIsNone(view.shopping_guide_sources)

        context = build_replanner_context_from_view(
            view, candidate_tool_schemas=[_review_schema()]
        )
        self.assertIsNone(context.shopping_guide_sources)

    async def test_local_life_with_valid_guide_replanner_accept_boundary_fails_closed(self):
        # Even a recovery proposal that (wrongly) claims a shopping-guide source
        # must be rejected at the Replanner accept boundary for a non-ecommerce
        # task — the goal check passes, the missing source is what fails.
        failed = await self._insufficient_state(
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
        context = build_replanner_context(
            failed, "继续", [_review_schema(), _knowledge_schema()]
        )
        recovery = ReplannerModelOutput(
            outcome="replanned",
            steps=[
                {
                    "stepId": "recovery-step-1",
                    "description": "尝试把导购分类塞进本地生活任务",
                    "toolName": "search_shop_reviews",
                    "arguments": {
                        "query": context.goal,
                        "shopName": "手机",
                    },
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "shopName": {"kind": "shopping_guide", "reference": "category"},
                    },
                    "expectedOutput": {"requiresReviewEvidence": True},
                }
            ],
        )

        result = accept_replanner_model_output(
            context, recovery, plan_id_factory=lambda: "recovery-1"
        )

        self.assertEqual(result.outcome, "replanning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")

    async def test_prompt_renders_five_shapes_and_only_actual_policy_keys(self):
        failed = await self._failed_used_phone_state()
        context = build_replanner_context(
            failed,
            "继续",
            [_search_products_schema_with_limit()],
            system_policies={"maxCatalogCandidates": 20},
        )

        messages = build_replanner_messages(context)
        serialized = "\n".join(message["content"] for message in messages)

        for kind in (
            "task_goal",
            "task_state",
            "shopping_guide",
            "prior_step",
            "system_policy",
        ):
            self.assertIn(kind, serialized)
        self.assertIn('systemPolicies keys：["maxCatalogCandidates"]', serialized)
        self.assertIn('"keys": ["maxCatalogCandidates"]', serialized)
        self.assertNotIn("searchResultLimit", serialized)

        empty_context = build_replanner_context(
            failed,
            "继续",
            [_search_products_schema_with_limit()],
        )
        empty_serialized = "\n".join(
            message["content"] for message in build_replanner_messages(empty_context)
        )
        self.assertIn("systemPolicies keys：[]", empty_serialized)
        self.assertIn('"keys": []', empty_serialized)
        self.assertNotIn("searchResultLimit", empty_serialized)

    async def test_frozen_smoke_005_policy_payloads_fail_closed(self):
        failed = await self._failed_used_phone_state()
        context = build_replanner_context(
            failed,
            "继续",
            [_search_products_schema_with_limit()],
        )

        for payload in _SMOKE_005_REPLANNER_PAYLOADS:
            with self.subTest(step_id=payload["steps"][0]["stepId"]):
                output = ReplannerModelOutput.model_validate(payload)
                result = accept_replanner_model_output(context, output)
                self.assertEqual(result.outcome, "replanning_failed")
                self.assertEqual(result.error_code, "invalid_argument_source")
                self.assertEqual(result.reason, "系统策略中不存在：searchResultLimit")

    async def test_only_repair_echoes_payload_error_path_and_available_sources(self):
        failed = await self._failed_used_phone_state()
        context = build_replanner_context(
            failed,
            "继续",
            [_search_products_schema_with_limit()],
        )
        create_mock = AsyncMock(side_effect=[
            _replanner_payload_reply(_SMOKE_005_REPLANNER_PAYLOADS[0]),
            _replanner_payload_reply(_SMOKE_005_REPLANNER_PAYLOADS[1]),
        ])

        result = await create_replan(
            context,
            client=_fake_client(create_mock),
            model="offline-test-model",
        )

        self.assertEqual(result.outcome, "replanning_failed")
        self.assertEqual(result.error_code, "invalid_argument_source")
        self.assertEqual(create_mock.await_count, 2)
        second_request = create_mock.await_args_list[1].kwargs
        repair = json.loads(second_request["messages"][-1]["content"])[
            "replannerRepairRequest"
        ]
        self.assertEqual(repair["attempt"], 1)
        self.assertEqual(
            repair["previousToolCall"],
            {
                "toolName": REPLANNER_SUBMISSION_TOOL_NAME,
                "arguments": _SMOKE_005_REPLANNER_PAYLOADS[0],
            },
        )
        self.assertEqual(
            repair["validationError"]["errors"],
            [{
                "path": "steps.0.argumentSources.limit.reference",
                "message": "系统策略中不存在：searchResultLimit",
            }],
        )
        available = repair["availableArgumentSources"]
        self.assertEqual(available["system_policy"]["keys"], [])
        self.assertEqual(available["system_policy"]["references"], {})
        self.assertEqual(
            available["shopping_guide"]["references"]["category"],
            "手机",
        )
