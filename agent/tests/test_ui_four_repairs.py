import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from app.workspace_archive import WorkspaceArchive
from app.api import commerce_controls as controls, commerce_workspace as ws, catalog_workspace as workflow
from app import catalog_react
from app.control.react_decision import ReactActionProposal, materialize_next_action
from .test_commerce_workspace import setup, client, async_test, state_key
from .test_catalog_workspace import scope, prepare_client, finish_job


def test_stopped_followup_new_budget_precedes_old_history():
    from app.llm import _explicit_phone_price_ceiling
    message = controls.stopped_context_message('改成2000元以内的二手手机',
        '有没有5000以下的？\n二手手机，预算3000元以内')
    assert _explicit_phone_price_ceiling(message) == 200000
    assert message.index('2000') < message.index('5000')


def test_archive_empty_rows_hidden_before_paging_not_deleted(tmp_path):
    archive=WorkspaceArchive(tmp_path/'archive.db')
    archive.record('a',dict(engine='old',messages=[dict(role='user',requestId='one',content='保留的问题')]))
    for i in range(55):
        archive.record('a',dict(engine=str(i),messages=[]))
    rows=archive.history('a')
    assert len(rows['conversations'])==1 and rows['nextOffset'] is None
    assert rows['conversations'][0]['title']=='保留的问题'
    from app.workspace_archive import digest
    assert archive.read('a',digest('0')[:32])['messages']==[]
    assert archive.history('b')['conversations']==[]


def pick(view,index=0):
    option=view.allowed_action_options[index]
    return materialize_next_action(ReactActionProposal(taskId=view.task_id,basedOnRevision=view.task_revision,
        decisionViewHash=view.decision_view_hash,optionId=option.option_id),view)


def react_run():
    return dict(id='r',message='透明收纳盒',catalogPlan=dict(action='search',route='catalog'),
        catalogNext=dict(query='透明收纳盒',retrievalQuery='透明收纳盒',requirements=[dict(facet='商品',mode='require',value='收纳盒')]))


def test_catalog_options_observe_evidence_and_never_authorize_writes():
    run=react_run()
    first,queries=catalog_react.decision_view(run)
    assert queries=={'catalog_search_1':'透明收纳盒'}
    assert not any(o.kind=='ANSWER' for o in first.allowed_action_options)
    run['catalogQueries']=['透明收纳盒'];run['catalogNext']['scope']=scope()
    second,queries=catalog_react.decision_view(run)
    assert queries=={'catalog_search_2':'收纳盒'}
    assert {'ANSWER','CALL_TOOL','ASK_CLARIFICATION'}==set(second.allowed_actions)
    assert all(t.name=='catalog_search' for t in second.allowed_tools)
    with pytest.raises(ValueError):
        materialize_next_action(ReactActionProposal(taskId='r',basedOnRevision=1,
            decisionViewHash=second.decision_view_hash,optionId='tool.create_order'),second)
    run['catalogModelDecisions']=2
    limited,_=catalog_react.decision_view(run)
    assert [o.kind for o in limited.allowed_action_options]==['ANSWER']


@async_test
async def test_composer_clarification_persists_reply_without_new_run(setup,monkeypatch):
    app,store,login=setup
    launches=[]
    monkeypatch.setattr(controls,'launch',lambda *args:launches.append(args))
    async with client(app) as c:
        login(c,'a');await c.get('/api/commerce-demo/workspace')
        key=state_key(store,':user:'+ws.auth._digest('a'))
        async with ws._lock(key):
            await controls.save_run(key,dict(id='r',revision=1,status='clarification',mode='continuous',
                requestId='original',clarification={'server':'owned'},nodes=[]))
        body=dict(runId='r',revision=1,answer='手机，6000以内',requestId='reply')
        result=await c.post('/api/commerce-demo/workspace/control/continue',json=body)
        assert result.status_code==200
        assert launches[0][3]=='手机，6000以内'
        assert (await ws._load(key))['messages'][-1]['requestId']=='reply'
        assert (await c.post('/api/commerce-demo/workspace/control/continue',json=body)).status_code==409
        assert len(launches)==1


