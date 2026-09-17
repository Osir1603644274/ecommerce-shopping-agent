import asyncio
from functools import wraps
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from app.api import commerce_demo as auth, commerce_workspace as ws
from app.schemas import ToolTrace


def async_test(fn):
    @wraps(fn)
    def run(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return run


class Redis:
    def __init__(self): self.values = {}
    async def get(self, key): return self.values.get(key)
    async def set(self, key, value, **kw):
        if kw.get("nx") and key in self.values: return False
        self.values[key] = value
        return True
    async def delete(self, key): return self.values.pop(key, None) is not None
    async def expire(self, *args): return True
    async def eval(self, script, n, *args):
        if script == ws._SAVE_OWNED:
            lock_key, key, token, value, ttl = args
            if self.values.get(lock_key) != token: return 0
            await self.set(key, value, ex=ttl)
            return 1
        key, token = args
        assert script == ws._UNLOCK
        if self.values.get(key) == token: return await self.delete(key)
        return 0


@pytest.fixture
def setup(monkeypatch, tmp_path):
    # Legacy fixed-workflow fixtures are not live model tests. ReAct adapter
    # tests explicitly enable the runtime and stub its model boundary.
    monkeypatch.setattr(auth.settings, 'agent_control_runtime', 'fixed_v1')
    # Protocol fixtures must not contact a real provider. Incremental provider
    # behavior is tested independently in test_workspace_answer_stream.py.
    from app.api import workspace_answer_stream
    async def presentation(key, run, material): return material['answer']
    monkeypatch.setattr(workspace_answer_stream, 'compose', presentation)
    monkeypatch.setattr(auth.settings, 'web_query_intake_path', str(tmp_path / 'queries.sqlite3'))
    store = Redis()
    monkeypatch.setattr(auth, "_client", store)
    monkeypatch.setattr(auth.settings, "commerce_demo_enabled", True)
    app = FastAPI(); app.include_router(ws.router)
    def login(client, user):
        token = "session-" + user
        store.values[auth._session_key(token)] = json.dumps({
            "accessToken": "private-" + user, "refreshToken": "refresh-" + user,
            "username": user, "csrfDigest": auth._digest("csrf-" + user),
            "sessionBinding": auth._digest(token), "accessExpiresAtEpoch": 4102444800,
        })
        client.cookies.set(auth.COOKIE_NAME, token)
        client.headers[auth.CSRF_HEADER] = "csrf-" + user
    return app, store, login


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"Sec-Fetch-Site": "same-origin"})


def state_key(store, suffix):
    return next(k for k in store.values if k.endswith(suffix))


@async_test
async def test_catalog_epoch_archives_without_deleting_old_workspace(setup, monkeypatch):
    app, store, login = setup
    async with client(app) as c:
        login(c, 'alice')
        await c.get('/api/commerce-demo/workspace')
        key = state_key(store, ':user:' + auth._digest('alice'))
        old = json.loads(store.values[key]); old['messages'] = [{'role':'user','content':'old catalog','requestId':'old'}]
        store.values[key] = json.dumps(old)
        monkeypatch.setattr(auth.settings, 'commerce_workspace_epoch', 'new-catalog')
        result = await c.get('/api/commerce-demo/workspace')
        assert result.status_code == 200 and result.json()['messages'] == []
        assert json.loads(store.values[key]) == old


@async_test
async def test_refund_preview_is_bound_to_session_and_requires_next_confirmation(setup, monkeypatch):
    app, store, login = setup
    async def dispatch(order_id, items, reason):
        from app.transaction_agent.runtime import _REQUEST
        context = _REQUEST.get()
        assert context.authorization == 'Bearer private-alice' and context.browser_confirmation
        assert (order_id, items, reason) == ('owned-order', [{'itemId':123,'quantity':1}], 'not needed')
        return ToolTrace(tool='preview_refund',ok=True,detail={'confirmationId':'cfm-abcdefghijkl','action':'create_refund'})
    monkeypatch.setattr(ws, 'dispatch_refund_preview', dispatch)
    async with client(app) as c:
        login(c, 'alice'); await c.get('/api/commerce-demo/workspace')
        r = await c.post('/api/commerce-demo/workspace/refund-preview', json={
            'orderId':'owned-order','items':[{'itemId':123,'quantity':1}],'reason':'not needed'})
        assert r.status_code == 200 and not r.json()['checkout']['pending']
        assert r.json()['checkout']['outcome'] is None
        assert 'private-alice' not in r.text


@async_test
async def test_guest_bootstrap_opaque_cookie_and_no_engine_leak(setup):
    app, store, _ = setup
    async with client(app) as c:
        r = await c.get("/api/commerce-demo/workspace")
        assert r.status_code == 200 and r.json()["csrfToken"]
        assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=lax" in r.headers["set-cookie"]
        assert "engine" not in r.text and "web-" not in r.text
        assert r.headers["cache-control"] == "no-store"
        assert len(store.values) == 1


