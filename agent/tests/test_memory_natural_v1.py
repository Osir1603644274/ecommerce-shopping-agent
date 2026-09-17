import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app import memory_candidate_worker as worker
from app.api import memory_bff
from app.memory import durable_snapshot as durable
from app.memory.v3_runtime import build_memory_run_binding, empty_memory_run_binding
from tests.test_memory_bff import FakeRedis, app_and_redis, async_test, digest
from tests.test_memory_v3_runtime import issued_projection, CATALOG_REVISION


def client_for(content):
    async def create(**kwargs):
        assert "evidenceQuote" in kwargs["messages"][0]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(content, ensure_ascii=False)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.parametrize("change,outcome", [
    ({}, "accepted"),
    ({"evidenceQuote": "我只买苹果"}, "ungrounded_evidence"),
    ({"attributeKey": []}, "invalid_model_output"),
    ({"normalizedValue": "unknown"}, "catalog_escape_rejected"),
    ({"consent": True}, "invalid_model_output"),
])
def test_natural_source_and_schema(monkeypatch, change, outcome):
    message = "买手机我一直喜欢苹果"
    row = dict(categoryId="phone", attributeKey="brand", normalizedValue="apple",
               displayLabel="苹果", catalogRevision="rev")
    monkeypatch.setattr(worker, "_catalog", lambda: (row,))
    item = dict(preferenceKind="prefer", attributeKey="brand", normalizedValue="apple",
                evidenceQuote=message, **{})
    item.update(change)
    result, receipt = asyncio.run(worker._extract_observed(
        message, "phone", "self", strategy="natural_v1", message_id="message-1",
        client=client_for({"preferences": [item]}), model="test-model"))
    assert receipt.outcome == outcome
    assert receipt.model_called and receipt.total_tokens == 120
    assert bool(result) == (outcome == "accepted")
    if result:
        assert result[0]["messageId"] == "message-1"


@pytest.mark.parametrize("message,scope,outcome", [
    ("我喜欢苹果", "self", "rule_not_explicit"),
    ("请记住我朋友喜欢苹果", "self", "recipient_suppressed"),
    ("请记住我喜欢苹果", "other", "recipient_suppressed"),
    ("请记住我的手机号13800138000", "self", "sensitive_suppressed"),
])
def test_explicit_and_safety_gates_stay_zero_model(message, scope, outcome):
    result, receipt = asyncio.run(worker._extract_observed(message, "phone", scope))
    assert not result and not receipt.model_called and receipt.outcome == outcome


def test_default_rollout_flags_are_false():
    from app.settings import Settings
    assert Settings.model_fields["memory_natural_candidates_enabled"].default is False
    assert Settings.model_fields["memory_durable_snapshot_enabled"].default is False


def preference():
    return dict(categoryId="phone", preferenceKind="prefer", attributeKey="brand",
                normalizedValue="apple", catalogRevision="rev", recipientScope="self",
                source="user_confirmed")


def add_session(fake, browser="browser-a", username="tester"):
    import time
    binding = digest(browser)
    session = dict(sessionBinding=binding, username=username, accessToken="test-access",
                   refreshToken="test-refresh", csrfDigest=digest("csrf"),
                   accessExpiresAtEpoch=int(time.time()) + 600,
                   canaryEpoch=memory_bff.settings.memory_bff_epoch)
    fake.values[memory_bff._session_key(browser)] = json.dumps(session)
    return binding


