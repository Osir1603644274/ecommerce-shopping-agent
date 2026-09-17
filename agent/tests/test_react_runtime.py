"""Executable ReAct V0 loop and execution-envelope tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import task_state
from app.control.react_actions import ActionOutcome
from app.control.react_context import build_decision_context_view
from app.control.react_decision import deterministic_next_action
from app.control.react_runtime import (
    ReactToolExecution,
    execute_react_tool_action,
    materialize_react_execution_plan,
    run_react_v0_loop,
)
from app.domains.ecommerce.ranking_contract import (
    RANKING_FORMULA,
    RANKING_TIE_BREAK,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
)
from app.schemas import ToolTrace
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    get_task_state,
    update_task_state,
)
from tests.fake_redis import FakeRedis
from tests.test_react_context import _state


_CANDIDATE_IDS = [
    4346166, 2157503, 1239068, 117916, 3934984,
    5989522, 634577, 3470593, 4524730, 1912721,
    2965840, 3956695, 3600996, 1967528, 5304970,
    5286377, 1092202, 2640402, 351899, 4244556,
]


def _search_state() -> TaskState:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["shoppingGuide"]["requirements"][0]["value"] = 250_000
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "complete_controlled_coverage",
    }
    return TaskState.model_validate(payload)


def _tool_schema(name: str) -> dict:
    properties = {}
    required = []
    if name == "search_products":
        properties = {
            "query": {"type": "string"},
            "category": {"type": "string"},
            "requirements": {"type": "array"},
        }
        required = ["query", "category", "requirements"]
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": required,
            },
        },
    }


def _search_detail() -> dict:
    raw = "iOS"
    evidence = [{
        "ref": f"product:{item}:attribute:os",
        "field": "relevance.attr_value",
        "rawValue": raw,
        "method": "used-phone-exact-token-seven-field-v2",
    } for item in _CANDIDATE_IDS]
    return {
        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
        "candidatePoolIds": list(_CANDIDATE_IDS),
        "rankedItemIds": list(_CANDIDATE_IDS),
        "candidateIds": list(_CANDIDATE_IDS),
        "candidates": [{
            "id": item,
            "attributes": [{
                "key": "os",
                "normalizedNumber": None,
                "normalizedBoolean": None,
                "normalizedText": "ios",
                "rawValue": raw,
                "evidenceField": "relevance.attr_value",
                "extractionMethod": "used-phone-exact-token-seven-field-v2",
            }],
            "checks": [{
                "key": "os",
                "operator": "eq",
                "expected": "ios",
                "unit": "enum",
                "priority": "hard",
                "source": "user",
                "actual": "ios",
                "status": "pass",
                "evidenceRef": f"product:{item}:attribute:os",
            }],
            "selectionType": "full_match",
            "evidenceRefs": [f"product:{item}:attribute:os"],
        } for item in _CANDIDATE_IDS],
        "retrievalTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "candidatePoolCount": len(_CANDIDATE_IDS),
            "authoritativeFactCount": len(_CANDIDATE_IDS),
        },
        "rankingTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "inputCandidateCount": len(_CANDIDATE_IDS),
            "rankedItemCount": len(_CANDIDATE_IDS),
            "tieBreak": RANKING_TIE_BREAK,
            "formula": RANKING_FORMULA,
        },
        "citationTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "sourceTool": "search_products",
            "rankedItemIds": list(_CANDIDATE_IDS),
            "evidenceRefCount": len(evidence),
            "binding": "current_successful_tool_call_ranked_items_only",
        },
        "evidenceRefs": [item["ref"] for item in evidence],
        "evidence": evidence,
        "eliminated": [],
    }


def test_search_action_materializes_server_owned_one_step_plan() -> None:
    state = _search_state()
    view = build_decision_context_view(
        state,
        user_message="预算改成2500",
        allowed_tool_names=["search_products"],
    )
    action = deterministic_next_action(view)
    assert action is not None

    plan = materialize_react_execution_plan(state, action)

    assert plan.based_on_revision == state.revision
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.tool_name == "search_products"
    assert step.arguments == {
        "query": state.goal,
        "category": "手机",
        "requirements": [{
            "key": "price_minor",
            "operator": "lte",
            "value": 250_000,
            "unit": "CNY_MINOR",
            "priority": "hard",
            "source": "user",
        }],
    }
    assert step.expected_output == {"requiresProductCandidates": True}


def test_unsupported_evidence_exposes_a_real_bounded_model_choice() -> None:
    payload = _state().model_dump(by_alias=True, mode="json")
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "unsupported_game_camera_evidence",
    }
    payload["pendingQuestions"] = ["缺少性能实测数据"]
    state = TaskState.model_validate(payload)
    view = build_decision_context_view(
        state,
        user_message="这三款谁帧率最高、散热最好？",
        allowed_tool_names=["search_products", "compare_products"],
    )

    assert view.observation_summary.adaptive_trigger == "unsupported_evidence"
    assert [item.option_id for item in view.allowed_action_options] == [
        "answer.validated_context",
        "clarify.unsupported_evidence",
    ]
    action = deterministic_next_action(view)
    assert action is not None
    assert action.kind == "ANSWER"
    assert action.reason_code == "answer_evidence_boundary"
    assert action.answer_context_ref == "evidence-boundary:gaming:task-react-v0:r18"


def test_tool_action_reuses_executor_and_validator_without_planner_model() -> None:
    initial = _search_state()
    view = build_decision_context_view(
        initial,
        user_message="预算改成2500",
        allowed_tool_names=["search_products"],
    )
    action = deterministic_next_action(view)
    assert action is not None
    planned = initial.model_copy(update={"revision": initial.revision + 1})
    executed = planned.model_copy(update={"revision": planned.revision + 2})

    validated = _state(validated=True)
    validated_payload = validated.model_dump(by_alias=True, mode="json")
    validated_payload["revision"] = executed.revision + 1
    validated_payload["domainState"]["validationResult"]["basedOnRevision"] = (
        validated_payload["revision"] - 1
    )
    validated = TaskState.model_validate(validated_payload)
    trace = ToolTrace(
        tool="search_products",
        ok=True,
        durationMs=1.0,
        detail={"count": 3},
    )
    executor_result = SimpleNamespace(
        outcome="step_executed",
        task_state=executed,
        execution_result=SimpleNamespace(tool_trace=trace),
    )
    validator_result = SimpleNamespace(outcome="passed", error_code=None)

    with patch(
        "app.control.react_runtime.persist_planned_result",
        new=AsyncMock(return_value=planned),
    ) as persist, patch(
        "app.control.react_runtime.run_executor_step",
        new=AsyncMock(return_value=executor_result),
    ) as execute, patch(
        "app.control.react_runtime.run_validator_phase",
        new=AsyncMock(return_value=(validator_result, validated)),
    ) as validate:
        result = asyncio.run(execute_react_tool_action(
            state=initial,
            view=view,
            action=action,
            user_message="预算改成2500",
            allowed_tool_schemas=[_tool_schema("search_products")],
        ))

    assert result.outcome.status == "SUCCEEDED"
    assert result.outcome.validator_outcome == "PASSED"
    assert result.tool_trace is trace
    assert persist.await_args.args[1].plan.steps[0].tool_name == "search_products"
    execute.assert_awaited_once()
    validate.assert_awaited_once()


def test_loop_runs_search_validate_second_decision_and_answer() -> None:
    initial = _search_state()
    validated = _state(validated=True)
    trace = ToolTrace(
        tool="search_products", ok=True, durationMs=1.0, detail={"count": 3}
    )

    async def fake_execute(**kwargs):
        action = kwargs["action"]
        return ReactToolExecution(
            state=validated,
            tool_trace=trace,
            outcome=ActionOutcome(
                actionId=action.action_id,
                status="SUCCEEDED",
                observationRef="validated-task:task-react-v0:r18",
                validatorOutcome="PASSED",
                stateRevisionAfter=validated.revision,
                retryable=False,
                errorCode=None,
            ),
        )

    answer_handler = AsyncMock(return_value="根据已验证候选，推荐第一款。")
    model_create = AsyncMock()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=model_create))
    )
    decisions = []
    outcomes = []
    observed_traces = []
    with patch(
        "app.control.react_runtime.execute_react_tool_action",
        new=AsyncMock(side_effect=fake_execute),
    ):
        result = asyncio.run(run_react_v0_loop(
            state=initial,
            user_message="预算改成2500",
            client=client,
            model="deepseek-chat",
            decision_timeout_seconds=1,
            max_iterations=4,
            tool_schema_provider=lambda _state: [_tool_schema("search_products")],
            answer_handler=answer_handler,
            on_decision=decisions.append,
            on_outcome=outcomes.append,
            on_tool_trace=observed_traces.append,
        ))

    assert result.terminal_action == "answer"
    assert result.answer == "根据已验证候选，推荐第一款。"
    assert [action.kind for action in result.actions] == ["CALL_TOOL", "ANSWER"]
    assert [outcome.status for outcome in result.outcomes] == [
        "SUCCEEDED", "SUCCEEDED",
    ]
    assert len(decisions) == 2
    assert len(outcomes) == 2
    assert observed_traces == [trace]
    answer_handler.assert_awaited_once()
    model_create.assert_not_awaited()


def test_loop_routes_zero_result_observation_to_model_option() -> None:
    initial = _search_state()
    zero_payload = initial.model_dump(by_alias=True, mode="json")
    zero_payload["revision"] = initial.revision + 1
    zero_payload["domainState"].pop("candidateScope", None)
    zero_payload["domainState"].pop("scopeRerankRequest", None)
    zero_payload["domainState"]["validationResult"] = {
        "outcome": "insufficient_evidence",
        "errorCode": "product_candidates_missing",
        "basedOnRevision": initial.revision,
        "stepResults": [{
            "evidenceSummary": {
                "requiresProductCandidates": {
                    "candidatePoolCount": 50,
                    "rankedItemCount": 0,
                    "hasCompleteMatch": False,
                },
            },
        }],
    }
    zero_state = TaskState.model_validate(zero_payload)
    trace = ToolTrace(
        tool="search_products", ok=True, durationMs=1.0, detail={"candidateIds": []}
    )

    async def fake_execute(**kwargs):
        return ReactToolExecution(
            state=zero_state,
            tool_trace=trace,
            outcome=ActionOutcome(
                actionId=kwargs["action"].action_id,
                status="REJECTED",
                observationRef=None,
                validatorOutcome="REJECTED",
                stateRevisionAfter=zero_state.revision,
                retryable=False,
                errorCode="product_candidates_missing",
            ),
        )

    async def model_create(**kwargs):
        submitted = json.loads(kwargs["messages"][1]["content"])
        arguments = {
            "taskId": zero_state.task_id,
            "basedOnRevision": zero_state.revision,
            "decisionViewHash": submitted["decisionViewHash"],
            "optionId": "clarify.pending.0",
        }
        call = SimpleNamespace(
            function=SimpleNamespace(
                name="submit_react_action",
                arguments=json.dumps(arguments),
            )
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=model_create)
    )))
    decisions = []
    with patch(
        "app.control.react_runtime.execute_react_tool_action",
        new=AsyncMock(side_effect=fake_execute),
    ):
        result = asyncio.run(run_react_v0_loop(
            state=initial,
            user_message="预算300以内，其他硬条件不变",
            client=client,
            model="deepseek-chat",
            decision_timeout_seconds=1,
            max_iterations=4,
            tool_schema_provider=lambda _state: [_tool_schema("search_products")],
            answer_handler=AsyncMock(),
            on_decision=decisions.append,
        ))

    assert result.terminal_action == "ask_clarification"
    assert [item.kind for item in result.actions] == [
        "CALL_TOOL", "ASK_CLARIFICATION",
    ]
    assert [item.status for item in result.outcomes] == [
        "REJECTED", "INTERRUPTED",
    ]
    assert [item.decision_source for item in decisions] == [
        "deterministic_policy", "model",
    ]


def test_real_task_state_executor_validator_path_commits_validated_scope() -> None:
    """The live adapter works with real OCC persistence, not only mocks."""

    async def scenario() -> None:
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()
        created = await create_task_state(TaskStateCreateRequest(
            goal="想找 iOS 二手机。",
            task_type="ecommerce_guide",
            domain_state={
                "shoppingGuide": {
                    "mode": "recommend",
                    "category": "phone",
                    "useCases": [],
                    "requirements": [{
                        "key": "os",
                        "operator": "eq",
                        "value": "ios",
                        "unit": "enum",
                        "priority": "hard",
                        "source": "user",
                    }],
                    "candidateIds": [],
                    "comparedIds": [],
                    "evidenceStatus": "missing",
                },
                "taskStateExtraction": {
                    "reason": "complete_controlled_coverage",
                },
            },
        ))
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        schema = _tool_schema("search_products")
        view = build_decision_context_view(
            ready,
            user_message="想找 iOS 二手机。",
            allowed_tool_names=["search_products"],
        )
        action = deterministic_next_action(view)
        assert action is not None
        phases: list[tuple[str, int]] = []

        async def capture_state(state: TaskState, phase: str) -> None:
            phases.append((phase, state.revision))

        result = await execute_react_tool_action(
            state=ready,
            view=view,
            action=action,
            user_message="想找 iOS 二手机。",
            allowed_tool_schemas=[schema],
            tool_caller=AsyncMock(return_value=ToolTrace(
                tool="search_products",
                ok=True,
                durationMs=1.0,
                detail=_search_detail(),
            )),
            on_task_state=capture_state,
        )
        persisted = await get_task_state(ready.task_id)

        assert result.outcome.status == "SUCCEEDED"
        assert result.outcome.validator_outcome == "PASSED"
        assert result.outcome.state_revision_after == persisted.revision
        assert persisted.active_plan is not None
        assert persisted.active_plan.status == "completed"
        assert persisted.domain_state["validationResult"]["outcome"] == "passed"
        assert persisted.domain_state["candidateScope"]["status"] == "active"
        assert len(
            persisted.domain_state["candidateScope"]["rankedItemIds"]
        ) == 20
        assert [phase for phase, _revision in phases] == [
            "react_plan_committed",
            "react_tool_committed",
            "react_validation_committed",
        ]
        assert [revision for _phase, revision in phases] == sorted(
            revision for _phase, revision in phases
        )

    asyncio.run(scenario())