@async_test
async def test_cross_origin_denied_even_bootstrap(setup):
    app, _, _ = setup
    async with client(app) as c:
        r = await c.get("/api/commerce-demo/workspace", headers={"Origin": "http://attacker"})
        assert r.status_code == 403


@async_test
async def test_guest_login_adopts_once_and_other_account_never_sees_history(setup):
    app, store, login = setup
    async with client(app) as c:
        await c.get("/api/commerce-demo/workspace")
        key = state_key(store, ":guest"); state = json.loads(store.values[key])
        state["messages"] = [{"role": "user", "content": "private wish", "requestId": "1"}]
        store.values[key] = json.dumps(state)
        login(c, "alice")
        assert "private wish" in (await c.get("/api/commerce-demo/workspace")).text
        login(c, "bob")
        assert "private wish" not in (await c.get("/api/commerce-demo/workspace")).text
        c.cookies.delete(auth.COOKIE_NAME); c.headers.pop(auth.CSRF_HEADER)
        assert "private wish" not in (await c.get("/api/commerce-demo/workspace")).text


@async_test
async def test_guest_write_requires_csrf_and_cannot_favorite(setup):
    app, _, _ = setup
    async with client(app) as c:
        r = await c.get("/api/commerce-demo/workspace")
        assert (await c.post("/api/commerce-demo/workspace/selection", json={"productId": 1})).status_code == 403
        c.headers[auth.CSRF_HEADER] = r.json()["csrfToken"]
        assert (await c.put("/api/commerce-demo/workspace/favorites/1")).status_code == 401
        assert (await c.post("/api/commerce-demo/workspace/preview", json={"productId":1})).status_code == 401


@async_test
async def test_cookie_switched_under_old_tab_fails_csrf(setup):
    app, _, login = setup
    async with client(app) as c:
        login(c, "alice"); await c.get("/api/commerce-demo/workspace")
        login(c, "bob"); c.headers[auth.CSRF_HEADER] = "csrf-alice"
        assert (await c.get("/api/commerce-demo/workspace")).status_code == 403


@async_test
async def test_arbitrary_selection_and_owner_fields_rejected(setup):
    app, _, _ = setup
    async with client(app) as c:
        r = await c.get("/api/commerce-demo/workspace"); c.headers[auth.CSRF_HEADER] = r.json()["csrfToken"]
        assert (await c.post("/api/commerce-demo/workspace/selection", json={"productId":123})).status_code == 409
        assert (await c.post("/api/commerce-demo/workspace/chat", json={"message":"hi","requestId":"abcdefghijklmnop","sessionId":"victim"})).status_code == 422


@async_test
async def test_favorite_uses_server_token_not_body_identity(setup, monkeypatch):
    app, _, login = setup
    async def java(method, path, **kw):
        assert method == "PUT" and path == "/api/product-favorites/123"
        assert kw == {"access_token":"private-alice"}
        return {"saved":True}
    monkeypatch.setattr(auth, "_java", java)
    async with client(app) as c:
        login(c, "alice"); await c.get("/api/commerce-demo/workspace")
        assert (await c.put("/api/commerce-demo/workspace/favorites/123", headers={"Authorization":"Bearer victim"})).json() == {"saved":True}


@pytest.mark.parametrize("category,price,stock,expected", [("二手手机",100,2,True),("耳机",100,2,False),("二手手机",None,2,False),("二手手机",100,0,False)])
@async_test
async def test_card_authority_gate(monkeypatch, category, price, stock, expected):
    async def java(*a, **kw):
        return {"title":"phone", "priceStatus":"verified", "categoryL3":category,
                "snapshotPriceMinor":price,"availableQuantity":stock}
    monkeypatch.setattr(auth, "_java", java)
    assert (await ws._card(123))["purchasable"] is expected