@async_test
async def test_semantic_dedupe_and_rejection_do_not_write_java(app_and_redis, monkeypatch):
    app, fake = app_and_redis
    binding = add_session(fake)
    evidence = dict(strategy="natural_v1", messageId="message-1", evidenceQuote="我一直喜欢苹果")
    first = await memory_bff.store_validated_candidate_for_binding(
        session_binding=binding, preference=preference(), display_text="是否保存？",
        candidate_id="candidate_first_12345", evidence=evidence)
    second = await memory_bff.store_validated_candidate_for_binding(
        session_binding=binding, preference=preference(), display_text="同义表达",
        candidate_id="candidate_second_12345", evidence={**evidence, "messageId": "message-2"})
    assert first and second == ""
    async def no_java(*args, **kwargs):
        raise AssertionError("dismissal cannot grant consent or revoke memory")
    monkeypatch.setattr(memory_bff, "_java", no_java)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                cookies={memory_bff.COOKIE_NAME: "browser-a"}) as client:
        cards = (await client.get("/api/web-memory/candidates")).json()["candidates"]
        assert cards[0]["evidenceQuote"] == evidence["evidenceQuote"]
        assert cards[0]["messageId"] == "message-1"
        for _ in range(2):
            response = await client.post(f"/api/web-memory/candidates/{first}/decision",
                json={"action": "reject"}, headers={"Origin": "http://test", "X-CSRF-Token": "csrf"})
            assert response.status_code == 200
            assert response.json() == {"confirmed": False, "status": "rejected"}
        response = await client.post(f"/api/web-memory/candidates/{first}/decision",
            json={"action": "confirm"}, headers={"Origin": "http://test", "X-CSRF-Token": "csrf"})
        assert response.status_code == 409
    # Simulate short-lived dedupe expiration, retaining the longer rejection cooldown.
    for key in list(fake.values):
        if ":pending:" in key:
            del fake.values[key]
    other_session = add_session(fake, "browser-b")
    suppressed = await memory_bff.store_validated_candidate_for_binding(
        session_binding=other_session, preference=preference(), display_text="再次建议",
        candidate_id="candidate_third_12345", evidence=evidence)
    assert suppressed == ""
    explicit = await memory_bff.store_validated_candidate_for_binding(
        session_binding=other_session, preference=preference(), display_text="明确要求记住",
        candidate_id="candidate_fourth_12345", evidence=evidence, explicit_request=True)
    assert explicit == "candidate_fourth_12345"


def test_durable_resume_requires_current_authority_and_same_snapshot(monkeypatch):
    projection = issued_projection(monkeypatch)
    async def scenario():
        for name in ("memory_durable_snapshot_enabled", "memory_bff_enabled",
                     "memory_projection_client_enabled"):
            monkeypatch.setattr(durable.settings, name, True)
        monkeypatch.setattr(durable.settings, "memory_active_catalog_revision", CATALOG_REVISION)
        binding = build_memory_run_binding(projection, category_id="phone", catalog_revision=CATALOG_REVISION)
        assert binding.preferences
        current = [binding, projection.owner_binding]
        async def refresh(): return tuple(current)
        durable.register_authority(binding, projection, "b" * 64, refresh)
        fake = FakeRedis()
        guard = await durable.prepare_memory_guard(redis=fake, binding=binding,
            task_id="task-1", run_id="run-1", session_id="session-1", resuming=False)
        await guard()
        # Process-local issuance is recreated from the authoritative projection;
        # no snapshot JSON is deserialized into a trusted binding.
        newer = build_memory_run_binding(projection, category_id="phone", catalog_revision=CATALOG_REVISION)
        durable.register_authority(newer, projection, "b" * 64, refresh)
        restored = await durable.prepare_memory_guard(redis=fake, binding=newer,
            task_id="task-1", run_id="run-1", session_id="session-1", resuming=True)
        await restored()
        with pytest.raises(durable.MemorySnapshotRejected, match="identity"):
            await restored("task-2", "run-1", "owner")
        with pytest.raises(durable.MemorySnapshotRejected, match="identity_or_revision"):
            await durable.prepare_memory_guard(redis=fake, binding=newer,
                task_id="task-1", run_id="run-1", session_id="other-session", resuming=True)
        current[0] = empty_memory_run_binding("phone", CATALOG_REVISION)
        with pytest.raises(durable.MemorySnapshotRejected, match="changed_during_run"):
            await guard()
        serialized = "".join(fake.values.values())
        assert "accessToken" not in serialized and "preferences" not in serialized
    asyncio.run(scenario())


def test_missing_durable_snapshot_is_not_recreated_on_resume(monkeypatch):
    async def scenario():
        monkeypatch.setattr(durable.settings, "memory_durable_snapshot_enabled", True)
        with pytest.raises(durable.MemorySnapshotRejected, match="snapshot_missing"):
            await durable.prepare_memory_guard(redis=FakeRedis(), binding=None,
                task_id="task-1", run_id="run-1", session_id="session-1", resuming=True)
    asyncio.run(scenario())