@async_test
async def test_catalog_decision_checkpoint_survives_pause_without_second_model_call(setup,monkeypatch):
    app,store,_=setup
    async with client(app) as c:
        key,_=await prepare_client(c,store,monkeypatch)
        run=await ws._load(key+':run')
        await workflow.work(key,run,'step',None)  # fixed preparation only
        run=await ws._load(key+':run')
        run.update(catalogReact=True,status='running',pauseRequested=True)
        view,queries=catalog_react.decision_view(run)
        action=pick(view)
        decision=dict(action=action.model_dump(by_alias=True,mode='json'),viewHash=view.decision_view_hash,
            optionId=view.allowed_action_options[0].option_id,source='model',receipt={},
            query=next(iter(queries.values())),durationMs=1)
        choose=AsyncMock(return_value=decision)
        monkeypatch.setattr(catalog_react,'decide',choose)
        async with ws._lock(key): await controls.save_run(key,run)
        assert not await workflow.select_catalog_action(key,run)
        saved=await ws._load(key+':run')
        assert saved['status']=='paused' and saved['catalogModelDecisions']==1
        saved.update(status='running',pauseRequested=False)
        async with ws._lock(key): await controls.save_run(key,saved)
        assert await workflow.select_catalog_action(key,saved)
        choose.assert_awaited_once()
        assert saved['catalogModelDecisions']==1
        bad=deepcopy(saved);bad['catalogNext']['query']='被替换需求'
        with pytest.raises(ValueError,match='scope_changed'):
            await workflow.select_catalog_action(key,bad)


@async_test
async def test_catalog_react_changes_next_action_from_observation_then_respects_budget(setup,monkeypatch):
    app,store,_=setup
    chosen=[]
    async def choose(run):
        view,queries=catalog_react.decision_view(run)
        # Two legal but different decisions: initial retrieval, then a second
        # query after observing returned evidence. Last answer is budget-bound.
        index=next((i for i,o in enumerate(view.allowed_action_options) if o.kind=='CALL_TOOL'),0)
        action=pick(view,index);source='model' if len(view.allowed_action_options)>1 else 'server_boundary'
        chosen.append((source,action.kind,view.last_outcome))
        return dict(action=action.model_dump(by_alias=True,mode='json'),viewHash=view.decision_view_hash,
            optionId=view.allowed_action_options[index].option_id,source=source,receipt={},
            query=queries.get((action.argument_refs or {}).get('query')),durationMs=1)
    searches=[]
    async def search(query,**kwargs):
        searches.append((query,kwargs));return scope(query)
    monkeypatch.setattr(catalog_react,'decide',choose)
    monkeypatch.setattr(workflow,'get_catalog_service',lambda:SimpleNamespace(search=search))
    monkeypatch.setattr(workflow,'answer_turn',AsyncMock(return_value=('依据已核验记录回答',None)))
    async with client(app) as c:
        key,_=await prepare_client(c,store,monkeypatch)
        run=await ws._load(key+':run')
        run['catalogPlan'].update(query='透明收纳盒',retrievalQuery='透明收纳盒',requirements=[dict(facet='商品',mode='require',value='收纳盒',terms=['收纳盒'])])
        run.update(catalogReact=True,mode='continuous',status='running')
        async with ws._lock(key): await controls.save_run(key,run)
        await workflow.work(key,run,'start',None)
        saved=await ws._load(key+':run')
        assert saved['status']=='completed',saved.get('catalogError')
        assert [s[1]['retrieval_query'] for s in searches]==['透明收纳盒','收纳盒']
        assert searches[0][1]['requirements']==searches[1][1]['requirements']
        assert [row[:2] for row in chosen]==[('model','CALL_TOOL'),('model','CALL_TOOL'),('server_boundary','ANSWER')]
        assert chosen[1][2]['titles'] and saved['catalogModelDecisions']==2
        assert len([m for m in (await ws._load(key))['messages'] if m['role']=='assistant'])==1


