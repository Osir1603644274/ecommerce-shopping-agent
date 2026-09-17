"""One repair sees independently checkable blockers even after an early error."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app import llm
from tests.test_react_context import _state
from tests.test_context_failure_repairs import _response

def blocked():
    return _state(validated=True).model_copy(update={
        'status':'collecting_information','unknowns':['比较对象未唯一绑定'],
        'pending_questions':['请明确两个商品。'],
    })

def error():
    return llm.TaskStatePayloadValidationError('invalid goal',code='invalid_goal_placeholder',field_path='goal')

def test_joint_error_diagnostic_preserves_state_and_original_patch():
    state=blocked();before=state.model_dump_json()
    payload={'goal':'null','status':'ready','pendingQuestions':[],'resolveUnknowns':[]}
    saved=json.dumps(payload)
    feedback=llm._task_state_validation_error_message(error(),state=state,proposed_arguments=payload)
    assert feedback['code']=='invalid_goal_placeholder'
    assert feedback['additionalViolations'][0]['code']=='cannot_clear_pending_while_unknowns_remain'
    assert feedback['currentBlockingState']['unknowns']==state.unknowns
    assert feedback['persist']==0 and feedback['additionalViolations'][0]['persist']==0
    assert json.dumps(payload)==saved and state.model_dump_json()==before
    with pytest.raises(llm.TaskStatePayloadValidationError):
        llm._validated_model_task_patch(payload)

@pytest.mark.parametrize('payload', [None,{'addUnknowns':None},{'resolveUnknowns':12},{'pendingQuestions':{}},{'addUnknowns':[{}]}])
def test_malformed_patch_cannot_crash_diagnostic_or_get_rewritten(payload):
    feedback=llm._task_state_validation_error_message(error(),state=blocked(),proposed_arguments=payload)
    assert feedback['code']=='invalid_goal_placeholder'
    assert 'additionalViolations' not in feedback

def test_valid_resolution_is_not_reported_as_an_unresolved_blocker():
    state=blocked()
    feedback=llm._task_state_validation_error_message(error(),state=state,proposed_arguments={
        'status':'ready','resolveUnknowns':state.unknowns,'pendingQuestions':[]})
    assert 'additionalViolations' not in feedback

def test_repair_contains_both_errors_without_an_extra_request(monkeypatch):
    state=blocked();original=_response('{"goal":"null","status":"ready","pendingQuestions":[]}')
    valid=_response(json.dumps({'status':'collecting_information','pendingQuestions':state.pending_questions}))
    submit=AsyncMock(return_value=(valid.choices[0].message.tool_calls[0],valid))
    monkeypatch.setattr(llm,'_submit_task_state_extraction',submit)
    monkeypatch.setattr(llm.settings,'task_state_extraction_strict_enabled',False)
    args,_=asyncio.run(llm._repair_task_state_payload(SimpleNamespace(),planning_messages=[],
        original_call=original.choices[0].message.tool_calls[0],validation_error=error(),state=state,
        proposed_arguments={'goal':'null','status':'ready','pendingQuestions':[]}))
    assert args['status']=='collecting_information' and submit.await_count==1
    messages=submit.await_args.args[1]
    feedback=json.loads(next(m['content'] for m in messages if m['role']=='tool'))
    assert feedback['additionalViolations'][0]['field_path']=='pendingQuestions'

def test_legacy_error_serialization_unchanged_without_state():
    assert llm._task_state_validation_error_message(error())=={
        'valid':False,'code':'invalid_goal_placeholder','field_path':'goal','message':'invalid goal','persist':0}