@async_test
async def test_preview_receipt_is_durable_and_confirmation_id_must_match(setup, monkeypatch):
    app, store, login = setup
    async def card(_): return {"id":123,"title":"phone","purchasable":True}
    async def preview(*args): return ToolTrace(tool="preview",ok=True,detail={"confirmationId":"cfm-abcdefghijkl", "action":"create_order", "confirmationPhrase":"确认下单"})
    called=[]
    async def confirm(action, confirmation):
        state = json.loads(store.values[state_key(store, ":user:" + auth._digest("alice"))])
        assert state["checkout"]["pending"] is True
        called.append((action,confirmation))
        return ToolTrace(tool="create_order",ok=True,detail={"result":{"id":"order-1"}})
    monkeypatch.setattr(ws,"_card",card)
    monkeypatch.setattr(ws,"dispatch_order_preview",preview); monkeypatch.setattr(ws,"dispatch_confirmed_handoff",confirm)
    async with client(app) as c:
        login(c,"alice"); await c.get("/api/commerce-demo/workspace")
        key = state_key(store, ":user:" + auth._digest("alice"))
        state = json.loads(store.values[key])
        state["messages"] = [{"role":"assistant", "content":"phone", "requestId":"fixture-card", "cards":[{"id":123}]}]
        store.values[key] = json.dumps(state)
        assert (await c.post("/api/commerce-demo/workspace/selection",json={"productId":123})).status_code == 200
        assert (await c.post("/api/commerce-demo/workspace/preview",json={"productId":123})).status_code == 200
        assert (await c.post("/api/commerce-demo/workspace/confirm",json={"confirmationId":"cfm-wrongvalue123"})).status_code == 409
        assert not called
        r=await c.post("/api/commerce-demo/workspace/confirm",json={"confirmationId":"cfm-abcdefghijkl"})
        assert r.json()["checkout"]["outcome"]["result"]["id"] == "order-1"
        assert len(called)==1 and "scopeId" not in r.text


@async_test
async def test_readonly_reconcile_does_not_dispatch_write(setup, monkeypatch):
    app, store, login=setup
    async def read(action, confirmation): return ToolTrace(tool="status",ok=True,detail={"status":"unknown"})
    monkeypatch.setattr(ws,"dispatch_confirmation_status",read)
    async with client(app) as c:
        login(c,"alice"); await c.get("/api/commerce-demo/workspace")
        key=state_key(store, ":user:"+auth._digest("alice")); state=json.loads(store.values[key])
        state["checkout"]={"taskId":"task","revision":1,"scopeId":"scope","pending":True,"proposal":{"action":"create_order","confirmationId":"cfm-abcdefghijkl","confirmationPhrase":"确认下单"}}
        store.values[key]=json.dumps(state)
        r=await c.post("/api/commerce-demo/workspace/reconcile",json={"confirmationId":"cfm-abcdefghijkl"})
        assert r.json()["checkout"]["pending"] is True


@async_test
async def test_missing_recovery_record_cannot_clear_uncertain_effect(setup, monkeypatch):
    app, store, login = setup
    async def read(action, confirmation):
        return ToolTrace(tool="status",ok=False,detail={"code":"confirmation_missing_or_expired","message":"确认已过期，请先查看我的订单。"})
    monkeypatch.setattr(ws, "dispatch_confirmation_status", read)
    async with client(app) as c:
        login(c,"alice"); await c.get("/api/commerce-demo/workspace")
        key=state_key(store, ":user:"+auth._digest("alice")); state=json.loads(store.values[key])
        state["checkout"]={"taskId":"task","revision":1,"scopeId":"scope","pending":True,"proposal":{"action":"create_order","confirmationId":"cfm-abcdefghijkl","confirmationPhrase":"确认下单"}}
        store.values[key]=json.dumps(state)
        r=await c.post("/api/commerce-demo/workspace/reconcile",json={"confirmationId":"cfm-abcdefghijkl"})
        assert r.json()["checkout"]["pending"] is True
        assert r.json()["checkout"]["outcome"]["status"] == "unknown"
        assert (await c.post("/api/commerce-demo/workspace/preview",json={"productId":123})).status_code == 409


@async_test
async def test_lock_releases_only_own_token(setup):
    _, store, _=setup
    async with ws._lock("work"):
        store.values["work:lock"]="successor"
    assert store.values["work:lock"] == "successor"


@async_test
async def test_historical_card_rechecks_catalog_and_uses_human_selection_context(setup, monkeypatch):
    app, store, login = setup
    calls = []
    async def card(product_id):
        calls.append(product_id)
        return {"id":product_id,"title":"latest phone","priceMinor":200,"available":2,"purchasable":True}
    async def preview(*args):
        from app.transaction_agent.runtime import _REQUEST
        context = _REQUEST.get()
        assert context.task_id.startswith("browser-order-")
        assert context.candidate_scope_id.startswith("browser-selection-")
        assert args == (123, 2, None)
        return ToolTrace(tool="preview",ok=True,detail={"confirmationId":"cfm-abcdefghijkl", "preview":{"unitPriceMinor":250,"availableQuantity":3,"currency":"CNY","title":"authority phone"}})
    monkeypatch.setattr(ws, "_card", card)
    monkeypatch.setattr(ws, "dispatch_order_preview", preview)
    async with client(app) as c:
        login(c,"alice"); await c.get("/api/commerce-demo/workspace")
        key = state_key(store, ":user:" + auth._digest("alice"))
        state = json.loads(store.values[key])
        state["messages"] = [{"role":"assistant", "content":"phone", "requestId":"fixture-card", "cards":[{"id":123,"priceMinor":100}]}]
        state["cards"] = [{"id":456}]
        store.values[key] = json.dumps(state)
        r = await c.post("/api/commerce-demo/workspace/selection",json={"productId":123})
        assert r.json()["selection"]["product"]["priceMinor"] == 200
        assert "selectionId" not in r.text
        r = await c.post("/api/commerce-demo/workspace/preview",json={"productId":123,"quantity":2})
        assert r.status_code == 200
        assert calls == [123]
        assert r.json()["selection"]["product"]["priceMinor"] == 250
        assert r.json()["selection"]["product"]["available"] == 3
        assert (await c.post("/api/commerce-demo/workspace/preview",json={"productId":456})).status_code == 409