def test_product_followup_reuses_decider_contract_without_fabricating_catalog_scope():
    run=dict(id='r',message='它是否带充电器？',catalogPlan=dict(route='product',action='inspect',
        productContext=dict(productId='123',number=1,title='测试手机'),followup='included'))
    view,_=catalog_react.decision_view(run)
    assert [o.tool_name for o in view.allowed_action_options]==['read_product_facts']
    run['productFacts']={'description':'包装内含充电器'}
    from app.catalog_service import fingerprint
    run['catalogEvidenceSha256']=fingerprint(run['productFacts'])
    view,_=catalog_react.decision_view(run)
    assert set(view.allowed_actions)=={'ANSWER','ASK_CLARIFICATION'}
    assert not view.allowed_tools and view.candidate_scope is None


@async_test
async def test_old_worker_cannot_publish_into_new_run(setup):
    from fastapi import HTTPException
    app,store,login=setup
    async with client(app) as c:
        login(c,'a');await c.get('/api/commerce-demo/workspace')
        key=state_key(store,':user:'+ws.auth._digest('a'))
        async with ws._lock(key):
            await controls.save_run(key,dict(id='new',status='running',requestId='new',revision=1))
            with pytest.raises(HTTPException) as error:
                await controls.publish(key,dict(id='old',engine='old'),{})
            assert error.value.status_code==409
        assert not (await ws._load(key))['messages']


@async_test
async def test_catalog_clarification_reply_replans_under_same_owner_and_keeps_transcript(setup,monkeypatch):
    from app import catalog_conversation, catalog_commerce
    app,store,_=setup
    async def choose(run):
        view,queries=catalog_react.decision_view(run)
        # First turn asks clarification; the reply searches and then answers.
        kind='ASK_CLARIFICATION' if run['requestId']=='fixture-catalog-0001' else 'ANSWER' if run['catalogNext'].get('scope') else 'CALL_TOOL'
        index=next(i for i,o in enumerate(view.allowed_action_options) if o.kind==kind)
        action=pick(view,index)
        return dict(action=action.model_dump(by_alias=True,mode='json'),viewHash=view.decision_view_hash,
            optionId=view.allowed_action_options[index].option_id,source='model',receipt={},
            query=queries.get((action.argument_refs or {}).get('query')),durationMs=1)
    monkeypatch.setattr(catalog_react,'decide',choose)
    monkeypatch.setattr(workflow,'get_catalog_service',lambda:SimpleNamespace(search=AsyncMock(return_value=scope())))
    monkeypatch.setattr(workflow,'answer_turn',AsyncMock(return_value=('这里是重新核验的收纳盒候选',None)))
    monkeypatch.setattr(catalog_commerce,'resolve_cards',AsyncMock(return_value=[]))
    async with client(app) as c:
        key,_=await prepare_client(c,store,monkeypatch)
        monkeypatch.setattr(ws.auth.settings,'agent_control_runtime','react_v1')
        monkeypatch.setattr(ws.auth.settings,'agent_react_live_enabled',True)
        run=await ws._load(key+':run');run.update(catalogReact=True,mode='continuous',status='running')
        async with ws._lock(key):await controls.save_run(key,run)
        await workflow.work(key,run,'start',None)
        saved=await ws._load(key+':run')
        assert saved['status']=='clarification'
        result=await c.post('/api/commerce-demo/workspace/control/continue',json=dict(runId=run['id'],revision=saved['revision'],
            requestId='clarification-reply-0001',answer='想要透明收纳盒'))
        assert result.status_code==200,result.text
        await finish_job(key)
        saved=await ws._load(key+':run')
        assert saved['status']=='completed',saved.get('catalogError')
        messages=(await ws._load(key))['messages']
        assert [m['role'] for m in messages]==['user','assistant','user','assistant']
        assert messages[2]['content']=='想要透明收纳盒'
        assert saved['requestId']=='clarification-reply-0001'
