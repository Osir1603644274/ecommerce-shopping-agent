from app.api import commerce_support as support, commerce_demo as auth
from .test_commerce_workspace import setup, client, async_test
import httpx

ROOT = "/api/commerce-demo/workspace/support"


@async_test
async def test_java_bridge_preserves_idempotency_header(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(auth.settings, "backend_base_url", "http://java-authority")

    def transport(request):
        assert request.headers["Authorization"] == "Bearer server-only-token"
        assert request.headers["Idempotency-Key"] == "stable-request-001"
        assert request.url.path == "/api/after-sales/confirm"
        return httpx.Response(200, json={"success": True, "data": {"id": "case-1"}})

    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    assert await auth._java("POST", "/api/after-sales/confirm", access_token="server-only-token", body={"previewId": "preview-1"},
                            idempotency_key="stable-request-001") == {"id": "case-1"}


@async_test
async def test_support_forwards_owned_identity_and_stable_confirmation_key(setup, monkeypatch):
    app, _, login = setup
    app.include_router(support.router)
    calls = []

    async def java(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"previewId": "preview-001", "itemId": 9007199254740993, "amountMinor": 101}

    monkeypatch.setattr(auth, "_java", java)
    async with client(app) as c:
        await c.get("/api/commerce-demo/workspace")
        login(c, "alice")
        await c.get("/api/commerce-demo/workspace")
        body = {"orderId": "order-1", "itemId": "9007199254740993", "quantity": 1, "type": "EXCHANGE", "reason": "商品故障"}
        preview = await c.post(ROOT + "/preview", json=body)
        assert preview.status_code == 200
        assert preview.json()["itemId"] == "9007199254740993"
        assert "private-alice" not in preview.text
        assert calls[-1][2]["access_token"] == "private-alice"
        assert calls[-1][2]["body"]["itemId"] == 9007199254740993
        confirmed = await c.post(ROOT + "/confirm", json={"previewId": "preview-001"}, headers={"Idempotency-Key": "browser-request-0001"})
        assert confirmed.status_code == 200
        assert calls[-1][1] == "/api/after-sales/confirm"
        assert calls[-1][2]["idempotency_key"] == "browser-request-0001"
        assert confirmed.headers["cache-control"] == "no-store"


@async_test
async def test_support_requires_login_csrf_and_origin_and_has_no_simulator_route(setup, monkeypatch):
    app, _, login = setup
    app.include_router(support.router)
    calls = []

    async def java(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr(auth, "_java", java)
    async with client(app) as c:
        await c.get("/api/commerce-demo/workspace")
        assert (await c.get(ROOT + "/tickets")).status_code == 401
        login(c, "alice")
        await c.get("/api/commerce-demo/workspace")
        assert (await c.get(ROOT + "/tickets", headers={auth.CSRF_HEADER: "wrong"})).status_code == 403
        assert (await c.get(ROOT + "/tickets", headers={"Origin": "http://attacker"})).status_code == 403
        assert (await c.post(ROOT + "/simulator/refund-success")).status_code == 404
        assert not calls


@async_test
async def test_support_rejects_forged_identity_or_missing_confirmation_key(setup, monkeypatch):
    app, _, login = setup
    app.include_router(support.router)
    calls = []

    async def java(*args, **kwargs):
        calls.append((args, kwargs))
        return {"id": "ticket-1", "status": "OPEN"}

    monkeypatch.setattr(auth, "_java", java)
    async with client(app) as c:
        await c.get("/api/commerce-demo/workspace")
        login(c, "alice")
        await c.get("/api/commerce-demo/workspace")
        assert (await c.post(ROOT + "/confirm", json={"previewId": "x"})).status_code == 422
        request = {"orderId": "order-1", "category": "COMPLAINT", "summary": "需核实", "userId": "victim"}
        assert (await c.post(ROOT + "/tickets", json=request, headers={"Idempotency-Key": "ticket-create-01"})).status_code == 422
        assert not calls
        del request["userId"]
        assert (await c.post(ROOT + "/tickets", json=request, headers={"Idempotency-Key": "ticket-create-01"})).status_code == 200
        assert calls[-1][0] == ("POST", "/api/support/tickets")
        assert calls[-1][1]["body"]["caseId"] is None