@async_test
async def test_favorite_selection_is_owner_scoped_and_stock_is_revalidated(setup, monkeypatch):
    app, _, login = setup
    stock = [2]
    async def java(method, path, **kw):
        assert method == "GET" and path == "/api/product-favorites"
        return [{"id":123}] if kw["access_token"] == "private-alice" else []
    async def card(product_id):
        return {"id":product_id,"title":"phone","purchasable":stock[0] > 0}
    async def preview(*args):
        assert stock[0] == 0
        return ToolTrace(tool="preview", ok=False, detail={"code":"order_preview_rejected", "message":"库存不足"})
    monkeypatch.setattr(auth, "_java", java)
    monkeypatch.setattr(ws, "_card", card)
    monkeypatch.setattr(ws, "dispatch_order_preview", preview)
    async with client(app) as c:
        login(c,"bob"); await c.get("/api/commerce-demo/workspace")
        assert (await c.post("/api/commerce-demo/workspace/selection",json={"productId":123})).status_code == 409
        login(c,"alice"); await c.get("/api/commerce-demo/workspace")
        assert (await c.post("/api/commerce-demo/workspace/selection",json={"productId":123})).status_code == 200
        stock[0] = 0
        r = await c.post("/api/commerce-demo/workspace/preview",json={"productId":123})
        assert r.status_code == 409 and "库存不足" in r.text


@async_test
async def test_chat_is_credential_free_and_request_replay_is_immutable(setup, monkeypatch):
    from fastapi.responses import StreamingResponse
    app, store, login = setup
    calls = []
    async def upstream(payload, **kwargs):
        calls.append(payload)
        assert kwargs == {"authorization":None,"shopping_memory_session":None}
        assert payload.session_id.startswith("web-")
        async def events():
            yield 'data: ' + json.dumps({"type":"complete","data":{"answer":"hello","guideResult":{"products":[]},"taskId":"private-task"}}) + '\n\n'
        return StreamingResponse(events())
    monkeypatch.setitem(sys.modules,"app.main",SimpleNamespace(chat_llm_stream=upstream))
    async with client(app) as c:
        login(c,"alice"); await c.get("/api/commerce-demo/workspace")
        body={"requestId":"abcdefghijklmnop","message":"hi"}
        r=await c.post("/api/commerce-demo/workspace/chat",json=body)
        assert '"type": "complete"' in r.text
        assert "private-task" not in r.text and "private-alice" not in r.text
        assert '"type": "complete"' in (await c.post("/api/commerce-demo/workspace/chat",json=body)).text
        conflict=await c.post("/api/commerce-demo/workspace/chat",json={**body,"message":"different"})
        assert "重试内容与原请求不一致" in conflict.text
        assert len(calls) == 1
        key=state_key(store, ":user:"+auth._digest("alice"))
        store.values[key+":lock"]="other-live-request"
        busy=await c.post("/api/commerce-demo/workspace/chat",json={**body,"requestId":"abcdefghijklmnop2"})
        assert "上一项操作还在处理" in busy.text
        assert len(calls) == 1


@async_test
async def test_upstream_failure_is_not_published_as_success(setup, monkeypatch):
    from fastapi.responses import StreamingResponse
    app, _, login = setup
    async def upstream(*args, **kwargs):
        async def events():
            yield 'data: ' + json.dumps({"type":"complete","data":{
                "answer":"provider-private-diagnostic", "trace":{"status":"error"}}}) + '\n\n'
        return StreamingResponse(events())
    monkeypatch.setitem(sys.modules,"app.main",SimpleNamespace(chat_llm_stream=upstream))
    async with client(app) as c:
        login(c,"alice"); await c.get("/api/commerce-demo/workspace")
        r=await c.post("/api/commerce-demo/workspace/chat",json={"message":"hello","requestId":"abcdefghijklmnop"})
        assert '"type": "error"' in r.text
        assert '"type": "complete"' not in r.text
        assert "provider-private-diagnostic" not in r.text
        restored=(await c.get("/api/commerce-demo/workspace")).json()
        assert all(m["role"] != "assistant" for m in restored["messages"])
