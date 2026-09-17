"""Product-question binding, isolated handoff, and factual-answer regressions."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from app import product_followup as followup, reference_context, catalog_conversation
from .test_commerce_workspace import setup, client, async_test, state_key
from .test_catalog_workspace import finish_job


def state():
    return {'engine':'owner-session','cards':[{'id':101,'title':'二手苹果11手机iphone11 wifi机'},
            {'id':102,'title':'华为Mate30二手手机'}], 'reference':{'handle':'a'*48}}


def plan(kind='ambiguous', numbers=None):
    return dict(route='product',action='inspect',followup=kind,accessory='充电器',referenceModel='iphone11',
                numbers=[1] if numbers is None else numbers,question='',query='',requirements=[])


@pytest.fixture
def resolved(monkeypatch):
    # Only signature/expiry infrastructure is doubled. Index/identity validation
    # and owner-bound workspace state are exercised by the real implementation.
    call=AsyncMock(return_value=SimpleNamespace(presentation_ids=(101,102)))
    monkeypatch.setattr(reference_context,'resolve_reference_context',call)
    return call


@async_test
async def test_question_binds_first_card_and_does_not_create_phone_filter(resolved):
    s=state();p=await followup.bind_plan(plan(),s,object(),'第一个苹果手机 有它的充电器吗？')
    assert p['productContext']['productId']=='101' and p['route']=='product'
    text,_=await followup.answer_question('原问题',p,{})
    assert '是否随附充电器' in text and '另外找适配' in text and 'iphone11' in text
    assert s==state()


@async_test
async def test_accessory_followup_reuses_anchor_but_not_phone_budget(resolved):
    s=state();first=await followup.bind_plan(plan(),s,object(),'第一个手机有充电器吗')
    s['productFollowup']=first['productContext']
    p=plan('accessory',[]);p['referenceModel']='';p['accessory']=''
    p['requirements']=[dict(facet='预算',mode='require',value='2000元以内',terms=['2000'])]
    result=await followup.bind_plan(p,s,object(),'另买一个，帮我找适配的')
    assert result['route']=='catalog' and result['action']=='new'
    assert result['retrievalQuery']=='适配iphone11的充电器'
    assert all(r['facet']!='预算' for r in result['requirements'])
    assert '2000' not in result['query']


@async_test
async def test_accessory_handoff_retains_only_new_explicit_budget(resolved):
    s=state();p=plan('accessory')
    p['requirements']=[dict(facet='预算',mode='require',value='100元以内',terms=['100'])]
    result=await followup.bind_plan(p,s,object(),'另买第一个手机的充电器，100元以内')
    assert result['requirements'][-1]['value']=='100元以内'


@async_test
async def test_out_of_range_and_stale_pending_do_not_resolve_other_product(resolved):
    s=state()
    result=await followup.bind_plan(plan(numbers=[9]),s,object(),'第九个')
    assert result['action']=='clarify' and not result['productContext']
    resolved.assert_not_awaited()
    first=await followup.bind_plan(plan(),s,object(),'第一个')
    s['productFollowup']=first['productContext'];s['cards'].reverse()
    result=await followup.bind_plan(plan('accessory',[]),s,object(),'另买一个')
    assert not result['productContext']


@async_test
async def test_expired_signed_reference_and_reordered_cards_fail_closed(resolved):
    s=state();s['cards'].reverse()
    assert not (await followup.bind_plan(plan(),s,object(),'第一个'))['productContext']
    resolved.side_effect=reference_context.ReferenceContextError('reference_context_expired')
    result=await followup.bind_plan(plan(),state(),object(),'第一个')
    assert '失效' in result['question'] and not result['productContext']


@async_test
async def test_fabricated_model_cannot_become_accessory_query(resolved):
    p=plan('accessory');p['referenceModel']='iPhone99'
    result=await followup.bind_plan(p,state(),object(),'第一个的充电器')
    assert result['route']=='product' and result['action']=='clarify'


@async_test
async def test_missing_or_fabricated_bundle_evidence_never_becomes_yes(resolved,monkeypatch):
    p=await followup.bind_plan(plan('included'),state(),object(),'第一个附赠充电器吗')
    monkeypatch.setattr(catalog_conversation,'model_call',AsyncMock(return_value=(
        SimpleNamespace(content=json.dumps({'quotes':[{'field':'description','text':'附赠充电器'}]})),{})))
    text,_=await followup.answer_question('附赠吗',p,{'description':'支持20W充电'})
    assert '暂时不能确认' in text


@async_test
async def test_bundle_answer_quotes_actual_listing_only(resolved,monkeypatch):
    p=await followup.bind_plan(plan('included'),state(),object(),'第一个附赠充电器吗')
    monkeypatch.setattr(catalog_conversation,'model_call',AsyncMock(return_value=(
        SimpleNamespace(content=json.dumps({'quotes':[{'field':'description','text':'仅手机，不含充电器'}]})),{})))
    text,_=await followup.answer_question('附赠吗',p,{'description':'包装：仅手机，不含充电器。'})
    assert '仅手机，不含充电器' in text and '商品记录写明' in text


@async_test
async def test_general_incomplete_accessories_does_not_establish_charger_inclusion(resolved,monkeypatch):
    p=await followup.bind_plan(plan('included'),state(),object(),'附赠充电器吗')
    monkeypatch.setattr(catalog_conversation,'model_call',AsyncMock(return_value=(
        SimpleNamespace(content=json.dumps({'quotes':[{'field':'description','text':'非原装配件不齐全'}]})),{})))
    text,_=await followup.answer_question('附赠充电器吗',p,{'description':'非原装配件不齐全'})
    assert '非原装配件不齐全' in text and '暂时不能确认' in text


@async_test
async def test_price_question_keeps_local_simulated_provenance(resolved,monkeypatch):
    from app.api import commerce_demo as auth
    p=await followup.bind_plan(plan('detail'),state(),object(),'第一个多少钱')
    monkeypatch.setattr(auth.settings,'commerce_workspace_local_offers_enabled',True)
    monkeypatch.setattr(auth,'_java',AsyncMock(return_value={'product':{'id':101,'title':'iphone11'},
        'offer':{'priceMinor':200000,'kind':'local_simulated','available':7}}))
    facts=await followup.read_facts(p)
    assert '非真实报价' in facts['price']
    monkeypatch.setattr(catalog_conversation,'model_call',AsyncMock(return_value=(
        SimpleNamespace(content=json.dumps({'quotes':[{'field':'price','text':'2000.00元'}]})),{})))
    text,_=await followup.answer_question('多少钱',p,facts)
    assert '2000.00元' in text and '非真实报价' in text


@async_test
async def test_product_questions_checkpoint_without_replacing_phone_cards(setup,resolved,monkeypatch):
    from app import task_state, graph
    from app.api import commerce_workspace as ws
    app,store,_=setup
    monkeypatch.setattr(ws.auth.settings,'catalog_workspace_enabled',True)
    monkeypatch.setattr(task_state,'get_session_task_state',AsyncMock(return_value=SimpleNamespace(task_id='task',domain_state={})))
    monkeypatch.setattr(graph,'read_task_cursor',AsyncMock(return_value=None))
    monkeypatch.setattr(catalog_conversation,'plan_turn',AsyncMock(return_value=(plan(),{})))
    async with client(app) as c:
        response=await c.get('/api/commerce-demo/workspace')
        c.headers['X-CSRF-Token']=response.json()['csrfToken']
        key=state_key(store,':guest')
        s=await ws._load(key);s.update(state())
        async with ws._lock(key):await ws._save(key,s)
        before=deepcopy(s)
        response=await c.post('/api/commerce-demo/workspace/run',json={
            'message':'第一个苹果手机 有它的充电器吗？','requestId':'question-fixture-001','mode':'step'})
        assert response.status_code==200,response.text
        run=response.json()['run']
        for _ in range(4):
            r=await c.post('/api/commerce-demo/workspace/control/step',json={'runId':run['id'],'revision':run['revision']})
            assert r.status_code==200,r.text
            await finish_job(key)
            run=(await c.get('/api/commerce-demo/workspace')).json()['run']
        assert run['status']=='completed'
        saved=await ws._load(key)
        assert saved['cards']==before['cards'] and saved['reference']==before['reference']
        assert saved['engine']==before['engine'] and not saved.get('catalogSearch')
        answer=saved['messages'][-1]
        assert answer['cards']==[] and '是否随附' in answer['content']
