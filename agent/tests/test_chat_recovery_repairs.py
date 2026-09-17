import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from .test_commerce_workspace import setup, client, async_test, state_key
from .test_brand_negation import _phone_state
from app.api import commerce_controls as controls, commerce_workspace as ws
from app.domains.ecommerce import ShoppingGuideState, ShoppingRequirement
from app.llm import (_build_validated_task_state_payload, _deterministic_used_phone_task_state_decision,
                     TaskStatePayloadValidationError)


def requirement(key, value):
    return ShoppingRequirement(key=key, operator='lte' if key=='price_minor' else 'eq', value=value,
        unit='CNY_MINOR' if key=='price_minor' else 'enum' if key=='os' else 'text', priority='hard', source='user')


@async_test
async def test_preview_read_retry_is_bounded_and_does_not_retry_rejections_or_cancellation(monkeypatch):
    from app.domains.ecommerce import transactions as tx
    context = object()
    payload = {'itemType': 'PRODUCT', 'itemId': 123, 'quantity': 1}
    result = {'itemId': 123}
    for failure in (httpx.ReadTimeout('test'), tx.BackendTransactionError(503, 'unavailable')):
        request = AsyncMock(side_effect=[failure, result])
        monkeypatch.setattr(tx, '_request_backend', request)
        assert await tx._read_order_preview(context, payload) == result
        assert request.await_count == 2
        for call in request.await_args_list:
            assert call.args == (context, 'POST', '/api/orders/preview')
            assert call.kwargs == {'json_body': payload}
    for failure in (tx.BackendTransactionError(409, 'stock changed'), ValueError('invalid JSON'), asyncio.CancelledError()):
        request = AsyncMock(side_effect=failure)
        monkeypatch.setattr(tx, '_request_backend', request)
        with pytest.raises(type(failure)):
            await tx._read_order_preview(context, payload)
        assert request.await_count == 1
    request = AsyncMock(side_effect=httpx.ReadTimeout('test'))
    monkeypatch.setattr(tx, '_request_backend', request)
    with pytest.raises(httpx.ReadTimeout):
        await tx._read_order_preview(context, payload)
    assert request.await_count == 2


@async_test
async def test_retried_preview_saves_one_confirmation_only_after_validated_success(monkeypatch):
    from app.domains.ecommerce import transactions as tx
    context = SimpleNamespace(browser_confirmation=False)
    monkeypatch.setattr(tx, '_require_context', lambda tool: context)
    preview = {'itemType': 'PRODUCT', 'itemId': 123, 'quantity': 1,
               'unitPriceMinor': 100, 'payableMinor': 100}
    request = AsyncMock(side_effect=[httpx.ReadTimeout('test'), preview])
    save = AsyncMock(return_value=object())
    monkeypatch.setattr(tx, '_request_backend', request)
    monkeypatch.setattr(tx, '_save_proposal', save)
    monkeypatch.setattr(tx, '_confirmation_detail', lambda proposal, phrase: {'status': 'confirmation_required'})
    trace = await tx.preview_order_tool(123, 1)
    assert trace.ok and request.await_count == 2 and save.await_count == 1
    assert save.await_args.kwargs['preview'] == preview
    save.reset_mock()
    request.side_effect = httpx.ReadTimeout('still unavailable')
    assert not (await tx.preview_order_tool(123, 1)).ok
    save.assert_not_awaited()


@pytest.mark.parametrize('brand,os,message,expected', [
    ('apple','ios','安卓手机有吗？',{'os':'android','price_minor':600000}),
    ('samsung','android','苹果手机有吗？',{'brand':'apple','os':'ios','price_minor':600000}),
    ('apple','ios','三星手机有吗？',{'brand':'samsung','price_minor':600000}),
    ('samsung','android','安卓手机有吗？',{'brand':'samsung','os':'android','price_minor':600000}),
])
def test_platform_switch_releases_only_incompatible_inherited_requirement(brand,os,message,expected):
    state=_phone_state(ShoppingGuideState(mode='recommend',category='phone',requirements=[
        requirement('brand',brand),requirement('os',os),requirement('price_minor',600000)]))
    args,_=_deterministic_used_phone_task_state_decision(state,message,None)
    assert args is not None
    payload,_=_build_validated_task_state_payload(state,args,message=message,require_status=True)
    actual={r['key']:r['value'] for r in payload['domainStatePatch']['shoppingGuide']['requirements']}
    assert actual==expected
    assert state.domain_state['shoppingGuide']['requirements'][0]['value']==brand


