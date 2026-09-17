"""Same-origin order reads without exposing or accepting browser JWTs."""
import asyncio
from functools import wraps
import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import commerce_demo


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, ex=None):
        self.values[key] = value
        return True

    async def expire(self, key, seconds):
        return key in self.values

    async def delete(self, key):
        return self.values.pop(key, None) is not None


@pytest.fixture
def orders_app(monkeypatch):
    store = FakeRedis()
    session_id = "orders-browser-session"
    store.values[commerce_demo._session_key(session_id)] = json.dumps({
        "accessToken": "server-only-jwt",
        "refreshToken": "server-only-refresh",
        "username": "orders-user",
        "csrfDigest": commerce_demo._digest("csrf-value"),
        "sessionBinding": commerce_demo._digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
    })
    monkeypatch.setattr(commerce_demo, "_client", store)
    monkeypatch.setattr(commerce_demo.settings, "commerce_demo_enabled", True)
    app = FastAPI()
    app.include_router(commerce_demo.router)
    return app, session_id, store


def browser(app, session_id):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={commerce_demo.COOKIE_NAME: session_id},
        headers={"Sec-Fetch-Site": "same-origin", commerce_demo.CSRF_HEADER: "csrf-value"},
    )


@async_test
async def test_page_forwards_only_session_identity_and_exact_cursor(orders_app, monkeypatch):
    app, session_id, _ = orders_app
    payload = {"orders": [{"id": "order-1", "items": []}], "nextCursor": "next.signature", "hasMore": True}

    async def java(method, path, **kwargs):
        assert (method, path) == ("GET", "/api/orders/page")
        assert kwargs == {
            "access_token": "server-only-jwt",
            "params": {"size": "20", "status": "PENDING_PAYMENT", "cursor": "encoded.signature"},
        }
        return payload

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with browser(app, session_id) as client:
        response = await client.get(
            "/api/commerce-demo/orders/page",
            params={"status": "PENDING_PAYMENT", "cursor": "encoded.signature"},
            headers={"Authorization": "Bearer attacker-token"},
        )
    assert response.status_code == 200
    assert response.json() == payload
    assert response.headers["Cache-Control"] == "no-store"
    assert "server-only" not in response.text and "attacker-token" not in response.text


@async_test
async def test_empty_page_contract_is_not_replaced_with_fake_orders(orders_app, monkeypatch):
    app, session_id, _ = orders_app

    async def java(method, path, **kwargs):
        return {"orders": [], "nextCursor": None, "hasMore": False}

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with browser(app, session_id) as client:
        response = await client.get("/api/commerce-demo/orders/page")
    assert response.json() == {"orders": [], "nextCursor": None, "hasMore": False}


