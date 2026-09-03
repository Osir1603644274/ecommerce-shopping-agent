import asyncio
from datetime import UTC, datetime, timedelta

from app.memory.long_term_memory import ShoppingPreference
from app.memory.projection_client import MemoryAccessCredential, MemoryProjectionV2Entry
from app.memory.runtime_bridge import load_authenticated_long_term_memory_context
from app.request_auth import _new_issuer_for_verified_provider
from tests.memory_v2_fetch_support import fetched_v2_result


NOW = datetime(2026, 8, 29, tzinfo=UTC)


class ProjectionClientStub:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.credential = None

    async def fetch_v2(self, credential):
        self.calls += 1
        self.credential = credential
        return self.result


def _entry(key="avoid_brand", value="apple"):
    return MemoryProjectionV2Entry(
        "entry-1", "shopping_preference", "phone", "self", key, value,
        "explicit_user", "long_term_preference", 1, "ACTIVE",
        NOW - timedelta(days=1), NOW, NOW + timedelta(days=30), None, True,
    )


def _request_context():
    issuer = _new_issuer_for_verified_provider()
    return issuer, issuer.issue(MemoryAccessCredential("bridge-token"))


def test_bridge_uses_only_issued_auth_and_returns_governed_model_context():
    client = ProjectionClientStub(fetched_v2_result((_entry(),)))
    issuer, request_context = _request_context()
    context = asyncio.run(load_authenticated_long_term_memory_context(
        enabled=True,
        projection_client=client,
        request_auth_context=request_context,
        product_category="phone",
        recipient_scope="self",
        now=NOW,
    ))
    assert client.calls == 1
    assert issuer is not None
    assert client.credential is not None
    assert "bridge-token" not in repr(context)
    assert context.plain()["preferences"] == [
        {"category": "shopping_preference", "semanticKey": "avoid_brand", "value": "apple"}
    ]


def test_bridge_is_default_off_and_does_not_fetch():
    client = ProjectionClientStub(fetched_v2_result((_entry(),)))
    issuer, request_context = _request_context()
    context = asyncio.run(load_authenticated_long_term_memory_context(
        enabled=False,
        projection_client=client,
        request_auth_context=request_context,
        product_category="phone",
        recipient_scope="self",
        now=NOW,
    ))
    assert client.calls == 0
    assert issuer is not None
    assert context.memory_revision == 0


def test_bridge_suppresses_memory_for_non_self_recipient_and_current_override():
    client = ProjectionClientStub(fetched_v2_result((_entry(),)))
    other_issuer, other_request_context = _request_context()
    other = asyncio.run(load_authenticated_long_term_memory_context(
        enabled=True,
        projection_client=client,
        request_auth_context=other_request_context,
        product_category="phone",
        recipient_scope="other",
        now=NOW,
    ))
    assert other_issuer is not None
    assert other.memory_revision == 7
    assert other.preferences == ()

    override_client = ProjectionClientStub(fetched_v2_result((_entry(),)))
    override_issuer, override_request_context = _request_context()
    overridden = asyncio.run(load_authenticated_long_term_memory_context(
        enabled=True,
        projection_client=override_client,
        request_auth_context=override_request_context,
        product_category="phone",
        recipient_scope="self",
        current_turn=(ShoppingPreference("shopping_preference", "avoid_brand", "huawei"),),
        now=NOW,
    ))
    assert override_issuer is not None
    assert overridden.memory_revision == 7
    assert overridden.preferences == ()
