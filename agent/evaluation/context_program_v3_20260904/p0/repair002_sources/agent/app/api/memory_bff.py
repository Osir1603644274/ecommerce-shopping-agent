"""Same-origin BFF for the explicit shopping-memory V13 canary.

The browser never receives a Java bearer, refresh token, owner id, memory id,
or client-selected semantic preference.  Redis stores the authenticated server
session and opaque candidate/entry handles with bounded TTLs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..settings import settings
from ..task_state import _get_client as _get_task_state_redis
from ..memory.projection_client import MemoryAccessCredential
from ..memory.v3_runtime import (
    MemoryProjectionV3Client,
    MemoryRunBinding,
    build_memory_run_binding,
    empty_memory_run_binding,
)

COOKIE_NAME = "shopping_memory_session"
CSRF_HEADER = "X-CSRF-Token"
_client: redis.Redis | None = None
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryRunResolution:
    binding: MemoryRunBinding
    summary: dict[str, Any]


def _resolution_summary(
    *, reason: str, started: float, binding: MemoryRunBinding,
    eligible: bool, attempted: bool, projection_outcome: str,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "reason": reason,
        "durationMs": round((time.perf_counter() - started) * 1000, 3),
        "eligible": eligible,
        "attempted": attempted,
        "projectionOutcome": projection_outcome,
        "retainedCount": len(binding.preferences),
        "truncated": truncated,
    }


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=128)


class CandidateDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern=r"^confirm$")


class MemoryEntryCorrectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preference_kind: Literal["prefer", "avoid", "indifferent"] = Field(
        alias="preferenceKind"
    )
    normalized_value: str = Field(
        alias="normalizedValue",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$",
        strict=True,
    )


router = APIRouter(prefix="/api/web-memory", tags=["购物记忆 BFF"])


@router.get("/capability")
async def capability() -> dict[str, bool]:
    return {"enabled": (
        settings.memory_bff_enabled is True
        and settings.memory_projection_client_enabled is True
    )}


def _canary_usernames() -> set[str]:
    return {
        item.strip() for item in settings.memory_bff_canary_usernames.split(",")
        if item.strip()
    }


def _require_enabled() -> None:
    if not (
        settings.memory_bff_enabled is True
        and settings.memory_projection_client_enabled is True
    ):
        raise HTTPException(status_code=404, detail="memory canary disabled")


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_key(session_id: str) -> str:
    return f"memory:bff:session:{_digest(session_id)}"


def _candidate_key(candidate_id: str) -> str:
    return f"memory:bff:candidate:{_digest(candidate_id)}"


def _handle_key(handle: str) -> str:
    return f"memory:bff:handle:{_digest(handle)}"


def _task_mode_key(task_id: str) -> str:
    return f"memory:task-mode:v13:{_digest(task_id)}"


async def _claim_task_mode(task_id: str | None, mode: str) -> str:
    if type(task_id) is not str or not task_id:
        return "unavailable"
    client = _get_task_state_redis()
    key = _task_mode_key(task_id)
    if await client.set(key, mode, nx=True):
        return "claimed"
    return "same" if await client.get(key) == mode else "conflict"


async def memory_bound_task_is_non_durable(task_id: str) -> bool:
    """A memory-bound canary task cannot later enter durable resume."""
    if type(task_id) is not str or not task_id:
        return False
    return await _get_task_state_redis().get(_task_mode_key(task_id)) == "memory_non_durable"


async def claim_task_durable_mode(task_id: str) -> bool:
    """Atomically admit only tasks whose sticky execution mode is durable."""
    return await _claim_task_mode(task_id, "durable") in {"claimed", "same"}


async def _mark_memory_bound_task_non_durable(task_id: str | None) -> str:
    try:
        # Task IDs are never reused. Keep the one-bit guard durable because a
        # sliding TaskState TTL could otherwise outlive a separately expiring
        # marker and later admit an unsafe resume.
        return await _claim_task_mode(task_id, "memory_non_durable")
    except Exception:
        logger.warning("shopping memory durable guard unavailable")
        return "unavailable"


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin != expected:
        raise HTTPException(status_code=403, detail="same-origin request required")


async def _session(session_id: str | None) -> dict[str, Any]:
    if not session_id or len(session_id) > 128:
        _require_enabled()
        raise HTTPException(status_code=401, detail="authentication required")
    if not (
        settings.memory_bff_enabled is True
        and settings.memory_projection_client_enabled is True
    ):
        await _redis().delete(_session_key(session_id))
        raise HTTPException(status_code=404, detail="memory canary disabled")
    raw = await _redis().get(_session_key(session_id))
    if raw is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="authentication required") from exc
    required = {
        "accessToken", "refreshToken", "username", "csrfDigest",
        "sessionBinding", "accessExpiresAtEpoch", "canaryEpoch",
    }
    if type(value) is not dict or set(value) != required:
        raise HTTPException(status_code=401, detail="authentication required")
    if value["sessionBinding"] != _digest(session_id):
        raise HTTPException(status_code=401, detail="authentication required")
    string_fields = required - {"accessExpiresAtEpoch"}
    if any(type(value[key]) is not str or not value[key] for key in string_fields):
        raise HTTPException(status_code=401, detail="authentication required")
    if type(value["accessExpiresAtEpoch"]) is not int:
        raise HTTPException(status_code=401, detail="authentication required")
    remaining = value["accessExpiresAtEpoch"] - int(time.time())
    if remaining <= 0 or value["canaryEpoch"] != settings.memory_bff_epoch:
        await _redis().delete(_session_key(session_id))
        raise HTTPException(status_code=401, detail="authentication expired")
    if value["username"] not in _canary_usernames():
        await _redis().delete(_session_key(session_id))
        raise HTTPException(status_code=403, detail="memory canary unavailable")
    await _redis().expire(
        _session_key(session_id),
        min(int(settings.memory_bff_session_ttl_seconds), remaining),
    )
    return value


def _require_csrf(session: dict[str, Any], csrf: str | None) -> None:
    if csrf is None or len(csrf) > 256 or not hmac.compare_digest(
        session["csrfDigest"], _digest(csrf)
    ):
        raise HTTPException(status_code=403, detail="csrf validation failed")


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
    async with httpx.AsyncClient(
        base_url=settings.backend_base_url,
        timeout=min(max(settings.memory_projection_client_timeout_seconds, 0.2), 5.0),
    ) as client:
        response = await client.request(method, path, headers=headers, json=body)
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="authentication expired")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="memory authority unavailable")
    try:
        root = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="memory authority invalid") from exc
    if type(root) is not dict or root.get("success") is not True or "data" not in root:
        raise HTTPException(status_code=502, detail="memory authority invalid")
    return root["data"]


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    _require_enabled()
    _require_same_origin(request)
    tokens = await _java(
        "POST", "/api/auth/login",
        body={"username": body.username, "password": body.password},
    )
    required = {
        "accessToken", "refreshToken", "tokenType", "accessExpiresInSeconds",
        "refreshExpiresInSeconds", "user",
    }
    if type(tokens) is not dict or set(tokens) != required or type(tokens["user"]) is not dict:
        raise HTTPException(status_code=502, detail="identity authority invalid")
    access = tokens["accessToken"]
    refresh = tokens["refreshToken"]
    username = tokens["user"].get("username")
    if any(type(item) is not str or not item for item in (access, refresh, username)):
        raise HTTPException(status_code=502, detail="identity authority invalid")
    access_expires = tokens["accessExpiresInSeconds"]
    if type(access_expires) is not int or access_expires <= 60:
        raise HTTPException(status_code=502, detail="identity authority invalid")
    if username not in _canary_usernames():
        raise HTTPException(status_code=403, detail="memory canary unavailable")
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    session_binding = _digest(session_id)
    session_ttl = min(
        int(settings.memory_bff_session_ttl_seconds),
        access_expires - 30,
    )
    envelope = {
        "accessToken": access,
        "refreshToken": refresh,
        "username": username,
        "csrfDigest": _digest(csrf),
        "sessionBinding": session_binding,
        "accessExpiresAtEpoch": int(time.time()) + session_ttl,
        "canaryEpoch": settings.memory_bff_epoch,
    }
    await _redis().set(
        _session_key(session_id),
        json.dumps(envelope, ensure_ascii=True, separators=(",", ":")),
        ex=session_ttl,
    )
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=session_ttl,
        httponly=True,
        secure=settings.memory_bff_cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"authenticated": True, "username": username, "csrfToken": csrf}


@router.get("/me")
async def me(
    shopping_memory_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    session = await _session(shopping_memory_session)
    csrf = secrets.token_urlsafe(32)
    session["csrfDigest"] = _digest(csrf)
    if shopping_memory_session is None:
        raise HTTPException(status_code=401, detail="authentication required")
    await _redis().set(
        _session_key(shopping_memory_session),
        json.dumps(session, ensure_ascii=True, separators=(",", ":")),
        ex=min(
            int(settings.memory_bff_session_ttl_seconds),
            session["accessExpiresAtEpoch"] - int(time.time()),
        ),
    )
    return {
        "authenticated": True,
        "username": session["username"],
        "csrfToken": csrf,
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    shopping_memory_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, bool]:
    _require_same_origin(request)
    session = await _session(shopping_memory_session)
    _require_csrf(session, csrf)
    try:
        await _java(
            "POST", "/api/auth/logout",
            body={"refreshToken": session["refreshToken"]},
        )
    finally:
        if shopping_memory_session:
            await _redis().delete(_session_key(shopping_memory_session))
        response.delete_cookie(COOKIE_NAME, path="/")
    return {"loggedOut": True}


@router.get("/entries")
async def entries(
    shopping_memory_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    session = await _session(shopping_memory_session)
    projection = await _java(
        "GET", "/api/memory/projection/v3",
        access_token=session["accessToken"],
    )
    if type(projection) is not dict or type(projection.get("entries")) is not list:
        raise HTTPException(status_code=502, detail="memory projection invalid")
    public_entries: list[dict[str, Any]] = []
    for entry in projection["entries"][:8]:
        if type(entry) is not dict:
            raise HTTPException(status_code=502, detail="memory projection invalid")
        handle = secrets.token_urlsafe(24)
        stored = {
            "sessionBinding": session["sessionBinding"],
            "entry": entry,
        }
        await _redis().set(
            _handle_key(handle),
            json.dumps(stored, ensure_ascii=True, separators=(",", ":")),
            ex=settings.memory_bff_session_ttl_seconds,
        )
        public_entries.append({
            "memoryHandle": handle,
            "categoryId": entry.get("categoryId"),
            "preferenceKind": entry.get("preferenceKind"),
            "attributeKey": entry.get("attributeKey"),
            "normalizedValue": entry.get("normalizedValue"),
            "source": "user_confirmed",
            "confidence": 1.0,
            "createdAt": entry.get("createdAt"),
            "updatedAt": entry.get("updatedAt"),
            "expiresAt": entry.get("expiresAt"),
        })
    return {
        "revision": projection.get("revision"),
        "truncated": projection.get("truncated", False),
        "entries": public_entries,
    }


@router.get("/candidates")
async def candidates(
    shopping_memory_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    session = await _session(shopping_memory_session)
    ids = await _redis().zrange(
        f"memory:bff:candidate-index:{session['sessionBinding']}", 0, -1
    )
    result = []
    for candidate_id in ids[-8:]:
        raw = await _redis().get(_candidate_key(candidate_id))
        if raw is None:
            continue
        candidate = json.loads(raw)
        if candidate.get("sessionBinding") != session["sessionBinding"]:
            continue
        result.append({
            "candidateId": candidate_id,
            "displayText": candidate["displayText"],
            "status": candidate["status"],
        })
    return {"candidates": result}


@router.post("/candidates/{candidate_id}/decision")
async def decide_candidate(
    candidate_id: str,
    body: CandidateDecisionBody,
    request: Request,
    shopping_memory_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, Any]:
    _require_same_origin(request)
    session = await _session(shopping_memory_session)
    _require_csrf(session, csrf)
    if not candidate_id or len(candidate_id) > 128 or body.action != "confirm":
        raise HTTPException(status_code=404, detail="candidate unavailable")
    raw = await _redis().get(_candidate_key(candidate_id))
    if raw is None:
        raise HTTPException(status_code=404, detail="candidate unavailable")
    candidate = json.loads(raw)
    if candidate.get("sessionBinding") != session["sessionBinding"]:
        raise HTTPException(status_code=404, detail="candidate unavailable")
    if candidate.get("status") == "confirmed":
        return {"confirmed": True}
    if candidate.get("status") != "pending" or type(candidate.get("preference")) is not dict:
        raise HTTPException(status_code=409, detail="candidate unavailable")
    lock_key = f"{_candidate_key(candidate_id)}:lock"
    if not await _redis().set(lock_key, session["sessionBinding"], ex=15, nx=True):
        raise HTTPException(status_code=409, detail="candidate is being confirmed")
    try:
        preference = candidate["preference"]
        projection = await _java(
            "GET", "/api/memory/projection/v3",
            access_token=session["accessToken"],
        )
        matching = None
        if type(projection) is dict and type(projection.get("entries")) is list:
            for entry in projection["entries"]:
                if (
                    type(entry) is dict
                    and entry.get("categoryId") == preference.get("categoryId")
                    and entry.get("recipientScope") == preference.get("recipientScope")
                    and entry.get("attributeKey") == preference.get("attributeKey")
                    and entry.get("status") == "ACTIVE"
                ):
                    matching = entry
                    break
        operation = "update" if matching is not None else "write"
        predecessor = (
            {
                "predecessorId": matching["entryId"],
                "previousVersion": matching["version"],
            }
            if matching is not None else {}
        )
        consent = await _java(
            "POST", "/api/memory/consents/v3",
            access_token=session["accessToken"],
            body={
                "operation": operation,
                **predecessor,
                "preference": preference,
                "consentAction": "confirm_suggestion",
            },
        )
        await _java(
            "POST", "/api/memory/commands/v3",
            access_token=session["accessToken"],
            body={
                "commandId": consent["commandId"],
                "operation": operation,
                **predecessor,
                "preference": preference,
                "consentEventId": consent["consentEventId"],
            },
        )
        candidate["status"] = "confirmed"
        await _redis().set(
            _candidate_key(candidate_id),
            json.dumps(candidate, ensure_ascii=True, separators=(",", ":")),
            ex=settings.memory_candidate_ttl_seconds,
        )
        return {"confirmed": True}
    finally:
        await _redis().delete(lock_key)


async def _entry_from_handle(
    memory_handle: str,
    session: dict[str, Any],
) -> dict[str, Any]:
    if not memory_handle or len(memory_handle) > 128:
        raise HTTPException(status_code=404, detail="memory unavailable")
    raw = await _redis().get(_handle_key(memory_handle))
    if raw is None:
        raise HTTPException(status_code=404, detail="memory unavailable")
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="memory unavailable") from exc
    if (
        type(stored) is not dict
        or stored.get("sessionBinding") != session["sessionBinding"]
        or type(stored.get("entry")) is not dict
    ):
        raise HTTPException(status_code=404, detail="memory unavailable")
    entry = stored["entry"]
    required = {
        "entryId", "categoryId", "recipientScope", "preferenceKind",
        "attributeKey", "normalizedValue", "catalogRevision", "version",
        "status", "chainVerified",
    }
    if (
        any(key not in entry for key in required)
        or entry.get("recipientScope") != "self"
        or entry.get("status") != "ACTIVE"
        or entry.get("chainVerified") is not True
    ):
        raise HTTPException(status_code=404, detail="memory unavailable")
    return entry


def _entry_preference(
    entry: dict[str, Any],
    *,
    correction: MemoryEntryCorrectionBody | None = None,
) -> dict[str, Any]:
    return {
        "categoryId": entry["categoryId"],
        "preferenceKind": (
            correction.preference_kind if correction is not None
            else entry["preferenceKind"]
        ),
        "attributeKey": entry["attributeKey"],
        "normalizedValue": (
            correction.normalized_value if correction is not None
            else entry["normalizedValue"]
        ),
        "catalogRevision": entry["catalogRevision"],
        "recipientScope": "self",
        "source": "user_confirmed",
    }


async def _mutate_entry(
    *,
    memory_handle: str,
    session: dict[str, Any],
    operation: Literal["update", "revoke", "suppress"],
    correction: MemoryEntryCorrectionBody | None = None,
) -> None:
    entry = await _entry_from_handle(memory_handle, session)
    preference = _entry_preference(entry, correction=correction)
    lock_key = f"{_handle_key(memory_handle)}:lock"
    if not await _redis().set(
        lock_key, session["sessionBinding"], ex=15, nx=True
    ):
        raise HTTPException(status_code=409, detail="memory is being changed")
    try:
        if operation == "update":
            await _java(
                "POST", "/api/memory/catalog/validate/v3",
                access_token=session["accessToken"],
                body=preference,
            )
        predecessor = {
            "predecessorId": entry["entryId"],
            "previousVersion": entry["version"],
        }
        consent_action = {
            "update": "confirm_suggestion",
            "revoke": "forget",
            "suppress": "disable",
        }[operation]
        consent = await _java(
            "POST", "/api/memory/consents/v3",
            access_token=session["accessToken"],
            body={
                "operation": operation,
                **predecessor,
                "preference": preference,
                "consentAction": consent_action,
            },
        )
        await _java(
            "POST", "/api/memory/commands/v3",
            access_token=session["accessToken"],
            body={
                "commandId": consent["commandId"],
                "operation": operation,
                **predecessor,
                "preference": preference,
                "consentEventId": consent["consentEventId"],
            },
        )
        await _redis().delete(_handle_key(memory_handle))
    finally:
        await _redis().delete(lock_key)


@router.patch("/entries/{memory_handle}")
async def correct_entry(
    memory_handle: str,
    body: MemoryEntryCorrectionBody,
    request: Request,
    shopping_memory_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, bool]:
    _require_same_origin(request)
    session = await _session(shopping_memory_session)
    _require_csrf(session, csrf)
    await _mutate_entry(
        memory_handle=memory_handle,
        session=session,
        operation="update",
        correction=body,
    )
    return {"updated": True}


@router.post("/entries/{memory_handle}/disable")
async def disable_entry(
    memory_handle: str,
    request: Request,
    shopping_memory_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, bool]:
    _require_same_origin(request)
    session = await _session(shopping_memory_session)
    _require_csrf(session, csrf)
    await _mutate_entry(
        memory_handle=memory_handle,
        session=session,
        operation="suppress",
    )
    return {"disabled": True}


@router.delete("/entries/{memory_handle}")
async def revoke_entry(
    memory_handle: str,
    request: Request,
    shopping_memory_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, bool]:
    _require_same_origin(request)
    session = await _session(shopping_memory_session)
    _require_csrf(session, csrf)
    await _mutate_entry(
        memory_handle=memory_handle,
        session=session,
        operation="revoke",
    )
    return {"revoked": True}


async def enqueue_validated_candidate(
    *,
    shopping_memory_session: str,
    preference: dict[str, str],
    display_text: str,
) -> str:
    """Worker-only seam called after deterministic catalog validation."""
    session = await _session(shopping_memory_session)
    candidate_id = secrets.token_urlsafe(24)
    candidate = {
        "sessionBinding": session["sessionBinding"],
        "preference": preference,
        "displayText": display_text[:256],
        "status": "pending",
    }
    await _redis().set(
        _candidate_key(candidate_id),
        json.dumps(candidate, ensure_ascii=True, separators=(",", ":")),
        ex=settings.memory_candidate_ttl_seconds,
    )
    index = f"memory:bff:candidate-index:{session['sessionBinding']}"
    await _redis().zadd(index, {candidate_id: 0})
    await _redis().expire(index, settings.memory_candidate_ttl_seconds)
    return candidate_id


async def session_for_binding(session_binding: str) -> dict[str, Any] | None:
    """Worker-only lookup; the raw browser session id is never queued."""
    if (
        settings.memory_bff_enabled is not True
        or settings.memory_projection_client_enabled is not True
        or type(session_binding) is not str
        or len(session_binding) != 64
    ):
        return None
    raw = await _redis().get(f"memory:bff:session:{session_binding}")
    if raw is None:
        return None
    try:
        session = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        type(session) is not dict
        or session.get("sessionBinding") != session_binding
        or session.get("username") not in _canary_usernames()
        or session.get("canaryEpoch") != settings.memory_bff_epoch
        or type(session.get("accessExpiresAtEpoch")) is not int
        or session["accessExpiresAtEpoch"] <= int(time.time())
    ):
        await _redis().delete(f"memory:bff:session:{session_binding}")
        return None
    return session


async def store_validated_candidate_for_binding(
    *,
    session_binding: str,
    preference: dict[str, str],
    display_text: str,
    candidate_id: str | None = None,
) -> str | None:
    """Persist a card only after the Java catalog authority accepted it."""
    if await session_for_binding(session_binding) is None:
        return None
    candidate_id = candidate_id or secrets.token_urlsafe(24)
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", candidate_id):
        return None
    candidate = {
        "sessionBinding": session_binding,
        "preference": preference,
        "displayText": display_text[:256],
        "status": "pending",
    }
    created = await _redis().set(
        _candidate_key(candidate_id),
        json.dumps(candidate, ensure_ascii=True, separators=(",", ":")),
        ex=settings.memory_candidate_ttl_seconds,
        nx=True,
    )
    if not created:
        raw = await _redis().get(_candidate_key(candidate_id))
        try:
            existing = json.loads(raw) if raw is not None else None
        except (TypeError, ValueError, json.JSONDecodeError):
            existing = None
        immutable = {key: candidate[key] for key in (
            "sessionBinding", "preference", "displayText"
        )}
        existing_immutable = (
            {key: existing.get(key) for key in immutable}
            if type(existing) is dict else None
        )
        if (
            existing_immutable != immutable
            or existing.get("status") not in {"pending", "confirmed"}
            or set(existing) != set(candidate)
        ):
            raise RuntimeError("memory candidate idempotency conflict")
    index = f"memory:bff:candidate-index:{session_binding}"
    await _redis().zadd(index, {candidate_id: 0})
    await _redis().expire(index, settings.memory_candidate_ttl_seconds)
    return candidate_id


async def resolve_memory_run_for_browser_session(
    browser_session_id: str | None,
    *,
    category_id: str | None,
    recipient_scope: str | None,
    catalog_revision: str,
    task_id: str | None = None,
) -> MemoryRunResolution:
    """Resolve one fail-closed, immutable V3 binding for an Agent run.

    The bearer remains inside the BFF process and is converted directly into
    an opaque projection credential.  Missing, expired, disabled or broken
    memory must never fail the shopping turn.
    """
    started = time.perf_counter()
    safe_category = category_id if type(category_id) is str else "unknown"
    empty = empty_memory_run_binding(safe_category, catalog_revision)
    if settings.memory_bff_enabled is not True or settings.memory_projection_client_enabled is not True:
        return MemoryRunResolution(empty, _resolution_summary(
            reason="disabled", started=started, binding=empty, eligible=False,
            attempted=False, projection_outcome="not_attempted",
        ))
    if type(browser_session_id) is not str:
        return MemoryRunResolution(empty, _resolution_summary(
            reason="no_browser_session", started=started, binding=empty, eligible=False,
            attempted=False, projection_outcome="not_attempted",
        ))
    if type(category_id) is not str:
        return MemoryRunResolution(empty, _resolution_summary(
            reason="category_unavailable", started=started, binding=empty, eligible=False,
            attempted=False, projection_outcome="not_attempted",
        ))
    if recipient_scope != "self":
        return MemoryRunResolution(empty, _resolution_summary(
            reason="recipient_suppressed", started=started, binding=empty, eligible=False,
            attempted=False, projection_outcome="not_attempted",
        ))
    try:
        session = await _session(browser_session_id)
        credential = MemoryAccessCredential(session["accessToken"])
        projection = await MemoryProjectionV3Client(
            enabled=True,
            backend_url=settings.backend_base_url,
            timeout_seconds=settings.memory_projection_client_timeout_seconds,
        ).fetch(credential)
        binding = build_memory_run_binding(
            projection,
            category_id=category_id,
            catalog_revision=catalog_revision,
        )
        outcome = projection.reason.value
        mode_claim = (
            await _mark_memory_bound_task_non_durable(task_id)
            if binding.preferences else "same"
        )
        if binding.preferences and mode_claim not in {"claimed", "same"}:
            return MemoryRunResolution(empty, _resolution_summary(
                reason=(
                    "durable_task_suppressed"
                    if mode_claim == "conflict" else "durable_guard_unavailable"
                ), started=started,
                binding=empty, eligible=True, attempted=True,
                projection_outcome=outcome, truncated=projection.truncated,
            ))
        reason = f"projection_{outcome}"
        if outcome == "available":
            reason = "available_retained" if binding.preferences else "available_empty"
        return MemoryRunResolution(binding, _resolution_summary(
            reason=reason, started=started, binding=binding,
            eligible=True, attempted=True, projection_outcome=outcome,
            truncated=projection.truncated,
        ))
    except HTTPException as exc:
        reason = "session_rejected"
        outcome = "session_rejected"
        eligible = False
    except Exception:
        reason = "authority_exception"
        outcome = "authority_exception"
        eligible = True
        logger.warning("shopping memory projection unavailable; continuing without memory")
    return MemoryRunResolution(empty, _resolution_summary(
        reason=reason, started=started, binding=empty, eligible=eligible,
        attempted=True, projection_outcome=outcome,
    ))


async def memory_run_binding_for_browser_session(
    browser_session_id: str | None,
    *, category_id: str | None, recipient_scope: str | None,
    catalog_revision: str,
    task_id: str | None = None,
) -> MemoryRunBinding:
    return (await resolve_memory_run_for_browser_session(
        browser_session_id,
        category_id=category_id,
        recipient_scope=recipient_scope,
        catalog_revision=catalog_revision,
        task_id=task_id,
    )).binding


__all__ = [
    "router",
    "enqueue_validated_candidate",
    "session_for_binding",
    "store_validated_candidate_for_binding",
    "memory_run_binding_for_browser_session",
    "resolve_memory_run_for_browser_session",
    "MemoryRunResolution",
    "memory_bound_task_is_non_durable",
    "claim_task_durable_mode",
]
