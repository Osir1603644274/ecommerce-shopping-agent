import asyncio
import copy
import hashlib
import json
import pickle
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from app.memory.projection_client import (
    MemoryAccessCredential,
    MemoryProjectionClient,
    MemoryProjectionEntry,
    MemoryProjectionReason,
    MemoryProjectionResult,
    MemoryProjectionV2Result,
    _validated_issued_v2_projection,
)


def _body(*, entries=None, revision=3, timestamp="2026-01-01T00:00:00Z"):
    return json.dumps(
        {
            "success": True,
            "data": {"schemaVersion": 1, "revision": revision, "entries": entries or []},
            "message": "ok",
            "timestamp": timestamp,
        },
        separators=(",", ":"),
    ).encode()


def _entry(**changes):
    value = {
        "entryId": "entry-1",
        "category": "shopping_preference",
        "semanticKey": "avoid_brand",
        "value": "brand-x",
        "version": 1,
        "status": "ACTIVE",
    }
    value.update(changes)
    return value


class _Response:
    def __init__(self, status, chunks, iteration_error=None, headers=None):
        self.status_code = status
        self._chunks = tuple(chunks)
        self._iteration_error = iteration_error
        self.headers = {} if headers is None else headers
        self.pull_count = 0
        self.chunk_size = None

    async def aiter_bytes(self, *, chunk_size=None):
        self.chunk_size = chunk_size
        for chunk in self._chunks:
            self.pull_count += 1
            yield chunk
        if self._iteration_error is not None:
            raise self._iteration_error


class _Stream:
    def __init__(self, response):
        self.response = response
        self.closed = False

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        self.closed = True


def _install_client(monkeypatch, *, status=200, chunks=(), error=None, iteration_error=None, headers=None):
    calls, streams = [], []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, method, url, headers):
            calls.append((method, url, headers))
            if error is not None:
                raise error
            stream = _Stream(_Response(status, chunks, iteration_error, outer_headers))
            streams.append(stream)
            return stream

    outer_headers = headers
    monkeypatch.setattr("app.memory.projection_client.httpx.AsyncClient", Client)
    return calls, streams


def _fetch():
    return asyncio.run(
        MemoryProjectionClient(enabled=True, backend_url="http://x").fetch(
            MemoryAccessCredential("secret")
        )
    )


def _v2_entry(**changes):
    value = {
        "entryId": "entry-v2-1",
        "memoryCategory": "shopping_preference",
        "productCategory": "phone",
        "recipientScope": "self",
        "semanticKey": "os",
        "value": "android",
        "source": "explicit_user",
        "dataClass": "long_term_preference",
        "version": 1,
        "status": "ACTIVE",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "expiresAt": "2030-01-01T00:00:00Z",
        "supersedes": None,
        "chainVerified": True,
    }
    value.update(changes)
    return value


def _v2_body(*, entries=None, revision=4, truncated=False, schema=2, owner_binding=None):
    binding = owner_binding or hashlib.sha256(b"user-1").hexdigest()
    return json.dumps({
        "success": True,
        "data": {"schemaVersion": schema, "revision": revision, "ownerBinding": binding, "truncated": truncated, "entries": entries or []},
        "message": "ok",
        "timestamp": "2026-01-03T00:00:00Z",
    }, separators=(",", ":")).encode()


def _fetch_v2():
    return asyncio.run(MemoryProjectionClient(enabled=True, backend_url="http://x").fetch_v2(MemoryAccessCredential("secret")))


def test_v2_fetch_requires_exact_governed_schema_and_endpoint(monkeypatch):
    calls, _ = _install_client(monkeypatch, chunks=[_v2_body(entries=[_v2_entry()], truncated=True)], headers={})
    result = _fetch_v2()
    assert isinstance(result, MemoryProjectionV2Result)
    assert result.reason is MemoryProjectionReason.AVAILABLE
    assert result.revision == 4 and result.truncated is True
    assert result.owner_binding == hashlib.sha256(b"user-1").hexdigest()
    assert result.entries[0].product_category == "phone"
    assert _validated_issued_v2_projection(result) is result
    assert calls == [("GET", "http://x/api/memory/projection/v2", {"Authorization": "Bearer secret"})]


def test_only_original_successful_fetch_v2_result_is_issued(monkeypatch):
    _install_client(monkeypatch,chunks=[_v2_body(entries=[_v2_entry()])],headers={})
    issued=_fetch_v2()
    direct=MemoryProjectionV2Result(
        revision=issued.revision,owner_binding=issued.owner_binding,truncated=issued.truncated,
        entries=issued.entries,reason=issued.reason,
    )
    forged=(direct,copy.copy(issued),copy.deepcopy(issued),pickle.loads(pickle.dumps(issued)),replace(issued))
    assert all(_validated_issued_v2_projection(item) is None for item in forged)

    mutated=_fetch_v2()
    object.__setattr__(mutated,"owner_binding","0"*64)
    assert _validated_issued_v2_projection(mutated) is None


