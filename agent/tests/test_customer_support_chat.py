import json

from fastapi import HTTPException
import pytest

from app.api import commerce_support as api
from app.customer_support import planner
from app.customer_support.knowledge import retrieve_policy
from app.customer_support.runtime import execute_plan
from .test_commerce_workspace import setup, client, async_test

ORDER = {"id": "owned-order", "status": "COMPLETED", "payableMinor": 303, "currency": "CNY", "version": 2,
         "items": [{"itemType": "PRODUCT", "itemId": 123, "titleSnapshot": "原商品", "quantity": 3}]}
ROOT = "/api/commerce-demo/workspace/support/orders/owned-order/conversation"


@async_test
async def test_stock_policy_for_existing_case_includes_live_progress_without_conversion():
    retrieval=retrieve_policy('换货预占超时释放结果未知')
    ids=[c['id'] for c in retrieval['citations'] if c['id'].endswith(':replacement-expiry')]
    assert ids
    async def java(method,path,**kw):
        assert method=='GET' and path=='/api/after-sales/orders/owned-order'
        return [{'id':'c','orderId':'owned-order','type':'EXCHANGE','phase':'WAITING_STOCK',
                 'itemId':1,'quantity':2,'amountMinor':398,'currency':'CNY','version':3}]
    result=await execute_plan(planner.SupportPlan(intent='policy',subject='current_order',policy_ids=ids),
        order={**ORDER,'_supportCases':[{'id':'c'}]},retrieval=retrieval,java=java,run_id='r',tool_receipts=[])
    assert '本单实际进度' in result['answer'] and '不允许直接转退款' in result['answer']
    assert any(c['kind']=='after_sale' and c['fields']['cases'][0]['quantity']==2 for c in result['citations'])
    assert not result.get('preview') and not result.get('actionDraft')


@async_test
async def test_missing_payment_is_not_failure_or_success_and_unrelated_errors_propagate():
    plan=planner.SupportPlan(intent='payment',subject='current_order')
    for status,detail in ((404,'订单对应的支付单不存在'),(403,'forbidden'),(404,'订单不存在')):
        async def java(method,path,**kw):raise HTTPException(status,detail)
        receipts=[]
        if detail=='订单对应的支付单不存在':
            result=await execute_plan(plan,order=ORDER,retrieval={},java=java,run_id='r',tool_receipts=receipts)
            assert '不表示支付失败' in result['answer']
            assert result['citations'][0]['fields']=={'orderId':ORDER['id'],'recordExists':False,'sourceStatusCode':404}
            assert receipts[0]['statusCode']==404
        else:
            with pytest.raises(HTTPException):
                await execute_plan(plan,order=ORDER,retrieval={},java=java,run_id='r',tool_receipts=receipts)


@async_test
async def test_fact_renderer_does_not_promote_pending_refund_or_shipment_to_success():
    async def java(method, path, **kw):
        if path.endswith('/fulfillment'): return {"orderId": "owned-order", "status": "SHIPPED", "trackingNo": "SIM-1"}
        return [{"id": "case-1", "orderId": "owned-order", "phase": "REFUND_PENDING", "type": "RETURN_REFUND", "quantity": 1, "amountMinor": 101, "currency": "CNY"}]
    for intent, required in [("logistics", "尚未签收"), ("after_sale", "尚未确认到账")]:
        receipts = []
        result = await execute_plan(planner.SupportPlan(intent=intent, subject="current_order"), order=ORDER, retrieval={}, java=java, run_id="r-1", tool_receipts=receipts)
        assert required in result["answer"]
        assert receipts[0]["status"] == "SUCCEEDED" and result["citations"][0]["contentSha256"]


@async_test
async def test_preview_dispatch_cannot_confirm_and_rejects_wrong_authority_identity():
    calls = []
    async def java(method, path, **kw):
        calls.append((method, path, kw))
        return {"orderId": "other-order", "itemId": 123, "quantity": 1, "type": "REFUND_ONLY"}
    plan = planner.SupportPlan(intent="preview", subject="current_order", item_number=1, quantity=1, after_sale_type="REFUND_ONLY", reason_quote="商品损坏")
    with pytest.raises(ValueError, match="identity"):
        await execute_plan(plan, order=ORDER, retrieval={}, java=java, run_id="r-1", tool_receipts=[])
    assert [(method, path) for method, path, _ in calls] == [("POST", "/api/after-sales/preview")]


