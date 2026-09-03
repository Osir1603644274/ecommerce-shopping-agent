import asyncio
import hashlib
import json
from types import SimpleNamespace

from app import memory_candidate_worker as worker


def test_catalog_is_hash_revision_and_identity_bound(tmp_path, monkeypatch):
    rows = [
        {
            "attributeKey": "brand",
            "catalogRevision": "catalog-rev",
            "categoryId": "phone",
            "displayLabel": "苹果",
            "normalizedValue": "apple",
        }
    ]
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")
    path = tmp_path / "catalog.jsonl"
    path.write_bytes(payload)
    monkeypatch.setattr(worker.settings, "memory_catalog_values_path", str(path))
    monkeypatch.setattr(worker.settings, "memory_catalog_values_sha256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(worker.settings, "memory_active_catalog_revision", "catalog-rev")
    worker._catalog.cache_clear()
    assert worker._catalog() == tuple(rows)

    monkeypatch.setattr(worker.settings, "memory_catalog_values_sha256", "0" * 64)
    worker._catalog.cache_clear()
    try:
        worker._catalog()
        raise AssertionError("hash mismatch must fail closed")
    except ValueError as exc:
        assert str(exc) == "memory catalog hash mismatch"
    finally:
        worker._catalog.cache_clear()


def test_shortlist_keeps_nonexact_second_preference(monkeypatch):
    rows = (
        {
            "attributeKey": "battery_health", "catalogRevision": "rev",
            "categoryId": "phone", "displayLabel": "电池健康 90%+",
            "normalizedValue": "90_plus",
        },
        {
            "attributeKey": "scratch_level", "catalogRevision": "rev",
            "categoryId": "phone", "displayLabel": "无划痕",
            "normalizedValue": "none",
        },
        {
            "attributeKey": "brand", "catalogRevision": "rev",
            "categoryId": "phone", "displayLabel": "苹果",
            "normalizedValue": "apple",
        },
    )
    monkeypatch.setattr(worker, "_catalog", lambda: rows)
    shortlist = worker._shortlist("电池健康90%以上，而且无划痕", "phone")
    identities = {
        (row["attributeKey"], row["normalizedValue"]) for row in shortlist
    }
    assert ("battery_health", "90_plus") in identities
    assert ("scratch_level", "none") in identities


def test_recipient_rule_only_allows_explicit_self_requests():
    assert worker.explicit_memory_recipient_scope(
        "请记住我喜欢安卓手机"
    ) == "self"
    assert worker.explicit_memory_recipient_scope(
        "请记住我只喜欢安卓手机"
    ) == "self"
    assert worker.explicit_memory_recipient_scope(
        "请记住我妈妈喜欢大屏手机"
    ) == "other"
    assert worker.explicit_memory_recipient_scope(
        "以后买手机不要曲面屏"
    ) is None
    assert worker.explicit_memory_recipient_scope(
        "给我看看适合妈妈的手机，记住她喜欢大屏"
    ) == "other"
    assert worker.explicit_memory_recipient_scope(
        "请记住：我自己不在意品牌，但我老婆喜欢苹果，给她买手机"
    ) == "other"
    assert worker.explicit_memory_recipient_scope(
        "请记住我同事喜欢小屏"
    ) == "other"
    assert worker.current_request_recipient_scope(
        "我自己买手机，但也在比较老婆喜欢的苹果"
    ) == "other"


def test_enqueue_failure_never_raises_into_completed_answer(monkeypatch):
    monkeypatch.setattr(worker.settings, "memory_bff_enabled", True)
    monkeypatch.setattr(worker.settings, "memory_projection_client_enabled", True)

    async def unavailable(_binding):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(worker.memory_bff, "session_for_binding", unavailable)
    result = asyncio.run(worker.enqueue_memory_extraction(
        browser_session_id="browser-session",
        user_message="请记住我喜欢安卓手机",
        category_id="phone",
        recipient_scope="self",
    ))
    assert result is None

    suppressed = asyncio.run(worker.enqueue_memory_extraction(
        browser_session_id="browser-session",
        user_message="请记住我喜欢安卓手机",
        category_id="phone",
        recipient_scope="other",
    ))
    assert suppressed is None

    sensitive = asyncio.run(worker.enqueue_memory_extraction(
        browser_session_id="browser-session",
        user_message="请记住，我喜欢把手机号13800138000作为偏好。",
        category_id="phone",
        recipient_scope="self",
    ))
    assert sensitive is None


def test_extraction_rules_fail_closed_before_model(monkeypatch):
    def forbidden_client():
        raise AssertionError("suppressed input must not reach the model")

    monkeypatch.setattr(worker, "_memory_extraction_client", forbidden_client)
    assert worker.is_sensitive_memory_request("请记住手机号13800138000")
    assert worker.is_sensitive_memory_request("请记住，我喜欢13800138000")
    assert worker.is_sensitive_memory_request("请记住，我喜欢ming@example.com")
    assert worker.is_sensitive_memory_request(
        "请记住，我喜欢sk-abcdefghijklmnop1234"
    )

    sensitive, sensitive_observation = asyncio.run(worker._extract_observed(
        "请记住，我喜欢把手机号13800138000作为偏好。", "phone", "self"
    ))
    assert sensitive == []
    assert sensitive_observation.outcome == "sensitive_suppressed"
    assert not sensitive_observation.model_called

    implicit, implicit_observation = asyncio.run(worker._extract_observed(
        "我喜欢华为手机。", "phone", "self"
    ))
    assert implicit == []
    assert implicit_observation.outcome == "rule_not_explicit"
    assert not implicit_observation.model_called

    recipient, recipient_observation = asyncio.run(worker._extract_observed(
        "请记住，我喜欢安卓，但这次给妈妈买苹果。", "phone", "self"
    ))
    assert recipient == []
    assert recipient_observation.outcome == "recipient_suppressed"
    assert not recipient_observation.model_called


def test_model_exception_retains_attempt_observation(monkeypatch):
    option = {
        "attributeKey": "brand",
        "catalogRevision": "catalog-rev",
        "categoryId": "phone",
        "displayLabel": "苹果",
        "normalizedValue": "apple",
    }

    class Completions:
        async def create(self, **_kwargs):
            raise TimeoutError("upstream timeout")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    monkeypatch.setattr(worker, "_shortlist", lambda *_args: [option])
    monkeypatch.setattr(worker, "_memory_extraction_client", lambda: client)
    monkeypatch.setattr(worker.settings, "deepseek_api_key", "test-key")

    try:
        asyncio.run(worker._extract_observed(
            "以后买手机请记住，我不喜欢苹果。", "phone", "self"
        ))
        raise AssertionError("model failure must remain observable")
    except worker.MemoryExtractionFailure as exc:
        assert exc.cause_type == "TimeoutError"
        assert exc.observation.model_called
        assert exc.observation.outcome == "model_exception"
        assert not exc.observation.usage_observed
        assert exc.observation.duration_ms >= 0


def test_success_without_usage_is_marked_unaccounted(monkeypatch):
    option = {
        "attributeKey": "brand",
        "catalogRevision": "catalog-rev",
        "categoryId": "phone",
        "displayLabel": "苹果",
        "normalizedValue": "apple",
    }
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "preferences": [{
                "preferenceKind": "avoid",
                "attributeKey": "brand",
                "normalizedValue": "apple",
            }]
        })))],
        usage=None,
    )

    class Completions:
        async def create(self, **_kwargs):
            return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(worker, "_shortlist", lambda *_args: [option])
    monkeypatch.setattr(worker, "_memory_extraction_client", lambda: client)
    monkeypatch.setattr(worker.settings, "deepseek_api_key", "test-key")

    extracted, observed = asyncio.run(worker._extract_observed(
        "以后买手机请记住，我不喜欢苹果。", "phone", "self"
    ))
    assert extracted[0]["attributeKey"] == "brand"
    assert observed.model_called
    assert observed.outcome == "accepted"
    assert not observed.usage_observed
    assert observed.total_tokens == 0