def test_explicit_impossible_same_turn_requirements_still_rejected():
    state=_phone_state(ShoppingGuideState(mode='recommend',category='phone',requirements=[requirement('brand','apple'),requirement('os','ios')]))
    args={'status':'ready','goal':'苹果手机必须安卓系统','pendingQuestions':[],
          'domainStatePatch':{'shoppingGuide':{'mode':'recommend','category':'phone','requirements':[
              requirement('brand','apple').model_dump(),requirement('os','android').model_dump()]}}}
    with pytest.raises(TaskStatePayloadValidationError):
        _build_validated_task_state_payload(state,args,message='苹果手机必须安卓系统',require_status=True)


@async_test
async def test_no_checkpoint_disables_resume_and_rejects_without_launch(setup,monkeypatch):
    app,store,login=setup
    launch=AsyncMock();monkeypatch.setattr(controls,'launch',launch)
    async with client(app) as c:
        login(c,'a');await c.get('/api/commerce-demo/workspace')
        key=state_key(store,':user:'+ws.auth._digest('a'))
        async with ws._lock(key):
            await controls.save_run(key,dict(id='r',revision=1,status='failed',mode='continuous',
                requestId='original',engine='engine',nodes=[],recoveryBlocked=True))
        view=(await c.get('/api/commerce-demo/workspace/control')).json()
        assert view['run']['canResume'] is False
        r=await c.post('/api/commerce-demo/workspace/control/continue',json=dict(runId='r',revision=1))
        assert r.status_code==409
        launch.assert_not_called()
        assert (await ws._load(key+':run'))['status']=='failed'
        # Ending a failed conversation does not require a missing checkpoint.
        assert (await c.post('/api/commerce-demo/workspace/control/end',json=dict(runId='r',revision=1))).status_code==200
        fresh=await c.post('/api/commerce-demo/workspace/conversations',json=dict(expectedConversationId=view['conversationId']))
        assert fresh.status_code==200 and fresh.json()['conversationId']!=view['conversationId']


@async_test
async def test_only_unfinished_current_cursor_is_resumable(monkeypatch):
    from app import task_state, graph
    task=SimpleNamespace(task_id='t',domain_state={'v2RunMarker':{'runId':'new'},'v2FinalAnswerReceipt':{'runId':'old'}})
    monkeypatch.setattr(task_state,'get_session_task_state',AsyncMock(return_value=task))
    cursor=AsyncMock(return_value={'revision':2});monkeypatch.setattr(graph,'read_task_cursor',cursor)
    run=dict(status='interrupted',mode='continuous',engine='e',initialCursor={'revision':1})
    assert await controls.resumable(run)
    cursor.return_value={'revision':1}
    assert not await controls.resumable(run)
    cursor.return_value={'revision':2};task.domain_state['v2FinalAnswerReceipt']['runId']='new'
    assert not await controls.resumable(run)
    assert await controls.resumable({**run,'presentationData':{'answer':'already validated'}})


@async_test
async def test_product_failure_is_structured_and_does_not_leak_internal_url(monkeypatch):
    from app.domains.ecommerce import tools
    response=httpx.Response(503,request=httpx.Request('POST','http://private-service/api/products/resolve'))
    monkeypatch.setattr(tools,'_product_http_client',lambda:SimpleNamespace(post=AsyncMock(return_value=response)))
    trace=await tools.get_product_details_tool([1])
    assert trace.ok is False
    assert trace.detail=={'code':'product_facts_http_error','httpStatus':503,'retryable':True,'attemptCount':2}
    from app.execution_view import safe_value,source_reference
    assert safe_value({'failureCode':'task_state_update_failed','password':'secret'})=={'failureCode':'task_state_update_failed'}
    assert source_reference('pre_harness')['snippet']
    assert source_reference('react_decision')['snippet']


