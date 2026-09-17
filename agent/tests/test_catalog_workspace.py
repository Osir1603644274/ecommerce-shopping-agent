"""Workspace protocol tests with explicit model/search doubles, not live quality."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_commerce_workspace import setup, client, async_test, state_key
from app.api import commerce_controls as controls, commerce_workspace as ws
from app.api import catalog_workspace as workflow
from app.catalog_conversation import transition, compact_evidence
from app.catalog_service import document_scope, fingerprint


def plan(action='search', query='收纳盒', numbers=None):
    return dict(route='catalog', action=action, query=query, numbers=numbers or [], question='')


def scope(query='收纳盒'):
    sources=[]
    for source in ['kuaisearch','multicpr']:
        hits=[]; metadata=[]
        for i in [1,2]:
            title='同标题收纳盒' if i==1 else source+'透明收纳盒'
            hit=dict(docid=source+':'+str(i), source=source, rank=i, score=-float(i))
            hits.append(hit)
            metadata.append(dict(docid=hit['docid'], titleGroupKey=title,
                fields=dict(title={'value':title}, brand={'value':None}, seller={'value':source+'卖家'}),
                provenance={'recordSha256':'a'*64}))
        sources.append(dict(source=source, hits=hits, metadata=metadata))
    return document_scope(query,sources,'b'*64)


def test_document_scope_keeps_cross_source_identity_without_score_comparison():
    result=scope()
    assert [g['number'] for g in result['groups']]==[1,2,3,4]
    assert [g['members'][0]['docid'] for g in result['groups'][:2]]==['kuaisearch:1','multicpr:1']
    assert all(len(g['members'])==1 for g in result['groups'])
    assert len([m for g in result['groups'] for m in g['members']])==4
    assert result['commerceAuthority'] is False
    projected=compact_evidence(result)
    assert all('recordSha256' not in row and 'seller' not in row for row in projected)
    assert all(not row['priceKnown'] for row in projected)


def test_raw_brand_placeholders_and_title_markup_are_not_published_as_authority():
    from app.catalog_conversation import render_documents
    value=scope()
    value['groups'][0]['members'][0]['brand']='others'
    value['groups'][1]['members'][0]['brand']='无品牌'
    value['groups'][0]['title']='[点击](https://example.invalid) <img> *声称*'
    assert compact_evidence(value)[0]['brandsInSource']==[]
    assert '\\[点击\\]' in render_documents(value)
    assert '\\<img\\>' in render_documents(value)


@async_test
async def test_deployment_package_name_uses_matching_request_model(monkeypatch):
    import importlib
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    deployed=importlib.import_module('agent.app.catalog_worker')
    schema=importlib.import_module('agent.app.catalog_evidence')
    bridge=deployed.LiveBridge.__new__(deployed.LiveBridge)
    bridge.closed=False;bridge.strategy={};bridge.lock=asyncio.Lock()
    bridge.connection=SimpleNamespace(send=lambda data:None)
    bridge._receive=AsyncMock(return_value={'kind':'result','result':{'hits':[]}})
    binding=schema.CatalogBinding(dataRoot='D:/fixture',runId='deployed-package',manifestSha256='a'*64)
    request=schema.CatalogSearchRequest(query='fixture',source='multicpr',limit=10,binding=binding)
    result=await bridge.provider(binding)(request)
    assert result['query']=='fixture' and result['hits']==[]


def test_refine_undo_new_cancel_and_comparison_leave_no_stale_references():
    current=dict(revision=3,query='收纳盒',scope=scope(),history=[])
    modified,_=transition(current,plan('refine','透明收纳盒'))
    assert modified['scope'] is None and current['scope'] is not None
    restored,_=transition(modified,plan('undo',''))
    assert restored['scope']==current['scope'] and restored['query']=='收纳盒'
    new,_=transition(restored,plan('new','电脑支架'))
    assert not new['history'] and new['scope'] is None
    cancelled,_=transition(new,plan('cancel',''))
    assert not cancelled['query'] and cancelled['scope'] is None
    compared,_=transition(current,plan('compare','',[1,2]))
    assert compared==current


def test_corrupt_current_or_undo_scope_is_rejected():
    corrupt=scope();corrupt['groups'][0]['members'][0]['docid']='multicpr:foreign'
    with pytest.raises(ValueError,match='integrity'):
        transition(dict(revision=1,query='x',scope=corrupt,history=[]),plan('compare','',[1,2]))
    with pytest.raises(ValueError,match='integrity'):
        transition(dict(revision=2,query='y',scope=None,history=[dict(query='x',scope=corrupt)]),plan('undo',''))


@async_test
async def test_empty_results_do_not_invoke_a_model_to_invent_candidates(monkeypatch):
    from app import catalog_conversation
    call=AsyncMock()
    monkeypatch.setattr(catalog_conversation,'model_call',call)
    empty=document_scope('没有候选',[dict(source=s,hits=[],metadata=[]) for s in ['kuaisearch','multicpr']],'b'*64)
    answer,receipt=await catalog_conversation.answer_turn('找商品',plan(),dict(query='找商品',scope=empty))
    assert '没有可展示的候选' in answer and receipt is None
    call.assert_not_awaited()


@pytest.mark.parametrize('finish',['stop','length'])
@async_test
async def test_short_model_mode_retains_incomplete_receipt(monkeypatch,finish):
    from app import llm,catalog_conversation
    create=AsyncMock(return_value=SimpleNamespace(id='fixture',usage=None,
        choices=[SimpleNamespace(finish_reason=finish,message=SimpleNamespace(content='部分回答'))]))
    context=AsyncMock()
    context.__aenter__.return_value=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm,'get_client',lambda:context)
    monkeypatch.setattr(catalog_conversation.settings,'deepseek_model','deepseek-v4-fixture')
    if finish=='length':
        with pytest.raises(catalog_conversation.CatalogModelError) as error:
            await catalog_conversation.model_call([dict(role='user',content='fixture')])
        assert error.value.receipt['finishReason']=='length'
        assert error.value.receipt['partialOutput']=='部分回答'
    else:
        _,receipt=await catalog_conversation.model_call([dict(role='user',content='fixture')])
        assert receipt['thinking']=='disabled'
    assert create.call_args.kwargs['extra_body']=={'thinking':{'type':'disabled'}}


@async_test
async def test_version_change_does_not_resume_with_different_code(setup,monkeypatch):
    app,store,_=setup
    async with client(app) as c:
        key,_=await prepare_client(c,store,monkeypatch)
        run=await ws._load(key+':run');run['catalogCodeBinding']='different'
        await workflow.work(key,run,'step',None)
        saved=await ws._load(key+':run')
        assert saved['status']=='interrupted' and saved['catalogPhase']==0
        assert 'catalogSearch' not in (await ws._load(key))


async def prepare_client(c, store, monkeypatch, *, mode='step'):
    from app import catalog_conversation, task_state, graph
    monkeypatch.setattr(ws.auth.settings,'catalog_workspace_enabled',True)
    monkeypatch.setattr(task_state,'get_session_task_state',AsyncMock(return_value=None))
    monkeypatch.setattr(graph,'read_task_cursor',AsyncMock(return_value=None))
    monkeypatch.setattr(catalog_conversation,'plan_turn',AsyncMock(return_value=(plan(),{'fixture':True})))
    result=await c.get('/api/commerce-demo/workspace')
    c.headers['X-CSRF-Token']=result.json()['csrfToken']
    key=state_key(store,':guest')
    result=await c.post('/api/commerce-demo/workspace/run',json=dict(message='收纳盒',requestId='fixture-catalog-0001',mode=mode))
    assert result.status_code==200,result.text
    return key,result.json()['run']


async def finish_job(key):
    job=controls.JOBS.get(key)
    if job:
        await asyncio.wait_for(job,5)


@async_test
async def test_real_workspace_steps_checkpoint_reuse_and_no_commerce_cards(setup, monkeypatch):
    app,store,_=setup
    original_get=store.get
    async def yielding_get(key):
        await asyncio.sleep(.001)
        return await original_get(key)
    monkeypatch.setattr(store,'get',yielding_get)
    search=AsyncMock(return_value=scope())
    monkeypatch.setattr(workflow,'get_catalog_service',lambda:SimpleNamespace(search=search))
    monkeypatch.setattr(workflow,'answer_turn',AsyncMock(return_value=('候选1是收纳盒，价格未知。',{'fixture':True})))
    async with client(app) as c:
        key,run=await prepare_client(c,store,monkeypatch)
        for _ in range(4):
            result=await c.post('/api/commerce-demo/workspace/control/step',json=dict(runId=run['id'],revision=run['revision']))
            assert result.status_code==200,result.text
            await finish_job(key)
            run=(await c.get('/api/commerce-demo/workspace')).json()['run']
        assert run['status']=='completed' and search.await_count==1
        state=await ws._load(key)
        assert state['cards']==[] and state['selection'] is None and state['reference'] is None
        assert state['catalogSearch']['scope']['scopeId']==scope()['scopeId']
        assert len([m for m in state['messages'] if m['role']=='assistant'])==1
        # Re-enter publication after a crash: the same answer is not appended twice.
        saved=await ws._load(key+':run');saved['catalogPhase']=3;saved['status']='running'
        await workflow.work(key,saved,'continue',None)
        assert len([m for m in (await ws._load(key))['messages'] if m['role']=='assistant'])==1
        bad=await c.post('/api/commerce-demo/workspace/selection',json={'productId':'kuaisearch:1'})
        assert bad.status_code in {409,422}


@async_test
async def test_prepare_crash_recovery_is_idempotent(setup,monkeypatch):
    app,store,_=setup
    async with client(app) as c:
        key,_=await prepare_client(c,store,monkeypatch)
        run=await ws._load(key+':run')
        original=workflow.checkpoint
        monkeypatch.setattr(workflow,'checkpoint',AsyncMock(side_effect=RuntimeError('crash_after_state_write')))
        await workflow.work(key,run,'step',None)
        after=await ws._load(key)
        assert after['catalogSearch']['revision']==1
        monkeypatch.setattr(workflow,'checkpoint',original)
        run=await ws._load(key+':run');run['status']='running'
        await workflow.work(key,run,'continue',None)
        after=await ws._load(key)
        assert after['catalogSearch']['revision']==1 and len(after['catalogSearch']['history'])==1
        assert (await ws._load(key+':run'))['catalogPhase']==1


@async_test
async def test_pause_checkpoint_and_cross_owner_control(setup,monkeypatch):
    app,store,_=setup
    entered=asyncio.Event();release=asyncio.Event()
    async def search(query):
        entered.set();await release.wait();return scope(query)
    monkeypatch.setattr(workflow,'get_catalog_service',lambda:SimpleNamespace(search=search))
    monkeypatch.setattr(workflow,'answer_turn',AsyncMock(return_value=('候选，价格未知。',None)))
    async with client(app) as a,client(app) as b:
        key,run=await prepare_client(a,store,monkeypatch,mode='continuous')
        await asyncio.wait_for(entered.wait(),3)
        await b.get('/api/commerce-demo/workspace')
        # Missing owner CSRF cannot control another visitor's run.
        assert (await b.post('/api/commerce-demo/workspace/control/pause',json=dict(runId=run['id'],revision=run['revision']))).status_code in {403,409}
        run=(await a.get('/api/commerce-demo/workspace')).json()['run']
        paused=await a.post('/api/commerce-demo/workspace/control/pause',json=dict(runId=run['id'],revision=run['revision']))
        assert paused.status_code==200
        release.set();await finish_job(key)
        run=(await a.get('/api/commerce-demo/workspace')).json()['run']
        assert run['status']=='paused' and run['nextStage']=='依据证据回答'
        resume=await a.post('/api/commerce-demo/workspace/control/continue',json=dict(runId=run['id'],revision=run['revision']))
        assert resume.status_code==200
        await finish_job(key)
        assert (await a.get('/api/commerce-demo/workspace')).json()['run']['status']=='completed'
