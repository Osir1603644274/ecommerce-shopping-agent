import hashlib
import json
import asyncio
from functools import wraps

import httpx
import pytest
from fastapi import FastAPI

from app.api import commerce_demo


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, ex=None):
        self.values[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    async def expire(self, key, _seconds):
        return key in self.values


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def app_and_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(commerce_demo, "_client", fake)
    monkeypatch.setattr(commerce_demo.settings, "commerce_demo_enabled", True)
    monkeypatch.setattr(
        commerce_demo.settings, "commerce_demo_payment_simulation_enabled", True
    )
    app = FastAPI()
    app.include_router(commerce_demo.router)
    return app, fake


@async_test
async def test_register_keeps_bearer_server_side_and_sets_httponly_cookie(
    app_and_redis, monkeypatch
):
    app, fake = app_and_redis

    async def java(method, path, **kwargs):
        assert (method, path) == ("POST", "/api/auth/register")
        assert kwargs["body"]["username"] == "demo-user"
        return {
            "accessToken": "access-secret",
            "refreshToken": "refresh-secret",
            "tokenType": "Bearer",
            "accessExpiresInSeconds": 900,
            "refreshExpiresInSeconds": 2592000,
            "user": {"id": "owner-id", "username": "demo-user", "roles": ["USER"]},
        }

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/commerce-demo/register",
            headers={"Origin": "http://testserver"},
            json={"username": "demo-user", "password": "StrongPassword123!"},
        )

    assert response.status_code == 200
    assert set(response.json()) == {"authenticated", "username", "csrfToken"}
    assert "access-secret" not in response.text and "refresh-secret" not in response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    stored = json.loads(next(iter(fake.values.values())))
    assert stored["accessToken"] == "access-secret"


@async_test
async def test_cookie_authorization_requires_same_origin_and_csrf(app_and_redis):
    app, fake = app_and_redis
    session_id = "opaque-session"
    csrf = "csrf-token"
    fake.values[commerce_demo._session_key(session_id)] = json.dumps({
        "accessToken": "server-only-token",
        "refreshToken": "refresh",
        "username": "demo-user",
        "csrfDigest": _digest(csrf),
        "sessionBinding": _digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
    })
    from fastapi import Request

    async def echo(request: Request):
        return {
            "authorization": await commerce_demo.optional_browser_authorization(
                request, request.headers.get(commerce_demo.CSRF_HEADER)
            )
        }

    app.add_api_route("/echo", echo, methods=["POST"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(commerce_demo.COOKIE_NAME, session_id)
        ok = await client.post(
            "/echo",
            headers={"Origin": "http://testserver", commerce_demo.CSRF_HEADER: csrf},
        )
        rejected = await client.post(
            "/echo",
            headers={"Origin": "http://testserver", commerce_demo.CSRF_HEADER: "wrong"},
        )

    assert ok.status_code == 200
    assert ok.json()["authorization"] == "Bearer server-only-token"
    assert rejected.status_code == 403


@async_test
async def test_session_restore_rejects_cross_site_csrf_rotation(app_and_redis):
    app, fake = app_and_redis
    session_id = "restore-session"
    csrf = "restore-csrf"
    fake.values[commerce_demo._session_key(session_id)] = json.dumps({
        "accessToken": "server-only-token",
        "refreshToken": "refresh",
        "username": "demo-user",
        "csrfDigest": _digest(csrf),
        "sessionBinding": _digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
    })
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(commerce_demo.COOKIE_NAME, session_id)
        rejected = await client.get(
            "/api/commerce-demo/me",
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        accepted = await client.get(
            "/api/commerce-demo/me",
            headers={"Sec-Fetch-Site": "same-origin"},
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["username"] == "demo-user"


@async_test
async def test_payment_simulator_bridge_is_double_gated_and_returns_server_path(
    app_and_redis, monkeypatch
):
    app, fake = app_and_redis
    session_id = "payment-session"
    csrf = "payment-csrf"
    fake.values[commerce_demo._session_key(session_id)] = json.dumps({
        "accessToken": "access-token",
        "refreshToken": "refresh-token",
        "username": "demo-user",
        "csrfDigest": _digest(csrf),
        "sessionBinding": _digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
    })

    async def java(method, path, **kwargs):
        assert (method, path) == ("POST", "/api/payments/payment-1/simulate-success")
        assert kwargs["access_token"] == "access-token"
        return {"id": "payment-1", "status": "PAID", "orderId": "order-1"}

    monkeypatch.setattr(commerce_demo, "_java", java)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(commerce_demo.COOKIE_NAME, session_id)
        response = await client.post(
            "/api/commerce-demo/payments/payment-1/simulate-success",
            headers={"Origin": "http://testserver", commerce_demo.CSRF_HEADER: csrf},
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "PAID"
    assert [item["stage"] for item in response.json()["executionPath"]] == [
        "Browser", "Commerce Demo BFF", "Java PaymentController", "MySQL payment/order authority"
    ]