def test_ambiguous_shared_value_requires_attribute_grounding(monkeypatch):
    options = [
        {
            "attributeKey": "battery_originality",
            "catalogRevision": "catalog-rev",
            "categoryId": "phone",
            "displayLabel": "原装",
            "normalizedValue": "original",
        },
        {
            "attributeKey": "screen_originality",
            "catalogRevision": "catalog-rev",
            "categoryId": "phone",
            "displayLabel": "原装",
            "normalizedValue": "original",
        },
    ]
    def forbidden_client():
        raise AssertionError("ambiguous input must not reach the model")

    monkeypatch.setattr(worker, "_shortlist", lambda *_args: options)
    monkeypatch.setattr(worker, "_memory_extraction_client", forbidden_client)
    monkeypatch.setattr(worker.settings, "deepseek_api_key", "test-key")

    extracted, observed = asyncio.run(worker._extract_observed(
        "请记住，我喜欢原装。", "phone", "self"
    ))
    assert extracted == []
    assert observed.outcome == "ambiguous_input_suppressed"
    assert not observed.model_called
    assert not observed.usage_observed
    assert worker._ambiguous_attribute_is_grounded(
        "请记住，我喜欢原装屏幕。", options[1], options
    )
    assert not worker._ambiguous_attribute_is_grounded(
        "请记住，我喜欢原装屏幕。", options[0], options
    )


