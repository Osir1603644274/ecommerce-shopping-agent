"""Opt-in same-origin BFF for the local commerce journey demo.

The browser receives an HttpOnly opaque session plus a CSRF token.  Java JWTs
and refresh tokens remain server-side in Redis.  The payment simulator bridge
requires two independent flags: this Agent-side demo flag and the Java-side
``PAYMENT_SIMULATOR_ENABLED`` guard.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..settings import settings
from ..java_http import java_connection, start_java_client, close_java_client


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
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    from ..backend_observer import observer_headers, observe_response, begin_call
    headers.update(observer_headers())
    if access_token is not None:
        headers["Authorization"] = f"Bearer {access_token}"
    timing = begin_call(body, params)
    try:
        async with java_connection(timeout=5.0) as client:
            response = await client.request(method, f"{settings.backend_base_url}{path}", headers=headers, json=body, params=params)
    except httpx.HTTPError as exc:
        observe_response(None, method, path, timing)
        raise HTTPException(status_code=503, detail="Java authority unavailable") from exc
    observe_response(response, method, path, timing)
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=response.status_code, detail="authentication rejected")
    if response.status_code >= 500:
        raise HTTPException(status_code=503, detail="Java authority unavailable")
    if response.status_code >= 400:
        try:
            error = response.json().get("message")
        except (ValueError, AttributeError):
            error = None
        # Preserve the authority's client-error meaning: in particular an
        # invalid cursor is 400, a missing order is 404, and rate limiting is
        # 429. Do not forward arbitrary upstream headers or response bodies.
        retry_after = response.headers.get("Retry-After")
        headers = (
            {"Retry-After": retry_after}
            if response.status_code == 429 and retry_after is not None
            else None
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=error if isinstance(error, str) and error else "Java authority rejected request",
            headers=headers,
        )
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
    renewable = {"refreshExpiresAtEpoch", "sessionExpiresAtEpoch"}
    allowed = required | renewable | {"refreshPendingAtEpoch"}
    if type(value) is not dict or not required <= set(value) or not set(value) <= allowed:
        raise HTTPException(status_code=401, detail="authentication required")
    if value.get("sessionBinding") != _digest(session_id):
        raise HTTPException(status_code=401, detail="authentication required")
    if any(type(value.get(key)) is not str or not value[key] for key in required - {"accessExpiresAtEpoch"}):
        raise HTTPException(status_code=401, detail="authentication required")
    if type(value.get("accessExpiresAtEpoch")) is not int:
        raise HTTPException(status_code=401, detail="authentication required")
    if renewable & set(value):
        if not renewable <= set(value) or any(type(value[k]) is not int for k in renewable):
            raise HTTPException(status_code=401, detail="authentication required")
        return await _renewable_session(session_id)
    # Existing cookies cannot be extended without their original refresh deadline.
    # They retain their old lifetime; new logins use the renewable envelope below.
    remaining = value["accessExpiresAtEpoch"] - int(time.time())
    if remaining <= 0:
        await _redis().delete(_session_key(session_id))
        raise HTTPException(status_code=401, detail="authentication expired")
    await _redis().expire(
        _session_key(session_id),
        min(int(settings.commerce_demo_session_ttl_seconds), remaining),
    )
    return value


_SESSION_CAS = """-- COMMERCE_SESSION_CAS
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if ARGV[2] == '' then return redis.call('DEL', KEYS[1]) end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


def _session_json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


async def _session_cas(key, expected, value=None, ttl=1):
    return await _redis().eval(_SESSION_CAS, 1, key, expected,
                               _session_json(value) if value is not None else "", max(1, ttl))


def _token_fields(tokens):
    required = {"accessToken", "refreshToken", "tokenType", "accessExpiresInSeconds",
                "refreshExpiresInSeconds", "user"}
    if type(tokens) is not dict or set(tokens) != required or type(tokens["user"]) is not dict:
        raise HTTPException(502, "identity authority invalid")
    access, refresh, username = tokens["accessToken"], tokens["refreshToken"], tokens["user"].get("username")
    if (any(type(v) is not str or not v for v in (access, refresh, username))
            or tokens["tokenType"] != "Bearer"
            or any(type(tokens[k]) is not int or tokens[k] <= 60
                   for k in ("accessExpiresInSeconds", "refreshExpiresInSeconds"))):
        raise HTTPException(502, "identity authority invalid")
    return access, refresh, username