@async_test
async def test_replacement_query_never_reuses_original_order_tracking():
    async def java(method, path, **kwargs):
        assert path == '/api/after-sales/orders/owned-order'
        return [{'id': 'case-2', 'orderId': 'owned-order', 'phase': 'REPLACEMENT_READY', 'type': 'EXCHANGE', 'quantity': 1}]
    result = await execute_plan(planner.SupportPlan(intent='logistics', subject='current_order', logistics_scope='replacement'),
                                order=ORDER, retrieval={}, java=java, run_id='r-replacement', tool_receipts=[])
    assert '尚未确认出库' in result['answer'] and '已签收' not in result['answer'] and '已出库' not in result['answer']


@async_test
async def test_owned_conversation_replay_does_not_repeat_model_or_preview(setup, monkeypatch):
    app, store, login = setup; app.include_router(api.router)
    monkeypatch.setattr(api.auth.settings, 'customer_support_agent_enabled', True)
    model_calls, writes = [], []
    async def java(method, path, **kwargs):
        assert kwargs['access_token'] == 'private-alice'
        if method == 'GET': return ORDER
        writes.append(path)
        return {"previewId": "preview-1", "orderId": "owned-order", "itemId": 123, "quantity": 1, "type": "RETURN_REFUND", "amountMinor": 101, "currency": "CNY", "expiresAt": "2099-01-01T00:00:00Z"}
    async def plan(*args, **kwargs):
        model_calls.append(kwargs['run_id'])
        return planner.SupportPlan(intent="preview", subject="current_order", item_number=1, quantity=1, after_sale_type="RETURN_REFUND", reason_quote="商品损坏"), {"usage": {"prompt_tokens": 10}, "status": "SUCCEEDED"}
    monkeypatch.setattr(api.auth, '_java', java); monkeypatch.setattr(planner, 'plan_turn', plan)
    async with client(app) as c:
        login(c, 'alice'); await c.get('/api/commerce-demo/workspace')
        body = {"message": "商品损坏，退货退款一件", "requestId": "support-request-0001"}
        first = await c.post(ROOT, json=body)
        assert first.status_code == 200 and first.json()['turns'][0]['status'] == 'COMPLETED'
        assert first.json()['turns'][0]['result']['preview']['amountMinor'] == 101
        assert 'private-alice' not in first.text and 'modelReceipt' not in first.text
        replay = await c.post(ROOT, json=body)
        assert replay.json() == first.json()
        assert len(model_calls) == 1 and writes == ['/api/after-sales/preview']
        assert (await c.post(ROOT, json={**body, 'message': '改成三件'})).status_code == 409
        assert (await c.get(ROOT)).json() == first.json()
        saved = next(json.loads(value) for key, value in store.values.items() if key.endswith(':support:owned-order'))
        assert saved['turns'][0]['attempts'][0]['firstContentMs'] >= 0


@async_test
async def test_missing_authority_never_exposes_another_users_saved_conversation(setup, monkeypatch):
    app, _, login = setup; app.include_router(api.router)
    monkeypatch.setattr(api.auth.settings, 'customer_support_agent_enabled', True)
    async def java(*args, **kwargs): raise HTTPException(404, '订单不存在')
    monkeypatch.setattr(api.auth, '_java', java)
    async with client(app) as c:
        login(c, 'alice'); await c.get('/api/commerce-demo/workspace')
        assert (await c.get(ROOT)).status_code == 404
        assert (await c.post(ROOT, json={'message': '查别人订单', 'requestId': 'support-request-0001'})).status_code == 404


@async_test
async def test_failed_model_usage_is_retained_when_same_request_retries(setup, monkeypatch):
    app, store, login = setup; app.include_router(api.router)
    monkeypatch.setattr(api.auth.settings, 'customer_support_agent_enabled', True)
    calls = []
    async def java(*args, **kwargs): return ORDER
    async def plan(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1: raise planner.PlanningFailure({'status': 'FAILED', 'usage': {'prompt_tokens': 50}, 'cost': None})
        return planner.SupportPlan(intent='order', subject='current_order'), {'status': 'SUCCEEDED', 'usage': None, 'cost': None}
    monkeypatch.setattr(api.auth, '_java', java); monkeypatch.setattr(planner, 'plan_turn', plan)
    async with client(app) as c:
        login(c, 'alice'); await c.get('/api/commerce-demo/workspace')
        body = {'message': '订单进度', 'requestId': 'support-request-0002'}
        assert (await c.post(ROOT, json=body)).json()['turns'][0]['status'] == 'FAILED'
        assert (await c.post(ROOT, json=body)).json()['turns'][0]['status'] == 'COMPLETED'
        saved = next(json.loads(value) for key, value in store.values.items() if key.endswith(':support:owned-order'))
        attempts = saved['turns'][0]['attempts']
        assert len(attempts) == 2 and attempts[0]['modelReceipt']['usage']['prompt_tokens'] == 50
        assert attempts[1]['modelReceipt']['usage'] is None