def test_schedule_returns_before_async_enqueue_finishes(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed(**_kwargs):
        started.set()
        await release.wait()
        return "job"

    monkeypatch.setattr(worker, "enqueue_memory_extraction", delayed)

    async def scenario():
        result = worker.schedule_memory_extraction(
            browser_session_id="browser-session",
            user_message="请记住我喜欢安卓手机",
            category_id="phone",
            recipient_scope="self",
        )
        assert result is None
        assert not started.is_set()
        await started.wait()
        assert len(worker._ENQUEUE_TASKS) == 1
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not worker._ENQUEUE_TASKS

    asyncio.run(scenario())


def test_stream_ack_happens_only_after_success(monkeypatch):
    calls = []

    class StreamRedis:
        async def xack(self, stream, group, message_id):
            calls.append((stream, group, message_id))

    async def process(fields):
        if fields["kind"] == "transient":
            raise RuntimeError("authority unavailable")

    monkeypatch.setattr(worker, "_process", process)
    asyncio.run(worker._consume_messages(
        StreamRedis(), "consumer", [
            ("1-0", {"kind": "transient"}),
            ("2-0", {"kind": "success"}),
        ],
    ))
    assert calls == [(worker.settings.memory_candidate_stream_key, worker._GROUP, "2-0")]


def test_pending_claim_cursor_advances_across_backlog(monkeypatch):
    starts = []

    class StreamRedis:
        async def xautoclaim(self, _stream, _group, _consumer, **kwargs):
            starts.append(kwargs["start_id"])
            if kwargs["start_id"] == "0-0":
                return ["10-0", []]
            if kwargs["start_id"] == "10-0":
                return ["20-0", []]
            return ["0-0", []]

    async def scenario():
        client = StreamRedis()
        cursor = "0-0"
        cursor = await worker._claim_pending(client, "consumer", cursor)
        cursor = await worker._claim_pending(client, "consumer", cursor)
        cursor = await worker._claim_pending(client, "consumer", cursor)
        return cursor

    assert asyncio.run(scenario()) == "0-0"
    assert starts == ["0-0", "10-0", "20-0"]


def test_retry_reuses_first_frozen_proposal_receipt(monkeypatch):
    class ReceiptRedis:
        def __init__(self):
            self.values = {}

        async def get(self, key):
            return self.values.get(key)

        async def set(self, key, value, *, ex=None, nx=False):
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

        async def eval(self, _script, _keys, key, token):
            if self.values.get(key) == token:
                self.values.pop(key, None)
                return 1
            return 0

    client = ReceiptRedis()
    calls = 0

    async def extract(*_args):
        nonlocal calls
        calls += 1
        return [{
            "categoryId": "phone", "preferenceKind": "avoid",
            "attributeKey": "brand", "normalizedValue": "apple",
            "catalogRevision": worker.settings.memory_active_catalog_revision,
            "recipientScope": "self", "source": "user_confirmed",
            "displayLabel": "苹果",
        }]

    monkeypatch.setattr(worker.memory_bff, "_redis", lambda: client)
    monkeypatch.setattr(worker, "_extract", extract)
    fields = {
        "jobId": "job-1", "sessionBinding": "binding",
        "categoryId": "phone", "recipientScope": "self",
        "userMessage": "请记住我不要苹果",
    }

    async def scenario():
        first = await worker._proposals_for_job(fields)
        second = await worker._proposals_for_job(fields)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second
    assert calls == 1


def test_concurrent_retry_uses_single_extraction_lease(monkeypatch):
    class ReceiptRedis:
        def __init__(self):
            self.values = {}

        async def get(self, key):
            return self.values.get(key)

        async def set(self, key, value, *, ex=None, nx=False):
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

        async def eval(self, _script, _keys, key, token):
            if self.values.get(key) == token:
                self.values.pop(key, None)
                return 1
            return 0

    client = ReceiptRedis()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def extract(*_args):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return [{
            "categoryId": "phone", "preferenceKind": "avoid",
            "attributeKey": "brand", "normalizedValue": "apple",
            "catalogRevision": worker.settings.memory_active_catalog_revision,
            "recipientScope": "self", "source": "user_confirmed",
            "displayLabel": "苹果",
        }]

    monkeypatch.setattr(worker.memory_bff, "_redis", lambda: client)
    monkeypatch.setattr(worker, "_extract", extract)
    fields = {
        "jobId": "job-concurrent", "sessionBinding": "binding",
        "categoryId": "phone", "recipientScope": "self",
        "userMessage": "请记住我不要苹果",
    }

    async def scenario():
        first = asyncio.create_task(worker._proposals_for_job(fields))
        await started.wait()
        second = asyncio.create_task(worker._proposals_for_job(fields))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(scenario())
    assert first == second
    assert calls == 1
