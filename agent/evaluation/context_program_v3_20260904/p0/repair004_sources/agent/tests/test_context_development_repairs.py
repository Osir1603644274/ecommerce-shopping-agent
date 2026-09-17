"""Development-discovered boundaries, including contrastive negative inputs."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app import llm
from app.domains.ecommerce.brand_negation import parse_brand_negations
from app.model_compat import tool_choice_kwargs
from app.control.react_decision import decide_next_action, ReactDecisionError
from tests.test_context_p4_repairs import model_state, view
from tests.test_shopping_state_update import _state


def storage_state():
    state=_state()
    state.domain_state['shoppingGuide']['requirements'].append({'key':'storage_gb','operator':'gte',
        'value':128,'unit':'GB','priority':'hard','source':'user'})
    return state


@pytest.mark.parametrize('message',[
    '取消存储容量要求，预算和系统条件不变。',
    '去掉存储要求；保留预算和系统。',
    '不再要求存储空间要求，系统不变。',
])
def test_explicit_storage_withdrawal_removes_only_storage(message):
    state=storage_state()
    args,obs=llm._deterministic_used_phone_task_state_decision(state,message,None)
    assert args is not None and 'storage_gb' in obs['coveredKeys']
    guide=args['domainStatePatch']['shoppingGuide']
    assert guide['removeRequirementKeys']==['storage_gb']
    assert {r['key'] for r in guide['upsertRequirements']}=={'os','price_minor'}
    payload,_=llm._build_validated_task_state_payload(
        state,args,message=message,require_status=True,allow_auto_ready=False)
    assert 'storage_gb' not in {r['key'] for r in payload['domainStatePatch']['shoppingGuide']['requirements']}


@pytest.mark.parametrize('message',[
    '不要取消存储要求，预算不变。','别去掉存储要求。','不能删掉存储限制。',
])
def test_negated_withdrawal_never_removes_requirement(message):
    assert 'storage_gb' not in llm._explicit_used_phone_requirement_removals(message)


def test_new_storage_value_cannot_be_ignored_by_partial_deterministic_parse():
    args,obs=llm._deterministic_used_phone_task_state_decision(
        storage_state(),'存储至少256GB，预算保持不变。',None)
    assert args is None and obs['reason']=='partial_controlled_coverage'


@pytest.mark.parametrize('message,value',[
    ('预算临时改为1元，找不到就明确说无候选，不要放宽条件。',100),
    ('预算暂时调整为1200元，其他条件不变。',120000),
    ('预算现在提高到两千元。',200000),
])
def test_temporary_budget_is_explicit_not_ignored(message,value):
    assert llm._explicit_phone_price_ceiling(message)==value
    assert not parse_brand_negations(message).unresolved_cues
    args,_=llm._deterministic_used_phone_task_state_decision(_state(),message,None)
    requirements=args['domainStatePatch']['shoppingGuide']['upsertRequirements']
    assert next(r['value'] for r in requirements if r['key']=='price_minor')==value


@pytest.mark.parametrize('message',['不要放宽条件','不要擅自修改硬要求','不要调整预算','不要更改系统要求'])
def test_instruction_prohibition_is_not_brand_rejection(message):
    result=parse_brand_negations(message)
    assert not result.targets and not result.unresolved_cues


def test_real_unbound_or_named_brand_negation_is_still_enforced():
    assert parse_brand_negations('不要那个牌子').unresolved_cues
    assert parse_brand_negations('不要苹果').targets[0].values==['apple']


def test_v4_forcing_requires_explicit_non_thinking():
    choice={'type':'function','function':{'name':'test'}}
    assert tool_choice_kwargs('deepseek-v4-flash',choice)=={}
    assert tool_choice_kwargs('deepseek-v4-flash',choice,thinking_enabled=True)=={}
    assert tool_choice_kwargs('deepseek-v4-flash',choice,thinking_enabled=False)=={'tool_choice':choice}


@pytest.mark.parametrize('finish',['tool_calls','length'])
def test_decider_forces_tool_in_non_thinking_mode_and_rejects_truncation(finish):
    v=view(model_state())
    call=SimpleNamespace(function=SimpleNamespace(name='submit_react_action',arguments=json.dumps({
        'schemaVersion':'react-action-proposal-v1','taskId':v.task_id,'basedOnRevision':v.task_revision,
        'decisionViewHash':v.decision_view_hash,'optionId':'answer.validated_context'})))
    create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(tool_calls=[call]))]))
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    if finish=='length':
        with pytest.raises(ReactDecisionError,match='truncated'):
            asyncio.run(decide_next_action(v,client=client,model='deepseek-v4-flash'))
    else: assert asyncio.run(decide_next_action(v,client=client,model='deepseek-v4-flash')).kind=='ANSWER'
    assert create.await_args.kwargs['tool_choice']['function']['name']=='submit_react_action'
    assert create.await_args.kwargs['extra_body']['thinking']['type']=='disabled'