@pytest.mark.parametrize("query", [
    "userId=someone-else", "size=20&size=100", "status=PAID&status=REFUNDED",
    "size=0", "size=101", "size=1.5", "size=-1", "size=",
    "cursor=" + "x" * 513, "status=" + "X" * 33,
])
@async_test
async def test_page_rejects_unbounded_unknown_and_duplicate_parameters(orders_app, monkeypatch, query):
    app, session_id, _ = orders_app

    async def java(*args, **kwargs):
        pytest.fail("invalid query must never reach Java")

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with browser(app, session_id) as client:
        response = await client.get("/api/commerce-demo/orders/page?" + query)
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/orders/page", "/orders/order-1"])
@async_test
async def test_reads_require_live_same_origin_session(orders_app, monkeypatch, path):
    app, session_id, store = orders_app

    async def java(*args, **kwargs):
        pytest.fail("unauthorized read must never reach Java")

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with browser(app, session_id) as client:
        cross_site = await client.get(
            "/api/commerce-demo" + path,
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert cross_site.status_code == 403
        client.cookies.clear()
        assert (await client.get("/api/commerce-demo" + path)).status_code == 401
        client.cookies.set(commerce_demo.COOKIE_NAME, session_id)
        key = commerce_demo._session_key(session_id)
        session = json.loads(store.values[key])
        session["accessExpiresAtEpoch"] = 1
        store.values[key] = json.dumps(session)
        assert (await client.get("/api/commerce-demo" + path)).status_code == 401
        assert key not in store.values
        monkeypatch.setattr(commerce_demo.settings, "commerce_demo_enabled", False)
        assert (await client.get("/api/commerce-demo" + path)).status_code == 404


@pytest.mark.parametrize("path", ["/orders/page", "/orders/order-1"])
@async_test
async def test_reads_reject_missing_csrf_and_cross_tab_account_switch(orders_app, monkeypatch, path):
    app, session_id, store = orders_app
    calls = []

    async def java(method, java_path, **kwargs):
        calls.append(kwargs["access_token"])
        return {"orders": [], "nextCursor": None, "hasMore": False} if path.endswith("page") else {"id": "order-1", "items": []}

    monkeypatch.setattr(commerce_demo, "_java", java)
    second_session_id = "second-account-session"
    second_session = json.loads(store.values[commerce_demo._session_key(session_id)])
    second_session.update({
        "accessToken": "second-account-jwt",
        "username": "second-account",
        "csrfDigest": commerce_demo._digest("second-account-csrf"),
        "sessionBinding": commerce_demo._digest(second_session_id),
    })
    store.values[commerce_demo._session_key(second_session_id)] = json.dumps(second_session)
    async with browser(app, session_id) as client:
        client.headers.pop(commerce_demo.CSRF_HEADER)
        missing = await client.get("/api/commerce-demo" + path)
        assert missing.status_code == 403
        assert missing.json() == {"detail": "csrf validation failed"}
        assert not calls
        client.headers[commerce_demo.CSRF_HEADER] = "csrf-value"
        assert (await client.get("/api/commerce-demo" + path)).status_code == 200
        assert calls == ["server-only-jwt"]
        # Another tab signs into account B while this tab still remembers A.
        client.cookies.clear()
        client.cookies.set(commerce_demo.COOKIE_NAME, second_session_id)
        switched = await client.get("/api/commerce-demo" + path)
        assert switched.status_code == 403
        assert switched.json() == {"detail": "csrf validation failed"}
        assert calls == ["server-only-jwt"]
        client.headers[commerce_demo.CSRF_HEADER] = "second-account-csrf"
        assert (await client.get("/api/commerce-demo" + path)).status_code == 200
        assert calls == ["server-only-jwt", "second-account-jwt"]


@pytest.mark.parametrize("path", ["/orders/page", "/orders/order-1"])
@async_test
async def test_reads_reject_rotated_csrf_until_tab_restores_identity(orders_app, monkeypatch, path):
    app, session_id, _ = orders_app
    calls = []

    async def java(method, java_path, **kwargs):
        calls.append(kwargs["access_token"])
        return {"orders": [], "nextCursor": None, "hasMore": False} if path.endswith("page") else {"id": "order-1", "items": []}

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with browser(app, session_id) as client:
        # /me rotates the actual server-side CSRF digest, just as a tab restore does.
        restored = await client.get("/api/commerce-demo/me")
        assert restored.status_code == 200
        csrf = restored.json()["csrfToken"]
        assert csrf != "csrf-value"
        stale = await client.get("/api/commerce-demo" + path)
        assert stale.status_code == 403
        assert stale.json() == {"detail": "csrf validation failed"}
        assert not calls
        client.headers[commerce_demo.CSRF_HEADER] = csrf
        accepted = await client.get("/api/commerce-demo" + path)
        assert accepted.status_code == 200
        assert calls == ["server-only-jwt"]


@async_test
async def test_detail_uses_authority_ownership_and_preserves_items(orders_app, monkeypatch):
    app, session_id, _ = orders_app
    order = {"id": "order-1", "orderNo": "LL123456", "items": [{"orderId": "order-1", "titleSnapshot": "手机"}]}

    async def java(method, path, **kwargs):
        assert (method, path) == ("GET", "/api/orders/LL123456")
        assert kwargs == {"access_token": "server-only-jwt"}
        return order

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with browser(app, session_id) as client:
        response = await client.get("/api/commerce-demo/orders/LL123456")
    assert response.status_code == 200
    assert response.json() == order
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("reference", ["order-1?userId=victim", "order-1%3FuserId%3Dvictim", "x" * 81])
@async_test
async def test_detail_rejects_path_or_query_injection(orders_app, monkeypatch, reference):
    app, session_id, _ = orders_app

    async def java(*args, **kwargs):
        pytest.fail("invalid reference must never reach Java")

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with browser(app, session_id) as client:
        response = await client.get("/api/commerce-demo/orders/" + reference)
    assert response.status_code == 400


@async_test
async def test_java_helper_transmits_bearer_and_url_encoded_query(monkeypatch):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(commerce_demo.settings, "backend_base_url", "http://java-authority")

    def java_transport(request):
        assert request.url.host == "java-authority"
        assert request.url.path == "/api/orders/page"
        assert request.url.params["cursor"] == "signed+value&userId=not-an-extra-param"
        assert list(request.url.params) == ["size", "cursor"]
        assert request.headers["Authorization"] == "Bearer server-only-jwt"
        return httpx.Response(200, json={"success": True, "data": {"orders": [], "hasMore": False, "nextCursor": None}})

    monkeypatch.setattr(commerce_demo.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(java_transport), **kwargs
    ))
    result = await commerce_demo._java(
        "GET", "/api/orders/page", access_token="server-only-jwt",
        params={"size": "20", "cursor": "signed+value&userId=not-an-extra-param"},
    )
    assert result["orders"] == []


@pytest.mark.parametrize("upstream, expected", [(400, 400), (401, 401), (403, 403), (404, 404), (409, 409), (422, 422), (429, 429), (500, 503), (503, 503)])
@async_test
async def test_java_errors_keep_client_status_and_rate_limit_retry_after(monkeypatch, upstream, expected):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(commerce_demo.settings, "backend_base_url", "http://java-authority")
    transport = httpx.MockTransport(lambda request: httpx.Response(
        upstream, json={"success": False, "message": "authority message"},
        headers={"Retry-After": "12", "Set-Cookie": "unsafe=upstream-cookie"},
    ))
    monkeypatch.setattr(commerce_demo.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    with pytest.raises(HTTPException) as caught:
        await commerce_demo._java("GET", "/api/orders/page")
    assert caught.value.status_code == expected
    if upstream == 429:
        assert caught.value.headers == {"Retry-After": "12"}
    else:
        assert caught.value.headers is None
    if upstream in {401, 403}:
        assert caught.value.detail == "authentication rejected"
    elif upstream >= 500:
        assert caught.value.detail == "Java authority unavailable"
    else:
        assert caught.value.detail == "authority message"


@async_test
async def test_detail_preserves_ownership_rejection(orders_app, monkeypatch):
    app, session_id, _ = orders_app

    async def java(*args, **kwargs):
        raise HTTPException(status_code=403, detail="authentication rejected")

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with browser(app, session_id) as client:
        response = await client.get("/api/commerce-demo/orders/someone-elses-order")
    assert response.status_code == 403
    assert response.json() == {"detail": "authentication rejected"}
