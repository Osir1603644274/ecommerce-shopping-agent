from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest

from app.memory.historical_events import (
    HistoricalEvent,
    HistoricalEventApplicationContext,
    HistoricalEventSnapshot,
    resolve_historical_clicks,
)


NOW = datetime(2026, 8, 30, tzinfo=UTC)
OWNER = "a" * 64
OTHER = "b" * 64
REVISION = "used-phone-439-09807c773ce6"


def event(
    event_id: str,
    *,
    event_type: str = "product_click",
    owner: str = OWNER,
    category: str = "phone",
    catalog_revision: str = REVISION,
    recipient_scope: str = "self",
    status: str = "active",
    occurred_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> HistoricalEvent:
    occurred = occurred_at or NOW - timedelta(days=1)
    return HistoricalEvent(
        event_id=event_id,
        owner_binding=owner,
        task_id="task-1",
        event_type=event_type,
        category_id=category,
        product_id="9007199254740993",
        catalog_revision=catalog_revision,
        recipient_scope=recipient_scope,
        occurred_at=occurred,
        expires_at=expires_at or occurred + timedelta(days=30),
        status=status,
    )


def context(**overrides) -> HistoricalEventApplicationContext:
    values = {
        "authenticated_owner_binding": OWNER,
        "category_id": "phone",
        "catalog_revision": REVISION,
        "recipient_scope": "self",
        "now": NOW,
        "behavior_memory_enabled": True,
    }
    values.update(overrides)
    return HistoricalEventApplicationContext(**values)


def snapshot(*events: HistoricalEvent, **overrides) -> HistoricalEventSnapshot:
    values = {
        "owner_binding": OWNER,
        "revision": 7,
        "events": tuple(events),
        "authority_verified": True,
    }
    values.update(overrides)
    return HistoricalEventSnapshot(**values)


def test_click_is_a_separate_owner_free_ranking_fact_not_a_preference():
    result = resolve_historical_clicks(snapshot(event("click-1")), context())
    assert [item.product_id for item in result.clicks] == ["9007199254740993"]
    assert result.decisions[0].reason == "eligible_click"
    plain = result.ranking_plain()
    assert set(plain) == {"schemaVersion", "revision", "clicks", "bindingHash"}
    assert "owner" not in str(plain).lower()
    assert "preference" not in {item.name for item in fields(HistoricalEvent)}


def test_purchase_can_be_stored_but_cannot_influence_ranking_yet():
    result = resolve_historical_clicks(
        snapshot(event("purchase-1", event_type="purchase")),
        context(),
    )
    assert result.clicks == ()
    assert result.decisions[0].reason == "purchase_signal_hold"


@pytest.mark.parametrize(
    ("snapshot_overrides", "context_overrides", "event_overrides", "reason"),
    [
        ({}, {"behavior_memory_enabled": False}, {}, "behavior_memory_disabled"),
        ({"authority_verified": False}, {}, {}, "snapshot_authority_unverified"),
        ({}, {"authenticated_owner_binding": OTHER}, {}, "snapshot_owner_mismatch"),
        ({}, {}, {"owner": OTHER}, "event_owner_mismatch"),
        ({}, {"recipient_scope": "other"}, {}, "recipient_not_self"),
        ({}, {}, {"recipient_scope": "unknown"}, "recipient_not_self"),
        ({}, {}, {"status": "deleted"}, "event_deleted"),
        ({}, {}, {"expires_at": NOW}, "event_expired"),
        ({}, {}, {"category": "laptop"}, "category_mismatch"),
        ({}, {}, {"catalog_revision": "other-revision"}, "catalog_revision_mismatch"),
    ],
)
def test_scope_lifecycle_and_authority_fail_closed(
    snapshot_overrides, context_overrides, event_overrides, reason
):
    result = resolve_historical_clicks(
        snapshot(event("event-1", **event_overrides), **snapshot_overrides),
        context(**context_overrides),
    )
    assert result.clicks == ()
    assert result.decisions[0].reason == reason


def test_snapshot_rejects_duplicate_events_and_numeric_product_identity():
    duplicate = event("same")
    with pytest.raises(ValueError, match="duplicate"):
        snapshot(duplicate, duplicate)
    with pytest.raises(ValueError, match="productId"):
        HistoricalEvent(
            event_id="bad-product",
            owner_binding=OWNER,
            task_id="task-1",
            event_type="product_click",
            category_id="phone",
            product_id=9007199254740993,  # type: ignore[arg-type]
            catalog_revision=REVISION,
            recipient_scope="self",
            occurred_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )


def test_resolution_and_hash_are_deterministic_independent_of_input_order():
    older = event("older", occurred_at=NOW - timedelta(days=2))
    newer = event("newer", occurred_at=NOW - timedelta(days=1))
    first = resolve_historical_clicks(snapshot(older, newer), context())
    second = resolve_historical_clicks(snapshot(newer, older), context())
    assert [item.event_id for item in first.clicks] == ["newer", "older"]
    assert first == second
