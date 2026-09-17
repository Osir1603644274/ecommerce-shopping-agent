import asyncio
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.llm import (
    MAX_TOOL_ROUNDS,
    TASK_STATE_PLANNING_PROMPT,
    TASK_STATE_REPAIR_PROMPT,
    TASK_STATE_TOOL_SCHEMA,
    TaskStatePayloadValidationError,
    _MODEL_TASK_STATE_ITEM_CONTRACTS,
    _MODEL_TASK_STATE_REQUIRED_KEYS,
    _apply_task_state_update,
    _assert_model_task_state_schema_contract,
    _build_validated_task_state_payload,
    _deterministic_used_phone_task_state_decision,
    _enforce_tool_answer_constraints,
    _generate_pending_task_question,
    _generate_final_answer,
    _harness_stop_answer,
    _is_context_only_turn,
    _parse_task_state_arguments,
    _react_boundary_answer,
    _should_use_comparison_judge,
    _run_unified_harness_agent,
    _should_use_explicit_harness,
    _update_task_state_for_unified_harness,
    begin_agent_llm_call_span,
    classify_task_relation,
    end_agent_llm_call_span,
    run_agent,
    select_tool_schemas,
)
from app.schemas import ToolTrace
from app.settings import settings
from app.domains.ecommerce.models import (
    ShoppingGuideState,
    compiled_shopping_requirements,
)
from app.domains.ecommerce.shopping_task_state_v2 import (
    parse_shopping_task_state_v2_snapshot,
)
from app import task_state as task_state_store
from app.task_state import (
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    get_task_state,
    update_task_state,
)
from app.planning import (
    PlanStep,
    TaskPlan,
    transition_plan_status,
    transition_plan_step_status,
)
from tests.fake_redis import FakeRedis


def _make_response(content=None, tool_calls=None):
    """造一个长得像 DeepSeek 返回的假对象：response.choices[0].message.{content,tool_calls}。"""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _fake_client(create_mock):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )


async def _create_ecommerce_state_with_scope(
    request: TaskStateCreateRequest,
    candidate_ids: list[int],
):
    """Build an explicit server-owned CandidateScope fixture.

    Tests that exercise references or invalidation must not use naked legacy
    candidateIds now that V2.1 is the default authority.
    """

    task_id = f"task-scope-{request.session_id}"
    domain = dict(request.domain_state)
    guide = ShoppingGuideState.model_validate(domain["shoppingGuide"])
    guide = guide.model_copy(update={
        "candidate_ids": list(candidate_ids),
        "evidence_status": "complete",
    })
    requirements = [
        item.model_dump(by_alias=True, mode="json")
        for item in compiled_shopping_requirements(guide)
    ]
    domain.update({
        "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
        "candidateScope": {
            "scopeId": f"scope-{request.session_id}",
            "taskId": task_id,
            "sourceRevision": 1,
            "sourcePlanId": "plan-fixture",
            "sourceStepId": "step-fixture",
            "category": guide.category,
            "candidatePoolIds": list(candidate_ids),
            "rankedItemIds": list(candidate_ids),
            "visibleProductIds": list(candidate_ids),
            "requirementsSnapshot": requirements,
            "brandAvoidancesSnapshot": [
                item.model_dump(by_alias=True, mode="json")
                for item in guide.brand_avoidances
            ],
            "evidenceRefs": [f"product:{item}:validated" for item in candidate_ids],
            "createdAt": "2026-08-29T00:00:00Z",
            "status": "active",
            "invalidationReason": None,
        },
    })
    scoped_request = request.model_copy(update={"domain_state": domain})
    with patch("app.task_state._new_task_id", return_value=task_id):
        return await create_task_state(scoped_request)


# The exact first update_task_state payload the DeepSeek model submitted during
# E2E-STAGE5-RETRIEVAL-SMOKE-004: status=ready together with blocking unknowns
# and a pending question. The server invariant must reject this with zero
# persistence, and the bounded repair must correct it without relaxing the
# executability rule.
_SMOKE_004_FROZEN_ARGS = {
    "domainStatePatch": {
        "shoppingGuide": {
            "mode": "recommend",
            "category": "phone",
            "requirements": [{
                "key": "os",
                "operator": "eq",
                "value": "ios",
                "unit": "enum",
                "priority": "hard",
                "source": "user",
            }],
        }
    },
    "goal": "想找 iOS 二手机。",
    "addUnknowns": ["预算或具体用途尚不清楚"],
    "optionalShoppingQuestions": [
        {"kind": "budget"},
        {"kind": "model"},
        {"kind": "use_case"},
    ],
    "pendingQuestions": ["请问您的预算大概是多少？另外对机型、屏幕状态、电池健康等有没有特别要求？"],
    "status": "ready",
    "upsertFacts": [{
        "key": "os",
        "value": "ios",
        "certainty": "confirmed",
        "source": "user",
    }],
}

_SMOKE_004_PENDING_QUESTION = "请问您的预算大概是多少？另外对机型、屏幕状态、电池健康等有没有特别要求？"

_SMOKE_006_FROZEN_ARGS = {
    "status": "collecting_information",
    "upsertFacts": [{
        "key": "goal",
        "value": "想找 iOS 二手机",
        "source": "user",
        "certainty": "confirmed",
    }],
    "upsertConstraints": [{
        "key": "os",
        "operator": "eq",
        "value": "ios",
        "source": "user",
    }],
    "domainStatePatch": {
        "shoppingGuide": {
            "mode": "recommend",
            "category": "phone",
            "requirements": [{
                "key": "os",
                "operator": "eq",
                "value": "ios",
                "unit": "enum",
                "priority": "hard",
                "source": "user",
            }],
        }
    },
    "addUnknowns": ["目标机型或品牌", "预算范围", "用途场景"],
    "pendingQuestions": ["你好！请问您想找哪款 iOS 二手机（如 iPhone 具体型号），预算大概是多少呢？"],
}


def _smoke_006_legal_ready_args():
    args = json.loads(json.dumps(_SMOKE_006_FROZEN_ARGS, ensure_ascii=False))
    args["status"] = "ready"
    args["addUnknowns"] = []
    args["pendingQuestions"] = []
    args["optionalShoppingQuestions"] = [
        {"kind": "brand"},
        {"kind": "model"},
        {"kind": "budget"},
        {"kind": "use_case"},
    ]
    return args


def _gbk_over_latin1(value: str) -> str:
    return value.encode("gbk").decode("latin-1")


def _smoke_007_frozen_args():
    return {
        "status": "collecting_information",
        "goal": _gbk_over_latin1("想找 iOS 二手机。"),
        "upsertConstraints": [{
            "key": "os", "operator": "eq", "value": "ios", "source": "user",
        }],
        "addUnknowns": [_gbk_over_latin1("需要确定预算、型号和用途，以便进入推荐")],
        "pendingQuestions": [_gbk_over_latin1("请补充预算、型号和用途。")],
        "domainStatePatch": {
            "shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "requirements": [{
                    "key": "os", "operator": "eq", "value": "ios",
                    "unit": "enum", "priority": "hard", "source": "user",
                }],
                "evidenceStatus": "missing",
            }
        },
    }


def _smoke_004_legal_collecting_args():
    """The legal clarification-shape correction the repair should submit."""
    args = json.loads(json.dumps(_SMOKE_004_FROZEN_ARGS, ensure_ascii=False))
    args["status"] = "collecting_information"
    args.pop("optionalShoppingQuestions")
    return args


def _smoke_004_legal_ready_args():
    """Strict unified repair: optional profile questions cannot block search."""
    args = json.loads(json.dumps(_SMOKE_004_FROZEN_ARGS, ensure_ascii=False))
    args["status"] = "ready"
    args["addUnknowns"] = []
    args["pendingQuestions"] = []
    return args


def _tool_names(schemas):
    return [schema["function"]["name"] for schema in schemas]


