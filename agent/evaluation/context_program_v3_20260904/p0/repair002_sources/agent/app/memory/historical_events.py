"""Typed, storage-neutral contract for weak shopping behavior signals.

Historical events are deliberately not preferences.  They never enter the
long-term preference projection or model prompt and cannot create hard
constraints.  This module only selects bounded, still-applicable click facts
for a later deterministic ranking adapter.  Production ingestion remains
default-off and must provide an authority-verified snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal


HistoricalEventType = Literal["product_click", "purchase"]
HistoricalEventStatus = Literal["active", "deleted"]
RecipientScope = Literal["self", "other", "unknown"]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_CATEGORY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_REVISION = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_OWNER_BINDING = re.compile(r"[0-9a-f]{64}\Z")
_PRODUCT_ID = re.compile(r"[1-9]\d{0,39}\Z")
_MAX_SOURCE_EVENTS = 128
_MAX_APPLIED_CLICKS = 64


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"invalid {name}")
    return value.astimezone(UTC)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class HistoricalEvent:
    event_id: str
    owner_binding: str = field(repr=False)
    task_id: str
    event_type: HistoricalEventType
    category_id: str
    product_id: str
    catalog_revision: str
    recipient_scope: RecipientScope
    occurred_at: datetime
    expires_at: datetime
    status: HistoricalEventStatus = "active"
    source: Literal["observed_user_action"] = "observed_user_action"
    data_class: Literal["historical_event"] = "historical_event"

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or _ID.fullmatch(self.event_id) is None:
            raise ValueError("invalid eventId")
        if type(self.owner_binding) is not str or _OWNER_BINDING.fullmatch(self.owner_binding) is None:
            raise ValueError("invalid ownerBinding")
        if type(self.task_id) is not str or _ID.fullmatch(self.task_id) is None:
            raise ValueError("invalid taskId")
        if self.event_type not in {"product_click", "purchase"}:
            raise ValueError("invalid eventType")
        if type(self.category_id) is not str or _CATEGORY.fullmatch(self.category_id) is None:
            raise ValueError("invalid categoryId")
        if type(self.product_id) is not str or _PRODUCT_ID.fullmatch(self.product_id) is None:
            raise ValueError("invalid productId")
        if type(self.catalog_revision) is not str or _REVISION.fullmatch(self.catalog_revision) is None:
            raise ValueError("invalid catalogRevision")
        if self.recipient_scope not in {"self", "other", "unknown"}:
            raise ValueError("invalid recipientScope")
        if self.status not in {"active", "deleted"}:
            raise ValueError("invalid historical event status")
        if self.source != "observed_user_action" or self.data_class != "historical_event":
            raise ValueError("historical events cannot be conflated with preferences")
        occurred = _utc(self.occurred_at, "occurredAt")
        expires = _utc(self.expires_at, "expiresAt")
        if expires <= occurred:
            raise ValueError("invalid historical event timestamps")


@dataclass(frozen=True, slots=True)
class HistoricalEventSnapshot:
    owner_binding: str = field(repr=False)
    revision: int
    events: tuple[HistoricalEvent, ...] = ()
    authority_verified: bool = False

    def __post_init__(self) -> None:
        if type(self.owner_binding) is not str or _OWNER_BINDING.fullmatch(self.owner_binding) is None:
            raise ValueError("invalid ownerBinding")
        if type(self.revision) is not int or not 0 <= self.revision <= 2**63 - 1:
            raise ValueError("invalid historical event revision")
        if type(self.events) is not tuple or len(self.events) > _MAX_SOURCE_EVENTS:
            raise ValueError("invalid historical event snapshot")
        if any(type(item) is not HistoricalEvent for item in self.events):
            raise ValueError("invalid historical event")
        ids = [item.event_id for item in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate historical event ID")
        if type(self.authority_verified) is not bool:
            raise ValueError("invalid historical event authority")


@dataclass(frozen=True, slots=True)
class HistoricalEventApplicationContext:
    authenticated_owner_binding: str = field(repr=False)
    category_id: str
    catalog_revision: str
    recipient_scope: RecipientScope
    now: datetime
    behavior_memory_enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.authenticated_owner_binding) is not str or _OWNER_BINDING.fullmatch(
            self.authenticated_owner_binding
        ) is None:
            raise ValueError("invalid authenticated ownerBinding")
        if type(self.category_id) is not str or _CATEGORY.fullmatch(self.category_id) is None:
            raise ValueError("invalid categoryId")
        if type(self.catalog_revision) is not str or _REVISION.fullmatch(self.catalog_revision) is None:
            raise ValueError("invalid catalogRevision")
        if self.recipient_scope not in {"self", "other", "unknown"}:
            raise ValueError("invalid recipientScope")
        _utc(self.now, "now")
        if type(self.behavior_memory_enabled) is not bool:
            raise ValueError("invalid behavior memory flag")


@dataclass(frozen=True, slots=True)
class HistoricalEventDecision:
    event_id: str
    outcome: Literal["applied", "suppressed"]
    reason: str


@dataclass(frozen=True, slots=True)
class EligibleHistoricalClick:
    product_id: str
    occurred_at: datetime
    event_id: str

    def ranking_plain(self) -> dict[str, str]:
        return {
            "eventId": self.event_id,
            "productId": self.product_id,
            "occurredAt": _utc(self.occurred_at, "occurredAt").isoformat(),
        }


@dataclass(frozen=True, slots=True)
class HistoricalSignalSnapshot:
    revision: int
    clicks: tuple[EligibleHistoricalClick, ...]
    decisions: tuple[HistoricalEventDecision, ...]
    binding_hash: str

    def ranking_plain(self) -> dict[str, object]:
        """Owner-free, prompt-free payload for deterministic ranking only."""

        return {
            "schemaVersion": "historical-click-signal-v1",
            "revision": self.revision,
            "clicks": [item.ranking_plain() for item in self.clicks],
            "bindingHash": self.binding_hash,
        }


def _finalize(
    revision: int,
    clicks: list[EligibleHistoricalClick],
    decisions: list[HistoricalEventDecision],
) -> HistoricalSignalSnapshot:
    click_plain = [item.ranking_plain() for item in clicks]
    decision_plain = [
        {"eventId": item.event_id, "outcome": item.outcome, "reason": item.reason}
        for item in decisions
    ]
    binding_hash = hashlib.sha256(_canonical({
        "schemaVersion": "historical-click-signal-v1",
        "revision": revision,
        "clicks": click_plain,
        "decisions": decision_plain,
    })).hexdigest()
    return HistoricalSignalSnapshot(
        revision,
        tuple(clicks),
        tuple(decisions),
        binding_hash,
    )


def resolve_historical_clicks(
    snapshot: HistoricalEventSnapshot,
    context: HistoricalEventApplicationContext,
) -> HistoricalSignalSnapshot:
    """Select weak click facts; purchase-derived influence remains HOLD."""

    if type(snapshot) is not HistoricalEventSnapshot or type(context) is not HistoricalEventApplicationContext:
        raise ValueError("unvalidated historical event resolver input")
    ordered = sorted(snapshot.events, key=lambda item: (item.occurred_at, item.event_id), reverse=True)
    decisions: list[HistoricalEventDecision] = []
    clicks: list[EligibleHistoricalClick] = []

    def suppress(event: HistoricalEvent, reason: str) -> None:
        decisions.append(HistoricalEventDecision(event.event_id, "suppressed", reason))

    for event in ordered:
        if not context.behavior_memory_enabled:
            suppress(event, "behavior_memory_disabled")
        elif not snapshot.authority_verified:
            suppress(event, "snapshot_authority_unverified")
        elif snapshot.owner_binding != context.authenticated_owner_binding:
            suppress(event, "snapshot_owner_mismatch")
        elif event.owner_binding != snapshot.owner_binding:
            suppress(event, "event_owner_mismatch")
        elif context.recipient_scope != "self" or event.recipient_scope != "self":
            suppress(event, "recipient_not_self")
        elif event.status != "active":
            suppress(event, "event_deleted")
        elif event.expires_at <= context.now:
            suppress(event, "event_expired")
        elif event.category_id != context.category_id:
            suppress(event, "category_mismatch")
        elif event.catalog_revision != context.catalog_revision:
            suppress(event, "catalog_revision_mismatch")
        elif event.event_type == "purchase":
            # KuaiSearch-Lite has only one evaluable strict-phone purchase user;
            # storage is allowed, ranking influence is not yet authorized.
            suppress(event, "purchase_signal_hold")
        elif len(clicks) >= _MAX_APPLIED_CLICKS:
            suppress(event, "event_budget_exceeded")
        else:
            clicks.append(EligibleHistoricalClick(
                event.product_id,
                _utc(event.occurred_at, "occurredAt"),
                event.event_id,
            ))
            decisions.append(HistoricalEventDecision(event.event_id, "applied", "eligible_click"))
    return _finalize(snapshot.revision, clicks, decisions)


__all__ = [
    "EligibleHistoricalClick",
    "HistoricalEvent",
    "HistoricalEventApplicationContext",
    "HistoricalEventDecision",
    "HistoricalEventSnapshot",
    "HistoricalSignalSnapshot",
    "resolve_historical_clicks",
]
