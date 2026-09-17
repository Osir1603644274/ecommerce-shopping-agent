"""Strict decision and action-validation tests for ReAct V0."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.control.react_actions import ActionOutcome, NextAction
from app.control.react_context import DecisionContextView
from app.control.react_decision import (
    REACT_ACTION_TOOL_NAME,
    ReactDecisionError,
    decide_react_v0_once,
    decide_next_action,
    observe_react_v0_shadow,
    validate_next_action,
)
from tests.test_react_context import _state


def _view(*, answer_ref=None, pending=None, unknowns=None) -> DecisionContextView:
    options = [{
        "optionId": "tool.search_products",
        "kind": "CALL_TOOL",
        "reasonCode": "bounded_choice",
        "toolName": "search_products",
        "argumentRefs": {
            "query": "taskState.goal",
            "category": "shoppingGuide.category",
            "requirements": "shoppingGuide.compiledRequirements",
        },
    }]
    if answer_ref is not None:
        options.append({
            "optionId": "answer.validated_context",
            "kind": "ANSWER",
            "reasonCode": "bounded_choice",
            "answerContextRef": answer_ref,
        })
    for index, question in enumerate(pending or []):
        options.append({
            "optionId": f"clarify.pending.{index}",
            "kind": "ASK_CLARIFICATION",
            "reasonCode": "bounded_choice",
            "question": question,
        })
    return DecisionContextView.model_validate({
        "taskId": "task-1",
        "taskRevision": 7,
        "taskStatus": "ready",
        "goal": "三千以内手机",
        "userMessage": "继续",
        "shoppingMode": "recommend",
        "category": "phone",
        "useCases": [],
        "requirements": [],
        "unknowns": list(unknowns or []),
        "pendingQuestions": list(pending or []),
        "candidateScope": None,
        "serverSignals": {
            "candidateScopeAvailable": False,
            "comparisonBound": False,
            "rerankRequested": False,
            "validatedEvidenceAvailable": answer_ref is not None,
        },
        "allowedActions": list(dict.fromkeys(item["kind"] for item in options)),
        "allowedTools": [{
            "name": "search_products",
            "argumentRefs": {
                "query": "taskState.goal",
                "category": "shoppingGuide.category",
                "requirements": "shoppingGuide.compiledRequirements",
            },
            "effectClass": "READ_ONLY",
        }],
        "observationSummary": {
            "evidenceGapKeys": [],
            "staleCandidateScope": False,
            "adaptiveTrigger": None,
        },
        "allowedActionOptions": options,
        "answerContextRef": answer_ref,
        "lastOutcome": None,
        "decisionViewHash": "a" * 64,
    })


def _action(kind: str, **payload) -> NextAction:
    return NextAction.model_validate({
        "actionId": "action-1",
        "taskId": "task-1",
        "basedOnRevision": 7,
        "decisionViewHash": "a" * 64,
        "kind": kind,
        "reasonCode": "bounded_choice",
        **payload,
    })


def _proposal(option_id: str, **payload) -> dict:
    return {
        "taskId": "task-1",
        "basedOnRevision": 7,
        "decisionViewHash": "a" * 64,
        "optionId": option_id,
        **payload,
    }


def _response(arguments, *, name=REACT_ACTION_TOOL_NAME, extra_calls=0):
    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name=name,
            arguments=(arguments if isinstance(arguments, str) else json.dumps(arguments)),
        ),
    )
    calls = [call] + [call] * extra_calls
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=calls))]
    )


def test_validate_call_tool_requires_exact_published_refs() -> None:
    view = _view()
    action = _action(
        "CALL_TOOL",
        toolName="search_products",
        argumentRefs=view.allowed_tools[0].argument_refs,
    )
    assert validate_next_action(action, view) is action

    forged = _action(
        "CALL_TOOL",
        toolName="search_products",
        argumentRefs={"query": "literal user-controlled value"},
    )
    with pytest.raises(ReactDecisionError, match="published server-owned option") as exc:
        validate_next_action(forged, view)
    assert exc.value.code == "action_option_mismatch"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("task_id", "task-other", "task_identity_mismatch"),
        ("based_on_revision", 6, "stale_task_revision"),
        ("decision_view_hash", "b" * 64, "decision_view_hash_mismatch"),
    ],
)
def test_validate_rejects_stale_or_cross_identity(field, value, code) -> None:
    action = _action(
        "CALL_TOOL",
        toolName="search_products",
        argumentRefs=_view().allowed_tools[0].argument_refs,
    ).model_copy(update={field: value})
    with pytest.raises(ReactDecisionError) as exc:
        validate_next_action(action, _view())
    assert exc.value.code == code


def test_answer_requires_exact_current_validator_reference() -> None:
    with pytest.raises(ReactDecisionError) as exc:
        validate_next_action(
            _action("ANSWER", answerContextRef="validated-task:task-1:r7"),
            _view(),
        )
    assert exc.value.code == "action_not_allowed"

    view = _view(answer_ref="validated-task:task-1:r7")
    assert validate_next_action(
        _action("ANSWER", answerContextRef=view.answer_context_ref), view
    ).kind == "ANSWER"


def test_clarification_must_be_published_when_question_exists() -> None:
    view = _view(pending=["请告诉我预算。"])
    with pytest.raises(ReactDecisionError) as exc:
        validate_next_action(
            _action("ASK_CLARIFICATION", question="请告诉我品牌。"), view
        )
    assert exc.value.code == "action_option_mismatch"
    assert validate_next_action(
        _action("ASK_CLARIFICATION", question="请告诉我预算。"), view
    ).kind == "ASK_CLARIFICATION"


def test_decide_sends_only_bounded_view_and_parses_one_action() -> None:
    view = _view()
    args = _proposal("tool.search_products")
    create = AsyncMock(return_value=_response(args))
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    action = asyncio.run(
        decide_next_action(view, client=client, model="deepseek-chat")
    )

    assert action.kind == "CALL_TOOL"
    assert action.action_id.startswith("action-")
    assert action.argument_refs == view.allowed_tools[0].argument_refs
    kwargs = create.await_args.kwargs
    assert kwargs["tools"][0]["function"]["name"] == REACT_ACTION_TOOL_NAME
    properties = kwargs["tools"][0]["function"]["parameters"]["properties"]
    assert "actionId" not in properties
    assert "argumentRefs" not in properties
    assert "kind" not in properties
    assert set(properties) == {
        "schemaVersion", "taskId", "basedOnRevision", "decisionViewHash",
        "optionId",
    }
    assert json.loads(kwargs["messages"][1]["content"])["decisionViewHash"] == "a" * 64
    assert len(kwargs["messages"]) == 2


def test_model_cannot_select_unpublished_option_id() -> None:
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=_response(_proposal("tool.create_order")))
    )))
    with pytest.raises(ReactDecisionError) as exc:
        asyncio.run(decide_next_action(
            _view(), client=client, model="deepseek-chat"
        ))
    assert exc.value.code == "action_option_not_allowed"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_response("not-json"), "action_json_invalid"),
        (_response({}, extra_calls=1), "action_call_count_invalid"),
        (_response({}, name="search_products"), "action_tool_name_invalid"),
    ],
)
def test_decide_fails_closed_on_malformed_model_output(response, code) -> None:
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=response)
    )))
    with pytest.raises(ReactDecisionError) as exc:
        asyncio.run(
            decide_next_action(_view(), client=client, model="deepseek-chat")
        )
    assert exc.value.code == code


def test_shadow_observation_has_zero_taskstate_writes() -> None:
    state = _state()
    state_payload = state.model_dump(by_alias=True, mode="json")
    state_payload["domainState"]["taskStateExtraction"] = {
        "reason": "unsupported_game_camera_evidence",
    }
    state = state.__class__.model_validate(state_payload)
    before = state.model_dump(by_alias=True, mode="json")
    view_args = {
        "taskId": state.task_id,
        "basedOnRevision": state.revision,
        "decisionViewHash": None,
        "optionId": "answer.validated_context",
    }

    async def create(**kwargs):
        submitted_view = json.loads(kwargs["messages"][1]["content"])
        view_args["decisionViewHash"] = submitted_view["decisionViewHash"]
        return _response(view_args)

    create_mock = AsyncMock(side_effect=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create_mock
    )))
    observed = asyncio.run(observe_react_v0_shadow(
        state=state,
        user_message="继续",
        allowed_tool_names=["search_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
    ))

    assert observed.status == "accepted"
    assert observed.action is not None
    assert observed.decision_source == "deterministic_policy"
    assert observed.action.kind == "ANSWER"
    create_mock.assert_not_awaited()
    assert state.model_dump(by_alias=True, mode="json") == before


def test_shadow_rejection_is_an_observation_not_an_exception() -> None:
    state = _state()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=_response("bad-json"))
    )))
    observed = asyncio.run(observe_react_v0_shadow(
        state=state,
        user_message="继续",
        allowed_tool_names=["search_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
    ))
    assert observed.status == "rejected"
    assert observed.error_code == "action_json_invalid"


def test_shadow_uses_deterministic_single_progress_tool_without_model() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["shoppingGuide"]["requirements"][0]["value"] = 250_000
    changed = state.__class__.model_validate(payload)
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create
    )))

    observed = asyncio.run(observe_react_v0_shadow(
        state=changed,
        user_message="预算改成2500",
        allowed_tool_names=["search_products", "compare_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
    ))

    assert observed.status == "accepted"
    assert observed.decision_source == "deterministic_policy"
    assert observed.action is not None
    assert (observed.action.kind, observed.action.tool_name) == (
        "CALL_TOOL", "search_products",
    )
    create.assert_not_awaited()


def test_shadow_clarifies_server_proven_unbound_negative_target() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "unbound_negative_target",
    }
    ambiguous = state.__class__.model_validate(payload)
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create
    )))

    observed = asyncio.run(observe_react_v0_shadow(
        state=ambiguous,
        user_message="不要那个牌子",
        allowed_tool_names=["search_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
    ))

    assert observed.decision_source == "deterministic_policy"
    assert observed.action is not None
    assert observed.action.kind == "ASK_CLARIFICATION"
    assert observed.action.question == "你说的“那个牌子”具体指哪个品牌？"
    create.assert_not_awaited()


def test_shadow_answers_server_proven_evidence_gap_from_valid_scope() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["status"] = "collecting_information"
    payload["unknowns"] = ["当前冻结商品快照不含可验证的芯片性能指标。"]
    payload["pendingQuestions"] = ["当前数据没有芯片性能指标。"]
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "unsupported_game_camera_evidence",
    }
    evidence_gap = state.__class__.model_validate(payload)
    async def create(**kwargs):
        submitted = json.loads(kwargs["messages"][1]["content"])
        return _response({
            "taskId": evidence_gap.task_id,
            "basedOnRevision": evidence_gap.revision,
            "decisionViewHash": submitted["decisionViewHash"],
            "optionId": "answer.validated_context",
        })

    create = AsyncMock(side_effect=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create
    )))

    observed = asyncio.run(observe_react_v0_shadow(
        state=evidence_gap,
        user_message="这三款谁打游戏帧率最高？",
        allowed_tool_names=["search_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
    ))

    assert observed.decision_source == "deterministic_policy"
    assert observed.action is not None
    assert observed.action.kind == "ANSWER"
    create.assert_not_awaited()


def test_shadow_executes_server_bound_comparison_without_model() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["shoppingGuide"]["comparedIds"] = [1, 2]
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "bound_comparison",
    }
    payload["domainState"].pop("scopeRerankRequest", None)
    comparison = state.__class__.model_validate(payload)
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create
    )))

    observed = asyncio.run(observe_react_v0_shadow(
        state=comparison,
        user_message="比较第一个和第二个",
        allowed_tool_names=["search_products", "compare_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
    ))

    assert observed.decision_source == "deterministic_policy"
    assert observed.action is not None
    assert (observed.action.kind, observed.action.tool_name) == (
        "CALL_TOOL", "compare_products",
    )
    create.assert_not_awaited()


def test_shadow_executes_server_proven_broad_discovery_without_model() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "broad_catalog_discovery",
    }
    discovery = state.__class__.model_validate(payload)
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create
    )))

    observed = asyncio.run(observe_react_v0_shadow(
        state=discovery,
        user_message="性价比高、其他质量好的手机",
        allowed_tool_names=["search_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
    ))

    assert observed.decision_source == "deterministic_policy"
    assert observed.action is not None
    assert (observed.action.kind, observed.action.tool_name) == (
        "CALL_TOOL", "search_products",
    )
    create.assert_not_awaited()


@pytest.mark.parametrize(
    "message",
    [
        "先不用把20个都详细写出来，只展示前三个",
        "没有实测数据也没关系，就根据你确实知道的属性告诉我怎么选",
    ],
)
def test_shadow_answers_presentation_or_advice_request_from_scope(message) -> None:
    state = _state()
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create
    )))

    observed = asyncio.run(observe_react_v0_shadow(
        state=state,
        user_message=message,
        allowed_tool_names=["search_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
    ))

    assert observed.decision_source == "deterministic_policy"
    assert observed.action is not None
    assert observed.action.kind == "ANSWER"
    create.assert_not_awaited()


def test_live_second_decision_answers_after_validated_tool_outcome() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "broad_catalog_discovery",
    }
    current = state.__class__.model_validate(payload)
    outcome = ActionOutcome(
        actionId="action-search",
        status="SUCCEEDED",
        observationRef="validated-scope:scope-react-v0-r17",
        validatorOutcome="PASSED",
        stateRevisionAfter=current.revision,
        retryable=False,
        errorCode=None,
    )
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create
    )))

    observed = asyncio.run(decide_react_v0_once(
        state=current,
        user_message="性价比高的手机",
        allowed_tool_names=["search_products"],
        client=client,
        model="deepseek-chat",
        timeout_seconds=1,
        last_outcome=outcome,
    ))

    assert observed.decision_source == "deterministic_policy"
    assert observed.action is not None
    assert observed.action.kind == "ANSWER"
    assert observed.action.reason_code == "answer_after_validated_action"
    create.assert_not_awaited()