@async_test
async def test_facts_retry_only_transient_reads_and_stop_after_two_attempts(monkeypatch):
    from app.domains.ecommerce import tools
    req=httpx.Request('POST','http://test/api/products/resolve')
    success=httpx.Response(200,json={'success':True,'data':[{'id':1}]},request=req)
    for first in [httpx.Response(503,request=req), httpx.ReadTimeout('temporary')]:
        post=AsyncMock(side_effect=[first,success])
        monkeypatch.setattr(tools,'_product_http_client',lambda:SimpleNamespace(post=post))
        trace=await tools.get_product_details_tool([1])
        assert trace.ok and trace.detail['attemptCount']==2 and post.await_count==2
        assert all(call.kwargs['json']=={'productIds':[1]} for call in post.await_args_list)
    for bad in [httpx.Response(403,request=req), httpx.Response(200,json={'data':None},request=req)]:
        post=AsyncMock(return_value=bad)
        monkeypatch.setattr(tools,'_product_http_client',lambda:SimpleNamespace(post=post))
        trace=await tools.get_product_details_tool([1])
        assert not trace.ok and trace.detail['attemptCount']==1 and post.await_count==1
    post=AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(tools,'_product_http_client',lambda:SimpleNamespace(post=post))
    with pytest.raises(asyncio.CancelledError):
        await tools.get_product_details_tool([1])
    assert post.await_count==1


@async_test
async def test_history_activation_keeps_transcript_but_rechecks_shopping_authority(setup,monkeypatch):
    from app.workspace_archive import get_archive
    app,store,login=setup
    async with client(app) as c, client(app) as other:
        login(c,'owner');login(other,'intruder')
        await c.get('/api/commerce-demo/workspace');await other.get('/api/commerce-demo/workspace')
        key=state_key(store,':user:'+ws.auth._digest('owner'))
        old=ws._fresh()
        old['messages']=[dict(role='user',requestId='old-q',content='苹果手机有吗？'),
            dict(role='assistant',requestId='old-a',content='有候选',cards=[dict(id=1,title='old phone',brand='Apple',priceMinor=100,currency='CNY')])]
        get_archive().record(key,old)
        current=(await c.get('/api/commerce-demo/workspace')).json()
        denied=await other.post('/api/commerce-demo/workspace/conversations/'+old['conversationId']+'/activate',
            json=dict(expectedConversationId=(await other.get('/api/commerce-demo/workspace')).json()['conversationId']))
        assert denied.status_code==404
        response=await c.post('/api/commerce-demo/workspace/conversations/'+old['conversationId']+'/activate',
            json=dict(expectedConversationId=current['conversationId']))
        assert response.status_code==200
        active=await ws._load(key)
        assert active['conversationId']==old['conversationId']
        assert active['engine']!=old['engine']
        assert active['messages']==old['messages']
        assert active['checkout'] is None and active['reference'] is None and active['selection'] is None
        assert (await c.post('/api/commerce-demo/workspace/preview',json={'productId':1})).status_code==409
        current_card=dict(id=1,title='current phone',brand='Apple',priceMinor=200,currency='CNY',available=2,purchasable=True)
        read=AsyncMock(return_value=current_card);monkeypatch.setattr(ws,'_card',read)
        selected=await c.post('/api/commerce-demo/workspace/selection',json={'productId':1})
        assert selected.status_code==200 and selected.json()['selection']['product']['priceMinor']==200
        read.assert_awaited_once_with(1)
        assert (await ws._load(key))['selectionId']
        # New turns append to the same history, rather than creating a new archive.
        async with ws._lock(key):
            active=await ws._load(key)
            active['messages'].append(dict(role='user',requestId='new-q',content='续航如何？'))
            await ws._save(key,active)
        assert get_archive().read(key,old['conversationId'])['messages'][-1]['content']=='续航如何？'
        async with ws._lock(key):
            active['checkout']={'pending':True};await ws._save(key,active)
        blocked=await c.post('/api/commerce-demo/workspace/conversations/'+current['conversationId']+'/activate',json=dict(expectedConversationId=old['conversationId']))
        assert blocked.status_code==409