async def _renewable_session(session_id):
    """Rotate once across processes, preserving the browser and workspace identity.

    A durable pending marker is written BEFORE consuming a one-use refresh token.
    If the authority reply is lost, never retry that token. A crashed owner leaves
    the marker for readers to invalidate, rather than risking family revocation.
    """
    key = _session_key(session_id)
    deadline = time.monotonic() + 8
    while True:
        raw = await _redis().get(key)
        if raw is None:
            raise HTTPException(401, "authentication expired")
        value = json.loads(raw)
        now = int(time.time())
        ttl = min(value["refreshExpiresAtEpoch"], value["sessionExpiresAtEpoch"]) - now
        if ttl <= 0:
            await _session_cas(key, raw)
            raise HTTPException(401, "authentication expired")
        pending = value.get("refreshPendingAtEpoch")
        if pending is not None:
            if type(pending) is not int or now - pending >= 30:
                await _session_cas(key, raw)
                raise HTTPException(401, "authentication expired")
            if time.monotonic() >= deadline:
                raise HTTPException(503, "authentication refresh in progress")
            await asyncio.sleep(0.05)
            continue
        if value["accessExpiresAtEpoch"] > now:
            return value
        claimed = {**value, "refreshPendingAtEpoch": now}
        if not await _session_cas(key, raw, claimed, ttl):
            continue
        claimed_raw = _session_json(claimed)
        try:
            tokens = await asyncio.wait_for(_java("POST", "/api/auth/refresh",
                body={"refreshToken": value["refreshToken"]}), timeout=6)
            access, refresh, username = _token_fields(tokens)
            if username != value["username"]:
                raise HTTPException(401, "authentication rejected")
            now = int(time.time())
            updated = {**value, "accessToken": access, "refreshToken": refresh,
                       "accessExpiresAtEpoch": now + tokens["accessExpiresInSeconds"] - 30,
                       "refreshExpiresAtEpoch": now + tokens["refreshExpiresInSeconds"] - 30}
            ttl = min(updated["refreshExpiresAtEpoch"], updated["sessionExpiresAtEpoch"]) - now
            if ttl <= 0 or not await _session_cas(key, claimed_raw, updated, ttl):
                raise HTTPException(401, "authentication expired")
            return updated
        except BaseException as exc:
            # A concurrent logout/delete must never be undone by this owner.
            await asyncio.shield(_session_cas(key, claimed_raw))
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(401, "authentication expired") from exc


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
    access, refresh, username = _token_fields(tokens)
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    # Fixed maximum lifetime from this login. Rotating Java refresh tokens does
    # not extend the browser session indefinitely, nor does access expiry kill it.
    ttl = tokens["refreshExpiresInSeconds"] - 30
    now = int(time.time())
    envelope = {
        "accessToken": access,
        "refreshToken": refresh,
        "username": username,
        "csrfDigest": _digest(csrf),
        "sessionBinding": _digest(session_id),
        "accessExpiresAtEpoch": now + tokens["accessExpiresInSeconds"] - 30,
        "refreshExpiresAtEpoch": now + ttl,
        "sessionExpiresAtEpoch": now + ttl,
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
    if "sessionExpiresAtEpoch" in session:
        # Never overwrite a newly rotated token pair with an older /me snapshot.
        for _ in range(10):
            csrf = secrets.token_urlsafe(32)
            updated = {**session, "csrfDigest": _digest(csrf)}
            ttl = min(session["sessionExpiresAtEpoch"], session["refreshExpiresAtEpoch"]) - int(time.time())
            if ttl > 0 and await _session_cas(_session_key(commerce_demo_session),
                                            _session_json(session), updated, ttl):
                return {"authenticated": True, "username": session["username"], "csrfToken": csrf}
            session = await _load_session(commerce_demo_session)
        raise HTTPException(503, "authentication refresh in progress")
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


def _order_page_params(request: Request) -> dict[str, str]:
    """Forward only the bounded paging contract, never a browser user id."""
    allowed = {"size", "status", "cursor"}
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in params:
            raise HTTPException(status_code=400, detail="invalid order page parameters")
        params[key] = value
    size = params.get("size", "20")
    if not re.fullmatch(r"[0-9]{1,3}", size) or not 1 <= int(size) <= 100:
        raise HTTPException(status_code=400, detail="size must be between 1 and 100")
    if len(params.get("cursor", "")) > 512:
        raise HTTPException(status_code=400, detail="invalid order cursor")
    if len(params.get("status", "")) > 32:
        raise HTTPException(status_code=400, detail="invalid order status")
    params["size"] = str(int(size))
    return params


@router.get("/orders/page")
async def order_page(
    request: Request,
    response: Response,
    commerce_demo_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, Any]:
    _require_same_origin(request)
    session = await _load_session(commerce_demo_session)
    # Bind this tab's in-memory identity to its current cookie session, even
    # for reads: another tab may have replaced the browser's session cookie.
    _require_csrf(session, csrf)
    params = _order_page_params(request)
    result = await _java(
        "GET", "/api/orders/page", access_token=session["accessToken"], params=params
    )
    response.headers["Cache-Control"] = "no-store"
    from .browser_ids import browser_ids
    return browser_ids(result)


@router.get("/orders/{order_reference}")
async def order_detail(
    order_reference: str,
    request: Request,
    response: Response,
    commerce_demo_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, Any]:
    _require_same_origin(request)
    session = await _load_session(commerce_demo_session)
    _require_csrf(session, csrf)
    # Only one opaque reference segment is forwarded. In particular, encoded
    # query delimiters, slashes, and path traversal cannot change the Java URL.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", order_reference):
        raise HTTPException(status_code=400, detail="invalid order reference")
    if request.query_params:
        raise HTTPException(status_code=400, detail="order detail does not accept query parameters")
    result = await _java(
        "GET", f"/api/orders/{order_reference}", access_token=session["accessToken"]
    )
    response.headers["Cache-Control"] = "no-store"
    from .browser_ids import browser_ids
    return browser_ids(result)


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