@pytest.mark.parametrize("body", [
    _v2_body(entries=[_v2_entry()], schema=1),
    _v2_body(entries=[_v2_entry(memoryCategory="health")]),
    _v2_body(entries=[_v2_entry(recipientScope="unknown")]),
    _v2_body(entries=[_v2_entry(source="model_inferred")]),
    _v2_body(entries=[_v2_entry(chainVerified=False)]),
    _v2_body(entries=[_v2_entry(expiresAt="2025-01-01T00:00:00Z")]),
    _v2_body(entries=[_v2_entry(updatedAt="2031-01-01T00:00:00Z")]),
    _v2_body(entries=[dict(_v2_entry(), ownerUserId="other")]),
    _v2_body(entries=[_v2_entry()], owner_binding="0" * 63),
])
def test_v2_fetch_fails_closed_on_legacy_sensitive_or_unverified_payload(monkeypatch, body):
    _install_client(monkeypatch, chunks=[body], headers={})
    assert _fetch_v2().reason is MemoryProjectionReason.INVALID


def test_disabled_and_no_credential_make_no_http(monkeypatch):
    called = []

    class Client:
        def __init__(self, **kwargs):
            called.append(1)

    monkeypatch.setattr("app.memory.projection_client.httpx.AsyncClient", Client)
    client = MemoryProjectionClient(enabled=False, backend_url="http://x")
    assert asyncio.run(client.fetch(None)).reason is MemoryProjectionReason.DISABLED
    assert asyncio.run(MemoryProjectionClient(enabled=True, backend_url="http://x").fetch(None)).reason is MemoryProjectionReason.NO_CREDENTIAL
    assert called == []


def test_valid_streamed_payload_has_exact_header_and_immutable_entries(monkeypatch):
    calls, streams = _install_client(
        monkeypatch,
        chunks=[_body(entries=[_entry()])],
        headers={"content-encoding": "identity"},
    )
    result = _fetch()
    assert result.revision == 3 and len(result.entries) == 1
    assert result.reason is MemoryProjectionReason.AVAILABLE
    assert calls == [("GET", "http://x/api/memory/projection", {"Authorization": "Bearer secret"})]
    assert streams[0].closed is True
    assert streams[0].response.chunk_size == 4096
    assert "secret" not in repr(MemoryAccessCredential("secret"))
    assert "brand-x" not in repr(result)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        result.entries[0].value = "brand-a"


def test_public_result_construction_and_entry_repr_are_strict():
    entry = MemoryProjectionEntry("entry-1", "shopping_preference", "avoid_brand", "brand-x", 1, "ACTIVE")
    assert all(fragment not in repr(entry) for fragment in ("entry-1", "shopping_preference", "avoid_brand", "brand-x"))
    assert MemoryProjectionResult(1, (entry,), MemoryProjectionReason.AVAILABLE).entries == (entry,)
    for args in [
        (True, (), MemoryProjectionReason.AVAILABLE),
        (-1, (), MemoryProjectionReason.AVAILABLE),
        (2**63, (), MemoryProjectionReason.AVAILABLE),
        (1, [entry], MemoryProjectionReason.AVAILABLE),
        (1, ({"entryId": "entry-1"},), MemoryProjectionReason.AVAILABLE),
        (1, (), "available"),
    ]:
        with pytest.raises(ValueError):
            MemoryProjectionResult(*args)


def test_direct_entry_rejects_mutable_string_subclasses_and_equality_gadgets():
    class MutableText(str):
        pass

    class MutableStatus:
        def __eq__(self, other):
            return other == "ACTIVE"

    base = {
        "entry_id": "entry-1",
        "category": "shopping_preference",
        "semantic_key": "avoid_brand",
        "value": "brand-x",
        "version": 1,
        "status": "ACTIVE",
    }
    for field in ("entry_id", "category", "semantic_key", "value", "status"):
        invalid = dict(base)
        invalid[field] = MutableText(invalid[field])
        with pytest.raises(ValueError):
            MemoryProjectionEntry(**invalid)
    invalid = dict(base)
    invalid["status"] = MutableStatus()
    with pytest.raises(ValueError):
        MemoryProjectionEntry(**invalid)


@pytest.mark.parametrize(
    "credential",
    ["", " a", "a ", "a\r\nb", "a b", "x" * 2049, "é", "=="],
)
def test_credential_is_strict_ascii_bearer(credential):
    with pytest.raises(ValueError):
        MemoryAccessCredential(credential)