class _AsyncChunks:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class RunAgentTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # This module exercises the legacy tool loop itself. Production defaults
        # to the unified Harness; legacy behavior must now be requested explicitly.
        self._legacy_mode = patch("app.llm.settings.agent_orchestrator_mode", "legacy")
        self._legacy_mode.start()
        self.addCleanup(self._legacy_mode.stop)
        # Mocked legacy call-shape tests must not inherit a provider model from
        # the developer's local .env. V4 compatibility is covered explicitly.
        self._model = patch("app.llm.settings.deepseek_model", "test-model")
        self._model.start()
        self.addCleanup(self._model.stop)

    def test_task_state_tool_schema_exposes_only_allowlisted_domain_key(self):
        parameters = TASK_STATE_TOOL_SCHEMA["function"]["parameters"]
        domain_patch = parameters["properties"]["domainStatePatch"]
        shopping_guide = domain_patch["properties"]["shoppingGuide"]

        self.assertFalse(parameters["additionalProperties"])
        self.assertFalse(domain_patch["additionalProperties"])
        self.assertEqual(set(domain_patch["properties"]), {"shoppingGuide"})
        self.assertFalse(shopping_guide["additionalProperties"])
        self.assertEqual(
            set(shopping_guide["properties"]),
            {
                "mode", "category", "requirements",
                "upsertRequirements", "removeRequirementKeys",
            },
        )

    async def test_failed_model_attempts_are_attributed_by_stage(self):
        fake_redis = FakeRedis()
        task_state_store._client = fake_redis
        task_state_store._task_locks.clear()
        active = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal="find a park",
                sessionId="session-call-accounting",
            )
        )
        prior = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="buy headphones",
                sessionId="session-call-accounting",
            )
        )
        failing_client = _fake_client(AsyncMock(side_effect=RuntimeError("down")))

        begin_agent_llm_call_span()
        with patch("app.llm.get_client", return_value=failing_client):
            decision = await classify_task_relation("继续", active, [active, prior])
        self.assertEqual(decision.relation, "continue_current")

        with self.assertRaisesRegex(RuntimeError, "down"):
            await _update_task_state_for_unified_harness(
                "please update this task",
                history=[],
                client=failing_client,
                task_state=active,
                on_task_state=None,
            )

        with self.assertRaisesRegex(RuntimeError, "down"):
            await _generate_final_answer(
                failing_client,
                messages=[{"role": "user", "content": "answer"}],
                tool_traces=[],
                on_answer_delta=None,
                fallback="fallback",
            )

        observed = end_agent_llm_call_span()
        self.assertEqual(
            observed["modelCalls"],
            {"task_manager": 1, "task_state": 1, "final_answer": 1},
        )
        self.assertEqual(
            observed["modelFailures"],
            {"task_manager": 1, "task_state": 1, "final_answer": 1},
        )

    async def test_deferred_paused_category_mention_does_not_resume_or_start_task(self):
        active = SimpleNamespace(
            task_id="headphones-task",
            task_type="ecommerce_guide",
            status="ready",
            domain_state={"shoppingGuide": {"category": "headphones"}},
        )
        paused_phone = SimpleNamespace(
            task_id="phone-task",
            task_type="ecommerce_guide",
            status="paused",
            domain_state={"shoppingGuide": {"category": "phone"}},
        )

        decision = await classify_task_relation(
            "手机任务先保留，我说继续手机时再回来",
            active,
            [active, paused_phone],
        )

        self.assertEqual(decision.relation, "continue_current")
        self.assertEqual(decision.target_task_id, active.task_id)
        self.assertEqual(decision.confidence, 1.0)

    async def test_failed_stream_creation_is_attributed_in_both_answer_modes(self):
        failing_client = _fake_client(AsyncMock(side_effect=RuntimeError("down")))
        answer_view = SimpleNamespace(
            goal="answer safely",
            validated_results=[],
            allowed_facts=[],
            unknowns=[],
            evidence_refs=[],
            answer_format={},
            long_term_memory=[],
        )
        begin_agent_llm_call_span()
        for view in (answer_view, None):
            with self.assertRaisesRegex(RuntimeError, "down"):
                await _generate_final_answer(
                    failing_client,
                    messages=[{"role": "user", "content": "answer"}],
                    tool_traces=[],
                    on_answer_delta=AsyncMock(),
                    fallback="fallback",
                    final_answer_view=view,
                )
        observed = end_agent_llm_call_span()
        self.assertEqual(observed["modelCalls"], {"final_answer": 2})
        self.assertEqual(observed["modelFailures"], {"final_answer": 2})

    def test_nested_item_schema_and_runtime_contracts_are_identical(self):
        _assert_model_task_state_schema_contract()
        parameters = TASK_STATE_TOOL_SCHEMA["function"]["parameters"]

        for field_name, (runtime_keys, runtime_required) in (
            _MODEL_TASK_STATE_ITEM_CONTRACTS.items()
        ):
            with self.subTest(field_name=field_name):
                item_schema = parameters["properties"][field_name]["items"]
                self.assertFalse(item_schema["additionalProperties"])
                self.assertEqual(set(item_schema["properties"]), runtime_keys)
                self.assertEqual(set(item_schema["required"]), runtime_required)

    async def test_fact_observed_at_and_unknown_nested_key_are_rejected_before_persistence(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal="find a park",
                sessionId="session-fact-nested-fields-rejected",
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            r"model cannot write upsertFacts\[0\] keys: factExtra, observedAt",
        ):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "upsertFacts": [{
                        "key": "district",
                        "value": "Haidian",
                        "certainty": "confirmed",
                        "source": "user",
                        "observedAt": "2000-01-01T00:00:00Z",
                        "factExtra": "schema-forbidden",
                    }],
                },
                message="find a park in Haidian",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertEqual(unchanged.status, "collecting_information")
        self.assertEqual(unchanged.facts, [])

    async def test_constraint_unknown_nested_key_is_rejected_before_persistence(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal="find a park",
                sessionId="session-constraint-nested-fields-rejected",
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            r"model cannot write upsertConstraints\[0\] keys: constraintExtra",
        ):
            await _apply_task_state_update(
                state,
                {
                    "upsertConstraints": [{
                        "key": "transportMode",
                        "operator": "eq",
                        "value": "cycling",
                        "source": "user",
                        "constraintExtra": "schema-forbidden",
                    }],
                },
                message="cycle to a park",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertEqual(unchanged.constraints, [])

    async def test_constraint_schema_required_operator_cannot_use_runtime_default(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal="find a park",
                sessionId="session-constraint-required-operator",
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            r"upsertConstraints\[0\] missing required keys: operator",
        ):
            await _apply_task_state_update(
                state,
                {
                    "upsertConstraints": [{
                        "key": "transportMode",
                        "value": "cycling",
                        "source": "user",
                    }],
                },
                message="cycle to a park",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)

    async def test_legal_fact_and_constraint_items_still_persist(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal="find a park",
                sessionId="session-legal-fact-constraint-items",
            )
        )

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "pendingQuestions": [],
                "upsertFacts": [{
                    "key": "district",
                    "value": "Haidian",
                    "certainty": "confirmed",
                    "source": "user",
                }],
                "upsertConstraints": [{
                    "key": "transportMode",
                    "operator": "eq",
                    "value": "cycling",
                    "source": "user",
                }],
            },
            message="cycle to a park in Haidian",
            on_task_state=None,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.facts[0].key, "district")
        self.assertEqual(updated.facts[0].value, "Haidian")
        self.assertEqual(updated.constraints[0].key, "transportMode")
        self.assertEqual(updated.constraints[0].operator, "eq")

    async def test_unexposed_top_level_runtime_fields_are_rejected_before_persistence(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend a used phone",
                sessionId="session-top-level-runtime-allowlist",
            )
        )

        for field, value in (
            ("activePlan", None),
            ("planningFailure", None),
            ("expectedRevision", state.revision),
            ("actor", "system"),
            ("unknownTopLevel", {"modelControlled": True}),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ValueError,
                    f"model cannot write task state keys: {field}",
                ):
                    await _apply_task_state_update(
                        state,
                        {field: value},
                        message="continue",
                        on_task_state=None,
                    )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertIsNone(unchanged.active_plan)
        self.assertIsNone(unchanged.planning_failure)

    async def test_new_ecommerce_turn_retires_completed_plan_after_validation(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="先找 iOS 二手机",
                sessionId="session-retire-terminal-plan",
            )
        )
        active_plan = TaskPlan(
            planId="plan-first-turn",
            basedOnRevision=state.revision,
            steps=[PlanStep(
                stepId="step-first-search",
                description="first search",
                toolName="search_products",
                arguments={},
                argumentSources={},
                expectedOutput={"requiresProductCandidates": True},
            )],
        )
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="system",
                status="ready",
                activePlan=active_plan,
                domainStatePatch={
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "requirements": [],
                    },
                },
            ),
        )
        executing_plan = transition_plan_step_status(
            state.active_plan, "step-first-search", "executing"
        )
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="system",
                activePlan=executing_plan,
            ),
        )
        executed_plan = transition_plan_step_status(
            state.active_plan, "step-first-search", "executed"
        )
        completed_plan = transition_plan_status(executed_plan, "completed")
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="system",
                status="ready",
                activePlan=completed_plan,
                domainStatePatch={"validationResult": {"outcome": "passed"}},
            ),
        )

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "pendingQuestions": [],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "upsertRequirements": [{
                            "key": "battery_health",
                            "operator": "in",
                            "value": ["90_plus"],
                            "unit": "enum",
                            "priority": "hard",
                            "source": "user",
                        }],
                    },
                },
            },
            message="再加上电池健康 90% 以上",
            on_task_state=None,
            require_status=True,
        )

        self.assertIsNone(updated.active_plan)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(
            updated.domain_state["shoppingGuide"]["requirements"][0]["key"],
            "battery_health",
        )

    async def test_new_ecommerce_turn_never_retires_active_plan(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="找二手机",
                sessionId="session-keep-active-plan",
            )
        )
        active_plan = TaskPlan(
            planId="plan-running",
            basedOnRevision=state.revision,
            steps=[PlanStep(
                stepId="step-running",
                description="running search",
                toolName="search_products",
                arguments={},
                argumentSources={},
                expectedOutput={"requiresProductCandidates": True},
            )],
        )
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="system",
                status="ready",
                activePlan=active_plan,
            ),
        )

        payload, _ = _build_validated_task_state_payload(
            state,
            {"status": "ready", "pendingQuestions": []},
            message="继续",
            require_status=True,
        )

        self.assertNotIn("activePlan", payload)

    async def test_server_owned_shopping_guide_fields_are_rejected_before_merge(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        original_guide = {
            "mode": "recommend",
            "category": "phone",
            "requirements": [],
            "candidateIds": [],
            "comparedIds": [],
            "evidenceStatus": "missing",
        }
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend a used phone",
                sessionId="session-shopping-runtime-fields-rejected",
                domainState={"shoppingGuide": original_guide},
            )
        )

        for field, value in (
            ("useCases", ["model-controlled"]),
            ("candidateIds", [999]),
            ("comparedIds", [998, 999]),
            ("evidenceStatus", "complete"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ValueError,
                    f"model cannot write shoppingGuide keys: {field}",
                ):
                    await _apply_task_state_update(
                        state,
                        {"domainStatePatch": {"shoppingGuide": {field: value}}},
                        message="continue",
                        on_task_state=None,
                    )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertEqual(unchanged.domain_state["shoppingGuide"], original_guide)

    async def test_legal_shopping_constraint_change_invalidates_server_candidate_fields(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await _create_ecommerce_state_with_scope(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone",
                sessionId="session-shopping-runtime-fields-preserved",
                domainState={
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "useCases": ["daily"],
                        "requirements": [],
                        "candidateIds": [101, 102],
                        "comparedIds": [101, 102],
                        "evidenceStatus": "complete",
                    }
                },
            ),
            [101, 102],
        )

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "pendingQuestions": [],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "requirements": [{
                            "key": "os", "operator": "eq", "value": "ios",
                            "unit": "enum", "priority": "hard", "source": "user",
                        }],
                    }
                },
            },
            message="recommend an iOS phone",
            on_task_state=None,
        )

        guide = updated.domain_state["shoppingGuide"]
        self.assertEqual(guide["candidateIds"], [])
        self.assertEqual(guide["comparedIds"], [])
        self.assertEqual(guide["evidenceStatus"], "missing")
        self.assertEqual(guide["useCases"], ["daily"])
        self.assertEqual(guide["requirements"][0]["key"], "os")

    async def test_task_state_persists_all_seven_controlled_used_phone_fields(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="find a reliable used phone",
                sessionId="session-seven-controlled-used-phone-fields",
            )
        )
        requirements = [
            {"key": "os", "operator": "in", "value": ["ios"], "unit": "enum", "priority": "hard", "source": "user"},
            {"key": "battery_health", "operator": "eq", "value": "90_plus", "unit": "enum", "priority": "hard", "source": "user"},
            {"key": "screen_originality", "operator": "eq", "value": "original", "unit": "enum", "priority": "hard", "source": "user"},
            {"key": "motherboard_repair", "operator": "not_in", "value": ["repaired"], "unit": "enum", "priority": "hard", "source": "user"},
            {"key": "battery_originality", "operator": "eq", "value": "original", "unit": "enum", "priority": "soft", "source": "user"},
            {"key": "scratch_level", "operator": "eq", "value": "light", "unit": "enum", "priority": "soft", "source": "user"},
            {"key": "shell_condition", "operator": "eq", "value": "normal", "unit": "enum", "priority": "soft", "source": "user"},
        ]

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "pendingQuestions": [],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "requirements": requirements,
                    }
                },
            },
            message="iOS，主板不要修过，其他条件按要求",
            on_task_state=None,
        )

        persisted = updated.domain_state["shoppingGuide"]["requirements"]
        self.assertEqual({item["key"] for item in persisted}, {
            "os", "battery_health", "screen_originality", "motherboard_repair",
            "battery_originality", "scratch_level", "shell_condition",
        })
        self.assertEqual(
            next(item for item in persisted if item["key"] == "motherboard_repair")["operator"],
            "not_in",
        )

    async def test_task_state_rejects_invalid_new_enum_before_revision_changes(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="find a used phone",
                sessionId="session-invalid-seven-field-enum",
            )
        )

        with self.assertRaisesRegex(ValueError, "invalid shoppingGuide patch"):
            await _apply_task_state_update(
                state,
                {
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "recommend",
                            "category": "phone",
                            "requirements": [{
                                "key": "shell_condition",
                                "operator": "eq",
                                "value": "看起来还行",
                                "unit": "enum",
                                "priority": "hard",
                                "source": "user",
                            }],
                        }
                    }
                },
                message="外观看起来还行",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertEqual(unchanged.domain_state, state.domain_state)

    async def test_nested_optional_shopping_questions_are_rejected_before_merge(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone",
                sessionId="session-nested-optional-domain-rejected",
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "model cannot write domainStatePatch keys: optionalShoppingQuestions",
        ):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "recommend",
                            "category": "phone",
                            "requirements": [{
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }],
                        },
                        "optionalShoppingQuestions": [{
                            "kind": "model", "question": "which model?", "bogus": True,
                        }],
                    },
                },
                message="recommend one",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertEqual(unchanged.status, "collecting_information")
        self.assertNotIn("optionalShoppingQuestions", unchanged.domain_state)

    async def test_unknown_model_domain_key_is_rejected_without_fallback(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend a used phone",
                sessionId="session-unknown-model-domain-key",
            )
        )

        with patch("app.llm._persist_task_patch", new=AsyncMock()) as persist_mock:
            with self.assertRaisesRegex(
                ValueError,
                "model cannot write domainStatePatch keys: unknownModelKey",
            ):
                await _apply_task_state_update(
                    state,
                    {"domainStatePatch": {"unknownModelKey": {"modelControlled": True}}},
                    message="continue",
                    on_task_state=None,
                )

        persist_mock.assert_not_awaited()
        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertNotIn("unknownModelKey", unchanged.domain_state)

    async def test_non_object_model_domain_patch_is_rejected(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend a used phone",
                sessionId="session-non-object-model-domain-patch",
            )
        )

        for invalid_value in (None, [], "shoppingGuide"):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(
                    ValueError,
                    "domainStatePatch must be an object",
                ):
                    await _apply_task_state_update(
                        state,
                        {"domainStatePatch": invalid_value},
                        message="continue",
                        on_task_state=None,
                    )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)

    async def test_server_owned_domain_ledger_keys_are_rejected(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend a used phone",
                sessionId="session-server-ledger-domain-rejected",
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "model cannot write domainStatePatch keys: lastUserMessage, turnCount",
        ):
            await _apply_task_state_update(
                state,
                {
                    "domainStatePatch": {
                        "turnCount": 999,
                        "lastUserMessage": "model-forged",
                    },
                },
                message="continue",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertNotIn("turnCount", unchanged.domain_state)
        self.assertNotIn("lastUserMessage", unchanged.domain_state)

    async def test_compare_disguise_cannot_persist_nested_optional_question(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="compare two used phones",
                sessionId="session-compare-nested-domain-rejected",
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "model cannot write domainStatePatch keys: optionalShoppingQuestions, unknownModelKey",
        ):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "compare",
                            "category": "phone",
                            "requirements": [],
                        },
                        "optionalShoppingQuestions": [{
                            "kind": "model",
                            "question": "Which two candidate identities?",
                            "bogus": True,
                        }],
                        "unknownModelKey": {"modelControlled": True},
                    },
                },
                message="compare them",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertEqual(unchanged.status, "collecting_information")
        self.assertEqual(unchanged.unknowns, [])
        self.assertEqual(unchanged.pending_questions, [])
        self.assertNotIn("shoppingGuide", unchanged.domain_state)

    async def test_fallback_reuses_only_the_validated_domain_patch(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone",
                sessionId="session-fallback-validated-domain-only",
            )
        )

        persist_mock = AsyncMock(side_effect=[ValueError("injected persistence failure"), state])
        with patch("app.llm._persist_task_patch", new=persist_mock):
            updated = await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "optionalShoppingQuestions": [{"kind": "budget"}],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "recommend",
                            "category": "phone",
                            "requirements": [{
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }],
                        },
                    },
                },
                message="recommend one",
                on_task_state=None,
            )

        self.assertIs(updated, state)
        self.assertEqual(persist_mock.await_count, 2)
        fallback_payload = persist_mock.await_args_list[1].args[1]
        self.assertEqual(set(fallback_payload), {"domainStatePatch"})
        fallback_domain_patch = fallback_payload["domainStatePatch"]
        self.assertEqual(
            set(fallback_domain_patch),
            {
                "shoppingGuide", "shoppingTaskStateV2",
                "turnCount", "lastUserMessage",
            },
        )
        self.assertNotIn("optionalShoppingQuestions", fallback_domain_patch)

    async def test_occ_retry_keeps_latest_unknown_with_allowlisted_domain_patch(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone",
                sessionId="session-occ-allowlisted-domain-patch",
            )
        )
        real_update = task_state_store.update_task_state
        injected = False

        async def update_with_concurrent_unknown(task_id, patch_request):
            nonlocal injected
            if not injected:
                injected = True
                latest = await get_task_state(task_id)
                await real_update(
                    task_id,
                    TaskStatePatchRequest(
                        expectedRevision=latest.revision,
                        actor="user",
                        addUnknowns=["candidate identity"],
                        pendingQuestions=["which candidate?"],
                    ),
                )
            return await real_update(task_id, patch_request)

        with patch("app.llm.update_task_state", side_effect=update_with_concurrent_unknown):
            updated = await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "recommend",
                            "category": "phone",
                            "requirements": [{
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }],
                        },
                    },
                },
                message="recommend one",
                on_task_state=None,
            )

        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(updated.unknowns, ["candidate identity"])
        self.assertEqual(updated.pending_questions, ["which candidate?"])
        self.assertIn("shoppingGuide", updated.domain_state)
        self.assertNotIn("optionalShoppingQuestions", updated.domain_state)

    async def test_occ_retry_rebuilds_latest_server_ledger_and_guide_runtime_fields(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone",
                sessionId="session-occ-server-ledger-rebuild",
                domainState={
                    "turnCount": 3,
                    "lastUserMessage": "older turn",
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "requirements": [],
                        "candidateIds": [],
                        "comparedIds": [],
                        "evidenceStatus": "missing",
                    },
                },
            )
        )
        real_update = task_state_store.update_task_state
        injected = False

        async def update_with_concurrent_server_ledger(task_id, patch_request):
            nonlocal injected
            if not injected:
                injected = True
                latest = await get_task_state(task_id)
                await real_update(
                    task_id,
                    TaskStatePatchRequest(
                        expectedRevision=latest.revision,
                        actor="system",
                        domainStatePatch={
                            "turnCount": 40,
                            "lastUserMessage": "concurrent turn",
                            "concurrentMarker": "keep-me",
                            "shoppingGuide": {
                                "mode": "recommend",
                                "category": "phone",
                                "requirements": [],
                                "candidateIds": [],
                                "comparedIds": [],
                                "evidenceStatus": "missing",
                            },
                        },
                    ),
                )
            return await real_update(task_id, patch_request)

        with patch(
            "app.llm.update_task_state",
            side_effect=update_with_concurrent_server_ledger,
        ):
            updated = await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "requirements": [
                                {
                                    "key": "os", "operator": "eq", "value": "ios",
                                    "unit": "enum", "priority": "hard", "source": "user",
                                },
                                {
                                    "key": "battery_health", "operator": "eq",
                                    "value": "90_plus", "unit": "enum", "priority": "soft",
                                    "source": "inferred: 续航好映射为较高电池健康度偏好",
                                },
                            ],
                        },
                    },
                },
                message="current user turn",
                on_task_state=None,
            )

        self.assertEqual(updated.domain_state["turnCount"], 41)
        self.assertEqual(updated.domain_state["lastUserMessage"], "current user turn")
        self.assertEqual(updated.domain_state["concurrentMarker"], "keep-me")
        guide = updated.domain_state["shoppingGuide"]
        self.assertEqual(guide["candidateIds"], [])
        self.assertEqual(guide["comparedIds"], [])
        self.assertEqual(guide["evidenceStatus"], "missing")
        self.assertEqual(guide["requirements"][0]["value"], "ios")
        shadow = parse_shopping_task_state_v2_snapshot(
            updated.domain_state["shoppingTaskStateV2"]
        )
        battery = next(item for item in shadow.requirements if item.key == "battery_health")
        self.assertEqual(
            battery.source_provenance,
            "inferred: 续航好映射为较高电池健康度偏好",
        )

    async def test_ecommerce_followup_uses_incremental_requirements_as_single_source(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await _create_ecommerce_state_with_scope(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="先看 iOS 二手机，电池健康 90% 以上更好。",
                sessionId="session-incremental-shopping-guide",
                constraints=[
                    {"key": "os", "operator": "eq", "value": "ios", "source": "user"},
                    {
                        "key": "battery_health", "operator": "in",
                        "value": ["90_plus"], "source": "user",
                    },
                ],
                domainState={
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "requirements": [
                            {
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            },
                            {
                                "key": "battery_health", "operator": "in",
                                "value": ["90_plus"], "unit": "enum",
                                "priority": "hard", "source": "user",
                            },
                        ],
                        "candidateIds": [101],
                        "comparedIds": [],
                        "evidenceStatus": "complete",
                    }
                },
            ),
            [101],
        )
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="system",
                status="ready",
            ),
        )

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "pendingQuestions": [],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "upsertRequirements": [
                            {
                                "key": "os", "operator": "eq", "value": "android",
                                "unit": "enum", "priority": "hard", "source": "user",
                            },
                            {
                                "key": "motherboard_repair", "operator": "eq",
                                "value": "not_repaired", "unit": "enum",
                                "priority": "hard", "source": "user",
                            },
                        ],
                    }
                },
            },
            message="系统改成安卓，原偏好保留，再加主板没修过。",
            on_task_state=None,
            require_status=True,
        )

        requirements = updated.domain_state["shoppingGuide"]["requirements"]
        self.assertEqual(
            [(item["key"], item["value"]) for item in requirements],
            [
                ("os", "android"),
                ("battery_health", ["90_plus"]),
                ("motherboard_repair", "not_repaired"),
            ],
        )
        self.assertEqual(
            {item.key: item.value for item in updated.constraints},
            {
                "os": "android",
                "battery_health": ["90_plus"],
                "motherboard_repair": "not_repaired",
            },
        )
        self.assertEqual(
            updated.domain_state["shoppingGuide"]["candidateIds"], []
        )

    async def test_existing_requirements_reject_full_rewrite_before_persistence(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="找 iOS 二手机",
                sessionId="session-reject-full-requirement-rewrite",
                domainState={
                    "shoppingGuide": {
                        "mode": "recommend", "category": "phone",
                        "requirements": [{
                            "key": "os", "operator": "eq", "value": "ios",
                            "unit": "enum", "priority": "hard", "source": "user",
                        }],
                    }
                },
            )
        )
        with patch("app.llm._persist_task_patch", new=AsyncMock()) as persist_mock:
            with self.assertRaisesRegex(
                TaskStatePayloadValidationError,
                "must be updated with upsertRequirements",
            ):
                await _apply_task_state_update(
                    state,
                    {
                        "status": "ready",
                        "domainStatePatch": {
                            "shoppingGuide": {"requirements": [{
                                "key": "os", "operator": "eq", "value": "android",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }]}
                        },
                    },
                    message="改成安卓",
                    on_task_state=None,
                    require_status=True,
                )
        persist_mock.assert_not_awaited()

    async def test_negative_request_canonicalizes_complement_to_not_in(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="不要非原装屏的二手机。",
            sessionId="session-explicit-negation",
        ))
        complement = {
            "status": "ready",
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "screen_originality", "operator": "eq",
                    "value": "original", "unit": "enum",
                    "priority": "hard", "source": "user",
                }],
            }},
        }
        updated = await _apply_task_state_update(
            state, complement, message=state.goal,
            on_task_state=None, require_status=True,
        )
        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.constraints[0].operator, "not_in")
        self.assertEqual(updated.constraints[0].value, ["non_original"])

    async def test_confirmed_phone_category_cannot_remain_a_blocking_unknown(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="不要非原装屏的二手机。",
            sessionId="session-category-not-unknown",
        ))
        updated = await _apply_task_state_update(
            state,
            {
                "status": "collecting_information",
                "addUnknowns": ["需要确认目标品类（手机/笔记本/耳机）"],
                "pendingQuestions": ["您想购买哪类二手机？"],
                "domainStatePatch": {"shoppingGuide": {
                    "mode": "recommend",
                    "category": "phone",
                    "requirements": [{
                        "key": "screen_originality",
                        "operator": "not_in",
                        "value": ["non_original"],
                        "unit": "enum",
                        "priority": "hard",
                        "source": "user",
                    }],
                }},
            },
            message=state.goal,
            on_task_state=None,
            require_status=True,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.unknowns, [])
        self.assertEqual(updated.pending_questions, [])

    async def test_vague_phone_description_cannot_invent_controlled_constraints(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="我想挑一台成色好点的二手机。",
            sessionId="session-vague-condition",
        ))

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "pendingQuestions": [],
                "domainStatePatch": {"shoppingGuide": {
                    "mode": "recommend", "category": "phone",
                    "requirements": [
                        {
                            "key": "scratch_level", "operator": "in",
                            "value": ["none", "light"], "unit": "enum",
                            "priority": "hard", "source": "user",
                        },
                        {
                            "key": "shell_condition", "operator": "eq",
                            "value": "normal", "unit": "enum",
                            "priority": "hard", "source": "user",
                        },
                    ],
                }},
            },
            message=state.goal,
            on_task_state=None,
            require_status=True,
        )

        guide = updated.domain_state["shoppingGuide"]
        self.assertEqual(guide["requirements"], [])
        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(len(updated.unknowns), 1)
        self.assertEqual(len(updated.pending_questions), 1)

    async def test_comparison_dimensions_are_not_persisted_as_preferences(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="这两台二手机哪台更稳妥？请按电池、屏幕、维修和外观证据逐项比较。",
            sessionId="session-compare-dimensions-not-preferences",
            domainState={"shoppingGuide": {
                "mode": "compare", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [1105898, 2613960], "evidenceStatus": "missing",
            }},
        ))

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "domainStatePatch": {"shoppingGuide": {
                    "mode": "compare", "category": "phone",
                    "requirements": [
                        {
                            "key": "battery_originality", "operator": "eq",
                            "value": "original", "unit": "enum",
                            "priority": "soft", "source": "inferred:dimension",
                        },
                        {
                            "key": "screen_originality", "operator": "eq",
                            "value": "original", "unit": "enum",
                            "priority": "soft", "source": "inferred:dimension",
                        },
                    ],
                }},
            },
            message=state.goal,
            on_task_state=None,
            require_status=True,
        )

        self.assertEqual(
            updated.domain_state["shoppingGuide"]["requirements"], []
        )
        self.assertEqual(updated.status, "ready")

    async def test_exact_multiturn_update_retains_every_unmodified_key(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="先看电池健康 90% 以上的二手机，主板没修过更好。",
            sessionId="session-explicit-multiturn-priority",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [
                    {
                        "key": "battery_health", "operator": "eq",
                        "value": "90_plus", "unit": "enum",
                        "priority": "hard", "source": "user",
                    },
                    {
                        "key": "motherboard_repair", "operator": "eq",
                        "value": "not_repaired", "unit": "enum",
                        "priority": "soft", "source": "user",
                    },
                ],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        message = "补充一下，系统要iOS 系统，原来的偏好保留，再加上主板没修过。"
        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "domainStatePatch": {"shoppingGuide": {
                    "upsertRequirements": [
                        {
                            "key": "os", "operator": "eq", "value": "ios",
                            "unit": "enum", "priority": "hard", "source": "user",
                        },
                        {
                            "key": "motherboard_repair", "operator": "eq",
                            "value": "not_repaired", "unit": "enum",
                            "priority": "soft", "source": "inferred:user_preference",
                        },
                    ],
                }},
            },
            message=message,
            on_task_state=None,
            require_status=True,
        )

        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["os"]["priority"], "hard")
        self.assertEqual(by_key["motherboard_repair"]["priority"], "hard")
        self.assertEqual(by_key["battery_health"]["priority"], "hard")
        self.assertEqual(
            list(by_key), ["battery_health", "motherboard_repair", "os"]
        )

    async def test_public_hc09_compiles_all_comma_separated_hard_constraints(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "iOS，主板不能修过。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-v2-public-hc09",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(create_mock),
            task_state=state, on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(
            [
                (item["key"], item["value"], item["priority"])
                for item in updated.domain_state["shoppingGuide"]["requirements"]
            ],
            [
                ("os", "ios", "hard"),
                ("motherboard_repair", "not_repaired", "hard"),
            ],
        )

    async def test_clause_local_priority_does_not_soften_sibling_constraints(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "安卓，电池90%以上，屏幕要原装更好。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-v2-clause-local-priority",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["os"]["priority"], "hard")
        self.assertEqual(by_key["battery_health"]["priority"], "hard")
        self.assertEqual(by_key["screen_originality"]["priority"], "soft")

    async def test_clause_local_exclusion_coexists_with_positive_constraints(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "iOS，主板修过的不要，原装屏优先。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-v2-clause-local-exclusion",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["os"]["value"], "ios")
        self.assertEqual(by_key["screen_originality"]["priority"], "soft")
        self.assertEqual(by_key["motherboard_repair"]["operator"], "not_in")
        self.assertEqual(by_key["motherboard_repair"]["value"], ["repaired"])
        self.assertEqual(by_key["motherboard_repair"]["priority"], "hard")

    async def test_prefix_exclusion_does_not_hide_later_positive_clause(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "不要非原装屏，同时电池90%以上。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-v2-prefix-exclusion",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["screen_originality"]["operator"], "not_in")
        self.assertEqual(by_key["screen_originality"]["value"], ["non_original"])
        self.assertEqual(by_key["battery_health"]["value"], "90_plus")

    async def test_longest_surface_keeps_direct_non_original_value(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "非原装屏，非原装电池。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-v2-longest-surface",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["screen_originality"]["value"], "non_original")
        self.assertEqual(by_key["battery_originality"]["value"], "non_original")

    async def test_soft_suffix_applies_only_to_its_attribute(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "iOS和主板没修过更好。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-v2-soft-suffix-scope",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["os"]["priority"], "hard")
        self.assertEqual(by_key["motherboard_repair"]["priority"], "soft")

    async def test_shared_soft_prefix_applies_to_coordinated_attributes(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "优先电池健康90%以上和主板没修过。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-v2-shared-soft-prefix",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["battery_health"]["priority"], "soft")
        self.assertEqual(by_key["motherboard_repair"]["priority"], "soft")

    async def test_public_mt08_retains_replaces_adds_and_changes_priority(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="先看外壳正常的二手机，主板没修过更好。",
            sessionId="session-v2-public-mt08",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [
                    {
                        "key": "shell_condition", "operator": "eq",
                        "value": "normal", "unit": "enum",
                        "priority": "hard", "source": "user",
                    },
                    {
                        "key": "motherboard_repair", "operator": "eq",
                        "value": "not_repaired", "unit": "enum",
                        "priority": "soft", "source": "user",
                    },
                    {
                        "key": "os", "operator": "eq", "value": "android",
                        "unit": "enum", "priority": "hard", "source": "user",
                    },
                ],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        message = (
            "我改主意了，系统要iOS 系统，原来的偏好保留，"
            "再加上主板没修过。"
        )

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["shell_condition"]["value"], "normal")
        self.assertEqual(by_key["os"]["value"], "ios")
        self.assertEqual(by_key["motherboard_repair"]["priority"], "hard")

    async def test_explicit_from_to_replacement_uses_rhs_value(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="先看安卓原装屏二手机",
            sessionId="session-v2-explicit-rhs-replacement",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [
                    {
                        "key": "os", "operator": "eq", "value": "android",
                        "unit": "enum", "priority": "hard", "source": "user",
                    },
                    {
                        "key": "screen_originality", "operator": "eq",
                        "value": "original", "unit": "enum",
                        "priority": "hard", "source": "user",
                    },
                ],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            "系统从安卓改成iOS，其他条件保留。",
            history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["os"]["value"], "ios")
        self.assertEqual(by_key["screen_originality"]["value"], "original")

    async def test_named_preference_retention_does_not_upgrade_to_hard(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="先看安卓、原装屏优先的二手机。",
            sessionId="session-v2-named-preference-retention",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [
                    {
                        "key": "os", "operator": "eq", "value": "android",
                        "unit": "enum", "priority": "hard", "source": "user",
                    },
                    {
                        "key": "screen_originality", "operator": "eq",
                        "value": "original", "unit": "enum",
                        "priority": "soft", "source": "user",
                    },
                ],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            "系统从安卓改成iOS，原装屏偏好保留，再加主板不能修过。",
            history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["os"]["value"], "ios")
        self.assertEqual(by_key["screen_originality"]["priority"], "soft")
        self.assertEqual(by_key["motherboard_repair"]["value"], "not_repaired")

    async def test_explicit_priority_change_updates_same_key(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="主板必须没修过",
            sessionId="session-v2-priority-change",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "motherboard_repair", "operator": "eq",
                    "value": "not_repaired", "unit": "enum",
                    "priority": "hard", "source": "user",
                }],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            "主板没修过改为优先项，不再是硬条件。",
            history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        requirement = updated.domain_state["shoppingGuide"]["requirements"][0]
        self.assertEqual(requirement["key"], "motherboard_repair")
        self.assertEqual(requirement["priority"], "soft")

    async def test_exact_multiturn_removal_is_key_addressed(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="找 iOS 原装屏二手机",
            sessionId="session-v2-key-removal",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [
                    {
                        "key": "os", "operator": "eq", "value": "ios",
                        "unit": "enum", "priority": "hard", "source": "user",
                    },
                    {
                        "key": "screen_originality", "operator": "eq",
                        "value": "original", "unit": "enum",
                        "priority": "soft", "source": "user",
                    },
                ],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            "取消原装屏这个偏好，系统条件保留。",
            history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        requirements = updated.domain_state["shoppingGuide"]["requirements"]
        self.assertEqual([item["key"] for item in requirements], ["os"])

    async def test_generic_scratch_preference_removal_is_key_addressed(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="安卓、无划痕优先、外壳正常。",
            sessionId="session-v2-generic-scratch-removal",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [
                    {
                        "key": "os", "operator": "eq", "value": "android",
                        "unit": "enum", "priority": "hard", "source": "user",
                    },
                    {
                        "key": "scratch_level", "operator": "eq", "value": "none",
                        "unit": "enum", "priority": "soft", "source": "user",
                    },
                    {
                        "key": "shell_condition", "operator": "eq", "value": "normal",
                        "unit": "enum", "priority": "hard", "source": "user",
                    },
                ],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            "取消划痕偏好，再加主板不能修过。",
            history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(
            set(by_key), {"os", "shell_condition", "motherboard_repair"}
        )
        self.assertEqual(by_key["motherboard_repair"]["value"], "not_repaired")

    async def test_contradictory_os_is_not_materialized_as_a_constraint(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "iOS 或安卓都还没想好，主板不能修过。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-v2-os-contradiction",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertNotIn("os", by_key)
        self.assertEqual(by_key["motherboard_repair"]["value"], "not_repaired")

    async def test_exact_used_phone_turn_compiles_without_model_call(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="要原装屏的二手机。",
            sessionId="session-deterministic-used-phone",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            "要原装屏的二手机。",
            history=None,
            client=_fake_client(create_mock),
            task_state=state,
            on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(
            updated.domain_state["shoppingGuide"]["requirements"],
            [{
                "key": "screen_originality", "operator": "eq",
                "value": "original", "unit": "enum",
                "priority": "hard", "source": "user",
            }],
        )

    async def test_reference_substitution_retains_bound_requirements_without_model(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await _create_ecommerce_state_with_scope(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="这台先当参考，帮我找系统和电池状态不降级的替代品。",
            sessionId="session-reference-substitution",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [
                    {
                        "key": "os", "operator": "eq", "value": "android",
                        "unit": "enum", "priority": "hard",
                        "source": "system:reference_product:5356741",
                    },
                    {
                        "key": "battery_health", "operator": "eq", "value": "80_90",
                        "unit": "enum", "priority": "hard",
                        "source": "system:reference_product:5356741",
                    },
                ],
                "candidateIds": [5356741], "comparedIds": [],
                "evidenceStatus": "missing",
            }},
        ), [5356741])
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            "想换掉当前这台，但系统和电池健康不能放宽；请给几个更稳妥的替代款。",
            history=None,
            client=_fake_client(create_mock),
            task_state=state,
            on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(
            updated.domain_state["shoppingGuide"]["requirements"],
            state.domain_state["shoppingGuide"]["requirements"],
        )
        self.assertEqual(updated.domain_state["taskStateExtraction"], {
            "schemaVersion": "used-phone-task-state-extraction-decision-v1",
            "route": "deterministic_complete",
            "reason": "bound_substitution",
            "mentionedKeys": ["battery_health", "os"],
            "coveredKeys": ["battery_health", "os"],
            "uncoveredKeys": [],
            "executionKind": "deterministic",
            "modelCalled": False,
            "modelCallCount": 0,
            "llmDurationMs": 0.0,
            "repairUsed": False,
        })

    async def test_exact_android_and_battery_phrasings_compile_without_model(self):
        for index, (message, expected) in enumerate((
            ("只看安卓二手机。", ("os", "android")),
            ("电池健康要在 90% 以上。", ("battery_health", "90_plus")),
        )):
            with self.subTest(message=message):
                task_state_store._client = FakeRedis()
                task_state_store._task_locks.clear()
                state = await create_task_state(TaskStateCreateRequest(
                    taskType="ecommerce_guide", goal=message,
                    sessionId=f"session-exact-phrase-{index}",
                    domainState={"shoppingGuide": {
                        "mode": "recommend", "category": "phone",
                        "requirements": [], "candidateIds": [],
                        "comparedIds": [], "evidenceStatus": "missing",
                    }},
                ))
                create_mock = AsyncMock()
                updated = await _update_task_state_for_unified_harness(
                    message, history=None, client=_fake_client(create_mock),
                    task_state=state, on_task_state=None,
                )
                requirement = updated.domain_state["shoppingGuide"]["requirements"][0]
                self.assertEqual(create_mock.await_count, 0)
                self.assertEqual((requirement["key"], requirement["value"]), expected)

    async def test_iphone_budget_ceiling_compiles_without_model(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "推荐一台 3000 元以内的二手 iPhone"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-iphone-budget",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(create_mock),
            task_state=state, on_task_state=None,
        )

        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(by_key["os"]["value"], "ios")
        self.assertEqual(by_key["price_minor"], {
            "key": "price_minor", "operator": "lte", "value": 300000.0,
            "unit": "CNY_MINOR", "priority": "hard", "source": "user",
        })

    async def test_battery_quality_direction_compiles_as_soft_health_bands(self):
        message = "二手苹果手机中，尽量用原装屏，电池质量要好，价格两千以下，有啥推荐？"
        now_state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-vague-battery-quality",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        arguments, observation = _deterministic_used_phone_task_state_decision(
            now_state, message,
        )

        self.assertIsNotNone(arguments)
        self.assertEqual(observation["route"], "deterministic_complete")
        requirements = {
            item["key"]: item
            for item in arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"]
        }
        self.assertEqual(requirements["battery_health"], {
            "key": "battery_health",
            "operator": "in",
            "value": ["90_plus", "80_90"],
            "unit": "enum",
            "priority": "soft",
            "source": "inferred: 电池质量好映射为较高电池健康度偏好",
        })

    async def test_endurance_priority_compiles_as_ordered_soft_health_bands(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "续航好的优先"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="想要安卓、主板未维修的二手机",
            sessionId="session-endurance-priority",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        arguments, observation = _deterministic_used_phone_task_state_decision(
            state, message,
        )

        requirements = {
            item["key"]: item
            for item in arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"]
        }
        self.assertEqual(observation["route"], "deterministic_complete")
        self.assertEqual(requirements["battery_health"], {
            "key": "battery_health", "operator": "in",
            "value": ["90_plus", "80_90"], "unit": "enum",
            "priority": "soft",
            "source": "inferred: 续航好映射为较高电池健康度偏好",
        })

    async def test_apple_joint_battery_screen_quality_uses_deterministic_contract(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "苹果的，然后电池屏幕都要尽量好的"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="销量最高的手机有哪些？",
            sessionId="session-real-apple-quality",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            message,
            history=None,
            client=_fake_client(create_mock),
            task_state=state,
            on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(updated.goal, message)
        requirements = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(requirements["brand"]["value"], "apple")
        self.assertEqual(requirements["brand"]["priority"], "hard")
        self.assertEqual(requirements["battery_health"]["priority"], "soft")
        self.assertEqual(requirements["screen_originality"]["priority"], "soft")

    async def test_sales_ranking_capability_boundary_is_direct_and_model_free(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="销量最高的手机有哪些？",
            sessionId="session-real-sales-boundary",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()
        client = _fake_client(create_mock)

        updated = await _update_task_state_for_unified_harness(
            state.goal,
            history=None,
            client=client,
            task_state=state,
            on_task_state=None,
        )
        deltas: list[str] = []

        async def collect_delta(value: str) -> None:
            deltas.append(value)

        answer = await _generate_pending_task_question(
            client,
            messages=[],
            state=updated,
            on_answer_delta=collect_delta,
        )

        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(updated.status, "collecting_information")
        self.assertIn("没有销量字段", answer)
        self.assertEqual(deltas, [answer])
        self.assertEqual(
            updated.domain_state["taskStateExtraction"]["reason"],
            "unsupported_sales_ranking",
        )

    def test_latest_price_is_capability_boundary_not_fake_budget(self):
        from datetime import datetime, timezone
        from app.task_state import TaskState

        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-latest-price",
            taskType="ecommerce_guide",
            sessionId="session-latest-price",
            status="ready",
            revision=1,
            goal="苹果最新版手机 价格",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
            createdAt=now,
            updatedAt=now,
        )

        arguments, observation = _deterministic_used_phone_task_state_decision(
            state,
            state.goal,
        )
        requirements = arguments["domainStatePatch"]["shoppingGuide"][
            "upsertRequirements"
        ]

        self.assertEqual(arguments["status"], "collecting_information")
        self.assertEqual(observation["reason"], "unsupported_recency")
        self.assertEqual(
            [(item["key"], item["value"]) for item in requirements],
            [("brand", "apple")],
        )
        self.assertNotIn("price_minor", {item["key"] for item in requirements})

    def test_game_camera_question_stops_at_capability_boundary(self):
        from datetime import datetime, timezone
        from app.task_state import TaskState

        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-game-camera",
            taskType="ecommerce_guide",
            sessionId="session-game-camera",
            status="ready",
            revision=1,
            goal="这三个手机，哪个适合打游戏拍照？",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [1, 2, 3],
                "comparedIds": [], "evidenceStatus": "complete",
            }},
            createdAt=now,
            updatedAt=now,
        )

        arguments, observation = _deterministic_used_phone_task_state_decision(
            state,
            state.goal,
        )

        self.assertEqual(arguments["status"], "collecting_information")
        self.assertEqual(
            observation["reason"],
            "unsupported_game_camera_evidence",
        )
        self.assertIn("芯片性能和相机指标", arguments["pendingQuestions"][0])

    async def test_vague_quality_does_not_expand_into_six_preferences(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "有什么优质二手手机推荐？"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal=message,
            sessionId="session-vague-quality-boundary",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            message,
            history=None,
            client=_fake_client(create_mock),
            task_state=state,
            on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(
            updated.domain_state["shoppingGuide"]["requirements"],
            [],
        )
        self.assertIn("不会擅自替你展开", updated.pending_questions[0])
        self.assertEqual(
            updated.domain_state["taskStateExtraction"]["reason"],
            "ambiguous_quality_request",
        )

    async def test_postposed_explicit_negation_compiles_as_not_in(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "主板修过的不要。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-postposed-negation",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()
        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(create_mock),
            task_state=state, on_task_state=None,
        )
        requirement = updated.domain_state["shoppingGuide"]["requirements"][0]
        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(requirement["key"], "motherboard_repair")
        self.assertEqual(requirement["operator"], "not_in")
        self.assertEqual(requirement["value"], ["repaired"])

    async def test_deterministic_parser_covers_all_three_controlled_mentions(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "安卓必须满足，屏幕得是原装，原装电池更好。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-parser-coverage-three-fields",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(create_mock),
            task_state=state, on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 0)
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(set(by_key), {
            "os", "screen_originality", "battery_originality",
        })
        self.assertEqual(by_key["os"]["priority"], "hard")
        self.assertEqual(by_key["screen_originality"]["priority"], "hard")
        self.assertEqual(by_key["battery_originality"]["priority"], "soft")
        self.assertEqual(updated.domain_state["taskStateExtraction"], {
            "schemaVersion": "used-phone-task-state-extraction-decision-v1",
            "route": "deterministic_complete",
            "reason": "complete_controlled_coverage",
            "mentionedKeys": [
                "battery_originality", "os", "screen_originality",
            ],
            "coveredKeys": [
                "battery_originality", "os", "screen_originality",
            ],
            "uncoveredKeys": [],
            "executionKind": "deterministic",
            "modelCalled": False,
            "modelCallCount": 0,
            "llmDurationMs": 0.0,
            "repairUsed": False,
        })

    async def test_acceptance_clause_does_not_become_a_requirement(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "电池不是原装的不要，外壳正常，轻微划痕可以接受。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-parser-coverage-acceptance",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(set(by_key), {"battery_originality", "shell_condition"})
        self.assertEqual(by_key["battery_originality"]["operator"], "not_in")
        self.assertNotIn("scratch_level", by_key)

    async def test_named_priority_change_reuses_existing_value_without_upgrade(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="安卓与原装屏都是硬要求。",
            sessionId="session-parser-coverage-priority-only",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [
                    {"key":"os","operator":"eq","value":"android","unit":"enum","priority":"hard","source":"user"},
                    {"key":"screen_originality","operator":"eq","value":"original","unit":"enum","priority":"hard","source":"user"},
                ],
                "candidateIds": [], "comparedIds": [],
                "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            "屏幕要求降为偏好，安卓继续作为硬条件。",
            history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )
        by_key = {
            item["key"]: item
            for item in updated.domain_state["shoppingGuide"]["requirements"]
        }
        self.assertEqual(by_key["os"]["priority"], "hard")
        self.assertEqual(by_key["screen_originality"]["value"], "original")
        self.assertEqual(by_key["screen_originality"]["priority"], "soft")

    async def test_partial_controlled_parse_falls_back_to_bounded_extractor(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "安卓，电池健康大约八成半，主板不能修过。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-parser-coverage-fallback",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        model_call = _make_tool_call(
            "call-parser-coverage",
            "update_task_state",
            json.dumps({
            "status": "ready", "goal": message,
            "resolveUnknowns": [], "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "upsertRequirements": [
                    {"key":"os","operator":"eq","value":"android","unit":"enum","priority":"hard","source":"user"},
                    {"key":"battery_health","operator":"eq","value":"80_90","unit":"enum","priority":"hard","source":"user"},
                    {"key":"motherboard_repair","operator":"eq","value":"not_repaired","unit":"enum","priority":"hard","source":"user"},
                ],
                "removeRequirementKeys": [],
            }},
            }, ensure_ascii=False),
        )
        create_mock = AsyncMock(return_value=_make_response(tool_calls=[model_call]))

        with patch("app.llm.settings.deepseek_model", "deepseek-v4-flash"):
            updated = await _update_task_state_for_unified_harness(
                message, history=None, client=_fake_client(create_mock),
                task_state=state, on_task_state=None,
            )

        self.assertEqual(create_mock.await_count, 1)
        self.assertEqual(create_mock.await_args.kwargs['tool_choice']['function']['name'], 'update_task_state')
        self.assertEqual(
            {item["key"] for item in updated.domain_state["shoppingGuide"]["requirements"]},
            {"os", "battery_health", "motherboard_repair"},
        )
        receipt = dict(updated.domain_state["taskStateExtraction"])
        self.assertEqual(receipt.pop("executionKind"), "model")
        self.assertIs(receipt.pop("modelCalled"), True)
        self.assertEqual(receipt.pop("modelCallCount"), 1)
        self.assertGreaterEqual(receipt.pop("llmDurationMs"), 0)
        self.assertIs(receipt.pop("repairUsed"), False)
        self.assertEqual(receipt, {
            "schemaVersion": "used-phone-task-state-extraction-decision-v1",
            "route": "model_fallback",
            "reason": "partial_controlled_coverage",
            "mentionedKeys": ["battery_health", "motherboard_repair", "os"],
            "coveredKeys": ["motherboard_repair", "os"],
            "uncoveredKeys": ["battery_health"],
        })

    async def test_no_deterministic_signal_persists_model_fallback_observation(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "想找一台靠谱耐用、省心的二手手机。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-parser-observation-no-signal",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        model_call = _make_tool_call(
            "call-no-signal", "update_task_state",
            json.dumps({
                "status": "collecting_information",
                "addUnknowns": ["缺少明确的受控属性条件"],
                "pendingQuestions": ["系统或电池健康有什么要求？"],
                "domainStatePatch": {"shoppingGuide": {
                    "mode": "recommend", "category": "phone",
                    "upsertRequirements": [], "removeRequirementKeys": [],
                }},
            }, ensure_ascii=False),
        )
        create_mock = AsyncMock(return_value=_make_response(tool_calls=[model_call]))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(create_mock),
            task_state=state, on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 1)
        receipt = dict(updated.domain_state["taskStateExtraction"])
        self.assertEqual(receipt.pop("executionKind"), "model")
        self.assertIs(receipt.pop("modelCalled"), True)
        self.assertEqual(receipt.pop("modelCallCount"), 1)
        self.assertGreaterEqual(receipt.pop("llmDurationMs"), 0)
        self.assertIs(receipt.pop("repairUsed"), False)
        self.assertEqual(receipt, {
            "schemaVersion": "used-phone-task-state-extraction-decision-v1",
            "route": "model_fallback",
            "reason": "no_deterministic_signal",
            "mentionedKeys": [], "coveredKeys": [], "uncoveredKeys": [],
        })

    async def test_model_cannot_forge_task_state_extraction_observation(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="找二手手机",
            sessionId="session-parser-observation-forgery",
        ))

        with self.assertRaisesRegex(
            ValueError,
            "model cannot write domainStatePatch keys: taskStateExtraction",
        ):
            await _apply_task_state_update(
                state,
                {"domainStatePatch": {"taskStateExtraction": {
                    "route": "deterministic_complete",
                }}},
                message="continue",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertNotIn("taskStateExtraction", unchanged.domain_state)

    def test_used_phone_final_renderer_uses_only_validator_search_summary(self):
        from app.context_view import FinalAnswerContextView
        from app.llm import _render_validated_used_phone_answer
        from app.task_state import TaskState
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-phone-render", taskType="ecommerce_guide",
            status="ready", revision=3, goal="想找 iOS 二手机。",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "os", "operator": "eq", "value": "ios",
                    "unit": "enum", "priority": "hard", "source": "user",
                }],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }},
            createdAt=now, updatedAt=now,
        )
        view = FinalAnswerContextView(
            runId="run-phone-render", taskId=state.task_id,
            baseContextRevision=state.revision,
            phaseTaskRevision=state.revision,
            contextHash="hash-phone-render", goal=state.goal,
            answerCategory="phone",
            answerConstraints=state.domain_state["shoppingGuide"]["requirements"],
            validatedResults=[{
                "tool": "search_products", "evidence": {}, "evidenceRefs": [],
                "validationSummary": {"requiresProductCandidates": {
                    "candidateCount": 3, "productIds": [11, 22, 33],
                }},
            }],
        )

        answer = _render_validated_used_phone_answer(state, view)

        self.assertIn("3 个候选", answer)
        self.assertIn("11、22、33", answer)
        self.assertIn("iOS", answer)
        self.assertIn("未核验字段保持未知", answer)

    def test_react_final_answer_fallback_uses_validator_only_renderer(self):
        from app.context_view import FinalAnswerContextView
        from app.llm import _render_react_final_answer_fallback
        from app.task_state import TaskState
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-react-answer-fallback", taskType="ecommerce_guide",
            status="ready", revision=3, goal="找二手手机",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone", "requirements": [],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }}, createdAt=now, updatedAt=now,
        )
        view = FinalAnswerContextView(
            runId="run-react-answer-fallback", taskId=state.task_id,
            baseContextRevision=3, phaseTaskRevision=3,
            contextHash="hash-react-answer-fallback", goal=state.goal,
            answerCategory="phone", answerConstraints=[],
            validatedResults=[{
                "tool": "search_products", "evidence": {}, "evidenceRefs": [],
                "validationSummary": {"requiresProductCandidates": {
                    "candidateCount": 0, "productIds": [],
                }},
            }],
        )

        answer = _render_react_final_answer_fallback(
            state, view, "answer_zero_result_boundary"
        )

        self.assertIn("回答模型本轮超时", answer)
        self.assertIn("Validator 已放行", answer)
        self.assertIn("未找到满足这些条件的候选", answer)
        self.assertNotIn("推荐购买", answer)

        boundary = _render_react_final_answer_fallback(
            state, view, "answer_evidence_boundary"
        )
        self.assertIn("游戏帧率、散热或拍照质量测试", boundary)
        self.assertIn("不能据此判断", boundary)
        self.assertNotIn("未找到满足这些条件的候选", boundary)

    def test_used_phone_final_renderer_shows_validator_product_presentation(self):
        from app.context_view import FinalAnswerContextView
        from app.llm import _render_validated_used_phone_answer
        from app.task_state import TaskState
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-phone-card-render", taskType="ecommerce_guide",
            status="ready", revision=3, goal="推荐原装屏 iPhone",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone", "requirements": [],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }}, createdAt=now, updatedAt=now,
        )
        attributes = [
            {
                "key": key,
                "status": "known" if key in {"os", "screen_originality"} else "unknown",
                "value": (
                    "ios" if key == "os"
                    else "original" if key == "screen_originality"
                    else None
                ),
                "evidenceRef": (
                    f"product:11:attribute:{key}"
                    if key in {"os", "screen_originality"}
                    else None
                ),
            }
            for key in (
                "os", "battery_health", "screen_originality",
                "motherboard_repair", "battery_originality",
                "scratch_level", "shell_condition",
            )
        ]
        view = FinalAnswerContextView(
            runId="run-phone-card-render", taskId=state.task_id,
            baseContextRevision=3, phaseTaskRevision=3,
            contextHash="hash-phone-card-render", goal=state.goal,
            answerCategory="phone", answerConstraints=[],
            validatedResults=[{
                "tool": "search_products", "evidence": {}, "evidenceRefs": [],
                "validationSummary": {"requiresProductCandidates": {
                    "productIds": [11], "hasCompleteMatch": True,
                    "productPresentations": [{
                        "productId": 11, "title": "Apple iPhone 13 128GB",
                        "brand": "Apple", "priceMinor": None, "currency": "CNY",
                        "priceStatus": "unverified", "selectionType": "full_match",
                        "titleEvidenceRef": "product:11:title",
                        "brandEvidenceRef": "product:11:brand",
                        "priceEvidenceRef": None, "attributes": attributes,
                    }],
                }},
            }],
        )

        answer = _render_validated_used_phone_answer(state, view)

        self.assertIn("Apple iPhone 13 128GB", answer)
        self.assertIn("品牌：Apple", answer)
        self.assertIn("系统：iOS", answer)
        self.assertIn("屏幕：原装", answer)
        self.assertIn("价格：未核验", answer)

    def test_used_phone_price_unknown_is_not_confused_with_missing_lower_bound(self):
        from app.context_view import FinalAnswerContextView
        from app.llm import _render_validated_used_phone_answer
        from app.task_state import TaskState
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-phone-price-unknown", taskType="ecommerce_guide",
            status="ready", revision=3, goal="推荐 3000 元以内的二手 iPhone",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "price_minor", "operator": "lte", "value": 300000,
                    "unit": "CNY_MINOR", "priority": "hard", "source": "user",
                }],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }}, createdAt=now, updatedAt=now,
        )
        view = FinalAnswerContextView(
            runId="run-phone-price-unknown", taskId=state.task_id,
            baseContextRevision=3, phaseTaskRevision=3,
            contextHash="hash-phone-price-unknown", goal=state.goal,
            answerCategory="phone",
            answerConstraints=state.domain_state["shoppingGuide"]["requirements"],
            validatedResults=[{
                "tool": "search_products", "evidence": {}, "evidenceRefs": [],
                "validationSummary": {"requiresProductCandidates": {
                    "productIds": [11, 22, 33], "hasCompleteMatch": False,
                    "hardUnknownsByProduct": {
                        "11": ["price_minor"], "22": ["price_minor"],
                        "33": ["price_minor"],
                    },
                }},
            }],
        )

        answer = _render_validated_used_phone_answer(state, view)

        self.assertIn("预算下界已按 0 元处理", answer)
        self.assertIn("未知价格当成 0 元", answer)
        self.assertIn("11、22、33", answer)
        self.assertNotIn("price_minor", answer)

    def test_used_phone_compare_renderer_uses_validator_field_evidence(self):
        from app.context_view import FinalAnswerContextView
        from app.llm import _render_validated_used_phone_answer
        from app.task_state import TaskState
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-phone-compare-render", taskType="ecommerce_guide",
            status="ready", revision=3, goal="比较 A 和 B",
            domainState={"shoppingGuide": {
                "mode": "compare", "category": "phone", "requirements": [],
                "candidateIds": [], "comparedIds": [21, 22], "evidenceStatus": "missing",
            }}, createdAt=now, updatedAt=now,
        )
        fields = [{
            "key": "battery_health", "status": "known", "actual": "90_plus",
            "evidenceRef": "product:21:attribute:battery_health",
        }, {
            "key": "screen_originality", "status": "unknown", "actual": None,
            "evidenceRef": None,
        }]
        view = FinalAnswerContextView(
            runId="run-phone-compare-render", taskId=state.task_id,
            baseContextRevision=state.revision, phaseTaskRevision=state.revision,
            contextHash="hash-phone-compare-render", goal=state.goal,
            answerCategory="phone", answerConstraints=[],
            validatedResults=[{
                "tool": "compare_products", "evidence": {"rankedFinalists": [
                    {"productId": 21, "fieldEvidence": fields},
                    {"productId": 22, "fieldEvidence": fields},
                ]}, "evidenceRefs": ["product:21:attribute:battery_health"],
                "validationSummary": {},
            }],
        )
        answer = _render_validated_used_phone_answer(state, view)
        self.assertIn("电池健康 90%+", answer)
        self.assertIn("product:21:attribute:battery_health", answer)
        self.assertIn("屏幕：未知", answer)

    def test_comparison_reference_guard_rejects_missing_bound_third_claim(self):
        from app.context_view import FinalAnswerContextView
        from app.llm import _comparison_reference_claim_is_invalid

        view = FinalAnswerContextView(
            runId="run-reference-guard", taskId="task-reference-guard",
            baseContextRevision=3, phaseTaskRevision=3,
            contextHash="hash-reference-guard", goal="比较第一个和第三个",
            answerFormat={
                "comparisonSelection": {
                    "selectedProductIds": [11, 33],
                    "sourceDisplayOrdinals": [1, 3],
                },
            },
        )

        self.assertTrue(_comparison_reference_claim_is_invalid(
            "只找到2款完全匹配，没有第3款可比较。", view,
        ))
        self.assertFalse(_comparison_reference_claim_is_invalid(
            "下面保留原展示序号，比较第1款和第3款。", view,
        ))

    def test_used_phone_compare_renderer_never_prefers_unknown_as_zero_risk(self):
        from app.context_view import FinalAnswerContextView
        from app.llm import _render_validated_used_phone_answer
        from app.task_state import TaskState
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-phone-unknown-risk", taskType="ecommerce_guide",
            status="ready", revision=3, goal="比较 A 和 B",
            domainState={"shoppingGuide": {
                "mode": "compare", "category": "phone", "requirements": [],
                "candidateIds": [], "comparedIds": [21, 22], "evidenceStatus": "missing",
            }}, createdAt=now, updatedAt=now,
        )
        unknowns = [
            {"key": key, "status": "unknown", "actual": None, "evidenceRef": None}
            for key in (
                "battery_health", "battery_originality", "motherboard_repair",
                "os", "scratch_level", "screen_originality", "shell_condition",
            )
        ]
        known = [
            {"key": "battery_health", "status": "known", "actual": "90_plus", "evidenceRef": "product:22:attribute:battery_health"},
            {"key": "battery_originality", "status": "known", "actual": "original", "evidenceRef": "product:22:attribute:battery_originality"},
            {"key": "motherboard_repair", "status": "known", "actual": "not_repaired", "evidenceRef": "product:22:attribute:motherboard_repair"},
            {"key": "os", "status": "known", "actual": "ios", "evidenceRef": "product:22:attribute:os"},
            {"key": "scratch_level", "status": "known", "actual": "light", "evidenceRef": "product:22:attribute:scratch_level"},
            {"key": "screen_originality", "status": "known", "actual": "original", "evidenceRef": "product:22:attribute:screen_originality"},
            {"key": "shell_condition", "status": "known", "actual": "normal", "evidenceRef": "product:22:attribute:shell_condition"},
        ]
        view = FinalAnswerContextView(
            runId="run", taskId=state.task_id, baseContextRevision=3,
            phaseTaskRevision=3, contextHash="hash", goal=state.goal,
            answerCategory="phone", answerConstraints=[], validatedResults=[{
                "tool": "compare_products", "evidence": {"rankedFinalists": [
                    {"productId": 21, "fieldEvidence": unknowns},
                    {"productId": 22, "fieldEvidence": known},
                ]}, "evidenceRefs": [], "validationSummary": {},
            }],
        )
        answer = _render_validated_used_phone_answer(state, view)
        self.assertIn("商品 22 更适合作为当前选择", answer)
        self.assertNotIn("商品 21 更适合作为当前选择", answer)

    async def test_vague_used_phone_turn_still_uses_strict_model_extractor(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="想买台靠谱的二手机。",
            sessionId="session-vague-used-phone",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        call = _make_tool_call("call-state", "update_task_state", json.dumps({
            "status": "collecting_information",
            "goal": state.goal,
            "addUnknowns": ["缺少明确的二手手机筛选条件"],
            "pendingQuestions": ["请补充一个明确筛选条件。"],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend", "category": "phone",
            }},
        }, ensure_ascii=False))
        create_mock = AsyncMock(return_value=_make_response(tool_calls=[call]))

        updated = await _update_task_state_for_unified_harness(
            state.goal,
            history=None,
            client=_fake_client(create_mock),
            task_state=state,
            on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 1)
        self.assertEqual(updated.status, "collecting_information")

    async def test_gaming_title_discovery_searches_without_claiming_performance(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="找标题提到打游戏的手机",
            sessionId="session-gaming-title-discovery",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            state.goal, history=None, client=_fake_client(create_mock),
            task_state=state, on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(
            updated.domain_state["taskStateExtraction"]["route"],
            "deterministic_text_claim_discovery",
        )

    async def test_natural_gaming_discovery_searches_title_claims(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="有没有适合打游戏的手机？",
            sessionId="session-natural-gaming-discovery",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        create_mock = AsyncMock()

        updated = await _update_task_state_for_unified_harness(
            state.goal, history=None, client=_fake_client(create_mock),
            task_state=state, on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(
            updated.domain_state["taskStateExtraction"]["reason"],
            "gaming_title_claim",
        )
        self.assertEqual(
            updated.domain_state["shoppingGuide"]["useCases"],
            ["gaming_title_claim"],
        )

        followup = await _update_task_state_for_unified_harness(
            "我要华为的",
            history=None,
            client=_fake_client(create_mock),
            task_state=updated,
            on_task_state=None,
        )

        self.assertIn("有没有适合打游戏的手机", followup.goal)
        self.assertIn("我要华为的", followup.goal)
        self.assertEqual(
            followup.domain_state["shoppingGuide"]["useCases"],
            ["gaming_title_claim"],
        )
        self.assertEqual(
            followup.domain_state["shoppingGuide"]["requirements"][0]["key"],
            "brand",
        )

    async def test_broad_student_gaming_query_keeps_gaming_use_case(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "学生用二手手机 要便宜点的 主要是打游戏"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal=message,
            sessionId="session-broad-student-gaming",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )

        self.assertEqual(
            updated.domain_state["taskStateExtraction"]["route"],
            "deterministic_text_claim_discovery",
        )
        self.assertEqual(
            updated.domain_state["shoppingGuide"]["useCases"],
            ["gaming_title_claim"],
        )

    async def test_domestic_used_phone_phrase_persists_brand_group(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "预算2000以内，国产二手手机"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal=message,
            sessionId="session-domestic-used-phone",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )

        brand = next(
            item for item in updated.domain_state["shoppingGuide"]["requirements"]
            if item["key"] == "brand"
        )
        self.assertEqual(brand["operator"], "in")
        self.assertEqual(brand["priority"], "hard")
        self.assertIn("huawei", brand["value"])

    async def test_unbound_negative_target_remains_a_blocking_clarification(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await _create_ecommerce_state_with_scope(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="预算一千以内的二手手机",
            sessionId="session-unbound-negative-target",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "price_minor", "operator": "lte", "value": 100000,
                    "unit": "CNY_MINOR", "priority": "hard", "source": "user",
                }],
                "candidateIds": [101, 102, 103],
                "comparedIds": [], "evidenceStatus": "complete",
            }},
        ), [101, 102, 103])

        updated = await _update_task_state_for_unified_harness(
            "不要那个牌子",
            history=None,
            client=_fake_client(AsyncMock()),
            task_state=state,
            on_task_state=None,
        )

        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(
            updated.unknowns,
            ["否决表达没有绑定到明确品牌或商品"],
        )
        self.assertEqual(
            updated.pending_questions,
            ["请明确说出你不想要的品牌或商品。"],
        )
        self.assertEqual(
            updated.domain_state["taskStateExtraction"]["reason"],
            "unbound_negative_target",
        )

    async def test_presentation_control_is_a_no_tool_context_turn(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="预算两千五以内的二手手机",
            sessionId="session-presentation-control",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "price_minor", "operator": "lte", "value": 250000,
                    "unit": "CNY_MINOR", "priority": "hard", "source": "user",
                }],
                "candidateIds": [], "comparedIds": [],
                "evidenceStatus": "complete",
            }},
        ))

        with patch(
            "app.llm._trusted_validator_presentation_ids",
            return_value=[101, 102, 103, 104],
        ):
            updated = await _update_task_state_for_unified_harness(
                "先不用把20个都详细写出来，只展示前三个",
                history=None,
                client=_fake_client(AsyncMock()),
                task_state=state,
                on_task_state=None,
            )
            self.assertEqual(
                updated.domain_state["taskStateExtraction"]["reason"],
                "presentation_only",
            )
            self.assertTrue(_is_context_only_turn("只展示前三个", updated))
            self.assertFalse(_should_use_explicit_harness("只展示前三个", updated))

    async def test_validated_scope_answer_bypasses_comparison_judge_in_all_runtimes(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await _create_ecommerce_state_with_scope(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="根据你确实知道的属性告诉我怎么选",
            sessionId="session-validated-scope-answer-routing",
            domainState={
                "shoppingGuide": {
                    "mode": "compare", "category": "phone",
                    "requirements": [], "candidateIds": [101, 102, 103],
                    "comparedIds": [101, 102, 103], "evidenceStatus": "complete",
                },
                "taskStateExtraction": {
                    "route": "deterministic_validated_scope_answer",
                },
            },
        ), [101, 102, 103])
        self.assertFalse(_should_use_comparison_judge(state))
        model_comparison = state.model_copy(deep=True)
        model_comparison.domain_state["taskStateExtraction"] = {
            "route": "model_fallback",
        }
        self.assertTrue(_should_use_comparison_judge(model_comparison))

    async def test_presentation_control_unified_turn_calls_no_business_tool(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="预算两千五以内的二手手机",
            sessionId="session-presentation-control-no-tool",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "price_minor", "operator": "lte", "value": 250000,
                    "unit": "CNY_MINOR", "priority": "hard", "source": "user",
                }],
                "candidateIds": [], "comparedIds": [],
                "evidenceStatus": "complete",
            }},
        ))
        create_mock = AsyncMock(return_value=_make_response("前三项已按原顺序展示。"))

        with (
            patch("app.llm.get_client", return_value=_fake_client(create_mock)),
            patch(
                "app.llm._trusted_validator_presentation_ids",
                return_value=[101, 102, 103, 104],
            ),
            patch(
                "app.llm._validated_presentation_control_answer",
                return_value="前三项已按原顺序展示。",
            ),
            patch("app.tools.call_tool", new_callable=AsyncMock) as call_tool_mock,
            patch("app.llm._persist_trace_safely", new_callable=AsyncMock),
            patch.object(settings, "agent_control_runtime", "react_v1"),
        ):
            answer, traces, _messages, run_id, summary = (
                await _run_unified_harness_agent(
                    "先不用把20个都详细写出来，只展示前三个",
                    history=None,
                    task_state=state,
                    on_answer_delta=None,
                    on_task_state=None,
                )
            )

        self.assertEqual(answer, "前三项已按原顺序展示。")
        self.assertEqual(traces, [])
        self.assertIsNotNone(run_id)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.entered_runtime, "react_v1")
        self.assertEqual(summary.control_policy, "react_v1")
        self.assertEqual(summary.policy_revision, "react-v1-2026-08-27")
        create_mock.assert_not_awaited()
        call_tool_mock.assert_not_awaited()

    async def test_gaming_capability_evaluation_remains_fail_closed(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="哪款手机更适合打游戏？",
            sessionId="session-gaming-capability-boundary",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            state.goal, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )

        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(
            updated.domain_state["taskStateExtraction"]["reason"],
            "unsupported_game_camera_evidence",
        )

    async def test_validated_scope_answer_cue_uses_deterministic_state_path(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await _create_ecommerce_state_with_scope(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="预算1800以内的二手手机",
            sessionId="session-validated-scope-answer",
            domainState={
                "shoppingGuide": {
                    "mode": "recommend",
                    "category": "phone",
                    "requirements": [],
                    "candidateIds": [],
                    "comparedIds": [],
                    "evidenceStatus": "complete",
                },
            },
        ), [101, 102, 103])

        decision, observation = _deterministic_used_phone_task_state_decision(
            state,
            "没有实测数据也没关系，就根据你确实知道的属性告诉我怎么选",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["domainStatePatch"]["shoppingGuide"]["mode"], "compare")
        self.assertEqual(observation["reason"], "validated_scope_answer")

    async def test_explicit_unsupported_category_is_deterministically_blocked(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="预算1500元以内买电子书阅读器",
            sessionId="session-unsupported-category",
            domainState={"shoppingGuide": {
                "mode": "recommend",
                "category": None,
                "requirements": [],
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            }},
        ))

        decision, observation = _deterministic_used_phone_task_state_decision(
            state,
            "预算1500元以内，想买一台适合阅读和批注PDF的电子书阅读器。",
        )

        self.assertEqual(decision["status"], "collecting_information")
        self.assertIsNone(
            decision["domainStatePatch"]["shoppingGuide"]["category"]
        )
        self.assertEqual(
            observation["route"],
            "deterministic_unsupported_category_boundary",
        )

    def test_react_evidence_boundary_answer_is_server_authored(self):
        answer = _react_boundary_answer("evidence-boundary:gaming:task-a:r13")
        self.assertIn("不支持比较", answer)
        self.assertIn("不会把商品标题宣传当成已验证事实", answer)
        camera = _react_boundary_answer("evidence-boundary:camera:task-a:r13")
        self.assertIn("夜景", camera)
        self.assertNotIn("大型游戏", camera)
        self.assertIsNone(_react_boundary_answer("validated-task:task-a:r13"))

    def test_known_empty_snapshot_stop_names_the_requested_category(self):
        result = SimpleNamespace(
            replanner_result=None,
            validator_result=None,
            planner_result=None,
            executor_result=SimpleNamespace(
                execution_result=SimpleNamespace(
                    tool_trace=ToolTrace(
                        tool="search_products",
                        ok=False,
                        detail={
                            "code": "product_recall_unavailable",
                            "requestedCategory": "laptop",
                        },
                    )
                )
            ),
        )
        answer = _harness_stop_answer(result)
        self.assertIn("笔记本", answer)
        self.assertIn("没有", answer)

        durable_result = SimpleNamespace(
            replanner_result=None,
            validator_result=None,
            planner_result=None,
            executor_result=None,
        )
        answer = _harness_stop_answer(
            durable_result,
            [result.executor_result.execution_result.tool_trace],
        )
        self.assertIn("笔记本", answer)

    async def test_validator_owned_first_two_ordinals_bind_exact_display_ids(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        trusted = [5989522, 1092185, 5304970]
        state = await _create_ecommerce_state_with_scope(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="推荐 iOS 手机",
            sessionId="session-trusted-ordinal-binding",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "complete",
            }},
        ), trusted)
        create_mock = AsyncMock()

        with patch(
            "app.llm._trusted_validator_presentation_ids",
            return_value=trusted,
        ):
            updated = await _update_task_state_for_unified_harness(
                "第一个和第二个哪个好？",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        guide = updated.domain_state["shoppingGuide"]
        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(guide["mode"], "compare")
        self.assertEqual(guide["candidateIds"], trusted)
        self.assertEqual(guide["comparedIds"], [5989522, 1092185])

    async def test_validator_owned_three_card_followup_compares_without_new_search(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        trusted = [1841291965546398700, 8739261449574994280, 4156608880226841904]
        state = await _create_ecommerce_state_with_scope(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="三千以内的性价比好的手机",
            sessionId="session-trusted-three-card-binding",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "price_minor", "operator": "lte", "value": 300000,
                    "unit": "CNY_MINOR", "priority": "hard", "source": "user",
                }],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "complete",
            }},
        ), trusted)
        create_mock = AsyncMock()

        with patch(
            "app.llm._trusted_validator_presentation_ids",
            return_value=trusted,
        ):
            updated = await _update_task_state_for_unified_harness(
                "这三个手机哪个适合我啊？各自有什么特点？",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        guide = updated.domain_state["shoppingGuide"]
        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(guide["mode"], "compare")
        self.assertEqual(guide["comparedIds"], trusted)
        self.assertEqual(guide["requirements"][0]["value"], 300000)
        self.assertIn("三千以内的性价比好的手机", updated.goal)
        self.assertIn("这三个手机哪个适合我", updated.goal)

    async def test_natural_deictic_choice_compares_visible_cards_without_new_search(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        trusted = [1795901, 7441112016001245431, 1597799]
        state = await _create_ecommerce_state_with_scope(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="华为 vivo 之类的，不要苹果。预算2000吧。",
            sessionId="session-natural-visible-choice",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "complete",
            }},
        ), trusted)
        create_mock = AsyncMock()

        with patch(
            "app.llm._trusted_validator_presentation_ids",
            return_value=trusted,
        ):
            updated = await _update_task_state_for_unified_harness(
                "这里面那个适合打游戏 续航好？",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        guide = updated.domain_state["shoppingGuide"]
        self.assertEqual(create_mock.await_count, 0)
        self.assertEqual(guide["mode"], "compare")
        self.assertEqual(guide["candidateIds"], trusted)
        self.assertEqual(guide["comparedIds"], trusted)

    async def test_single_or_missing_ordinal_never_guesses_product_id(self):
        for index, (message, trusted) in enumerate((
            ("第一个哪个好？", [5989522, 1092185, 5304970]),
            ("比较前两个", []),
            ("这两个哪个好？", [5989522, 1092185, 5304970]),
        )):
            with self.subTest(message=message):
                task_state_store._client = FakeRedis()
                task_state_store._task_locks.clear()
                state = await create_task_state(TaskStateCreateRequest(
                    taskType="ecommerce_guide", goal="推荐手机",
                    sessionId=f"session-ordinal-clarify-{index}",
                    domainState={"shoppingGuide": {
                        "mode": "recommend", "category": "phone",
                        "requirements": [], "candidateIds": [],
                        "comparedIds": [], "evidenceStatus": "complete",
                    }},
                ))
                with patch(
                    "app.llm._trusted_validator_presentation_ids",
                    return_value=trusted,
                ):
                    updated = await _update_task_state_for_unified_harness(
                        message, history=None, client=_fake_client(AsyncMock()),
                        task_state=state, on_task_state=None,
                    )
                self.assertEqual(updated.status, "collecting_information")
                self.assertEqual(
                    updated.domain_state["shoppingGuide"]["comparedIds"], []
                )
                self.assertTrue(updated.pending_questions)

    async def test_same_turn_search_then_compare_is_server_staged(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        message = "ios原装屏幕手机，再比较前两个结果。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-compound-first-two",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))

        updated = await _update_task_state_for_unified_harness(
            message, history=None, client=_fake_client(AsyncMock()),
            task_state=state, on_task_state=None,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.domain_state["shoppingGuide"]["mode"], "recommend")
        self.assertEqual(
            updated.domain_state["compoundComparison"]["status"],
            "awaiting_search_validation",
        )

    async def test_real_harness_search_then_trusted_non_prefix_ordinal_compare_same_task(self):
        from app.domains.ecommerce.tools import compare_product_details
        from app.domains.ecommerce.models import ShoppingRequirement
        from tests.two_stage_ranking_fixtures import two_stage_search_detail

        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        ids = [5989522, 1092185, 5304970]
        products = [{
            "id": product_id,
            "source": "kuaisearch",
            "title": str(product_id),
            "brand": "Apple",
            "categoryL1": "数码",
            "categoryL2": "手机",
            "categoryL3": "二手手机",
            "snapshotPriceMinor": None,
            "currency": "CNY",
            "priceStatus": "unverified",
            "attributeText": "iOS",
            "provenanceUrl": f"https://example.test/{product_id}",
            "attributes": [],
        } for product_id in (ids[0], ids[2])]
        tool_calls = []

        async def controlled_tool(tool_name, arguments):
            tool_calls.append((tool_name, arguments))
            if tool_name == "search_products":
                return ToolTrace(
                    tool=tool_name, ok=True, durationMs=1.0,
                    detail=two_stage_search_detail(ids),
                )
            self.assertEqual(tool_name, "compare_products")
            self.assertEqual(arguments["productIds"], [ids[0], ids[2]])
            return ToolTrace(
                tool=tool_name, ok=True, durationMs=1.0,
                detail=compare_product_details(
                    "phone",
                    products,
                    [
                        ShoppingRequirement.model_validate(item)
                        for item in arguments["requirements"]
                    ],
                ),
            )

        first = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="ios手机",
            sessionId="session-real-harness-ordinal",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        with patch(
            "app.llm.settings.agent_control_runtime", "fixed_v1",
        ), patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", False,
        ), patch(
            "app.llm._generate_final_answer",
            new=AsyncMock(return_value="已完成两件商品的字段比较。"),
        ), patch("app.tools.call_tool", new=controlled_tool):
            _first_answer, first_traces, *_ = await _run_unified_harness_agent(
                "ios手机", history=None, task_state=first,
                on_answer_delta=None, on_task_state=None,
            )
            after_search = await get_task_state(first.task_id)
            second_answer, second_traces, *_ = await _run_unified_harness_agent(
                "第一个和第三个哪个好？",
                history=None,
                task_state=after_search,
                on_answer_delta=None,
                on_task_state=None,
            )

        latest = await get_task_state(first.task_id)
        self.assertEqual([trace.tool for trace in first_traces], ["search_products"])
        self.assertEqual([trace.tool for trace in second_traces], ["compare_products"])
        self.assertEqual([name for name, _args in tool_calls], [
            "search_products", "compare_products",
        ])
        self.assertEqual(
            latest.domain_state["shoppingGuide"]["comparedIds"],
            [ids[0], ids[2]],
        )
        self.assertEqual(latest.active_plan.steps[0].tool_name, "compare_products")
        self.assertIn("已完成两件商品", second_answer)
        from app.harness import build_validated_guide_result
        guide_result = build_validated_guide_result(latest)
        self.assertIsNotNone(guide_result)
        self.assertEqual(
            [row["product"]["id"] for row in guide_result["products"]],
            [str(ids[0]), str(ids[2])],
        )
        self.assertTrue(all(
            len(row["attributes"]) == 7
            and all(
                set(attribute) == {"key", "status", "value", "evidenceRef"}
                for attribute in row["attributes"]
            )
            for row in guide_result["products"]
        ))
        self.assertTrue(all("checks" not in row for row in guide_result["products"]))

    async def test_real_harness_compound_search_and_compare_runs_two_tools(self):
        from app.domains.ecommerce.tools import compare_product_details
        from app.domains.ecommerce.models import ShoppingRequirement
        from app.domains.ecommerce.used_phone_attributes import (
            USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
            USED_PHONE_ATTRIBUTE_RULESET_VERSION,
        )
        from tests.two_stage_ranking_fixtures import two_stage_search_detail

        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        ids = [1968628, 4194562, 3956311]
        search_detail = two_stage_search_detail(ids)
        for candidate in search_detail["candidates"]:
            product_id = candidate["id"]
            ref = f"product:{product_id}:attribute:screen_originality"
            candidate["attributes"].append({
                "key": "screen_originality",
                "rawValue": "原装屏",
                "normalizedText": "original",
                "evidenceField": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                "extractionMethod": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
            })
            candidate["checks"].append({
                "key": "screen_originality", "operator": "eq",
                "expected": "original", "unit": "enum",
                "priority": "hard", "source": "user", "status": "pass",
                "actual": "original", "evidenceRef": ref,
            })
            candidate["evidenceRefs"].append(ref)
            search_detail["evidenceRefs"].append(ref)
            search_detail["evidence"].append({
                "ref": ref,
                "field": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                "method": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
                "rawValue": "原装屏",
            })
        evidence_by_ref = {
            item["ref"]: item for item in search_detail["evidence"]
        }
        search_detail["evidenceRefs"] = [
            ref
            for candidate in search_detail["candidates"]
            for ref in candidate["evidenceRefs"]
        ]
        search_detail["evidence"] = [
            evidence_by_ref[ref] for ref in search_detail["evidenceRefs"]
        ]
        search_detail["citationTrace"]["evidenceRefCount"] = len(
            search_detail["evidence"]
        )
        compare_products = [{
            "id": product_id, "source": "kuaisearch", "title": str(product_id),
            "brand": "Apple", "categoryL1": "数码", "categoryL2": "手机",
            "categoryL3": "二手手机", "snapshotPriceMinor": None,
            "currency": "CNY", "priceStatus": "unverified",
            "attributeText": "iOS 原装屏", "attributes": [],
            "provenanceUrl": f"https://example.test/{product_id}",
        } for product_id in ids[:2]]
        calls = []

        async def controlled_tool(tool_name, arguments):
            calls.append((tool_name, arguments))
            if tool_name == "search_products":
                return ToolTrace(
                    tool=tool_name, ok=True, durationMs=1.0,
                    detail=search_detail,
                )
            return ToolTrace(
                tool=tool_name, ok=True, durationMs=1.0,
                detail=compare_product_details(
                    "phone",
                    compare_products,
                    [
                        ShoppingRequirement.model_validate(item)
                        for item in arguments["requirements"]
                    ],
                ),
            )

        message = "ios原装屏幕手机，再比较前两个结果。"
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal=message,
            sessionId="session-real-compound-compare",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        with patch(
            "app.llm.settings.agent_control_runtime", "fixed_v1",
        ), patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", False,
        ), patch(
            "app.llm._generate_final_answer",
            new=AsyncMock(return_value="已完成两件商品的字段比较。"),
        ), patch("app.tools.call_tool", new=controlled_tool):
            answer, traces, *_ = await _run_unified_harness_agent(
                message, history=None, task_state=state,
                on_answer_delta=None, on_task_state=None,
            )

        latest = await get_task_state(state.task_id)
        self.assertEqual([trace.tool for trace in traces], [
            "search_products", "compare_products",
        ])
        self.assertEqual(calls[1][1]["productIds"], ids[:2])
        self.assertEqual(latest.domain_state["shoppingGuide"]["comparedIds"], ids[:2])
        self.assertIsNone(latest.domain_state.get("compoundComparison"))
        self.assertIn("已完成两件商品", answer)

    async def test_direct_compare_evidence_gap_is_not_a_user_blocker(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="A 和 B 哪台风险更低？",
            sessionId="session-compare-evidence-precondition",
            domainState={"shoppingGuide": {
                "mode": "compare", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [2459775, 2754566],
                "evidenceStatus": "missing",
            }},
        ))

        updated = await _apply_task_state_update(
            state,
            {
                "status": "collecting_information",
                "addUnknowns": [
                    "两台设备的主板维修、电池和屏幕证据尚未检索，无法进行比较判断"
                ],
                "pendingQuestions": ["请确认是否继续读取证据。"],
            },
            message=state.goal,
            on_task_state=None,
            require_status=True,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.unknowns, [])
        self.assertEqual(updated.pending_questions, [])

    async def test_existing_executable_requirements_clear_speculative_other_conditions(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="先看原装屏的二手机，原装电池更好。",
            sessionId="session-existing-executable-requirements",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [{
                    "key": "screen_originality", "operator": "eq",
                    "value": "original", "unit": "enum",
                    "priority": "hard", "source": "user",
                }],
                "candidateIds": [], "comparedIds": [],
                "evidenceStatus": "missing",
            }},
        ))

        updated = await _apply_task_state_update(
            state,
            {
                "status": "collecting_information",
                "addUnknowns": ["待确认是否还有品牌或预算等其他硬性要求"],
                "pendingQuestions": ["还有其他要求吗？"],
            },
            message=state.goal,
            on_task_state=None,
            require_status=True,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.unknowns, [])
        self.assertEqual(updated.pending_questions, [])

    async def test_collecting_without_blocker_fails_before_persistence(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="必须满足条件，没有就说明无解。",
            sessionId="session-empty-collecting-shape",
        ))
        with patch("app.llm._persist_task_patch", new=AsyncMock()) as persist_mock:
            with self.assertRaises(TaskStatePayloadValidationError) as ctx:
                await _apply_task_state_update(
                    state,
                    {
                        "status": "collecting_information",
                        "addUnknowns": [],
                        "pendingQuestions": [],
                    },
                    message=state.goal,
                    on_task_state=None,
                    require_status=True,
                )
        self.assertEqual(ctx.exception.code, "collecting_information_requires_blocker")
        persist_mock.assert_not_awaited()

    async def test_real_persistence_fallback_keeps_only_server_ledger(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal="find a park",
                sessionId="session-real-fallback-ledger",
                domainState={"origin": "chat", "turnCount": 6},
            )
        )
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="system",
                status="ready",
            ),
        )

        updated = await _apply_task_state_update(
            state,
            {"status": "completed"},
            message="keep looking",
            on_task_state=None,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.revision, state.revision + 1)
        self.assertEqual(updated.domain_state["origin"], "chat")
        self.assertEqual(updated.domain_state["turnCount"], 7)
        self.assertEqual(updated.domain_state["lastUserMessage"], "keep looking")
        self.assertIsNone(updated.active_plan)

    async def test_local_life_update_without_model_domain_patch_still_works(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal="find a park",
                sessionId="session-local-life-domain-regression",
                domainState={"origin": "chat", "turnCount": 2},
            )
        )

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "upsertFacts": [{
                    "key": "district", "value": "海淀区",
                    "certainty": "confirmed", "source": "user",
                }],
                "pendingQuestions": [],
            },
            message="海淀区的公园",
            on_task_state=None,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.domain_state["origin"], "chat")
        self.assertEqual(updated.domain_state["turnCount"], 3)
        self.assertEqual(updated.domain_state["lastUserMessage"], "海淀区的公园")

    async def test_local_life_model_domain_key_is_rejected(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal="find a park",
                sessionId="session-local-life-model-domain-rejected",
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "model cannot write domainStatePatch keys: selectedDestination",
        ):
            await _apply_task_state_update(
                state,
                {"domainStatePatch": {"selectedDestination": "玉渊潭公园"}},
                message="玉渊潭公园",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)

    async def test_invalid_shopping_guide_patch_fails_closed_without_state_mutation(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="推荐一台二手手机",
                sessionId="session-invalid-shopping-guide",
            )
        )

        with self.assertRaisesRegex(ValueError, "invalid shoppingGuide patch"):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "category": "phone",
                            "requirements": [{
                                "key": "unsupported_used_phone_claim",
                                "operator": "eq",
                                "value": "yes",
                                "unit": "enum",
                                "priority": "hard",
                                "source": "user",
                            }],
                        }
                    },
                },
                message="推荐一台二手手机",
                on_task_state=None,
            )

        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertEqual(unchanged.status, "collecting_information")
        self.assertNotIn("shoppingGuide", unchanged.domain_state)

    async def test_registered_used_phone_patch_can_enter_ready_state(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找电池健康九成以上的二手手机",
                sessionId="session-valid-used-phone-guide",
            )
        )

        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "pendingQuestions": [],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "category": "phone",
                        "requirements": [{
                            "key": "battery_health",
                            "operator": "eq",
                            "value": "90_plus",
                            "unit": "enum",
                            "priority": "hard",
                            "source": "user",
                        }],
                    }
                },
            },
            message="想找电池健康九成以上的二手手机",
            on_task_state=None,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.pending_questions, [])
        requirement = updated.domain_state["shoppingGuide"]["requirements"][0]
        self.assertEqual(requirement["key"], "battery_health")
        self.assertEqual(requirement["value"], "90_plus")

    async def test_server_does_not_guess_that_free_text_questions_are_optional(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 且电池健康九成以上的二手手机",
                sessionId="session-defer-optional-profile",
            )
        )
        updated = await _apply_task_state_update(
            state,
            {
                "status": "collecting_information",
                "addUnknowns": ["预算范围", "具体机型偏好"],
                "pendingQuestions": ["预算多少？有没有具体机型偏好？"],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "category": "phone",
                        "requirements": [
                            {
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            },
                            {
                                "key": "battery_health", "operator": "eq",
                                "value": "90_plus", "unit": "enum",
                                "priority": "hard", "source": "user",
                            },
                        ],
                    }
                },
            },
            message="想找 iOS 且电池健康九成以上的二手手机",
            on_task_state=None,
        )
        self.assertEqual(updated.status, "collecting_information")
        self.assertTrue(updated.pending_questions)
        self.assertNotIn("deferredOptionalQuestions", updated.domain_state)

    async def test_structured_optional_shopping_questions_do_not_block_recommendation(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone with 90%+ battery health",
                sessionId="session-structured-optional",
            )
        )
        updated = await _apply_task_state_update(
            state,
            {
                "status": "ready",
                "pendingQuestions": [],
                "optionalShoppingQuestions": [
                    {"kind": "budget"},
                    {"kind": "model"},
                ],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "requirements": [{
                            "key": "battery_health", "operator": "eq",
                            "value": "90_plus", "unit": "enum",
                            "priority": "soft", "source": "user",
                        }],
                    }
                },
            },
            message="recommend one",
            on_task_state=None,
        )
        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.unknowns, [])
        self.assertEqual(updated.pending_questions, [])
        self.assertEqual(
            [item["kind"] for item in updated.domain_state["optionalShoppingQuestions"]],
            ["budget", "model"],
        )

    async def test_comparison_cannot_use_optional_questions_to_bypass_identity(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="compare two used phones",
                sessionId="session-comparison-optional-rejected",
            )
        )
        with self.assertRaisesRegex(ValueError, "explicit recommendation intent"):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "optionalShoppingQuestions": [
                        {"kind": "model"},
                    ],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "recommend",
                            "category": "phone",
                            "requirements": [{
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }],
                        }
                    },
                },
                message="compare them",
                on_task_state=None,
            )

    async def test_optional_questions_require_structured_executable_guide(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend a used phone",
                sessionId="session-optional-requires-guide",
            )
        )
        with self.assertRaisesRegex(ValueError, "executable recommendation"):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "optionalShoppingQuestions": [
                        {"kind": "budget"},
                    ],
                },
                message="recommend one",
                on_task_state=None,
            )

    async def test_compare_mode_rejects_optional_questions_even_without_goal_cue(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="help with these products",
                sessionId="session-compare-mode-optional-rejected",
            )
        )
        with self.assertRaisesRegex(ValueError, "executable recommendation"):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "optionalShoppingQuestions": [
                        {"kind": "model"},
                    ],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "compare",
                            "category": "phone",
                            "requirements": [{
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }],
                        }
                    },
                },
                message="continue",
                on_task_state=None,
            )

    async def test_optional_questions_require_explicit_recommend_mode(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend a used phone",
                sessionId="session-optional-explicit-mode",
            )
        )
        with self.assertRaisesRegex(ValueError, "executable recommendation"):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "optionalShoppingQuestions": [
                        {"kind": "budget"},
                    ],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "category": "phone",
                            "requirements": [{
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }],
                        }
                    },
                },
                message="recommend one",
                on_task_state=None,
            )

    async def test_optional_questions_validate_proposed_goal_not_stale_goal(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone",
                sessionId="session-optional-proposed-goal",
            )
        )
        with self.assertRaisesRegex(ValueError, "explicit recommendation intent"):
            await _apply_task_state_update(
                state,
                {
                    "goal": "compare two used phones",
                    "status": "ready",
                    "pendingQuestions": [],
                    "optionalShoppingQuestions": [{"kind": "model"}],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "recommend",
                            "category": "phone",
                            "requirements": [{
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }],
                        }
                    },
                },
                message="recommend one",
                on_task_state=None,
            )

    async def test_optional_questions_require_explicit_recommendation_message(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone",
                sessionId="session-optional-message-intent",
            )
        )
        with self.assertRaisesRegex(ValueError, "explicit recommendation intent"):
            await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "optionalShoppingQuestions": [{"kind": "model"}],
                    "domainStatePatch": {
                        "shoppingGuide": {
                            "mode": "recommend",
                            "category": "phone",
                            "requirements": [{
                                "key": "os", "operator": "eq", "value": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                            }],
                        }
                    },
                },
                message="help me choose between them",
                on_task_state=None,
            )

    async def test_optional_questions_are_not_replayed_after_occ_conflict(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend an iOS used phone",
                sessionId="session-optional-occ",
                status="collecting_information",
                domainState={
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "requirements": [{
                            "key": "os", "operator": "eq", "value": "ios",
                            "unit": "enum", "priority": "hard", "source": "user",
                        }],
                    }
                },
            )
        )
        real_update = task_state_store.update_task_state
        injected = False

        async def update_with_concurrent_goal(task_id, patch_request):
            nonlocal injected
            if not injected:
                injected = True
                latest = await get_task_state(task_id)
                await real_update(
                    task_id,
                    TaskStatePatchRequest(
                        expectedRevision=latest.revision,
                        actor="user",
                        goal="compare iPhone 13 and iPhone 14",
                    ),
                )
            return await real_update(task_id, patch_request)

        with patch("app.llm.update_task_state", side_effect=update_with_concurrent_goal):
            updated = await _apply_task_state_update(
                state,
                {
                    "status": "ready",
                    "pendingQuestions": [],
                    "optionalShoppingQuestions": [{"kind": "budget"}],
                },
                message="recommend one",
                on_task_state=None,
            )
        self.assertEqual(updated.goal, "compare iPhone 13 and iPhone 14")
        self.assertEqual(updated.status, "collecting_information")
        self.assertNotIn("optionalShoppingQuestions", updated.domain_state)

    async def test_existing_blocking_unknown_cannot_be_bypassed_by_new_question(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="compare two used phones",
                sessionId="session-existing-blocking-unknown",
                unknowns=["candidate identity"],
                pendingQuestions=["which two candidates should be compared?"],
            )
        )
        updated = await _apply_task_state_update(
            state,
            {
                "status": "collecting_information",
                "addUnknowns": ["budget"],
                "pendingQuestions": ["budget?"],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "category": "phone",
                        "requirements": [{
                            "key": "os", "operator": "eq", "value": "ios",
                            "unit": "enum", "priority": "hard", "source": "user",
                        }],
                    }
                },
            },
            message="budget does not matter; which one is better?",
            on_task_state=None,
        )
        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(updated.pending_questions, ["budget?"])
        self.assertIn("candidate identity", updated.unknowns)
        self.assertNotIn("deferredOptionalQuestions", updated.domain_state)

    async def test_existing_unknown_rejects_implicit_pending_clear(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="compare two used phones",
                sessionId="session-reject-implicit-clear",
                unknowns=["candidate identity"],
                pendingQuestions=["which two candidates?"],
            )
        )
        with self.assertRaisesRegex(ValueError, "cannot clear pending"):
            await _apply_task_state_update(
                state,
                {"pendingQuestions": []},
                message="continue",
                on_task_state=None,
            )
        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.status, "collecting_information")
        self.assertEqual(unchanged.pending_questions, ["which two candidates?"])

    async def test_existing_unknown_rejects_explicit_ready(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="compare two used phones",
                sessionId="session-reject-explicit-ready",
                unknowns=["candidate identity"],
                pendingQuestions=["which two candidates?"],
            )
        )
        with self.assertRaisesRegex(ValueError, "cannot clear pending"):
            await _apply_task_state_update(
                state,
                {"status": "ready", "pendingQuestions": []},
                message="continue",
                on_task_state=None,
            )
        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.status, "collecting_information")
        self.assertEqual(unchanged.unknowns, ["candidate identity"])

    async def test_occ_retry_rechecks_latest_unknowns_before_ready(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="recommend a used phone",
                sessionId="session-occ-unknown-guard",
            )
        )
        real_update = task_state_store.update_task_state
        injected = False

        async def update_with_concurrent_user_patch(task_id, patch_request):
            nonlocal injected
            if not injected:
                injected = True
                latest = await get_task_state(task_id)
                await real_update(
                    task_id,
                    TaskStatePatchRequest(
                        expectedRevision=latest.revision,
                        actor="user",
                        addUnknowns=["candidate identity"],
                        pendingQuestions=["which candidate?"],
                    ),
                )
            return await real_update(task_id, patch_request)

        with patch("app.llm.update_task_state", side_effect=update_with_concurrent_user_patch):
            updated = await _apply_task_state_update(
                state,
                {"pendingQuestions": []},
                message="continue",
                on_task_state=None,
            )

        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(updated.unknowns, ["candidate identity"])
        self.assertEqual(updated.pending_questions, ["which candidate?"])

    async def test_required_comparison_identity_is_never_deferred(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="比较两台二手手机",
                sessionId="session-required-comparison-identity",
            )
        )
        updated = await _apply_task_state_update(
            state,
            {
                "status": "collecting_information",
                "addUnknowns": ["比较对象 A/B 的候选身份"],
                "pendingQuestions": ["请明确要比较的候选 A 和 B。"],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "category": "phone",
                        "requirements": [{
                            "key": "os", "operator": "eq", "value": "ios",
                            "unit": "enum", "priority": "hard", "source": "user",
                        }],
                    }
                },
            },
            message="比较两台 iOS 二手手机",
            on_task_state=None,
        )
        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(
            updated.pending_questions,
            ["请明确要比较的候选 A 和 B。"],
        )
        self.assertNotIn("deferredOptionalQuestions", updated.domain_state)

    async def test_mixed_optional_and_required_questions_are_never_deferred(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="比较二手手机",
                sessionId="session-mixed-shopping-questions",
            )
        )
        updated = await _apply_task_state_update(
            state,
            {
                "status": "collecting_information",
                "addUnknowns": ["预算范围", "比较对象身份"],
                "pendingQuestions": ["预算多少，以及要比较哪两个候选？"],
                "domainStatePatch": {
                    "shoppingGuide": {
                        "category": "phone",
                        "requirements": [{
                            "key": "screen_originality", "operator": "eq",
                            "value": "original", "unit": "enum",
                            "priority": "hard", "source": "user",
                        }],
                    }
                },
            },
            message="比较原装屏二手手机",
            on_task_state=None,
        )
        self.assertEqual(updated.status, "collecting_information")
        self.assertTrue(updated.pending_questions)

    async def test_task_relation_classifier_can_resume_a_paused_task(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        active = await create_task_state(
            TaskStateCreateRequest(
                goal="帮我选一台电脑",
                sessionId="session-router",
            )
        )
        riding = await create_task_state(
            TaskStateCreateRequest(
                goal="今天骑车去公园",
                sessionId="session-router",
            )
        )
        riding = await update_task_state(
            riding.task_id,
            TaskStatePatchRequest(
                expectedRevision=riding.revision,
                actor="system",
                status="paused",
            ),
        )
        relation_call = _make_tool_call(
            "call-relation",
            "route_session_task",
            json.dumps(
                {
                    "relation": "resume_previous",
                    "targetTaskId": riding.task_id,
                    "reason": "用户明确说还是去骑行",
                    "confidence": 0.99,
                },
                ensure_ascii=False,
            ),
        )
        create_mock = AsyncMock(
            return_value=_make_response(tool_calls=[relation_call])
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            decision = await classify_task_relation(
                "算了，还是今天去骑行吧",
                active,
                [active, riding],
            )

        self.assertEqual(decision.relation, "resume_previous")
        self.assertEqual(decision.target_task_id, riding.task_id)
        request = create_mock.await_args.kwargs
        self.assertEqual(
            request["tool_choice"]["function"]["name"],
            "route_session_task",
        )
        self.assertIn(riding.task_id, request["messages"][1]["content"])

    async def test_task_state_pending_question_stops_business_tools(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                goal="半小时内骑车去一个公园",
                sessionId="session-clarify",
            )
        )
        state_call = _make_tool_call(
            "call-state",
            "update_task_state",
            json.dumps(
                {
                    "addUnknowns": ["origin"],
                    "pendingQuestions": ["你准备从哪里出发？"],
                },
                ensure_ascii=False,
            ),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[state_call]),
                _make_response(content="你准备从哪里出发？"),
            ]
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool",
            new=AsyncMock(),
        ) as call_tool_mock:
            answer, traces, _turn_messages, _run_id, _summary = await run_agent(
                "半小时内骑车去一个公园",
                task_state=state,
            )

        latest = await get_task_state(state.task_id)
        self.assertEqual(answer, "你准备从哪里出发？")
        self.assertEqual(traces, [])
        self.assertEqual(latest.status, "collecting_information")
        self.assertEqual(latest.unknowns, ["origin"])
        self.assertEqual(latest.pending_questions, ["你准备从哪里出发？"])
        call_tool_mock.assert_not_awaited()
        self.assertEqual(create_mock.await_count, 2)

    async def test_task_state_updates_before_business_tools_and_tracks_execution(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        task_state_store._session_locks.clear()
        question = "我想从北京西站骑车去公园"
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal=question,
                sessionId="session-agent-loop",
                domainState={"origin": "chat", "turnCount": 0},
            )
        )
        state_call = _make_tool_call(
            "call-state",
            "update_task_state",
            json.dumps(
                {
                    # The runtime should normalize this stale model status to
                    # ready because the same patch clears pending questions.
                    "status": "collecting_information",
                    "upsertFacts": [
                        {
                            "key": "origin",
                            "value": "北京西站",
                            "certainty": "confirmed",
                            "source": "user",
                        }
                    ],
                    "upsertConstraints": [
                        {
                            "key": "transportMode",
                            "operator": "eq",
                            "value": "cycling",
                            "source": "user",
                        }
                    ],
                    "pendingQuestions": [],
                },
                ensure_ascii=False,
            ),
        )
        planner_call = _make_tool_call(
            "call-planner",
            "submit_planner_output",
            json.dumps(
                {
                    "outcome": "planned",
                    "steps": [
                        {
                            "stepId": "step-place",
                            "description": "根据用户目标查询公园候选",
                            "toolName": "search_places",
                            "arguments": {"query": question},
                            "argumentSources": {
                                "query": {"kind": "task_goal"},
                            },
                            "expectedOutput": {
                                "requiresPlaceCandidates": True,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[state_call]),
                _make_response(tool_calls=[planner_call]),
                _make_response(content="已根据你的出发地和骑行方式查询公园。"),
            ]
        )
        place_trace = ToolTrace(
            tool="search_places",
            ok=True,
            durationMs=12.5,
            detail={
                "items": [{"id": "beijing-park-1", "name": "示例公园"}],
                "count": 1,
                "total": 1,
            },
        )
        state_updates = []

        async def collect_state(updated, phase):
            state_updates.append((phase, updated))

        with patch(
            "app.llm.settings.agent_control_runtime", "fixed_v1",
        ), patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", False,
        ), patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.tools.call_tool",
            new=AsyncMock(return_value=place_trace),
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent(
                question,
                task_state=state,
                on_task_state=collect_state,
            )

        latest = await get_task_state(state.task_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.status, "completed")
        self.assertEqual(latest.revision, 6)
        self.assertEqual(latest.facts[0].value, "北京西站")
        self.assertEqual(latest.constraints[0].value, "cycling")
        self.assertEqual(latest.domain_state["turnCount"], 1)
        self.assertEqual(latest.active_plan.status, "completed")
        self.assertEqual(
            latest.domain_state["validationResult"]["outcome"], "passed"
        )
        self.assertEqual(
            [phase for phase, _updated in state_updates],
            ["user_state_updated", "harness_task_completed"],
        )
        self.assertEqual(traces, [place_trace])
        self.assertIn("已根据", answer)
        call_tool_mock.assert_awaited_once()
        self.assertEqual(call_tool_mock.await_args.args[0], "search_places")
        self.assertEqual(call_tool_mock.await_args.args[1], {"query": question})
        first_request = create_mock.await_args_list[0].kwargs
        self.assertEqual(
            [schema["function"]["name"] for schema in first_request["tools"]],
            ["update_task_state"],
        )
        self.assertEqual(
            first_request["tool_choice"]["function"]["name"],
            "update_task_state",
        )
        self.assertNotIn("update_task_state", json.dumps(turn_messages, ensure_ascii=False))

    async def test_task_state_prompt_exposes_mutually_exclusive_state_matrix(self):
        self.assertIn("可执行形状", TASK_STATE_PLANNING_PROMPT)
        self.assertIn("status=ready", TASK_STATE_PLANNING_PROMPT)
        self.assertIn("addUnknowns 必须为空", TASK_STATE_PLANNING_PROMPT)
        self.assertIn("pendingQuestions 必须为 []", TASK_STATE_PLANNING_PROMPT)
        self.assertIn("optionalShoppingQuestions", TASK_STATE_PLANNING_PROMPT)
        self.assertIn("必须澄清形状", TASK_STATE_PLANNING_PROMPT)
        self.assertIn("collecting_information", TASK_STATE_PLANNING_PROMPT)
        self.assertIn("会被服务端拒绝", TASK_STATE_PLANNING_PROMPT)
        self.assertIn("TASK_STATE_REPAIR_PROMPT", "TASK_STATE_REPAIR_PROMPT")
        self.assertIn("persist=0", TASK_STATE_REPAIR_PROMPT)
        self.assertIn("code", TASK_STATE_REPAIR_PROMPT)
        self.assertIn("field_path", TASK_STATE_REPAIR_PROMPT)
        self.assertIn("可执行形状", TASK_STATE_REPAIR_PROMPT)
        self.assertIn("澄清形状", TASK_STATE_REPAIR_PROMPT)
        self.assertIn("不要删除用户明确提供的 hard 事实", TASK_STATE_REPAIR_PROMPT)

    async def test_smoke_004_frozen_conflicting_payload_rejected_with_zero_persist(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-smoke004-conflict-rejected",
            )
        )

        with patch("app.llm._persist_task_patch", new=AsyncMock()) as persist_mock:
            with self.assertRaises(TaskStatePayloadValidationError) as ctx:
                await _apply_task_state_update(
                    state,
                    _SMOKE_004_FROZEN_ARGS,
                    message="想找 iOS 二手机。",
                    on_task_state=None,
                )

        self.assertEqual(
            ctx.exception.code,
            "task_cannot_become_executable_with_unresolved_questions",
        )
        self.assertEqual(ctx.exception.field_path, "status")
        self.assertEqual(
            str(ctx.exception),
            "task cannot become executable with unresolved questions",
        )
        persist_mock.assert_not_awaited()
        unchanged = await get_task_state(state.task_id)
        self.assertEqual(unchanged.revision, state.revision)
        self.assertNotIn("shoppingGuide", unchanged.domain_state)
        self.assertEqual(unchanged.facts, [])

    async def test_first_legal_executable_payload_uses_one_call_and_one_persist(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-legal-executable-once",
            )
        )
        legal = {
            "status": "ready",
            "pendingQuestions": [],
            "upsertFacts": [{
                "key": "os",
                "value": "ios",
                "certainty": "confirmed",
                "source": "user",
            }],
            "domainStatePatch": {
                "shoppingGuide": {
                    "mode": "recommend",
                    "category": "phone",
                    "requirements": [{
                        "key": "os",
                        "operator": "eq",
                        "value": "ios",
                        "unit": "enum",
                        "priority": "hard",
                        "source": "user",
                    }],
                }
            },
        }
        state_call = _make_tool_call(
            "call-state",
            "update_task_state",
            json.dumps(legal, ensure_ascii=False),
        )
        create_mock = AsyncMock(side_effect=[_make_response(tool_calls=[state_call])])

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            updated = await _update_task_state_for_unified_harness(
                "想找 iOS 二手机。",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        self.assertEqual(create_mock.await_count, 1)
        self.assertEqual(updated.status, "ready")
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision + 1)
        self.assertEqual(latest.facts[0].key, "os")
        self.assertEqual(latest.facts[0].value, "ios")
        self.assertEqual(
            latest.domain_state["shoppingGuide"]["requirements"][0]["priority"],
            "hard",
        )

    async def test_smoke_004_legal_collecting_correction_persists_once(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-smoke004-legal-collecting",
            )
        )

        updated = await _apply_task_state_update(
            state,
            _smoke_004_legal_collecting_args(),
            message="想找 iOS 二手机。",
            on_task_state=None,
        )

        self.assertEqual(updated.status, "collecting_information")
        self.assertEqual(updated.unknowns, ["预算或具体用途尚不清楚"])
        self.assertEqual(updated.pending_questions, [_SMOKE_004_PENDING_QUESTION])
        self.assertEqual(updated.goal, "想找 iOS 二手机。")
        self.assertEqual(updated.facts[0].key, "os")
        self.assertEqual(updated.facts[0].value, "ios")
        requirement = updated.domain_state["shoppingGuide"]["requirements"][0]
        self.assertEqual(requirement["key"], "os")
        self.assertEqual(requirement["value"], "ios")
        self.assertEqual(requirement["priority"], "hard")
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision + 1)

    async def test_smoke_006_optional_preferences_rejected_before_persistence(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-smoke006-nonblocking-unknowns",
            )
        )

        with self.assertRaises(TaskStatePayloadValidationError) as ctx:
            _build_validated_task_state_payload(
                state,
                _SMOKE_006_FROZEN_ARGS,
                message="想找 iOS 二手机。",
                require_status=True,
                allow_auto_ready=False,
            )

        self.assertEqual(
            ctx.exception.code,
            "nonblocking_shopping_preference_marked_unknown",
        )
        self.assertEqual(ctx.exception.field_path, "addUnknowns[0,1,2]")
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision)
        self.assertEqual(latest.unknowns, [])

    async def test_smoke_006_payload_repairs_once_to_ready_and_persists_once(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-smoke006-bounded-repair",
            )
        )
        original_call = _make_tool_call(
            "call-smoke006",
            "update_task_state",
            json.dumps(_SMOKE_006_FROZEN_ARGS, ensure_ascii=False),
        )
        repaired_call = _make_tool_call(
            "call-smoke006-repair",
            "update_task_state",
            json.dumps(_smoke_006_legal_ready_args(), ensure_ascii=False),
        )
        create_mock = AsyncMock(side_effect=[
            _make_response(tool_calls=[original_call]),
            _make_response(tool_calls=[repaired_call]),
        ])

        with patch("app.llm._persist_task_patch", wraps=_apply_task_state_update.__globals__["_persist_task_patch"]) as persist_mock:
            updated = await _update_task_state_for_unified_harness(
                "想找 iOS 二手机。",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        self.assertEqual(create_mock.await_count, 2)
        self.assertEqual(persist_mock.await_count, 1)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.unknowns, [])
        self.assertEqual(updated.pending_questions, [])
        self.assertEqual(
            [item["kind"] for item in updated.domain_state["optionalShoppingQuestions"]],
            ["brand", "model", "budget", "use_case"],
        )
        repair_messages = create_mock.await_args_list[1].kwargs["messages"]
        rejection = json.loads(repair_messages[5]["content"])
        self.assertEqual(
            rejection["code"],
            "nonblocking_shopping_preference_marked_unknown",
        )
        self.assertEqual(rejection["field_path"], "addUnknowns[0,1,2]")
        self.assertEqual(rejection["persist"], 0)

    async def test_smoke_007_aggregates_mojibake_owned_key_and_optional_unknowns(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-smoke007-multiple-violations",
            )
        )
        original = _make_tool_call(
            "call-smoke007",
            "update_task_state",
            json.dumps(_smoke_007_frozen_args(), ensure_ascii=False),
        )
        repaired = _make_tool_call(
            "call-smoke007-repair",
            "update_task_state",
            json.dumps(_smoke_006_legal_ready_args(), ensure_ascii=False),
        )
        create_mock = AsyncMock(side_effect=[
            _make_response(tool_calls=[original]),
            _make_response(tool_calls=[repaired]),
        ])

        updated = await _update_task_state_for_unified_harness(
            "想找 iOS 二手机。",
            history=None,
            client=_fake_client(create_mock),
            task_state=state,
            on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 2)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.goal, "想找 iOS 二手机。")
        rejection = json.loads(
            create_mock.await_args_list[1].kwargs["messages"][5]["content"]
        )
        self.assertEqual(rejection["code"], "multiple_task_state_payload_violations")
        self.assertEqual(rejection["field_path"], "$")
        self.assertIn("$.goal", rejection["message"])
        self.assertIn("domainStatePatch.shoppingGuide.evidenceStatus", rejection["message"])
        self.assertIn("addUnknowns[0]", rejection["message"])
        self.assertEqual(rejection["persist"], 0)

    async def test_genuine_clarification_uses_string_context_pack_content(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="比较两台二手手机",
                sessionId="session-genuine-clarification-message-shape",
            )
        )
        collecting_call = _make_tool_call(
            "call-collecting",
            "update_task_state",
            json.dumps({
                "status": "collecting_information",
                "addUnknowns": ["比较对象身份"],
                "pendingQuestions": ["请明确要比较的两台手机。"],
                "domainStatePatch": {
                    "shoppingGuide": {"mode": "compare", "category": "phone"}
                },
            }, ensure_ascii=False),
        )
        create_mock = AsyncMock(side_effect=[
            _make_response(tool_calls=[collecting_call]),
            _make_response(content="请明确要比较的两台手机。"),
        ])

        with patch(
            "app.llm.settings.agent_control_runtime", "fixed_v1",
        ), patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", False,
        ), patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            answer, traces, _turns, _run_id, _summary = await _run_unified_harness_agent(
                "比较两台二手手机",
                history=None,
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
            )

        self.assertEqual(answer, "请明确要比较的两台手机。")
        self.assertEqual(traces, [])
        self.assertEqual(create_mock.await_count, 2)
        question_messages = create_mock.await_args_list[1].kwargs["messages"]
        self.assertEqual(question_messages[0]["role"], "system")
        self.assertIsInstance(question_messages[0]["content"], str)
        self.assertNotIsInstance(question_messages[0]["content"], dict)

    async def test_durable_clarification_enters_graph_and_restart_skips_extractor(self):
        from app.reference_context import ResolvedReferenceContext

        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        initial = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="比较两台二手手机",
                sessionId="session-durable-clarification-route",
            )
        )
        state = initial.model_copy(
            update={
                "revision": initial.revision + 1,
                "status": "collecting_information",
                "unknowns": ["比较对象身份"],
                "pending_questions": ["请明确要比较的两台手机。"],
            }
        )
        expected = ("请明确要比较的两台手机。", [], [], "run-durable", None)
        reference_context = ResolvedReferenceContext(
            task_id=state.task_id,
            task_revision=state.revision,
            scope_id="scope-durable-reference",
            scope_source_revision=1,
            presentation_mode="compact",
            presentation_ids=(101, 102, 103),
            compact_product_ids=(101, 102, 103),
            expanded_product_ids=(101, 102, 103),
            compared_product_ids=(),
            previous_batch_product_ids=(),
            focused_product_id=102,
        )
        extractor = AsyncMock(return_value=state)
        explicit = AsyncMock(return_value=expected)
        with patch("app.llm.get_client", return_value=SimpleNamespace()), patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.llm._update_task_state_for_unified_harness", new=extractor,
        ), patch(
            "app.llm._run_explicit_harness_agent", new=explicit,
        ):
            fresh = await _run_unified_harness_agent(
                "比较两台二手手机",
                history=None,
                task_state=initial,
                on_answer_delta=None,
                on_task_state=None,
                session_id=state.session_id,
                reference_context=reference_context,
            )
            restarted = await _run_unified_harness_agent(
                "继续任务",
                history=None,
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
                restart=True,
                session_id=state.session_id,
                reference_context=reference_context,
            )

        self.assertEqual(fresh, expected)
        self.assertEqual(restarted, expected)
        # Fresh extraction happened once; restart used the authoritative state.
        self.assertEqual(extractor.await_count, 1)
        self.assertEqual(explicit.await_count, 2)
        self.assertFalse(explicit.await_args_list[0].kwargs["restart"])
        self.assertTrue(explicit.await_args_list[1].kwargs["restart"])
        self.assertIs(
            explicit.await_args_list[1].kwargs["reference_context"],
            reference_context,
        )

    async def test_durable_clarification_applier_consumes_rebased_reference_context(self):
        """The graph's receipt revision must not erase a browser-selected focus."""

        from app.llm import _run_explicit_harness_agent
        from app.reference_context import ResolvedReferenceContext

        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        initial = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="比较这个和另一个",
                sessionId="session-durable-reference-applier",
                status="collecting_information",
                unknowns=["商品序号没有唯一绑定"],
                pendingQuestions=["请明确比较对象。"],
            )
        )
        answer_text = "比较这个和第三个"
        answer_hash = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()[:16]
        receipt_state = initial.model_copy(update={
            "revision": initial.revision + 1,
            "domain_state": {
                **initial.domain_state,
                "v2PendingClarification": {
                    "status": "resolved",
                    "taskId": initial.task_id,
                    "appliedRevision": initial.revision + 1,
                    "answerHash": answer_hash,
                },
            },
        })
        semantic_state = receipt_state.model_copy(
            update={"revision": receipt_state.revision + 1}
        )
        reference_context = ResolvedReferenceContext(
            task_id=initial.task_id,
            task_revision=initial.revision,
            scope_id="scope-reference-applier",
            scope_source_revision=1,
            presentation_mode="compact",
            presentation_ids=(101, 102, 103),
            compact_product_ids=(101, 102, 103),
            expanded_product_ids=(101, 102, 103),
            compared_product_ids=(),
            previous_batch_product_ids=(),
            focused_product_id=102,
        )
        seen_reference: list[ResolvedReferenceContext | None] = []

        async def semantic_applier(_message, **kwargs):
            seen_reference.append(kwargs.get("reference_context"))
            return semantic_state

        async def graph_call(**kwargs):
            applied = await kwargs["clarification_answer_applier"](
                receipt_state,
                answer_text,
            )
            return SimpleNamespace(
                task_state=applied,
                boundary="clarification",
                mode="resume",
                question="继续核对。",
                run_id="run-reference-applier",
                thread_id=f"v2-task:{initial.task_id}:run-reference-applier",
                proposal_hash="proposal-reference-applier",
            )

        with patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.llm.settings.agent_context_mode", "legacy",
        ), patch(
            "app.graph.resolve_durable_identity",
            new=AsyncMock(return_value=(
                "run-reference-applier",
                f"v2-task:{initial.task_id}:run-reference-applier",
            )),
        ), patch(
            "app.graph.run_graph_v2_durable", new=graph_call,
        ), patch(
            "app.llm._update_task_state_for_unified_harness",
            new=semantic_applier,
        ), patch(
            "app.llm._persist_trace_safely", new=AsyncMock(),
        ):
            await _run_explicit_harness_agent(
                answer_text,
                history=None,
                client=SimpleNamespace(),
                task_state=initial,
                on_answer_delta=None,
                on_task_state=None,
                resume={
                    "taskId": initial.task_id,
                    "runId": "run-reference-applier",
                    "threadId": f"v2-task:{initial.task_id}:run-reference-applier",
                    "revision": initial.revision,
                    "proposalHash": "proposal-reference-applier",
                    "answer": answer_text,
                },
                session_id=initial.session_id,
                reference_context=reference_context,
            )

        self.assertEqual(len(seen_reference), 1)
        applied_reference = seen_reference[0]
        self.assertIsNotNone(applied_reference)
        self.assertEqual(applied_reference.task_revision, receipt_state.revision)
        self.assertEqual(applied_reference.focused_product_id, 102)
        self.assertEqual(applied_reference.presentation_ids, (101, 102, 103))

    async def test_durable_clarification_applier_rejects_unbound_revision_jump(self):
        """A stale/forged focus cannot cross more than the graph-owned receipt write."""

        from app.llm import _run_explicit_harness_agent
        from app.reference_context import ResolvedReferenceContext

        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        created = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="比较这个和另一个",
            sessionId="session-durable-reference-drift",
            status="collecting_information",
            pendingQuestions=["请明确比较对象。"],
        ))
        initial = created.model_copy(update={"revision": 5})
        answer_text = "比较这个和第三个"
        drifted = initial.model_copy(update={
            "revision": 7,
            "domain_state": {
                "v2PendingClarification": {
                    "status": "resolved",
                    "taskId": initial.task_id,
                    "appliedRevision": 7,
                    "answerHash": hashlib.sha256(
                        answer_text.encode("utf-8")
                    ).hexdigest()[:16],
                }
            },
        })
        reference_context = ResolvedReferenceContext(
            task_id=initial.task_id,
            task_revision=5,
            scope_id="scope-reference-drift",
            scope_source_revision=1,
            presentation_mode="compact",
            presentation_ids=(101, 102, 103),
            compact_product_ids=(101, 102, 103),
            expanded_product_ids=(101, 102, 103),
            compared_product_ids=(),
            previous_batch_product_ids=(),
            focused_product_id=102,
        )
        semantic_applier = AsyncMock()

        async def graph_call(**kwargs):
            await kwargs["clarification_answer_applier"](drifted, answer_text)
            raise AssertionError("invalid transition must stop before graph result")

        with patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.llm.settings.agent_context_mode", "legacy",
        ), patch(
            "app.graph.resolve_durable_identity",
            new=AsyncMock(return_value=(
                "run-reference-drift",
                f"v2-task:{initial.task_id}:run-reference-drift",
            )),
        ), patch(
            "app.graph.run_graph_v2_durable", new=graph_call,
        ), patch(
            "app.llm._update_task_state_for_unified_harness",
            new=semantic_applier,
        ), patch(
            "app.llm._persist_trace_safely", new=AsyncMock(),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "durable_reference_context_transition_invalid",
            ):
                await _run_explicit_harness_agent(
                    answer_text,
                    history=None,
                    client=SimpleNamespace(),
                    task_state=initial,
                    on_answer_delta=None,
                    on_task_state=None,
                    resume={
                        "taskId": initial.task_id,
                        "runId": "run-reference-drift",
                        "threadId": f"v2-task:{initial.task_id}:run-reference-drift",
                        "revision": initial.revision,
                        "proposalHash": "proposal-reference-drift",
                        "answer": answer_text,
                    },
                    session_id=initial.session_id,
                    reference_context=reference_context,
                )

        semantic_applier.assert_not_awaited()

    async def test_durable_interrupt_notifies_rehydrated_live_revision(self):
        from app.llm import _run_explicit_harness_agent

        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        initial = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="不要这个，帮我挑一台二手手机。",
                sessionId="session-durable-live-boundary",
                unknowns=["否定对象"],
                pendingQuestions=["请明确不想要的品牌。"],
            )
        )
        live = initial.model_copy(update={"revision": initial.revision + 1})
        run_id = "run-live-boundary"
        result = SimpleNamespace(
            task_state=live,
            boundary="clarification",
            mode="fresh",
            question="请明确不想要的品牌。",
            run_id=run_id,
            thread_id=f"v2-task:{initial.task_id}:{run_id}",
            proposal_hash="proposal-live",
        )
        callback = AsyncMock()
        graph_call = AsyncMock(return_value=result)
        with patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.llm.settings.agent_context_mode", "legacy",
        ), patch(
            "app.graph.run_graph_v2_durable", new=graph_call,
        ), patch(
            "app.llm._persist_trace_safely", new=AsyncMock(),
        ):
            answer, _traces, _turns, observed_run, summary = (
                await _run_explicit_harness_agent(
                    initial.goal,
                    history=None,
                    client=SimpleNamespace(),
                    task_state=initial,
                    on_answer_delta=None,
                    on_task_state=callback,
                    session_id=initial.session_id,
                )
            )

        self.assertEqual(answer, result.question)
        # The harness owns the fresh run id and passes it into the graph; this
        # mock result uses a sentinel only for its thread fields.
        self.assertTrue(observed_run.startswith("run-"))
        self.assertEqual(summary.final_action, "ask_user")
        self.assertEqual(summary.durable_resume, {
            "taskId": initial.task_id,
            "runId": run_id,
            "threadId": result.thread_id,
            "revision": live.revision,
            "proposalHash": "proposal-live",
        })
        callback.assert_awaited_once_with(live, "durable_clarification")
        owned_trace = graph_call.await_args.kwargs["trace_builder"].finish()
        self.assertEqual(owned_trace.task_id, initial.task_id)
        self.assertEqual(owned_trace.session_id, initial.session_id)

    async def test_durable_final_answer_rebuilds_context_from_live_revision(self):
        from app import llm as llm_module
        from app.llm import _run_explicit_harness_agent

        fake_redis = FakeRedis()
        task_state_store._client = fake_redis
        task_state_store._task_locks.clear()
        initial = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="推荐一件商品",
                sessionId="session-durable-final-live",
                status="ready",
                domainState={
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "requirements": [],
                        "candidateIds": [],
                        "comparedIds": [],
                        "evidenceStatus": "missing",
                    }
                },
            )
        )
        live = await update_task_state(
            initial.task_id,
            TaskStatePatchRequest(
                expectedRevision=initial.revision,
                actor="agent",
                domainStatePatch={"finalProjectionMarker": "live"},
            ),
        )
        original_eval = fake_redis.eval
        lost_atomic_response = False

        async def commit_then_lose_response(script, numkeys, *args):
            nonlocal lost_atomic_response
            result = await original_eval(script, numkeys, *args)
            if numkeys == 2 and not lost_atomic_response:
                lost_atomic_response = True
                raise RuntimeError("atomic commit response lost")
            return result

        fake_redis.eval = commit_then_lose_response
        result = SimpleNamespace(
            task_state=live,
            boundary="task_completed",
            mode="fresh",
            proposal_hash=None,
            run_id="run-live-final",
            thread_id=f"v2-task:{initial.task_id}:run-live-final",
        )
        seen_revisions: list[int] = []
        published: list[str] = []
        original_build = llm_module.build_context_pack
        async def publish_after_receipt(delta: str) -> None:
            committed = await get_task_state(initial.task_id)
            self.assertIn("v2FinalAnswerReceipt", committed.domain_state)
            self.assertTrue(any(
                key.startswith("graph-v2:terminal:")
                for key in task_state_store._client.strings
            ))
            published.append(delta)

        async def recording_build(state, **kwargs):
            seen_revisions.append(state.revision)
            return await original_build(state, **kwargs)

        answer_model = AsyncMock(return_value="基于最新状态生成的回答")
        state_callback = AsyncMock()
        with patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.llm.settings.agent_context_mode", "context_pack",
        ), patch(
            "app.llm.settings.evidence_critic_enabled", False,
        ), patch(
            "app.graph.run_graph_v2_durable", new=AsyncMock(return_value=result),
        ), patch(
            "app.llm.build_context_pack", new=recording_build,
        ), patch(
            "app.llm._generate_final_answer", new=answer_model,
        ), patch(
            "app.llm._persist_trace_safely", new=AsyncMock(),
        ):
            answer, _traces, _turns, _run_id, _summary = (
                await _run_explicit_harness_agent(
                    initial.goal,
                    history=None,
                    client=SimpleNamespace(),
                    task_state=initial,
                    on_answer_delta=publish_after_receipt,
                    on_task_state=state_callback,
                    session_id=initial.session_id,
                )
            )

        self.assertEqual(answer, "基于最新状态生成的回答")
        self.assertTrue(lost_atomic_response)
        self.assertEqual(published, [answer])
        finalized = await get_task_state(initial.task_id)
        final_receipt = finalized.domain_state["v2FinalAnswerReceipt"]
        self.assertEqual(final_receipt["baseTaskRevision"], live.revision)
        self.assertEqual(final_receipt["finalizationRevision"], live.revision + 1)
        final_callback_state, final_callback_phase = state_callback.await_args_list[-1].args
        self.assertEqual(final_callback_phase, "durable_terminal_response_published")
        self.assertEqual(final_callback_state.revision, finalized.revision)
        self.assertEqual(
            final_callback_state.domain_state["v2FinalAnswerReceipt"],
            final_receipt,
        )
        self.assertEqual(seen_revisions[0], initial.revision)
        self.assertEqual(seen_revisions[-1], live.revision)
        final_view = answer_model.await_args.kwargs["final_answer_view"]
        self.assertEqual(final_view.phase_task_revision, live.revision)
        self.assertEqual(final_view.base_context_revision, live.revision)

        restart_result = SimpleNamespace(
            task_state=finalized,
            boundary="task_completed",
            mode="restart",
            proposal_hash=None,
            run_id=result.run_id,
            thread_id=result.thread_id,
        )
        restart_model = AsyncMock(return_value="不得生成的第二个答案")
        restart_publish = AsyncMock()
        before_restart_revision = finalized.revision
        with patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.llm.settings.agent_context_mode", "context_pack",
        ), patch(
            "app.graph.run_graph_v2_durable",
            new=AsyncMock(return_value=restart_result),
        ), patch(
            "app.llm._generate_final_answer", new=restart_model,
        ), patch(
            "app.llm._persist_trace_safely", new=AsyncMock(),
        ):
            replayed_answer, *_ = await _run_explicit_harness_agent(
                initial.goal,
                history=None,
                client=SimpleNamespace(),
                task_state=finalized,
                on_answer_delta=restart_publish,
                on_task_state=None,
                restart=True,
                session_id=initial.session_id,
            )
        self.assertEqual(replayed_answer, "基于最新状态生成的回答")
        restart_model.assert_not_awaited()
        restart_publish.assert_awaited_once_with(replayed_answer)
        after_restart = await get_task_state(initial.task_id)
        self.assertEqual(after_restart.revision, before_restart_revision)
        self.assertEqual(sum(
            key.startswith("graph-v2:terminal:")
            for key in task_state_store._client.strings
        ), 1)

    async def test_durable_final_answer_discards_output_on_generation_revision_drift(self):
        from app.llm import _run_explicit_harness_agent

        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        initial = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="推荐一台手机",
                sessionId="session-durable-final-drift",
                status="ready",
                domainState={
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "requirements": [],
                        "candidateIds": [],
                        "comparedIds": [],
                        "evidenceStatus": "missing",
                    }
                },
            )
        )
        live = await update_task_state(
            initial.task_id,
            TaskStatePatchRequest(
                expectedRevision=initial.revision,
                actor="agent",
                domainStatePatch={"finalProjectionMarker": "live"},
            ),
        )
        result = SimpleNamespace(
            task_state=live,
            boundary="task_completed",
            mode="fresh",
            proposal_hash=None,
            run_id="run-final-drift",
            thread_id=f"v2-task:{initial.task_id}:run-final-drift",
        )

        async def generate_then_drift(*_args, **_kwargs):
            await update_task_state(
                live.task_id,
                TaskStatePatchRequest(
                    expectedRevision=live.revision,
                    actor="user",
                    domainStatePatch={"concurrentConstraintUpdate": True},
                ),
            )
            return "这是一段已经过期的回答"

        callback = AsyncMock()
        with patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.llm.settings.agent_context_mode", "context_pack",
        ), patch(
            "app.llm.settings.evidence_critic_enabled", False,
        ), patch(
            "app.graph.run_graph_v2_durable", new=AsyncMock(return_value=result),
        ), patch(
            "app.llm._generate_final_answer", new=AsyncMock(
                side_effect=generate_then_drift
            ),
        ), patch(
            "app.llm._persist_trace_safely", new=AsyncMock(),
        ):
            answer, _traces, _turns, _run_id, summary = (
                await _run_explicit_harness_agent(
                    initial.goal,
                    history=None,
                    client=SimpleNamespace(),
                    task_state=initial,
                    on_answer_delta=callback,
                    on_task_state=None,
                    session_id=initial.session_id,
                )
            )

        self.assertIn("旧答案已丢弃", answer)
        self.assertNotIn("已经过期的回答", answer)
        callback.assert_awaited_once_with(answer)
        self.assertEqual(summary.final_action, "state_diverged")
        self.assertTrue(summary.degraded)
        self.assertEqual(summary.failure_code, "final_answer_state_diverged")

    async def test_concurrent_durable_finalizers_publish_only_one_candidate(self):
        from app.llm import _run_explicit_harness_agent

        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        initial = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="推荐一台手机",
            sessionId="session-concurrent-finalizers",
            status="ready",
            domainState={"shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        ))
        live = await update_task_state(
            initial.task_id,
            TaskStatePatchRequest(
                expectedRevision=initial.revision,
                actor="agent",
                domainStatePatch={"finalProjectionMarker": "live"},
            ),
        )
        durable_result = SimpleNamespace(
            task_state=live,
            boundary="task_completed",
            mode="fresh",
            proposal_hash=None,
            run_id="run-concurrent-finalizers",
            thread_id=f"v2-task:{initial.task_id}:run-concurrent-finalizers",
        )
        both_generated = asyncio.Event()
        generated_count = 0

        async def generate_candidate(*_args, **_kwargs):
            nonlocal generated_count
            generated_count += 1
            own = generated_count
            if generated_count == 2:
                both_generated.set()
            await both_generated.wait()
            return f"candidate-{own}"

        published: list[str] = []

        async def capture(delta: str) -> None:
            published.append(delta)

        async def run_one():
            return await _run_explicit_harness_agent(
                initial.goal,
                history=None,
                client=SimpleNamespace(),
                task_state=initial,
                on_answer_delta=capture,
                on_task_state=None,
                session_id=initial.session_id,
            )

        with patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.llm.settings.agent_context_mode", "context_pack",
        ), patch(
            "app.llm.settings.evidence_critic_enabled", False,
        ), patch(
            "app.graph.run_graph_v2_durable",
            new=AsyncMock(return_value=durable_result),
        ), patch(
            "app.llm._generate_final_answer", new=generate_candidate,
        ), patch(
            "app.llm._persist_trace_safely", new=AsyncMock(),
        ):
            results = await asyncio.gather(run_one(), run_one())

        answers = [item[0] for item in results]
        self.assertEqual(sum(answer.startswith("candidate-") for answer in answers), 1)
        self.assertEqual(sum(delta.startswith("candidate-") for delta in published), 1)
        finalized = await get_task_state(initial.task_id)
        receipt = finalized.domain_state["v2FinalAnswerReceipt"]
        self.assertEqual(receipt["baseTaskRevision"], live.revision)
        self.assertEqual(receipt["finalizationRevision"], live.revision + 1)

    async def test_unified_harness_conflict_triggers_bounded_repair_and_persists(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-smoke004-unified-repair",
            )
        )
        frozen_call = _make_tool_call(
            "call-state",
            "update_task_state",
            json.dumps(_SMOKE_004_FROZEN_ARGS, ensure_ascii=False),
        )
        corrected_call = _make_tool_call(
            "call-state-repair",
            "update_task_state",
            json.dumps(_smoke_004_legal_ready_args(), ensure_ascii=False),
        )
        create_mock = AsyncMock(side_effect=[
            _make_response(tool_calls=[frozen_call]),
            _make_response(tool_calls=[corrected_call]),
        ])

        updated = await _update_task_state_for_unified_harness(
            "想找 iOS 二手机。",
            history=None,
            client=_fake_client(create_mock),
            task_state=state,
            on_task_state=None,
        )

        self.assertEqual(create_mock.await_count, 2)
        self.assertEqual(updated.status, "ready")

        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.status, "ready")
        self.assertEqual(latest.unknowns, [])
        self.assertEqual(latest.pending_questions, [])
        self.assertEqual(latest.goal, "想找 iOS 二手机。")
        self.assertEqual(latest.facts[0].key, "os")
        self.assertEqual(latest.facts[0].value, "ios")
        requirement = latest.domain_state["shoppingGuide"]["requirements"][0]
        self.assertEqual(requirement["key"], "os")
        self.assertEqual(requirement["value"], "ios")
        self.assertEqual(requirement["priority"], "hard")

        # The repair request replays the original tool call and the exact
        # structured rejection (persist=0) so the model can correct itself.
        repair_messages = create_mock.await_args_list[1].kwargs["messages"]
        self.assertEqual(len(repair_messages), 7)
        replayed = repair_messages[4]
        self.assertEqual(replayed["role"], "assistant")
        self.assertEqual(replayed["tool_calls"][0]["id"], "call-state")
        self.assertEqual(
            replayed["tool_calls"][0]["function"]["name"],
            "update_task_state",
        )
        self.assertEqual(
            json.loads(replayed["tool_calls"][0]["function"]["arguments"]),
            _SMOKE_004_FROZEN_ARGS,
        )
        rejection = repair_messages[5]
        self.assertEqual(rejection["role"], "tool")
        self.assertEqual(rejection["tool_call_id"], "call-state")
        parsed_rejection = json.loads(rejection["content"])
        self.assertIs(parsed_rejection["valid"], False)
        self.assertEqual(parsed_rejection["persist"], 0)
        self.assertEqual(
            parsed_rejection["code"],
            "task_cannot_become_executable_with_unresolved_questions",
        )
        self.assertEqual(
            parsed_rejection["message"],
            "task cannot become executable with unresolved questions",
        )
        self.assertEqual(repair_messages[6]["role"], "system")
        self.assertEqual(repair_messages[6]["content"], TASK_STATE_REPAIR_PROMPT)

    async def test_unified_harness_second_invalid_repair_fails_closed_without_third_call(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-smoke004-repair-invalid",
            )
        )
        frozen_call = _make_tool_call(
            "call-state",
            "update_task_state",
            json.dumps(_SMOKE_004_FROZEN_ARGS, ensure_ascii=False),
        )
        still_invalid_call = _make_tool_call(
            "call-state-repair",
            "update_task_state",
            json.dumps(_SMOKE_004_FROZEN_ARGS, ensure_ascii=False),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[frozen_call]),
                _make_response(tool_calls=[still_invalid_call]),
            ]
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.tools.call_tool",
            new=AsyncMock(),
        ) as call_tool_mock:
            answer, traces, _turn_messages, _run_id, _summary = (
                await _run_unified_harness_agent(
                    "想找 iOS 二手机。",
                    history=None,
                    task_state=state,
                    on_answer_delta=None,
                    on_task_state=None,
                )
            )

        self.assertIn("任务状态构建失败", answer)
        self.assertIn("系统已安全停止", answer)
        self.assertEqual(traces, [])
        call_tool_mock.assert_not_awaited()
        self.assertEqual(create_mock.await_count, 2)
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision)
        self.assertNotIn("shoppingGuide", latest.domain_state)
        self.assertEqual(latest.facts, [])

    async def test_persist_backend_failure_does_not_trigger_repair(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-persist-failure-no-repair",
            )
        )
        valid_executable = {
            "status": "ready",
            "pendingQuestions": [],
            "upsertFacts": [{
                "key": "os",
                "value": "ios",
                "certainty": "confirmed",
                "source": "user",
            }],
            "domainStatePatch": {
                "shoppingGuide": {
                    "mode": "recommend",
                    "category": "phone",
                    "requirements": [{
                        "key": "os",
                        "operator": "eq",
                        "value": "ios",
                        "unit": "enum",
                        "priority": "hard",
                        "source": "user",
                    }],
                }
            },
        }
        state_call = _make_tool_call(
            "call-state",
            "update_task_state",
            json.dumps(valid_executable, ensure_ascii=False),
        )
        create_mock = AsyncMock(side_effect=[_make_response(tool_calls=[state_call])])

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.update_task_state",
            new=AsyncMock(side_effect=RuntimeError("backend unavailable")),
        ), patch("app.tools.call_tool", new=AsyncMock()) as call_tool_mock:
            answer, traces, _turn_messages, _run_id, _summary = (
                await _run_unified_harness_agent(
                    "想找 iOS 二手机。",
                    history=None,
                    task_state=state,
                    on_answer_delta=None,
                    on_task_state=None,
                )
            )

        self.assertIn("任务状态构建失败", answer)
        self.assertEqual(traces, [])
        call_tool_mock.assert_not_awaited()
        self.assertEqual(create_mock.await_count, 1)
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision)
        self.assertEqual(latest.facts, [])

    async def test_deadline_timeout_does_not_trigger_repair(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-timeout-no-repair",
            )
        )
        never = asyncio.Future()
        loop = asyncio.get_running_loop()

        async def _hang(*_args, **_kwargs):
            await never
            return _make_response(content="unreachable")

        create_mock = AsyncMock(side_effect=_hang)

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.tools.call_tool",
            new=AsyncMock(),
        ) as call_tool_mock:
            answer, traces, _turn_messages, _run_id, _summary = (
                await _run_unified_harness_agent(
                    "想找 iOS 二手机。",
                    history=None,
                    task_state=state,
                    on_answer_delta=None,
                    on_task_state=None,
                    deadline_at=loop.time() + 0.05,
                )
            )

        self.assertIn("总执行时间限制", answer)
        self.assertEqual(traces, [])
        call_tool_mock.assert_not_awaited()
        self.assertLessEqual(create_mock.await_count, 1)
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision)

    async def test_executable_repair_with_optional_questions_persists_and_retains_facts(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-executable-repair-optional",
            )
        )
        executable = {
            "domainStatePatch": {
                "shoppingGuide": {
                    "mode": "recommend",
                    "category": "phone",
                    "requirements": [{
                        "key": "os",
                        "operator": "eq",
                        "value": "ios",
                        "unit": "enum",
                        "priority": "hard",
                        "source": "user",
                    }],
                }
            },
            "goal": "想找 iOS 二手机。",
            "optionalShoppingQuestions": [
                {"kind": "budget"},
                {"kind": "model"},
                {"kind": "use_case"},
            ],
            "status": "ready",
            "upsertFacts": [{
                "key": "os",
                "value": "ios",
                "certainty": "confirmed",
                "source": "user",
            }],
        }
        updated = await _apply_task_state_update(
            state,
            executable,
            message="想找 iOS 二手机。",
            on_task_state=None,
        )

        self.assertEqual(updated.status, "ready")
        self.assertEqual(updated.unknowns, [])
        self.assertEqual(updated.pending_questions, [])
        self.assertEqual(updated.facts[0].key, "os")
        self.assertEqual(updated.facts[0].value, "ios")
        requirement = updated.domain_state["shoppingGuide"]["requirements"][0]
        self.assertEqual(requirement["key"], "os")
        self.assertEqual(requirement["value"], "ios")
        self.assertEqual(requirement["priority"], "hard")
        self.assertEqual(
            [item["kind"] for item in updated.domain_state["optionalShoppingQuestions"]],
            ["budget", "model", "use_case"],
        )

    async def test_task_state_review_task_runs_through_explicit_harness(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        task_state_store._session_locks.clear()
        question = "星河咖啡适合安静办公吗？"
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="local_life",
                goal=question,
                sessionId="session-explicit-harness",
            )
        )
        state_call = _make_tool_call(
            "call-state",
            "update_task_state",
            json.dumps(
                {
                    "status": "ready",
                    "upsertFacts": [
                        {
                            "key": "shopName",
                            "value": "星河咖啡",
                            "certainty": "confirmed",
                            "source": "user",
                        }
                    ],
                    "pendingQuestions": [],
                },
                ensure_ascii=False,
            ),
        )
        planner_call = _make_tool_call(
            "call-planner",
            "submit_planner_output",
            json.dumps(
                {
                    "outcome": "planned",
                    "steps": [
                        {
                            "stepId": "step-review",
                            "description": "查询目标商户的真实评论证据",
                            "toolName": "search_shop_reviews",
                            "arguments": {
                                "query": question,
                                "shopName": "星河咖啡",
                            },
                            "argumentSources": {
                                "query": {"kind": "task_goal"},
                                "shopName": {
                                    "kind": "task_state",
                                    "reference": "facts.shopName",
                                },
                            },
                            "expectedOutput": {
                                "requiresReviewEvidence": True,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[state_call]),
                _make_response(tool_calls=[planner_call]),
                _make_response(content="评论提到工作日下午较安静。[review-101]"),
            ]
        )
        review_trace = ToolTrace(
            tool="search_shop_reviews",
            ok=True,
            durationMs=15.0,
            detail={
                "resolvedShop": {"id": 101, "name": "星河咖啡"},
                "reviews": [
                    {
                        "reviewId": "review-101",
                        "shopId": 101,
                        "text": "工作日下午较安静。",
                    }
                ],
            },
        )
        state_updates = []

        async def collect_state(updated, phase):
            state_updates.append((phase, updated))

        with patch(
            "app.llm.settings.agent_control_runtime", "fixed_v1",
        ), patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", False,
        ), patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.tools.call_tool",
            new=AsyncMock(return_value=review_trace),
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent(
                question,
                task_state=state,
                on_task_state=collect_state,
            )

        latest = await get_task_state(state.task_id)
        self.assertEqual(answer, "评论提到工作日下午较安静。[review-101]")
        self.assertEqual(traces, [review_trace])
        self.assertEqual(latest.status, "completed")
        self.assertEqual(latest.active_plan.status, "completed")
        self.assertEqual(
            latest.domain_state["validationResult"]["outcome"],
            "passed",
        )
        self.assertIn(
            "harness_task_completed",
            [phase for phase, _updated in state_updates],
        )
        call_tool_mock.assert_awaited_once_with(
            "search_shop_reviews",
            {
                "query": question,
                "shopName": "星河咖啡",
            },
        )
        self.assertEqual(
            _tool_names(create_mock.await_args_list[1].kwargs["tools"]),
            ["submit_planner_output"],
        )
        self.assertNotIn("tools", create_mock.await_args_list[2].kwargs)
        self.assertEqual(
            turn_messages,
            [
                {"role": "user", "content": question},
                {
                    "role": "assistant",
                    "content": "评论提到工作日下午较安静。[review-101]",
                },
            ],
        )

    async def test_streaming_agent_keeps_planning_non_stream_and_streams_final_answer(self):
        planning = _make_response(content="READY_TO_ANSWER", tool_calls=None)
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="北京"))
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="出行"))
                ]
            ),
        ]
        create_mock = AsyncMock(side_effect=[planning, _AsyncChunks(chunks)])
        deltas = []

        async def collect_delta(delta):
            deltas.append(delta)

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            answer, traces, turn_messages, _run_id, _summary = await run_agent(
                "介绍北京出行",
                on_answer_delta=collect_delta,
            )

        self.assertEqual(answer, "北京出行")
        self.assertEqual(traces, [])
        self.assertEqual(deltas, ["北京", "出行"])
        self.assertNotIn("stream", create_mock.await_args_list[0].kwargs)
        self.assertIn("tools", create_mock.await_args_list[0].kwargs)
        self.assertTrue(create_mock.await_args_list[1].kwargs["stream"])
        self.assertNotIn("tools", create_mock.await_args_list[1].kwargs)
        self.assertEqual(turn_messages[-1]["content"], "北京出行")

    async def test_streaming_agent_receives_complete_tool_arguments_before_final_stream(self):
        tool_call = _make_tool_call("call-1", "list_shop_types", "{}")
        planning_with_tool = _make_response(content=None, tool_calls=[tool_call])
        planning_done = _make_response(content="READY_TO_ANSWER", tool_calls=None)
        final_chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="当前有"))
                ]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="5个分类。"))]
            ),
        ]
        create_mock = AsyncMock(
            side_effect=[
                planning_with_tool,
                planning_done,
                _AsyncChunks(final_chunks),
            ]
        )
        deltas = []

        async def collect_delta(delta):
            deltas.append(delta)

        trace = ToolTrace(
            tool="list_shop_types",
            ok=True,
            detail={"count": 5, "items": []},
        )
        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool",
            new=AsyncMock(return_value=trace),
        ) as call_tool_mock:
            answer, traces, _, _run_id, _summary = await run_agent(
                "现在有哪些商户分类？",
                on_answer_delta=collect_delta,
            )

        self.assertEqual(answer, "当前有5个分类。")
        self.assertEqual(traces, [trace])
        self.assertEqual(deltas, ["当前有", "5个分类。"])
        call_tool_mock.assert_awaited_once_with("list_shop_types", {})
        self.assertNotIn("stream", create_mock.await_args_list[0].kwargs)
        self.assertNotIn("stream", create_mock.await_args_list[1].kwargs)
        self.assertTrue(create_mock.await_args_list[2].kwargs["stream"])
        self.assertNotIn("tools", create_mock.await_args_list[2].kwargs)

    def test_place_snapshot_guard_rewrites_absolute_count_claim(self):
        trace = ToolTrace(
            tool="search_places",
            ok=True,
            detail={
                "scope": "demo_snapshot_not_realtime",
                "total": 7,
                "filters": {"district": "海淀区"},
            },
        )

        answer = _enforce_tool_answer_constraints(
            "当前演示目录中，海淀区共有7个一级综合公园。",
            [trace],
        )

        self.assertEqual(answer, "当前演示目录中找到7个符合条件的公园。")

        alternate = _enforce_tool_answer_constraints(
            "当前演示目录中，海淀区的一级综合公园共有7个，分别是：",
            [trace],
        )
        self.assertEqual(alternate, "当前演示目录中找到7个符合条件的公园，分别是：")

        markdown = _enforce_tool_answer_constraints(
            "在当前的演示目录中，海淀区**符合条件**的公园共有 **7个**，具体如下：",
            [trace],
        )
        self.assertEqual(markdown, "当前演示目录中找到7个符合条件的公园，具体如下：")

    def test_selects_generic_and_named_review_tools_for_experience_question(self):
        names = _tool_names(select_tool_schemas("有没有安静的咖啡店？或者其他可以办公的地方。"))

        self.assertEqual(names, ["search_shop_reviews", "search_knowledge"])

    def test_selects_category_tool_for_type_question(self):
        names = _tool_names(select_tool_schemas("现在有哪些商户分类？"))

        self.assertEqual(names, ["list_shop_types"])

    def test_selects_recommend_tool_for_personalized_recommendation(self):
        names = _tool_names(select_tool_schemas("我是 demo-user-1，根据我的历史给我推荐几家店"))

        self.assertEqual(names, ["recommend_shops"])

    def test_selects_policy_knowledge_before_personalized_recommendation(self):
        names = _tool_names(select_tool_schemas("个性化推荐会如何使用我的个人信息？"))

        self.assertEqual(names, ["search_knowledge"])

    def test_selects_merchant_knowledge_for_profile_fields(self):
        names = _tool_names(
            select_tool_schemas("St Honore Pastries 的 WiFi 和周末营业时间是什么？")
        )

        self.assertEqual(names, ["search_knowledge"])

    def test_selects_search_and_detail_tools_for_phone_question(self):
        names = _tool_names(select_tool_schemas("帮我找一家咖啡店并告诉我电话"))

        self.assertEqual(names, ["search_shops", "get_shop_detail"])

    async def test_calls_tool_then_answers(self):
        # 第一次返回“开单”，第二次返回最终回答
        tool_call = _make_tool_call("call_1", "search_shops", json.dumps({"typeId": 1}))
        first = _make_response(content=None, tool_calls=[tool_call])
        second = _make_response(content="附近有巷子口火锅、深夜食堂烧烤。")
        create_mock = AsyncMock(side_effect=[first, second])

        fake_trace = ToolTrace(
            tool="search_shops", ok=True, detail={"typeId": 1, "count": 2, "shops": []}
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=fake_trace)
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent("附近有什么美食？")

        self.assertIn("巷子口火锅", answer)
        sent_tool_names = _tool_names(create_mock.await_args_list[0].kwargs["tools"])
        self.assertIn("search_shops", sent_tool_names)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].tool, "search_shops")
        self.assertEqual(turn_messages[0], {"role": "user", "content": "附近有什么美食？"})
        self.assertEqual(turn_messages[-1]["role"], "assistant")
        # 模型“开单”的工具名和参数被正确转交给派单台
        call_tool_mock.assert_awaited_once_with("search_shops", {"typeId": 1})
        # 一共请求了两次：开单 + 总结
        self.assertEqual(create_mock.await_count, 2)

    async def test_tool_failure_stops_retries_and_summarizes_once(self):
        tool_call = _make_tool_call("call_1", "search_shops", json.dumps({"name": "咖啡"}))
        first = _make_response(content=None, tool_calls=[tool_call])
        second = _make_response(content="商户查询服务暂时不可用，请稍后再试。")
        create_mock = AsyncMock(side_effect=[first, second])
        failed_trace = ToolTrace(tool="search_shops", ok=False, detail="connection refused")

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=failed_trace)
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent("附近有哪些咖啡店？")

        self.assertIn("暂时不可用", answer)
        self.assertEqual(traces, [failed_trace])
        call_tool_mock.assert_awaited_once()
        self.assertEqual(create_mock.await_count, 2)
        self.assertEqual(turn_messages[-1]["role"], "assistant")

    async def test_landmark_shop_question_forces_geospatial_demo_search(self):
        trace = ToolTrace(
            tool="search_shops",
            ok=True,
            detail={
                "nearPlaceId": "beijing-park-178",
                "nearPlaceName": "朝阳公园",
                "radiusMeters": 2000,
                "count": 1,
                "shops": [
                    {
                        "name": "演示咖啡店",
                        "address": "北京市朝阳区朝阳公园周边",
                        "avgPrice": 30,
                        "distanceMeters": 500,
                    }
                ],
                "dataNotice": "商户为北京化演示实体，不代表真实登记商家。",
            },
        )

        with patch("app.llm.get_client") as get_client_mock, patch(
            "app.llm.call_tool", new=AsyncMock(return_value=trace)
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent("朝阳公园附近有哪些咖啡店？")

        self.assertIn("朝阳公园", answer)
        self.assertIn("约500米", answer)
        self.assertIn("不代表真实登记商家", answer)
        self.assertEqual(traces, [trace])
        call_tool_mock.assert_awaited_once_with(
            "search_shops",
            {
                "nearPlaceId": "beijing-park-178",
                "typeId": 2,
            },
        )
        get_client_mock.assert_not_called()
        self.assertEqual(turn_messages[-1]["role"], "assistant")

    async def test_ambiguous_place_search_stops_before_detail(self):
        search_call = _make_tool_call(
            "call_search",
            "search_places",
            json.dumps({"query": "永定门公园", "kind": "park"}, ensure_ascii=False),
        )
        east_detail_call = _make_tool_call(
            "call_east_detail",
            "get_place_detail",
            json.dumps({"placeId": "east"}),
        )
        west_detail_call = _make_tool_call(
            "call_west_detail",
            "get_place_detail",
            json.dumps({"placeId": "west"}),
        )
        first = _make_response(
            content=None,
            tool_calls=[search_call, east_detail_call, west_detail_call],
        )
        second = _make_response(content="找到东城区和西城区两个同名候选，请确认具体行政区。")
        create_mock = AsyncMock(side_effect=[first, second])
        trace = ToolTrace(
            tool="search_places",
            ok=True,
            detail={
                "items": [
                    {"id": "east", "name": "永定门公园（东城）", "district": "东城区"},
                    {"id": "west", "name": "永定门公园（西城)", "district": "西城区"},
                ],
            },
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=trace)
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent("永定门公园的电话是什么？")

        self.assertIn("请确认", answer)
        self.assertEqual(traces, [trace])
        call_tool_mock.assert_awaited_once()
        self.assertEqual(create_mock.await_count, 2)
        self.assertNotIn("tools", create_mock.await_args_list[1].kwargs)
        tool_messages = [item for item in turn_messages if item["role"] == "tool"]
        self.assertEqual(len(tool_messages), 3)
        self.assertIn("停止后续详情调用", tool_messages[1]["content"])
        self.assertIn("停止后续详情调用", tool_messages[2]["content"])

    async def test_place_search_merges_deterministic_user_filters(self):
        search_call = _make_tool_call(
            "call_search",
            "search_places",
            json.dumps(
                {
                    "district": "朝阳区",
                    "parkType": "综合公园",
                    "parkLevel": "一级",
                },
                ensure_ascii=False,
            ),
        )
        first = _make_response(content=None, tool_calls=[search_call])
        second = _make_response(content="当前演示目录中找到7个符合条件的公园。")
        create_mock = AsyncMock(side_effect=[first, second])
        trace = ToolTrace(
            tool="search_places",
            ok=True,
            detail={"total": 7, "items": [], "scope": "demo_snapshot_not_realtime"},
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=trace)
        ) as call_tool_mock:
            await run_agent("海淀区有哪些一级综合公园？")

        call_tool_mock.assert_awaited_once_with(
            "search_places",
            {
                "district": "海淀区",
                "parkType": "综合公园",
                "parkLevel": "一级",
                "kind": "park",
            },
        )

    async def test_answers_without_tool_call(self):
        only = _make_response(content="你好，我是本地生活助手。")
        create_mock = AsyncMock(return_value=only)

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            answer, traces, turn_messages, _run_id, _summary = await run_agent("你好")

        self.assertIn("你好", answer)
        self.assertEqual(traces, [])
        self.assertEqual(len(turn_messages), 2)
        # 明确问候走服务端确定性快路径，不消耗模型调用。
        self.assertEqual(create_mock.await_count, 0)

    async def test_stops_after_max_rounds(self):
        # 模型每轮都开单、从不收尾 -> 应在 MAX_TOOL_ROUNDS 轮后兜底返回，而不是死循环或空字符串
        tool_call = _make_tool_call("call_x", "search_shops", json.dumps({"typeId": 1}))
        always_open = _make_response(content=None, tool_calls=[tool_call])
        create_mock = AsyncMock(return_value=always_open)

        fake_trace = ToolTrace(
            tool="search_shops", ok=True, detail={"typeId": 1, "count": 0, "shops": []}
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=fake_trace)
        ):
            answer, traces, turn_messages, _run_id, _summary = await run_agent("一直开单不收尾")

        # 兜底回答非空，且请求次数 = 上限轮数 + 1 次“强制收尾”，证明没有无限循环
        self.assertNotEqual(answer, "")
        self.assertEqual(create_mock.await_count, MAX_TOOL_ROUNDS + 1)
        self.assertEqual(turn_messages[-1]["role"], "assistant")

    async def test_recovers_from_bad_json_arguments(self):
        # 第1轮模型发坏参数 -> 解析失败喂回错误；第2轮发好参数 -> 正常调工具；第3轮收尾回答
        bad_call = _make_tool_call("call_bad", "search_shops", '{"typeId": }')  # 坏 JSON
        good_call = _make_tool_call("call_good", "search_shops", json.dumps({"typeId": 1}))
        round1 = _make_response(content=None, tool_calls=[bad_call])
        round2 = _make_response(content=None, tool_calls=[good_call])
        round3 = _make_response(content="附近有巷子口火锅。")
        create_mock = AsyncMock(side_effect=[round1, round2, round3])

        fake_trace = ToolTrace(
            tool="search_shops", ok=True, detail={"typeId": 1, "count": 1, "shops": []}
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=fake_trace)
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent("帮我找美食")

        self.assertIn("巷子口火锅", answer)
        # 坏参数那轮没调工具，只有好参数那轮调了一次
        call_tool_mock.assert_awaited_once_with("search_shops", {"typeId": 1})
        self.assertEqual(len(traces), 1)
        self.assertEqual(turn_messages[1]["role"], "assistant")
        # 三次请求：坏参数轮、好参数轮、收尾轮
        self.assertEqual(create_mock.await_count, 3)

    async def test_can_chain_search_then_shop_detail(self):
        search_call = _make_tool_call("call_search", "search_shops", json.dumps({"typeId": 2}))
        detail_call = _make_tool_call("call_detail", "get_shop_detail", json.dumps({"shopId": 3}))
        round1 = _make_response(content=None, tool_calls=[search_call])
        round2 = _make_response(content=None, tool_calls=[detail_call])
        round3 = _make_response(content="清晨手冲咖啡的电话是 010-8888-0003。")
        create_mock = AsyncMock(side_effect=[round1, round2, round3])

        search_trace = ToolTrace(
            tool="search_shops",
            ok=True,
            detail={
                "typeId": 2,
                "count": 1,
                "shops": [{"id": 3, "name": "清晨手冲咖啡"}],
            },
        )
        detail_trace = ToolTrace(
            tool="get_shop_detail",
            ok=True,
            detail={
                "id": 3,
                "name": "清晨手冲咖啡",
                "phone": "010-8888-0003",
            },
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool",
            new=AsyncMock(side_effect=[search_trace, detail_trace]),
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent("帮我找一家咖啡店并告诉我电话")

        self.assertIn("010-8888-0003", answer)
        self.assertEqual([trace.tool for trace in traces], ["search_shops", "get_shop_detail"])
        self.assertEqual(
            call_tool_mock.await_args_list[0].args,
            ("search_shops", {"typeId": 2}),
        )
        self.assertEqual(
            call_tool_mock.await_args_list[1].args,
            ("get_shop_detail", {"shopId": 3}),
        )
        self.assertEqual(turn_messages[-1]["role"], "assistant")

    async def test_includes_previous_history_in_model_messages(self):
        reply = _make_response(content="第二家是深夜食堂烧烤。")
        create_mock = AsyncMock(return_value=reply)
        history = [
            {"role": "user", "content": "推荐两家美食店"},
            {"role": "assistant", "content": "第一家巷子口火锅，第二家深夜食堂烧烤。"},
        ]

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            answer, traces, turn_messages, _run_id, _summary = await run_agent("第二家是哪家？", history=history)

        sent_messages = create_mock.await_args.kwargs["messages"]
        self.assertEqual(sent_messages[1:3], history)
        self.assertEqual(sent_messages[3], {"role": "user", "content": "第二家是哪家？"})
        self.assertIn("深夜食堂", answer)
        self.assertEqual(traces, [])
        self.assertEqual(
            turn_messages,
            [
                {"role": "user", "content": "第二家是哪家？"},
                {"role": "assistant", "content": "第二家是深夜食堂烧烤。"},
            ],
        )

    async def test_calls_review_search_then_answers_with_citation(self):
        review_call = _make_tool_call(
            "call_review",
            "search_knowledge",
            json.dumps(
                {
                    "query": "哪家咖啡店适合下午带电脑办公？",
                    "sources": ["reviews"],
                }
            ),
        )
        first = _make_response(content=None, tool_calls=[review_call])
        second = _make_response(
            content="清晨手冲咖啡有插座和靠窗单人位，适合办公。[review-005]"
        )
        create_mock = AsyncMock(side_effect=[first, second])
        review_trace = ToolTrace(
            tool="search_knowledge",
            ok=True,
            detail={
                "query": "哪家咖啡店适合下午带电脑办公？",
                "sources": ["reviews"],
                "count": 1,
                "citations": [
                    {
                        "sourceId": "review-005",
                        "sourceType": "review",
                        "title": "清晨手冲咖啡评论",
                        "quote": "店里有靠窗的单人位和插座。",
                        "score": 0.89,
                        "metadata": {
                            "reviewId": "review-005",
                            "shopName": "清晨手冲咖啡",
                        },
                    }
                ],
            },
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=review_trace)
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent(
                "哪家咖啡店适合下午带电脑办公？"
            )

        call_tool_mock.assert_awaited_once_with(
            "search_knowledge",
            {"query": "哪家咖啡店适合下午带电脑办公？", "sources": ["reviews"]},
        )
        self.assertIn("[review-005]", answer)
        self.assertEqual(traces, [review_trace])
        tool_messages = [item for item in turn_messages if item["role"] == "tool"]
        self.assertIn("review-005", tool_messages[0]["content"])

    async def test_policy_question_forces_policy_source_even_if_model_requests_reviews(self):
        question = "个性化推荐会如何使用我的个人信息？"
        policy_call = _make_tool_call(
            "call_policy",
            "search_knowledge",
            json.dumps({"query": question, "sources": ["reviews"]}),
        )
        first = _make_response(content=None, tool_calls=[policy_call])
        second = _make_response(content="平台应说明个性化推荐的数据用途。")
        create_mock = AsyncMock(side_effect=[first, second])
        policy_trace = ToolTrace(
            tool="search_knowledge",
            ok=True,
            detail={
                "query": question,
                "sources": ["policy_docs"],
                "count": 1,
                "citations": [
                    {
                        "sourceId": "privacy-and-recommendation",
                        "sourceType": "policy_doc",
                        "quote": "个性化推荐需要说明数据用途。",
                    }
                ],
            },
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=policy_trace)
        ) as call_tool_mock:
            answer, traces, _turn_messages, _run_id, _summary = await run_agent(question)

        self.assertIn("数据用途", answer)
        self.assertEqual(traces, [policy_trace])
        self.assertEqual(
            _tool_names(create_mock.await_args_list[0].kwargs["tools"]),
            ["search_knowledge"],
        )
        self.assertEqual(create_mock.await_args_list[0].kwargs["tool_choice"], "required")
        call_tool_mock.assert_awaited_once_with(
            "search_knowledge",
            {"query": question, "sources": ["policy_docs"]},
        )
        final_messages = create_mock.await_args_list[1].kwargs["messages"]
        self.assertTrue(
            any(
                item.get("role") == "system"
                and "不得主动索要userId" in item.get("content", "")
                for item in final_messages
            )
        )

    async def test_merchant_profile_question_forces_merchant_docs_source(self):
        question = "St Honore Pastries 的 WiFi 和周末营业时间是什么？"
        merchant_call = _make_tool_call(
            "call_merchant_profile",
            "search_knowledge",
            json.dumps({"query": question, "sources": ["reviews"]}),
        )
        first = _make_response(content=None, tool_calls=[merchant_call])
        second = _make_response(content="该店有免费 WiFi，周末营业至 21:00。")
        create_mock = AsyncMock(side_effect=[first, second])
        merchant_trace = ToolTrace(
            tool="search_knowledge",
            ok=True,
            detail={
                "query": question,
                "sources": ["merchant_docs"],
                "count": 1,
                "citations": [
                    {
                        "sourceId": "MTSW4McQd7CbVtyjqoe9mw",
                        "sourceType": "merchant_doc",
                        "quote": "WiFi：免费。周六、周日 7:00-21:00。",
                    }
                ],
            },
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=merchant_trace)
        ) as call_tool_mock:
            answer, traces, _turn_messages, _run_id, _summary = await run_agent(question)

        self.assertIn("免费 WiFi", answer)
        self.assertEqual(traces, [merchant_trace])
        self.assertEqual(
            _tool_names(create_mock.await_args_list[0].kwargs["tools"]),
            ["search_knowledge"],
        )
        self.assertEqual(create_mock.await_args_list[0].kwargs["tool_choice"], "required")
        call_tool_mock.assert_awaited_once_with(
            "search_knowledge",
            {"query": question, "sources": ["merchant_docs"]},
        )
        final_messages = create_mock.await_args_list[1].kwargs["messages"]
        self.assertTrue(
            any(
                item.get("role") == "system"
                and "不得把WiFi" in item.get("content", "")
                for item in final_messages
            )
        )

    async def test_experience_question_requires_one_review_evidence_tool(self):
        first = _make_response(content="清晨手冲咖啡适合安静办公。[review-005]")
        second = _make_response(content="根据评论证据，清晨手冲咖啡适合安静办公。[review-005]")
        create_mock = AsyncMock(side_effect=[first, second])
        review_trace = ToolTrace(
            tool="search_knowledge",
            ok=True,
            detail={
                "query": "有没有安静的咖啡店？",
                "sources": ["reviews"],
                "count": 1,
                "citations": [
                    {
                        "sourceId": "review-005",
                        "sourceType": "review",
                        "quote": "工作日下午通常比较安静。",
                        "metadata": {"reviewId": "review-005"},
                    }
                ],
            },
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=review_trace)
        ) as call_tool_mock:
            answer, traces, _turn_messages, _run_id, _summary = await run_agent("有没有安静的咖啡店？")

        self.assertIn("清晨手冲", answer)
        self.assertEqual(traces, [review_trace])
        self.assertEqual(
            _tool_names(create_mock.await_args_list[0].kwargs["tools"]),
            ["search_shop_reviews", "search_knowledge"],
        )
        self.assertEqual(
            create_mock.await_args_list[0].kwargs["tool_choice"],
            "required",
        )
        call_tool_mock.assert_awaited_once_with(
            "search_knowledge",
            {"query": "有没有安静的咖啡店？", "sources": ["reviews"]},
        )

    async def test_named_shop_experience_uses_composite_review_tool(self):
        named_call = _make_tool_call(
            "call_named_review",
            "search_shop_reviews",
            json.dumps(
                {
                    "query": "Red Hook Coffee & Tea适合用电脑工作吗？",
                    "shopName": "Red Hook Coffee & Tea",
                }
            ),
        )
        first = _make_response(content=None, tool_calls=[named_call])
        second = _make_response(
            content="评论提到店内适合带电脑工作。[review-red-hook]"
        )
        create_mock = AsyncMock(side_effect=[first, second])
        named_trace = ToolTrace(
            tool="search_shop_reviews",
            ok=True,
            detail={
                "resolvedShop": {"id": 100011, "name": "Red Hook Coffee & Tea"},
                "reviews": [
                    {
                        "reviewId": "review-red-hook",
                        "shopId": 100011,
                        "text": "适合带电脑工作。",
                    }
                ],
            },
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool",
            new=AsyncMock(return_value=named_trace),
        ) as call_tool_mock:
            answer, traces, _turn_messages, _run_id, _summary = await run_agent(
                "Red Hook Coffee & Tea适合用电脑工作吗？"
            )

        self.assertIn("[review-red-hook]", answer)
        self.assertEqual(traces, [named_trace])
        self.assertEqual(
            _tool_names(create_mock.await_args_list[0].kwargs["tools"]),
            ["search_shop_reviews", "search_knowledge"],
        )
        self.assertEqual(
            create_mock.await_args_list[0].kwargs["tool_choice"],
            "required",
        )
        call_tool_mock.assert_awaited_once_with(
            "search_shop_reviews",
            {
                "query": "Red Hook Coffee & Tea适合用电脑工作吗？",
                "shopName": "Red Hook Coffee & Tea",
            },
        )

    async def test_rejects_tool_call_outside_selected_menu(self):
        illegal_call = _make_tool_call("call_review", "search_reviews", json.dumps({"query": "分类"}))
        first = _make_response(content=None, tool_calls=[illegal_call])
        second = _make_response(content="当前可以查看商户分类。")
        create_mock = AsyncMock(side_effect=[first, second])

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock()
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent("现在有哪些商户分类？")

        self.assertIn("商户分类", answer)
        call_tool_mock.assert_not_awaited()
        self.assertEqual(traces, [])
        tool_messages = [item for item in turn_messages if item["role"] == "tool"]
        self.assertIn("不允许调用 search_reviews", tool_messages[0]["content"])

    async def test_calls_recommend_shops_then_answers(self):
        recommend_call = _make_tool_call(
            "call_recommend",
            "recommend_shops",
            json.dumps({"userId": "demo-user-1", "limit": 2}),
        )
        first = _make_response(content=None, tool_calls=[recommend_call])
        second = _make_response(content="根据你的历史，推荐清晨手冲咖啡。")
        create_mock = AsyncMock(side_effect=[first, second])
        recommend_trace = ToolTrace(
            tool="recommend_shops",
            ok=True,
            detail={
                "userId": "demo-user-1",
                "count": 1,
                "shops": [{"shopId": 3, "shopName": "清晨手冲咖啡"}],
            },
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm.call_tool", new=AsyncMock(return_value=recommend_trace)
        ) as call_tool_mock:
            answer, traces, turn_messages, _run_id, _summary = await run_agent(
                "我是 demo-user-1，根据我的历史给我推荐两家店"
            )

        call_tool_mock.assert_awaited_once_with(
            "recommend_shops",
            {"userId": "demo-user-1", "limit": 2},
        )
        self.assertIn("清晨手冲", answer)
        self.assertEqual(traces, [recommend_trace])
        self.assertIn("recommend_shops", turn_messages[1]["tool_calls"][0]["function"]["name"])

    # ── REPAIR-002 input-boundary attack tests ───────────────────────────────
    # The extractor must not silently rewrite a missing/invalid/non-object
    # update_task_state input into an empty patch and auto-ready it. Every such
    # input either fails the shared strict parse boundary or is rejected by the
    # extractor's required-status contract, and a bounded repair (≤1) is the
    # only model-side recovery.

    async def test_task_state_schema_required_contains_status_and_runtime_matches(self):
        parameters = TASK_STATE_TOOL_SCHEMA["function"]["parameters"]
        self.assertEqual(parameters["required"], ["status"])
        self.assertEqual(_MODEL_TASK_STATE_REQUIRED_KEYS, frozenset({"status"}))
        # additionalProperties=false is preserved.
        self.assertFalse(parameters["additionalProperties"])
        # The runtime required contract stays in sync with the declared schema.
        _assert_model_task_state_schema_contract()

    async def test_parse_missing_tool_call_rejects_without_auto_ready(self):
        with self.assertRaises(TaskStatePayloadValidationError) as ctx:
            _parse_task_state_arguments(None)
        self.assertEqual(ctx.exception.code, "missing_task_state_tool_call")
        self.assertEqual(ctx.exception.field_path, "arguments")

        # End-to-end: a missing update_task_state tool call is a safe stop with
        # zero persistence, one bounded format retry, and never auto-readies.
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-missing-call-no-autoready",
            )
        )
        create_mock = AsyncMock(
            side_effect=[_make_response(content="READY_TO_ANSWER", tool_calls=None)] * 2
        )
        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm._persist_task_patch", new=AsyncMock(),
        ) as persist_mock, patch("app.tools.call_tool", new=AsyncMock()) as call_tool_mock:
            answer, traces, _tm, run_id, summary = await _run_unified_harness_agent(
                "想找 iOS 二手机。",
                history=None,
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
            )

        self.assertIn("任务状态构建失败", answer)
        self.assertIn("系统已安全停止", answer)
        self.assertEqual(traces, [])
        self.assertTrue(run_id.startswith("run-"))
        self.assertEqual(summary.agent_status, "failed")
        self.assertEqual(summary.final_action, "safe_stop")
        self.assertEqual(summary.failure_code, "task_state_update_missing")
        call_tool_mock.assert_not_awaited()
        persist_mock.assert_not_awaited()
        self.assertEqual(create_mock.await_count, 2)
        latest = await get_task_state(state.task_id)
        self.assertNotEqual(latest.status, "ready")

    async def test_malformed_json_rejects_and_repair_keeps_bad_string(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-bad-json-repair",
            )
        )
        bad_arguments = '{"status": "ready", "pendingQuestions": ['
        bad_call = _make_tool_call("call-bad-json", "update_task_state", bad_arguments)
        repair_call = _make_tool_call(
            "call-repair",
            "update_task_state",
            json.dumps(_smoke_004_legal_ready_args(), ensure_ascii=False),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[bad_call]),
                _make_response(tool_calls=[repair_call]),
            ]
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            updated = await _update_task_state_for_unified_harness(
                "想找 iOS 二手机。",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        # 1 extraction + exactly 1 repair = 2 model calls, 1 persist.
        self.assertEqual(create_mock.await_count, 2)
        repair_messages = create_mock.await_args_list[1].kwargs["messages"]
        self.assertEqual(len(repair_messages), 7)
        replayed = repair_messages[4]
        self.assertEqual(replayed["role"], "assistant")
        self.assertEqual(replayed["tool_calls"][0]["id"], "call-bad-json")
        self.assertEqual(
            replayed["tool_calls"][0]["function"]["arguments"],
            bad_arguments,
        )
        rejection = json.loads(repair_messages[5]["content"])
        self.assertIs(rejection["valid"], False)
        self.assertEqual(rejection["persist"], 0)
        self.assertEqual(rejection["code"], "invalid_task_state_arguments_json")
        self.assertEqual(rejection["field_path"], "arguments")
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision + 1)
        self.assertEqual(latest.status, "ready")

    async def test_non_object_array_and_null_rejected_precisely(self):
        for raw in ('["a", "b"]', "null", '"a plain string"'):
            call = _make_tool_call("call-non-object", "update_task_state", raw)
            with self.assertRaises(TaskStatePayloadValidationError) as ctx:
                _parse_task_state_arguments(call)
            self.assertEqual(
                ctx.exception.code,
                "task_state_arguments_not_an_object",
            )
            self.assertEqual(ctx.exception.field_path, "arguments")

        # End-to-end: an array root goes to a single bounded repair and the legal
        # repair persists exactly once.
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-non-object-repair",
            )
        )
        array_call = _make_tool_call(
            "call-array", "update_task_state", '["not", "an", "object"]'
        )
        repair_call = _make_tool_call(
            "call-repair",
            "update_task_state",
            json.dumps(_smoke_004_legal_ready_args(), ensure_ascii=False),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[array_call]),
                _make_response(tool_calls=[repair_call]),
            ]
        )
        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            updated = await _update_task_state_for_unified_harness(
                "想找 iOS 二手机。",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        self.assertEqual(create_mock.await_count, 2)
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision + 1)
        self.assertEqual(latest.status, "ready")

    async def test_empty_object_rejected_and_repaired_without_auto_ready(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-empty-object-no-autoready",
            )
        )
        empty_call = _make_tool_call("call-empty", "update_task_state", "{}")
        repair_call = _make_tool_call(
            "call-repair",
            "update_task_state",
            json.dumps(_smoke_004_legal_ready_args(), ensure_ascii=False),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[empty_call]),
                _make_response(tool_calls=[repair_call]),
            ]
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            updated = await _update_task_state_for_unified_harness(
                "想找 iOS 二手机。",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        self.assertEqual(create_mock.await_count, 2)
        repair_messages = create_mock.await_args_list[1].kwargs["messages"]
        self.assertEqual(
            repair_messages[4]["tool_calls"][0]["function"]["arguments"],
            "{}",
        )
        rejection = json.loads(repair_messages[5]["content"])
        self.assertEqual(rejection["code"], "missing_required_status")
        self.assertEqual(rejection["field_path"], "status")
        self.assertEqual(rejection["persist"], 0)
        # Exactly one persist, from the repair payload — the empty patch was
        # never auto-readied into an acceptance.
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision + 1)
        self.assertEqual(latest.status, "ready")

    async def test_non_empty_missing_status_rejected_and_repaired(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-no-status-repair",
            )
        )
        no_status_call = _make_tool_call(
            "call-no-status",
            "update_task_state",
            json.dumps({"goal": "想找 iOS 二手机。"}, ensure_ascii=False),
        )
        repair_call = _make_tool_call(
            "call-repair",
            "update_task_state",
            json.dumps(_smoke_004_legal_ready_args(), ensure_ascii=False),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[no_status_call]),
                _make_response(tool_calls=[repair_call]),
            ]
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)):
            updated = await _update_task_state_for_unified_harness(
                "想找 iOS 二手机。",
                history=None,
                client=_fake_client(create_mock),
                task_state=state,
                on_task_state=None,
            )

        self.assertEqual(create_mock.await_count, 2)
        repair_messages = create_mock.await_args_list[1].kwargs["messages"]
        self.assertEqual(
            json.loads(repair_messages[4]["tool_calls"][0]["function"]["arguments"]),
            {"goal": "想找 iOS 二手机。"},
        )
        rejection = json.loads(repair_messages[5]["content"])
        self.assertEqual(rejection["code"], "missing_required_status")
        self.assertEqual(rejection["field_path"], "status")
        self.assertEqual(rejection["persist"], 0)
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision + 1)
        self.assertEqual(latest.status, "ready")

    async def test_second_invalid_repair_malformed_json_zero_persist_two_calls(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-second-invalid-json",
            )
        )
        empty_call = _make_tool_call("call-empty", "update_task_state", "{}")
        bad_repair_call = _make_tool_call(
            "call-bad-repair", "update_task_state", "{bad json"
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[empty_call]),
                _make_response(tool_calls=[bad_repair_call]),
            ]
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm._persist_task_patch", new=AsyncMock(),
        ) as persist_mock, patch("app.tools.call_tool", new=AsyncMock()) as call_tool_mock:
            answer, traces, _tm, _rid, _sum = await _run_unified_harness_agent(
                "想找 iOS 二手机。",
                history=None,
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
            )

        self.assertIn("任务状态构建失败", answer)
        self.assertIn("系统已安全停止", answer)
        self.assertEqual(traces, [])
        call_tool_mock.assert_not_awaited()
        persist_mock.assert_not_awaited()
        # 1 extraction + 1 repair, never a third call.
        self.assertEqual(create_mock.await_count, 2)
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision)
        self.assertEqual(latest.facts, [])
        self.assertNotEqual(latest.status, "ready")

    async def test_second_invalid_repair_non_object_zero_persist_two_calls(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-second-invalid-nonobject",
            )
        )
        empty_call = _make_tool_call("call-empty", "update_task_state", "{}")
        non_object_repair_call = _make_tool_call(
            "call-nonobject-repair", "update_task_state", "[1, 2, 3]"
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[empty_call]),
                _make_response(tool_calls=[non_object_repair_call]),
            ]
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm._persist_task_patch", new=AsyncMock(),
        ) as persist_mock, patch("app.tools.call_tool", new=AsyncMock()) as call_tool_mock:
            answer, traces, _tm, _rid, _sum = await _run_unified_harness_agent(
                "想找 iOS 二手机。",
                history=None,
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
            )

        self.assertIn("任务状态构建失败", answer)
        self.assertEqual(traces, [])
        call_tool_mock.assert_not_awaited()
        persist_mock.assert_not_awaited()
        self.assertEqual(create_mock.await_count, 2)
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision)
        self.assertNotEqual(latest.status, "ready")

    async def test_second_invalid_repair_missing_status_zero_persist_two_calls(self):
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-second-invalid-nostatus",
            )
        )
        empty_call = _make_tool_call("call-empty", "update_task_state", "{}")
        no_status_repair_call = _make_tool_call(
            "call-nostatus-repair",
            "update_task_state",
            json.dumps({"goal": "想找 iOS 二手机。"}, ensure_ascii=False),
        )
        create_mock = AsyncMock(
            side_effect=[
                _make_response(tool_calls=[empty_call]),
                _make_response(tool_calls=[no_status_repair_call]),
            ]
        )

        with patch("app.llm.get_client", return_value=_fake_client(create_mock)), patch(
            "app.llm._persist_task_patch", new=AsyncMock(),
        ) as persist_mock, patch("app.tools.call_tool", new=AsyncMock()) as call_tool_mock:
            answer, traces, _tm, _rid, _sum = await _run_unified_harness_agent(
                "想找 iOS 二手机。",
                history=None,
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
            )

        self.assertIn("任务状态构建失败", answer)
        self.assertEqual(traces, [])
        call_tool_mock.assert_not_awaited()
        persist_mock.assert_not_awaited()
        self.assertEqual(create_mock.await_count, 2)
        latest = await get_task_state(state.task_id)
        self.assertEqual(latest.revision, state.revision)
        self.assertNotEqual(latest.status, "ready")

    async def test_frozen_probe_missing_bad_json_non_object_no_accept_ready(self):
        # Codex frozen probe inputs — missing tool call, invalid JSON, non-object
        # JSON. Under the P1 these collapsed into an empty patch that auto-readied
        # to ACCEPT status=ready. The shared parse boundary must reject each with
        # a stable code, and the strict extractor validation must reject an empty
        # object instead of auto-readying it.
        bad_json_call = _make_tool_call(
            "call-probe-bad", "update_task_state", '{"status": "ready", "'
        )
        array_call = _make_tool_call(
            "call-probe-array", "update_task_state", "[1, 2, 3]"
        )
        null_call = _make_tool_call("call-probe-null", "update_task_state", "null")

        for task_call, expected_code in (
            (None, "missing_task_state_tool_call"),
            (bad_json_call, "invalid_task_state_arguments_json"),
            (array_call, "task_state_arguments_not_an_object"),
            (null_call, "task_state_arguments_not_an_object"),
        ):
            with self.assertRaises(TaskStatePayloadValidationError) as ctx:
                _parse_task_state_arguments(task_call)
            self.assertEqual(ctx.exception.code, expected_code)

        # Even a literal empty object reaching validation is rejected with
        # missing_required_status instead of ACCEPT args={} status=ready.
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        state = await create_task_state(
            TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="想找 iOS 二手机。",
                sessionId="session-frozen-probe-no-accept",
            )
        )
        with patch("app.llm._persist_task_patch", new=AsyncMock()) as persist_mock:
            with self.assertRaises(TaskStatePayloadValidationError) as ctx:
                await _build_validated_task_state_payload(
                    state,
                    {},
                    message="想找 iOS 二手机。",
                    require_status=True,
                )
        self.assertEqual(ctx.exception.code, "missing_required_status")
        self.assertEqual(ctx.exception.field_path, "status")
        persist_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