def test_decision_memory_is_soft_hash_bound_and_current_requirement_wins(monkeypatch):
    from tests.test_react_context import _state
    from app.control.react_context import build_decision_context_view
    projection = issued_projection(monkeypatch)
    binding = build_memory_run_binding(projection, category_id="phone", catalog_revision=CATALOG_REVISION)
    state = _state()
    original = state.model_dump(mode="json")
    base = build_decision_context_view(state, user_message="有什么建议？", allowed_tool_names=["search_products"])
    with_memory = build_decision_context_view(state, user_message="有什么建议？",
        allowed_tool_names=["search_products"], memory_run_binding=binding)
    assert {row["attributeKey"] for row in with_memory.long_term_memory} == {"brand", "os"}
    assert with_memory.decision_view_hash != base.decision_view_hash
    assert with_memory.allowed_action_options == base.allowed_action_options
    assert with_memory.requirements == base.requirements
    assert state.model_dump(mode="json") == original
    state.domain_state["shoppingGuide"]["requirements"].append(dict(
        key="os", operator="eq", value="ios", unit="enum", priority="hard", source="user"))
    overridden = build_decision_context_view(state, user_message="这次只考虑iOS",
        allowed_tool_names=["search_products"], memory_run_binding=binding)
    assert all(row["attributeKey"] != "os" for row in overridden.long_term_memory)
    with pytest.raises(ValueError, match="unissued"):
        build_decision_context_view(state, user_message="问题", allowed_tool_names=[],
                                    memory_run_binding={"preferences": []})


def test_revocation_guard_runs_before_node_effect(monkeypatch):
    from app.graph import builder
    async def scenario():
        effects = []
        async def no_pause(**kwargs): return None
        async def rejected(*args): raise durable.MemorySnapshotRejected("revoked")
        async def node(*args): effects.append("effect")
        monkeypatch.setattr(builder, "matching_pause_request", no_pause)
        runtime = SimpleNamespace(context=SimpleNamespace(memory_guard=rejected,
            task_id="task-1", run_id="run-1", session_owner_hash="hash"))
        with pytest.raises(durable.MemorySnapshotRejected, match="revoked"):
            await builder._pause_guard("executor", node)({}, runtime)
        assert effects == []
    asyncio.run(scenario())


def test_enabled_bff_issues_durable_authority_without_legacy_marker(monkeypatch):
    projection = issued_projection(monkeypatch)
    async def scenario():
        fake = FakeRedis()
        add_session(fake)
        for name in ("memory_bff_enabled", "memory_projection_client_enabled", "memory_durable_snapshot_enabled"):
            monkeypatch.setattr(memory_bff.settings, name, True)
        monkeypatch.setattr(memory_bff.settings, "memory_bff_canary_usernames", "tester")
        monkeypatch.setattr(memory_bff.settings, "memory_active_catalog_revision", CATALOG_REVISION)
        monkeypatch.setattr(memory_bff, "_client", fake)
        monkeypatch.setattr(memory_bff, "_get_task_state_redis", lambda: fake)
        class ProjectionClient:
            def __init__(self, **kwargs): pass
            async def fetch(self, credential): return projection
        monkeypatch.setattr(memory_bff, "MemoryProjectionV3Client", ProjectionClient)
        resolved = await memory_bff.resolve_memory_run_for_browser_session("browser-a",
            category_id="phone", recipient_scope="self", catalog_revision=CATALOG_REVISION, task_id="task-1")
        assert resolved.summary["reason"] == "available_retained"
        assert durable.has_durable_authority(resolved.binding)
        assert await memory_bff.claim_task_durable_mode("task-1")
        assert not await memory_bff.memory_bound_task_is_non_durable("task-1")
        guard = await durable.prepare_memory_guard(redis=fake, binding=resolved.binding,
            task_id="task-1", run_id="run-1", session_id="chat-1", resuming=False)
        await guard()
        await fake.delete(memory_bff._session_key("browser-a"))
        with pytest.raises(durable.MemorySnapshotRejected, match="authority_unavailable"):
            await guard()
    asyncio.run(scenario())
