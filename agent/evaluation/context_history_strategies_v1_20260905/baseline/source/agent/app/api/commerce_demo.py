"""Opt-in same-origin BFF for the local commerce journey demo.

The browser receives an HttpOnly opaque session plus a CSRF token.  Java JWTs
and refresh tokens remain server-side in Redis.  The payment simulator bridge
requires two independent flags: this Agent-side demo flag and the Java-side
``PAYMENT_SIMULATOR_ENABLED`` guard.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..settings import settings


COOKIE_NAME = "commerce_demo_session"
CSRF_HEADER = "X-CSRF-Token"
router = APIRouter(prefix="/api/commerce-demo", tags=["电商全链路 Demo"])
_client: redis.Redis | None = None


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=128)


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_key(session_id: str) -> str:
    return f"commerce:demo:session:{_digest(session_id)}"


def _require_enabled() -> None:
    if settings.commerce_demo_enabled is not True:
        raise HTTPException(status_code=404, detail="commerce demo disabled")


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin == expected:
        return
    # Browsers commonly omit Origin on same-origin GET. Fetch Metadata keeps
    # the session-restore endpoint from becoming a cross-site CSRF rotator.
    if origin is None and request.headers.get("sec-fetch-site") in {"same-origin", "none"}:
        return
    raise HTTPException(status_code=403, detail="same-origin request required")


async def _java(
    method: str,
    path: str,
    *,
    access_token: str | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if access_token is not None:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        async with httpx.AsyncClient(base_url=settings.backend_base_url, timeout=5.0) as client:
            response = await client.request(method, path, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Java authority unavailable") from exc
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=response.status_code, detail="authentication rejected")
    if response.status_code >= 400:
        try:
            error = response.json().get("message")
        except (ValueError, AttributeError):
            error = None
        raise HTTPException(status_code=409, detail=error or "Java authority rejected request")
    try:
        root = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Java authority returned invalid JSON") from exc
    if type(root) is not dict or root.get("success") is not True or "data" not in root:
        raise HTTPException(status_code=502, detail="Java authority returned invalid contract")
    return root["data"]


async def _load_session(session_id: str | None) -> dict[str, Any]:
    _require_enabled()
    if type(session_id) is not str or not session_id or len(session_id) > 128:
        raise HTTPException(status_code=401, detail="authentication required")
    raw = await _redis().get(_session_key(session_id))
    if raw is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="authentication required") from exc
    required = {
        "accessToken", "refreshToken", "username", "csrfDigest",
        "sessionBinding", "accessExpiresAtEpoch",
    }
    if type(value) is not dict or set(value) != required:
        raise HTTPException(status_code=401, detail="authentication required")
    if value.get("sessionBinding") != _digest(session_id):
        raise HTTPException(status_code=401, detail="authentication required")
    if any(type(value.get(key)) is not str or not value[key] for key in required - {"accessExpiresAtEpoch"}):
        raise HTTPException(status_code=401, detail="authentication required")
    if type(value.get("accessExpiresAtEpoch")) is not int:
        raise HTTPException(status_code=401, detail="authentication required")
    remaining = value["accessExpiresAtEpoch"] - int(time.time())
    if remaining <= 0:
        await _redis().delete(_session_key(session_id))
        raise HTTPException(status_code=401, detail="authentication expired")
    await _redis().expire(
        _session_key(session_id),
        min(int(settings.commerce_demo_session_ttl_seconds), remaining),
    )
    return value


def _require_csrf(session: dict[str, Any], csrf: str | None) -> None:
    if csrf is None or len(csrf) > 256 or not hmac.compare_digest(
        session["csrfDigest"], _digest(csrf)
    ):
        raise HTTPException(status_code=403, detail="csrf validation failed")


async def optional_browser_authorization(
    request: Request,
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> str | None:
    """Resolve an opt-in browser session into a server-only Bearer header.

    No commerce cookie means normal API behavior is unchanged.  When the
    cookie is present, same-origin and CSRF checks are mandatory because an
    exact chat confirmation can perform a write.
    """

    session_id = request.cookies.get(COOKIE_NAME)
    if not session_id:
        return None
    _require_same_origin(request)
    session = await _load_session(session_id)
    _require_csrf(session, csrf)
    return f"Bearer {session['accessToken']}"


async def _establish_session(
    tokens: dict[str, Any],
    response: Response,
) -> dict[str, Any]:
    required = {
        "accessToken", "refreshToken", "tokenType", "accessExpiresInSeconds",
        "refreshExpiresInSeconds", "user",
    }
    if type(tokens) is not dict or set(tokens) != required or type(tokens["user"]) is not dict:
        raise HTTPException(status_code=502, detail="identity authority invalid")
    access = tokens["accessToken"]
    refresh = tokens["refreshToken"]
    username = tokens["user"].get("username")
    access_expires = tokens["accessExpiresInSeconds"]
    if (
        any(type(item) is not str or not item for item in (access, refresh, username))
        or type(access_expires) is not int
        or access_expires <= 60
    ):
        raise HTTPException(status_code=502, detail="identity authority invalid")
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    ttl = min(int(settings.commerce_demo_session_ttl_seconds), access_expires - 30)
    envelope = {
        "accessToken": access,
        "refreshToken": refresh,
        "username": username,
        "csrfDigest": _digest(csrf),
        "sessionBinding": _digest(session_id),
        "accessExpiresAtEpoch": int(time.time()) + ttl,
    }
    await _redis().set(
        _session_key(session_id),
        json.dumps(envelope, ensure_ascii=True, separators=(",", ":")),
        ex=ttl,
    )
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=ttl,
        httponly=True,
        secure=settings.commerce_demo_cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"authenticated": True, "username": username, "csrfToken": csrf}


@router.get("/capability")
async def capability() -> dict[str, bool]:
    return {
        "enabled": settings.commerce_demo_enabled is True,
        "paymentSimulationEnabled": (
            settings.commerce_demo_enabled is True
            and settings.commerce_demo_payment_simulation_enabled is True
        ),
    }


@router.post("/register")
async def register(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
    _require_enabled()
    _require_same_origin(request)
    tokens = await _java(
        "POST",
        "/api/auth/register",
        body={"username": body.username, "password": body.password},
    )
    return await _establish_session(tokens, response)


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
    _require_enabled()
    _require_same_origin(request)
    tokens = await _java(
        "POST",
        "/api/auth/login",
        body={"username": body.username, "password": body.password},
    )
    return await _establish_session(tokens, response)


@router.get("/me")
async def me(
    request: Request,
    commerce_demo_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    _require_same_origin(request)
    session = await _load_session(commerce_demo_session)
    csrf = secrets.token_urlsafe(32)
    session["csrfDigest"] = _digest(csrf)
    remaining = session["accessExpiresAtEpoch"] - int(time.time())
    await _redis().set(
        _session_key(commerce_demo_session or ""),
        json.dumps(session, ensure_ascii=True, separators=(",", ":")),
        ex=min(int(settings.commerce_demo_session_ttl_seconds), remaining),
    )
    return {"authenticated": True, "username": session["username"], "csrfToken": csrf}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    commerce_demo_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, bool]:
    _require_same_origin(request)
    session = await _load_session(commerce_demo_session)
    _require_csrf(session, csrf)
    try:
        await _java("POST", "/api/auth/logout", body={"refreshToken": session["refreshToken"]})
    finally:
        if commerce_demo_session:
            await _redis().delete(_session_key(commerce_demo_session))
        response.delete_cookie(COOKIE_NAME, path="/")
    return {"loggedOut": True}


@router.post("/payments/{payment_id}/simulate-success")
async def simulate_payment_success(
    payment_id: str,
    request: Request,
    commerce_demo_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, Any]:
    _require_enabled()
    if settings.commerce_demo_payment_simulation_enabled is not True:
        raise HTTPException(status_code=404, detail="payment simulation disabled")
    _require_same_origin(request)
    session = await _load_session(commerce_demo_session)
    _require_csrf(session, csrf)
    if not payment_id or len(payment_id) > 80:
        raise HTTPException(status_code=422, detail="invalid payment id")
    payment = await _java(
        "POST",
        f"/api/payments/{payment_id}/simulate-success",
        access_token=session["accessToken"],
    )
    return {
        "success": True,
        "data": payment,
        "executionPath": [
            {"stage": "Browser", "status": "confirmed"},
            {"stage": "Commerce Demo BFF", "status": "csrf_and_session_verified"},
            {"stage": "Java PaymentController", "status": "simulator_called"},
            {"stage": "MySQL payment/order authority", "status": "committed"},
        ],
    }


__all__ = [
    "COOKIE_NAME",
    "CSRF_HEADER",
    "optional_browser_authorization",
    "router",
]