@pytest.mark.parametrize(
    "body",
    [
        b'{"success":true,"success":true,"data":{},"message":"ok","timestamp":"2026-01-01T00:00:00Z"}',
        b'{"success":true,"data":{"schemaVersion":1,"revision":1,"entries":[{"entryId":"entry-1","category":"shopping_preference","semanticKey":"avoid_brand","semanticKey":"avoid_brand","value":"brand-x","version":1,"status":"ACTIVE"}]},"message":"ok","timestamp":"2026-01-01T00:00:00Z"}',
        b'<html>error</html>',
        _body(revision=-1),
        _body(entries=[_entry(ownerUserId="u")]),
        _body(entries=[_entry(version=0)]),
        _body(entries=[_entry(version=True)]),
        _body(entries=[_entry(version=1.0)]),
        _body(entries=[_entry(entryId="")]),
        _body(entries=[_entry(value="unknownsecret")]),
        _body(timestamp="2026-01-01T00:00:00X"),
        _body(timestamp="2026-01-01T00:00:00+00:00"),
        _body(timestamp="2026-01-01T00:00:00😀"),
        _body(timestamp="2026-02-30T00:00:00Z"),
        json.dumps({"success": True, "data": {"schemaVersion": "1", "revision": 1, "entries": []}, "message": "ok", "timestamp": "2026-01-01T00:00:00Z"}).encode(),
        json.dumps({"success": True, "data": {"schemaVersion": 1, "revision": 1, "entries": []}, "message": {"payload": "secret"}, "timestamp": "2026-01-01T00:00:00Z"}).encode(),
    ],
)
def test_malformed_payloads_fail_closed(monkeypatch, body):
    _install_client(monkeypatch, chunks=[body])
    result = _fetch()
    assert result.entries == () and result.reason is MemoryProjectionReason.INVALID
    assert "secret" not in repr(result)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_http_failures_are_sanitized_and_single_attempt(monkeypatch, status):
    calls, streams = _install_client(monkeypatch, status=status, chunks=[b"x" * 8193])
    result = _fetch()
    assert len(calls) == 1 and result.entries == ()
    assert result.reason is MemoryProjectionReason.UNAVAILABLE
    assert streams[0].response.pull_count == 0 and streams[0].closed is True
    assert "secret" not in repr(result)


def test_timeout_is_sanitized(monkeypatch):
    calls, _ = _install_client(monkeypatch, error=httpx.TimeoutException("secret"))
    result = _fetch()
    assert calls and result.reason is MemoryProjectionReason.UNAVAILABLE
    assert "secret" not in repr(result)


def test_stream_read_error_is_sanitized_and_closes_context(monkeypatch):
    _, streams = _install_client(
        monkeypatch,
        chunks=[b"partial"],
        iteration_error=httpx.ReadError("secret payload"),
    )
    result = _fetch()
    assert result.reason is MemoryProjectionReason.UNAVAILABLE
    assert streams[0].closed is True
    assert "secret" not in repr(result)


def test_streaming_cap_accepts_8192_rejects_8193_and_closes(monkeypatch):
    valid = _body(entries=[])
    accepted = valid + b" " * (8192 - len(valid))
    _, streams = _install_client(monkeypatch, chunks=[accepted[:4096], accepted[4096:]])
    assert _fetch().reason is MemoryProjectionReason.AVAILABLE
    assert streams[0].closed is True

    oversized = valid + b" " * (8193 - len(valid))
    _, streams = _install_client(monkeypatch, chunks=[oversized[:4096], oversized[4096:8192], oversized[8192:]])
    assert _fetch().reason is MemoryProjectionReason.INVALID
    assert streams[0].closed is True
    assert streams[0].response.pull_count == 3


def test_content_encoding_is_rejected_before_any_body_pull(monkeypatch):
    _, streams = _install_client(
        monkeypatch,
        chunks=[b"x" * 8193],
        headers={"content-encoding": "gzip"},
    )
    assert _fetch().reason is MemoryProjectionReason.INVALID
    assert streams[0].response.pull_count == 0 and streams[0].closed is True


def test_deep_json_is_invalid_without_escape(monkeypatch):
    deep = (b"[" * 3000) + (b"]" * 3000)
    _install_client(monkeypatch, chunks=[deep])
    assert _fetch().reason is MemoryProjectionReason.INVALID


def test_unknown_missing_and_oversize_payloads_are_invalid(monkeypatch):
    payloads = [
        json.dumps({"success": True, "data": {"schemaVersion": 1, "revision": 1, "entries": [], "owner": "x"}, "message": "ok", "timestamp": "2026-01-01T00:00:00Z"}).encode(),
        json.dumps({"success": True, "data": {"schemaVersion": 1, "revision": 1}, "message": "ok", "timestamp": "2026-01-01T00:00:00Z"}).encode(),
        _body(entries=[_entry(entryId="entry-" + "x" * 80)]),
    ]
    for payload in payloads:
        _install_client(monkeypatch, chunks=[payload])
        assert _fetch().reason is MemoryProjectionReason.INVALID
