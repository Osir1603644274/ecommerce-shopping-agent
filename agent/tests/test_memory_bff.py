import asyncio
import hashlib
import json
from functools import wraps
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import memory_bff


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    async def expire(self, key, seconds):
        return key in self.values or key in self.zsets

    async def zadd(self, key, values):
        self.zsets.setdefault(key, {}).update(values)
        return len(values)

    async def zrange(self, key, start, stop):
        values = sorted(self.zsets.get(key, {}))
        if stop == -1:
            return values[start:]
        return values[start : stop + 1]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def app_and_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(memory_bff, "_client", fake)
    monkeypatch.setattr(memory_bff.settings, "memory_bff_enabled", True)
    monkeypatch.setattr(memory_bff.settings, "memory_projection_client_enabled", True)
    monkeypatch.setattr(memory_bff.settings, "memory_bff_canary_usernames", "tester")
    app = FastAPI()
    app.include_router(memory_bff.router)
    return app, fake


@async_test
async def test_login_keeps_java_tokens_server_side_and_sets_httponly_cookie(
    app_and_redis, monkeypatch
):
    app, fake = app_and_redis

    async def java(method, path, **kwargs):
        assert method == "POST" and path == "/api/auth/login"
        return {
            "accessToken": "access-token",
            "refreshToken": "refresh-token",
            "tokenType": "Bearer",
            "accessExpiresInSeconds": 900,
            "refreshExpiresInSeconds": 2592000,
            "user": {"id": "owner-id", "username": "tester", "roles": ["USER"]},
        }

    monkeypatch.setattr(memory_bff, "_java", java)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/web-memory/login",
            headers={"Origin": "http://testserver"},
            json={"username": "tester", "password": "password-123"},
        )

    assert response.status_code == 200
    assert set(response.json()) == {"authenticated", "username", "csrfToken"}
    assert "access" not in response.text and "refresh" not in response.text
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert len(fake.values) == 1
    stored = json.loads(next(iter(fake.values.values())))
    assert stored["accessToken"] == "access-token"
    assert stored["refreshToken"] == "refresh-token"
    assert "owner-id" not in json.dumps(stored)


