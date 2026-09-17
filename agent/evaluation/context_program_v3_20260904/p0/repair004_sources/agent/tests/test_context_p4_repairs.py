"""Bounded P4 repairs: no provider, no business writes in invalid paths."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import llm
from app.control.react_context import build_decision_context_view
from app.control.react_decision import deterministic_next_action
from tests.test_react_context import _state
from tests.test_context_failure_repairs import _response


def model_state():
    state = _state(validated=True)
    state.domain_state.pop('scopeRerankRequest', None)
    state.domain_state['taskStateExtraction'] = {'executionKind': 'model', 'reason': 'no_deterministic_signal'}
    return state


def view(state):
    return build_decision_context_view(state, user_message='结合日常使用综合权衡',
        allowed_tool_names=['search_products', 'create_order'])


def test_unrouted_ready_model_state_has_bounded_choice():
    result = view(model_state())
    assert result.observation_summary.adaptive_trigger == 'bounded_action_choice'
    assert result.server_signals['adaptiveDecisionRequired']
    assert {x.option_id for x in result.allowed_action_options} == {'tool.search_products', 'answer.validated_context'}
    assert deterministic_next_action(result) is None


@pytest.mark.parametrize('boundary', ['unvalidated', 'stale', 'pending', 'unknown', 'deterministic'])
def test_bounded_choice_does_not_cross_existing_guards(boundary):
    state = model_state()
    if boundary == 'unvalidated': state.domain_state.pop('validationResult')
    elif boundary == 'stale': state.domain_state['candidateScope']['status'] = 'invalidated'
    elif boundary == 'pending': state = state.model_copy(update={'pending_questions': ['具体哪件？'], 'status': 'collecting_information'})
    elif boundary == 'unknown': state = state.model_copy(update={'unknowns': ['对象未知']})
    else: state.domain_state['taskStateExtraction']['executionKind'] = 'deterministic'
    assert view(state).observation_summary.adaptive_trigger != 'bounded_action_choice'


@pytest.mark.parametrize('second', ['valid', 'missing', 'invalid'])
def test_missing_call_retry_shares_payload_repair_budget(monkeypatch, second):
    state = model_state()
    monkeypatch.setattr(llm, '_deterministic_used_phone_task_state_decision', lambda *a: (None, {'reason': 'fixture'}))
    monkeypatch.setattr(llm, 'build_context_pack', AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(llm, 'context_pack_system_message', lambda p: {'role': 'system', 'content': 'fixture'})
    monkeypatch.setattr(llm.settings, 'task_state_extraction_strict_enabled', False)
    response = _response('{"status":"ready"}' if second == 'valid' else '{bad')
    call = None if second == 'missing' else response.choices[0].message.tool_calls[0]
    submit = AsyncMock(side_effect=[(None, _response('{}')), (call, response)])
    monkeypatch.setattr(llm, '_submit_task_state_extraction', submit)
    apply = AsyncMock(return_value=state.model_copy(update={'revision': state.revision + 1}))
    monkeypatch.setattr(llm, '_apply_task_state_update', apply)
    repair = AsyncMock(side_effect=AssertionError('no third request'))
    monkeypatch.setattr(llm, '_repair_task_state_payload', repair)
    async def run():
        return await llm._update_task_state_for_unified_harness('综合权衡', history=None,
            client=SimpleNamespace(), task_state=state, on_task_state=None)
    if second == 'invalid':
        with pytest.raises(llm.TaskStatePayloadValidationError): asyncio.run(run())
    else:
        result = asyncio.run(run())
        assert result.revision == state.revision + int(second == 'valid')
    assert submit.await_count == 2
    assert apply.await_count == int(second == 'valid')
    repair.assert_not_awaited()
    retry_messages = submit.await_args.args[1]
    assert all(m['role'] != 'tool' for m in retry_messages)
    assert all('tool_calls' not in m for m in retry_messages)


def test_validated_scope_reason_routes_without_a_model():
    state = model_state()
    state.domain_state['taskStateExtraction']['reason'] = 'validated_scope_answer'
    result = view(state)
    assert result.server_signals['scopeAnswerRequested']
    assert deterministic_next_action(result).kind == 'ANSWER'
