"""Test helper that obtains V2 results only through the real fetch_v2 issuer."""
from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import patch

from app.memory.projection_client import (
    MemoryAccessCredential,
    MemoryProjectionClient,
    MemoryProjectionReason,
    MemoryProjectionV2Entry,
    MemoryProjectionV2Result,
)


def _instant(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def fetched_v2_result(
    entries: tuple[MemoryProjectionV2Entry, ...],
    *,
    owner: str = "user-1",
    revision: int = 7,
    truncated: bool = False,
) -> MemoryProjectionV2Result:
    payload_entries = [
        {
            "entryId": item.entry_id,
            "memoryCategory": item.memory_category,
            "productCategory": item.product_category,
            "recipientScope": item.recipient_scope,
            "semanticKey": item.semantic_key,
            "value": item.value,
            "source": item.source,
            "dataClass": item.data_class,
            "version": item.version,
            "status": item.status,
            "createdAt": _instant(item.created_at),
            "updatedAt": _instant(item.updated_at),
            "expiresAt": _instant(item.expires_at),
            "supersedes": item.supersedes,
            "chainVerified": item.chain_verified,
        }
        for item in entries
    ]
    body = json.dumps({
        "success": True,
        "data": {
            "schemaVersion": 2,
            "revision": revision,
            "ownerBinding": hashlib.sha256(owner.encode("utf-8")).hexdigest(),
            "truncated": truncated,
            "entries": payload_entries,
        },
        "message": "ok",
        "timestamp": "2026-08-29T00:00:00Z",
    }, separators=(",", ":")).encode("utf-8")

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        async def aiter_bytes(self, *, chunk_size=None):
            yield body

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *ignored):
            return None

    class Client:
        def __init__(self, **ignored):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *ignored):
            return None

        def stream(self, method, url, headers):
            return Stream()

    with patch("app.memory.projection_client.httpx.AsyncClient", Client):
        result = asyncio.run(MemoryProjectionClient(
            enabled=True, backend_url="http://memory-test"
        ).fetch_v2(MemoryAccessCredential("test-credential")))
    assert result.reason is MemoryProjectionReason.AVAILABLE
    return result