@async_test
async def test_confirmation_uses_only_server_stored_candidate_semantics(
    app_and_redis, monkeypatch
):
    app, fake = app_and_redis
    session_id = "session-secret"
    csrf = "csrf-secret"
    session_binding = digest(session_id)
    fake.values[memory_bff._session_key(session_id)] = json.dumps({
        "accessToken": "access-token",
        "refreshToken": "refresh-token",
        "username": "tester",
        "csrfDigest": digest(csrf),
        "sessionBinding": session_binding,
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })
    candidate_id = "opaque-candidate"
    preference = {
        "categoryId": "exercise-fitness-equipment",
        "preferenceKind": "prefer",
        "attributeKey": "type_of_fitness_equipment",
        "normalizedValue": "indoor-bike",
        "catalogRevision": "shopping-companion-9a8a2a1c13f",
        "recipientScope": "self",
        "source": "user_confirmed",
    }
    fake.values[memory_bff._candidate_key(candidate_id)] = json.dumps({
        "sessionBinding": session_binding,
        "preference": preference,
        "displayText": "记住我偏好室内单车",
        "status": "pending",
    })
    calls = []

    async def java(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/projection/v3"):
            return {"revision": 0, "entries": []}
        if path.endswith("/consents/v3"):
            return {"commandId": "command-1", "consentEventId": "event-1"}
        return {"entryId": "entry-1", "version": 1, "status": "ACTIVE"}

    monkeypatch.setattr(memory_bff, "_java", java)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(memory_bff.COOKIE_NAME, session_id)
        response = await client.post(
            f"/api/web-memory/candidates/{candidate_id}/decision",
            headers={"Origin": "http://testserver", memory_bff.CSRF_HEADER: csrf},
            json={"action": "confirm"},
        )

    assert response.status_code == 200
    assert response.json() == {"confirmed": True}
    assert [item[1] for item in calls] == [
        "/api/memory/projection/v3",
        "/api/memory/consents/v3",
        "/api/memory/commands/v3",
    ]
    assert calls[1][2]["body"]["preference"] == preference
    assert calls[2][2]["body"]["preference"] == preference
    saved = json.loads(fake.values[memory_bff._candidate_key(candidate_id)])
    assert saved["status"] == "confirmed"


@async_test
async def test_entry_view_correction_disable_and_forget_are_server_bound(
    app_and_redis, monkeypatch
):
    app, fake = app_and_redis
    session_id = "entry-session"
    csrf = "entry-csrf"
    session_binding = digest(session_id)
    fake.values[memory_bff._session_key(session_id)] = json.dumps({
        "accessToken": "access-token",
        "refreshToken": "refresh-token",
        "username": "tester",
        "csrfDigest": digest(csrf),
        "sessionBinding": session_binding,
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })
    entry = {
        "entryId": "secret-entry-id",
        "categoryId": "phone",
        "recipientScope": "self",
        "preferenceKind": "avoid",
        "attributeKey": "brand",
        "normalizedValue": "apple",
        "catalogRevision": memory_bff.settings.memory_active_catalog_revision,
        "version": 2,
        "status": "ACTIVE",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-20T00:00:00Z",
        "expiresAt": "2027-02-16T00:00:00Z",
        "supersedes": "old-entry",
        "chainVerified": True,
    }
    calls = []

    async def java(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/projection/v3"):
            return {"revision": 2, "truncated": False, "entries": [entry]}
        if path.endswith("/catalog/validate/v3"):
            return {"valid": True}
        if path.endswith("/consents/v3"):
            return {"commandId": "command", "consentEventId": "event"}
        return {"entryId": "next", "version": 3, "status": "ACTIVE"}

    monkeypatch.setattr(memory_bff, "_java", java)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(memory_bff.COOKIE_NAME, session_id)

        viewed = await client.get("/api/web-memory/entries")
        assert viewed.status_code == 200
        public = viewed.json()["entries"][0]
        assert public["source"] == "user_confirmed"
        assert public["confidence"] == 1.0
        assert public["createdAt"] == entry["createdAt"]
        assert "secret-entry-id" not in viewed.text

        corrected = await client.patch(
            "/api/web-memory/entries/" + public["memoryHandle"],
            headers={"Origin": "http://testserver", memory_bff.CSRF_HEADER: csrf},
            json={"preferenceKind": "prefer", "normalizedValue": "android"},
        )
        assert corrected.status_code == 200
        assert corrected.json() == {"updated": True}

        viewed = await client.get("/api/web-memory/entries")
        disabled = await client.post(
            "/api/web-memory/entries/"
            + viewed.json()["entries"][0]["memoryHandle"]
            + "/disable",
            headers={"Origin": "http://testserver", memory_bff.CSRF_HEADER: csrf},
        )
        assert disabled.status_code == 200
        assert disabled.json() == {"disabled": True}

        viewed = await client.get("/api/web-memory/entries")
        forgotten = await client.delete(
            "/api/web-memory/entries/"
            + viewed.json()["entries"][0]["memoryHandle"],
            headers={"Origin": "http://testserver", memory_bff.CSRF_HEADER: csrf},
        )
        assert forgotten.status_code == 200
        assert forgotten.json() == {"revoked": True}

    mutation_calls = [item for item in calls if "/projection/" not in item[1]]
    assert [item[1] for item in mutation_calls] == [
        "/api/memory/catalog/validate/v3",
        "/api/memory/consents/v3",
        "/api/memory/commands/v3",
        "/api/memory/consents/v3",
        "/api/memory/commands/v3",
        "/api/memory/consents/v3",
        "/api/memory/commands/v3",
    ]
    consent_operations = [
        item[2]["body"]["operation"]
        for item in mutation_calls if item[1].endswith("/consents/v3")
    ]
    assert consent_operations == ["update", "suppress", "revoke"]
    correction = mutation_calls[0][2]["body"]
    assert correction["categoryId"] == "phone"
    assert correction["attributeKey"] == "brand"
    assert correction["normalizedValue"] == "android"


@async_test
async def test_bad_csrf_and_cross_session_candidate_fail_before_java(
    app_and_redis, monkeypatch
):
    app, fake = app_and_redis
    session_id = "session-a"
    fake.values[memory_bff._session_key(session_id)] = json.dumps({
        "accessToken": "access-token",
        "refreshToken": "refresh-token",
        "username": "tester",
        "csrfDigest": digest("right-csrf"),
        "sessionBinding": digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })
    fake.values[memory_bff._candidate_key("candidate-b")] = json.dumps({
        "sessionBinding": digest("session-b"),
        "preference": {},
        "displayText": "other",
        "status": "pending",
    })

    async def java(*args, **kwargs):
        raise AssertionError("java must not be called")

    monkeypatch.setattr(memory_bff, "_java", java)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(memory_bff.COOKIE_NAME, session_id)
        bad_csrf = await client.post(
            "/api/web-memory/candidates/candidate-b/decision",
            headers={"Origin": "http://testserver", memory_bff.CSRF_HEADER: "wrong"},
            json={"action": "confirm"},
        )
        cross_session = await client.post(
            "/api/web-memory/candidates/candidate-b/decision",
            headers={"Origin": "http://testserver", memory_bff.CSRF_HEADER: "right-csrf"},
            json={"action": "confirm"},
        )

    assert bad_csrf.status_code == 403
    assert cross_session.status_code == 404


@async_test
async def test_run_binding_uses_server_session_and_fails_closed(
    app_and_redis, monkeypatch
):
    _app, fake = app_and_redis
    session_id = "session-for-agent"
    fake.values[memory_bff._session_key(session_id)] = json.dumps({
        "accessToken": "server-only-token",
        "refreshToken": "refresh-token",
        "username": "tester",
        "csrfDigest": digest("csrf"),
        "sessionBinding": digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })
    captured = {}

    class ProjectionClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def fetch(self, credential):
            captured["credential"] = repr(credential)
            return type("Projection", (), {
                "reason": type("Reason", (), {"value": "available"})(),
                "truncated": False,
            })()

    sentinel = memory_bff.empty_memory_run_binding(
        "phone", memory_bff.settings.memory_active_catalog_revision
    )

    def build(projection, *, category_id, catalog_revision):
        captured["projection"] = projection
        captured["category"] = category_id
        captured["catalog_revision"] = catalog_revision
        return sentinel

    monkeypatch.setattr(memory_bff, "MemoryProjectionV3Client", ProjectionClient)
    monkeypatch.setattr(memory_bff, "build_memory_run_binding", build)
    binding = await memory_bff.memory_run_binding_for_browser_session(
        session_id, category_id="phone", recipient_scope="self",
        catalog_revision=memory_bff.settings.memory_active_catalog_revision,
    )
    assert binding is sentinel
    assert captured == {
        "client": {
            "enabled": True,
            "backend_url": memory_bff.settings.backend_base_url,
            "timeout_seconds": memory_bff.settings.memory_projection_client_timeout_seconds,
        },
        "credential": "MemoryAccessCredential(<opaque>)",
        "projection": captured["projection"],
        "category": "phone",
        "catalog_revision": memory_bff.settings.memory_active_catalog_revision,
    }

    fake.values.clear()
    empty = await memory_bff.memory_run_binding_for_browser_session(
        session_id, category_id="phone", recipient_scope="self",
        catalog_revision=memory_bff.settings.memory_active_catalog_revision,
    )
    assert empty.payload_for_phase("planner") is None

    unknown_recipient = await memory_bff.memory_run_binding_for_browser_session(
        session_id, category_id="phone", recipient_scope=None,
        catalog_revision=memory_bff.settings.memory_active_catalog_revision,
    )
    assert unknown_recipient.payload_for_phase("planner") is None


@async_test
async def test_run_resolution_exposes_only_bounded_observability(
    app_and_redis, monkeypatch
):
    _app, fake = app_and_redis
    session_id = "observable-session"
    fake.values[memory_bff._session_key(session_id)] = json.dumps({
        "accessToken": "server-only-token",
        "refreshToken": "server-only-refresh",
        "username": "tester",
        "csrfDigest": digest("csrf"),
        "sessionBinding": digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })

    class Projection:
        reason = type("Reason", (), {"value": "available"})()
        truncated = False

    class ProjectionClient:
        def __init__(self, **_kwargs):
            pass

        async def fetch(self, _credential):
            return Projection()

    binding = memory_bff.empty_memory_run_binding(
        "phone", memory_bff.settings.memory_active_catalog_revision
    )
    monkeypatch.setattr(memory_bff, "MemoryProjectionV3Client", ProjectionClient)
    monkeypatch.setattr(memory_bff, "build_memory_run_binding", lambda *_args, **_kwargs: binding)

    resolution = await memory_bff.resolve_memory_run_for_browser_session(
        session_id,
        category_id="phone",
        recipient_scope="self",
        catalog_revision=memory_bff.settings.memory_active_catalog_revision,
    )

    assert set(resolution.summary) == {
        "reason", "durationMs", "eligible", "attempted", "projectionOutcome",
        "retainedCount", "truncated",
    }
    assert resolution.summary["reason"] == "available_empty"
    assert resolution.summary["eligible"] is True
    assert resolution.summary["attempted"] is True
    serialized = json.dumps(resolution.summary)
    for forbidden in (
        "server-only-token", "server-only-refresh", "tester", session_id,
        memory_bff.settings.memory_active_catalog_revision,
    ):
        assert forbidden not in serialized


@async_test
async def test_retained_binding_marks_task_non_durable_and_guard_failure_suppresses(
    app_and_redis, monkeypatch
):
    _app, fake = app_and_redis
    task_redis = FakeRedis()
    session_id = "guarded-session"
    fake.values[memory_bff._session_key(session_id)] = json.dumps({
        "accessToken": "server-only-token",
        "refreshToken": "server-only-refresh",
        "username": "tester",
        "csrfDigest": digest("csrf"),
        "sessionBinding": digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })

    class ProjectionClient:
        def __init__(self, **_kwargs):
            pass

        async def fetch(self, _credential):
            return SimpleNamespace(
                reason=SimpleNamespace(value="available"), truncated=False,
            )

    retained = SimpleNamespace(preferences=("preference",))
    monkeypatch.setattr(memory_bff, "MemoryProjectionV3Client", ProjectionClient)
    monkeypatch.setattr(memory_bff, "build_memory_run_binding", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(memory_bff, "_get_task_state_redis", lambda: task_redis)

    resolution = await memory_bff.resolve_memory_run_for_browser_session(
        session_id, category_id="phone", recipient_scope="self",
        catalog_revision=memory_bff.settings.memory_active_catalog_revision,
        task_id="task-memory",
    )
    assert resolution.binding is retained
    assert resolution.summary["reason"] == "available_retained"
    assert await memory_bff.memory_bound_task_is_non_durable("task-memory") is True
    assert await memory_bff.claim_task_durable_mode("task-memory") is False

    durable_first = FakeRedis()
    monkeypatch.setattr(memory_bff, "_get_task_state_redis", lambda: durable_first)
    assert await memory_bff.claim_task_durable_mode("task-durable-first") is True
    conflicted = await memory_bff.resolve_memory_run_for_browser_session(
        session_id, category_id="phone", recipient_scope="self",
        catalog_revision=memory_bff.settings.memory_active_catalog_revision,
        task_id="task-durable-first",
    )
    assert conflicted.summary["reason"] == "durable_task_suppressed"
    assert conflicted.binding.payload_for_phase("planner") is None

    class BrokenRedis(FakeRedis):
        async def set(self, *args, **kwargs):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(memory_bff, "_get_task_state_redis", lambda: BrokenRedis())
    suppressed = await memory_bff.resolve_memory_run_for_browser_session(
        session_id, category_id="phone", recipient_scope="self",
        catalog_revision=memory_bff.settings.memory_active_catalog_revision,
        task_id="task-memory-2",
    )
    assert suppressed.summary["reason"] == "durable_guard_unavailable"
    assert suppressed.binding.payload_for_phase("planner") is None


@async_test
async def test_task_mode_claim_is_atomic_and_sticky(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(memory_bff, "_get_task_state_redis", lambda: fake)
    memory_claim, durable_claim = await asyncio.gather(
        memory_bff._mark_memory_bound_task_non_durable("task-race"),
        memory_bff.claim_task_durable_mode("task-race"),
    )
    assert (memory_claim, durable_claim) in {
        ("claimed", False), ("conflict", True),
    }
    mode = fake.values[memory_bff._task_mode_key("task-race")]
    assert mode in {"memory_non_durable", "durable"}
    assert await memory_bff._claim_task_mode("task-race", mode) == "same"
    opposite = "durable" if mode == "memory_non_durable" else "memory_non_durable"
    assert await memory_bff._claim_task_mode("task-race", opposite) == "conflict"


@async_test
async def test_candidate_store_is_idempotent_for_worker_retry(app_and_redis):
    _app, fake = app_and_redis
    session_binding = digest("worker-session")
    fake.values[f"memory:bff:session:{session_binding}"] = json.dumps({
        "accessToken": "token",
        "refreshToken": "refresh",
        "username": "tester",
        "csrfDigest": digest("csrf"),
        "sessionBinding": session_binding,
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })
    preference = {
        "categoryId": "phone", "preferenceKind": "avoid",
        "attributeKey": "brand", "normalizedValue": "apple",
        "catalogRevision": memory_bff.settings.memory_active_catalog_revision,
        "recipientScope": "self", "source": "user_confirmed",
    }
    first = await memory_bff.store_validated_candidate_for_binding(
        session_binding=session_binding, preference=preference,
        display_text="仅用于你本人：是否记住？", candidate_id="stable_candidate_123456",
    )
    candidate_key = memory_bff._candidate_key("stable_candidate_123456")
    confirmed = json.loads(fake.values[candidate_key])
    confirmed["status"] = "confirmed"
    fake.values[candidate_key] = json.dumps(confirmed)
    second = await memory_bff.store_validated_candidate_for_binding(
        session_binding=session_binding, preference=preference,
        display_text="仅用于你本人：是否记住？", candidate_id="stable_candidate_123456",
    )
    assert first == second == "stable_candidate_123456"
    assert list(fake.zsets[f"memory:bff:candidate-index:{session_binding}"]) == [
        "stable_candidate_123456"
    ]


@async_test
async def test_existing_session_is_revoked_when_flag_or_allowlist_changes(
    app_and_redis, monkeypatch
):
    app, fake = app_and_redis
    session_id = "revocable-session"
    envelope = json.dumps({
        "accessToken": "access-token",
        "refreshToken": "refresh-token",
        "username": "tester",
        "csrfDigest": digest("csrf"),
        "sessionBinding": digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })
    fake.values[memory_bff._session_key(session_id)] = envelope
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(memory_bff.COOKIE_NAME, session_id)
        monkeypatch.setattr(memory_bff.settings, "memory_projection_client_enabled", False)
        disabled = await client.get("/api/web-memory/me")
        assert disabled.status_code == 404
        assert memory_bff._session_key(session_id) not in fake.values

        monkeypatch.setattr(memory_bff.settings, "memory_projection_client_enabled", True)
        fake.values[memory_bff._session_key(session_id)] = envelope
        monkeypatch.setattr(memory_bff.settings, "memory_bff_canary_usernames", "other")
        revoked = await client.get("/api/web-memory/me")
        assert revoked.status_code == 403

    assert memory_bff._session_key(session_id) not in fake.values


@async_test
async def test_expired_access_token_deletes_bff_session(app_and_redis, monkeypatch):
    app, fake = app_and_redis
    session_id = "expired-session"
    fake.values[memory_bff._session_key(session_id)] = json.dumps({
        "accessToken": "access-token",
        "refreshToken": "refresh-token",
        "username": "tester",
        "csrfDigest": digest("csrf"),
        "sessionBinding": digest(session_id),
        "accessExpiresAtEpoch": 100,
        "canaryEpoch": memory_bff.settings.memory_bff_epoch,
    })
    monkeypatch.setattr(memory_bff.time, "time", lambda: 101)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(memory_bff.COOKIE_NAME, session_id)
        response = await client.get("/api/web-memory/me")
    assert response.status_code == 401
    assert memory_bff._session_key(session_id) not in fake.values


@async_test
async def test_canary_epoch_change_invalidates_old_session(app_and_redis, monkeypatch):
    app, fake = app_and_redis
    session_id = "old-epoch-session"
    fake.values[memory_bff._session_key(session_id)] = json.dumps({
        "accessToken": "access-token",
        "refreshToken": "refresh-token",
        "username": "tester",
        "csrfDigest": digest("csrf"),
        "sessionBinding": digest(session_id),
        "accessExpiresAtEpoch": 4102444800,
        "canaryEpoch": "old-epoch",
    })
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(memory_bff.COOKIE_NAME, session_id)
        response = await client.get("/api/web-memory/me")
    assert response.status_code == 401
    assert memory_bff._session_key(session_id) not in fake.values
