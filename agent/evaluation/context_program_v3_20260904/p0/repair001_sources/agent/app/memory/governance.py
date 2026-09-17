"""Deterministic governance for the bounded shopping-memory slice.

This module is deliberately storage-free and disabled-by-default at the
runtime boundary.  It decides whether already-authoritative records may be
projected; it cannot write TaskState, CandidateScope, orders, or memory rows.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterable, Literal

from .long_term_memory import ShoppingPreference
from .projection_client import (
    MemoryProjectionEntry,
    MemoryProjectionReason,
    MemoryProjectionResult,
    MemoryProjectionV2Result,
    MemoryProjectionV2Entry,
    _validated_issued_v2_projection,
)

MemoryDataClass = Literal[
    "current_task_fact",
    "session_history",
    "long_term_preference",
    "historical_event",
]
ProductCategory = Literal["phone", "laptop", "headphones"]
RecipientScope = Literal["self", "other", "unknown"]
RecordStatus = Literal["active", "revoked", "expired", "superseded", "disabled"]
RecordSource = Literal["explicit_user", "user_confirmed"]
DecisionOutcome = Literal["applied", "suppressed"]

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_MAX_RECORDS = 8


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError(f"invalid {field_name}")
    return value


def _utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"invalid {field_name}")
    return value.astimezone(UTC)


def _preference(value: object) -> ShoppingPreference:
    if type(value) is not ShoppingPreference:
        raise ValueError("invalid shopping preference")
    return ShoppingPreference.from_plain(value.plain())


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ScopedMemoryRecord:
    entry_id: str
    owner_user_id: str
    product_category: ProductCategory
    recipient_scope: Literal["self"]
    preference: ShoppingPreference
    source: RecordSource
    status: RecordStatus
    version: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    supersedes: str | None = None
    data_class: Literal["long_term_preference"] = "long_term_preference"

    def __post_init__(self) -> None:
        _identifier(self.entry_id, "entryId")
        _identifier(self.owner_user_id, "ownerUserId")
        if self.product_category not in {"phone", "laptop", "headphones"}:
            raise ValueError("invalid product category")
        if self.recipient_scope != "self":
            raise ValueError("v1 durable memory is self-scoped only")
        _preference(self.preference)
        if self.source not in {"explicit_user", "user_confirmed"}:
            raise ValueError("model inference cannot become durable memory")
        if self.status not in {"active", "revoked", "expired", "superseded", "disabled"}:
            raise ValueError("invalid memory status")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("invalid memory version")
        if self.version == 1 and self.supersedes is not None:
            raise ValueError("initial memory cannot supersede another record")
        if self.version > 1:
            _identifier(self.supersedes, "supersedes")
        created = _utc(self.created_at, "createdAt")
        updated = _utc(self.updated_at, "updatedAt")
        expires = _utc(self.expires_at, "expiresAt")
        if updated < created or expires <= created:
            raise ValueError("invalid memory timestamps")
        if self.data_class != "long_term_preference":
            raise ValueError("memory data classes cannot be conflated")

    @property
    def logical_key(self) -> tuple[str, str, str]:
        return (
            self.product_category,
            self.preference.category,
            self.preference.semantic_key,
        )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class MemorySnapshot:
    owner_user_id: str
    revision: int
    records: tuple[ScopedMemoryRecord, ...] = ()
    terminal_projection_verified: bool = False

    def __post_init__(self) -> None:
        _identifier(self.owner_user_id, "ownerUserId")
        if type(self.revision) is not int or not 0 <= self.revision <= 2**63 - 1:
            raise ValueError("invalid memory revision")
        if type(self.records) is not tuple or len(self.records) > _MAX_RECORDS:
            raise ValueError("invalid memory snapshot")
        if any(type(item) is not ScopedMemoryRecord for item in self.records):
            raise ValueError("invalid memory snapshot record")
        if type(self.terminal_projection_verified) is not bool:
            raise ValueError("invalid memory snapshot authority")
        if self.terminal_projection_verified:
            keys = [item.logical_key for item in self.records]
            if len(keys) != len(set(keys)) or any(item.status != "active" for item in self.records):
                raise ValueError("invalid verified terminal projection")


@dataclass(frozen=True, slots=True)
class MemoryApplicationContext:
    authenticated_owner_user_id: str
    product_category: ProductCategory
    recipient_scope: RecipientScope
    now: datetime
    current_turn: tuple[ShoppingPreference, ...] = ()
    task_state: tuple[ShoppingPreference, ...] = ()
    memory_enabled: bool = False

    def __post_init__(self) -> None:
        _identifier(self.authenticated_owner_user_id, "ownerUserId")
        if self.product_category not in {"phone", "laptop", "headphones"}:
            raise ValueError("invalid product category")
        if self.recipient_scope not in {"self", "other", "unknown"}:
            raise ValueError("invalid recipient scope")
        _utc(self.now, "now")
        if type(self.memory_enabled) is not bool:
            raise ValueError("invalid memory feature flag")
        for layer in (self.current_turn, self.task_state):
            if type(layer) is not tuple:
                raise ValueError("preference layers must be tuples")
            seen: dict[tuple[str, str], ShoppingPreference] = {}
            for item in layer:
                checked = _preference(item)
                key = (checked.category, checked.semantic_key)
                if key in seen and seen[key] != checked:
                    raise ValueError("same-layer preference conflict")
                seen[key] = checked


@dataclass(frozen=True, slots=True)
class MemoryApplicabilityDecision:
    entry_id: str
    outcome: DecisionOutcome
    reason: str

    def __post_init__(self) -> None:
        _identifier(self.entry_id, "entryId")
        if self.outcome not in {"applied", "suppressed"}:
            raise ValueError("invalid applicability outcome")
        if type(self.reason) is not str or not self.reason or len(self.reason) > 96:
            raise ValueError("invalid applicability reason")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class EffectivePreferenceSnapshot:
    owner_user_id: str
    memory_revision: int
    preferences: tuple[ShoppingPreference, ...]
    decisions: tuple[MemoryApplicabilityDecision, ...]
    snapshot_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.owner_user_id, "ownerUserId")
        if type(self.memory_revision) is not int or self.memory_revision < 0:
            raise ValueError("invalid memory revision")
        if type(self.preferences) is not tuple or len(self.preferences) > _MAX_RECORDS:
            raise ValueError("invalid effective preferences")
        if type(self.decisions) is not tuple:
            raise ValueError("invalid applicability decisions")
        preference_rows = [_preference(item).plain() for item in self.preferences]
        decision_rows = [
            {"entryId": item.entry_id, "outcome": item.outcome, "reason": item.reason}
            for item in self.decisions
        ]
        digest = hashlib.sha256(_canonical({
            "ownerUserId": self.owner_user_id,
            "memoryRevision": self.memory_revision,
            "preferences": preference_rows,
            "decisions": decision_rows,
        })).hexdigest()
        object.__setattr__(self, "snapshot_hash", digest)


@dataclass(frozen=True, slots=True)
class _IssuedAuthority:
    reference: weakref.ReferenceType[object]
    sealed: bytes
    digest: bytes


_AUTHORITY_BINDING = secrets.token_bytes(32)
_ISSUED_V2_SNAPSHOTS: dict[int, _IssuedAuthority] = {}
_ISSUED_EFFECTIVE: dict[int, _IssuedAuthority] = {}
_ISSUED_PROJECTIONS: dict[int, _IssuedAuthority] = {}


def _register_authority(registry: dict[int, _IssuedAuthority], value: object, sealed: bytes, kind: bytes) -> None:
    key = id(value)
    digest = hashlib.sha256(_AUTHORITY_BINDING + kind + sealed + str(key).encode("ascii")).digest()

    def cleanup(reference: weakref.ReferenceType[object]) -> None:
        saved = registry.get(key)
        if saved is not None and saved.reference is reference:
            registry.pop(key, None)

    reference = weakref.ref(value, cleanup)
    registry[key] = _IssuedAuthority(reference, sealed, digest)


def _has_authority(
    registry: dict[int, _IssuedAuthority], value: object, sealed: bytes, kind: bytes
) -> bool:
    issued = registry.get(id(value))
    return bool(
        issued is not None
        and issued.reference() is value
        and issued.sealed == sealed
        and issued.digest == hashlib.sha256(
            _AUTHORITY_BINDING + kind + sealed + str(id(value)).encode("ascii")
        ).digest()
    )


def _record_plain(item: ScopedMemoryRecord) -> dict[str, object]:
    return {
        "entryId": item.entry_id,
        "ownerUserId": item.owner_user_id,
        "productCategory": item.product_category,
        "recipientScope": item.recipient_scope,
        "preference": item.preference.plain(),
        "source": item.source,
        "status": item.status,
        "version": item.version,
        "createdAt": _utc(item.created_at, "createdAt").isoformat(),
        "updatedAt": _utc(item.updated_at, "updatedAt").isoformat(),
        "expiresAt": _utc(item.expires_at, "expiresAt").isoformat(),
        "supersedes": item.supersedes,
        "dataClass": item.data_class,
    }


def _snapshot_seal(snapshot: MemorySnapshot) -> bytes:
    return _canonical({
        "ownerUserId": snapshot.owner_user_id,
        "revision": snapshot.revision,
        "terminalProjectionVerified": snapshot.terminal_projection_verified,
        "records": [_record_plain(item) for item in snapshot.records],
    })


def _effective_seal(effective: EffectivePreferenceSnapshot) -> bytes:
    return _canonical({
        "ownerUserId": effective.owner_user_id,
        "memoryRevision": effective.memory_revision,
        "preferences": [item.plain() for item in effective.preferences],
        "decisions": [
            {"entryId": item.entry_id, "outcome": item.outcome, "reason": item.reason}
            for item in effective.decisions
        ],
        "snapshotHash": effective.snapshot_hash,
    })


def _projection_seal(result: MemoryProjectionResult) -> bytes:
    return _canonical({
        "revision": result.revision,
        "reason": result.reason.value,
        "entries": [
            {
                "entryId": item.entry_id,
                "category": item.category,
                "semanticKey": item.semantic_key,
                "value": item.value,
                "version": item.version,
                "status": item.status,
            }
            for item in result.entries
        ],
    })


def _issued_v2_snapshot(snapshot: MemorySnapshot) -> bool:
    try:
        return _has_authority(_ISSUED_V2_SNAPSHOTS, snapshot, _snapshot_seal(snapshot), b"v2-snapshot")
    except (TypeError, ValueError, AttributeError):
        return False


def _finalize_effective(
    effective: EffectivePreferenceSnapshot, *, v2_authoritative: bool
) -> EffectivePreferenceSnapshot:
    if v2_authoritative:
        _register_authority(_ISSUED_EFFECTIVE, effective, _effective_seal(effective), b"effective")
    return effective


def _validated_governed_projection(result: object) -> MemoryProjectionResult | None:
    if type(result) is not MemoryProjectionResult or result.reason is not MemoryProjectionReason.AVAILABLE:
        return None
    try:
        sealed = _projection_seal(result)
    except (TypeError, ValueError, AttributeError):
        return None
    return result if _has_authority(_ISSUED_PROJECTIONS, result, sealed, b"projection") else None


def _suppressed(
    records: Iterable[ScopedMemoryRecord], reason: str
) -> tuple[MemoryApplicabilityDecision, ...]:
    return tuple(
        MemoryApplicabilityDecision(item.entry_id, "suppressed", reason)
        for item in sorted(records, key=lambda row: row.entry_id)
    )


def resolve_effective_preferences(
    snapshot: MemorySnapshot,
    context: MemoryApplicationContext,
) -> EffectivePreferenceSnapshot:
    """Apply authority, scope, lifecycle, and override rules fail-closed."""

    if type(snapshot) is not MemorySnapshot or type(context) is not MemoryApplicationContext:
        raise ValueError("unvalidated memory resolver input")
    v2_authoritative = _issued_v2_snapshot(snapshot)
    records = snapshot.records
    if not context.memory_enabled:
        decisions = _suppressed(records, "memory_disabled")
        return _finalize_effective(EffectivePreferenceSnapshot(
            context.authenticated_owner_user_id, snapshot.revision, (), decisions
        ), v2_authoritative=v2_authoritative)
    if snapshot.owner_user_id != context.authenticated_owner_user_id:
        decisions = _suppressed(records, "snapshot_owner_mismatch")
        return _finalize_effective(EffectivePreferenceSnapshot(
            context.authenticated_owner_user_id, snapshot.revision, (), decisions
        ), v2_authoritative=v2_authoritative)
    if any(item.owner_user_id != snapshot.owner_user_id for item in records):
        decisions = _suppressed(records, "foreign_owner_in_snapshot")
        return _finalize_effective(EffectivePreferenceSnapshot(
            context.authenticated_owner_user_id, snapshot.revision, (), decisions
        ), v2_authoritative=v2_authoritative)

    terminal: list[ScopedMemoryRecord] = []
    conflict_ids: set[str] = set()
    superseded_ids: set[str] = set()
    if snapshot.terminal_projection_verified:
        terminal.extend(records)
    else:
        groups: dict[tuple[str, str, str], list[ScopedMemoryRecord]] = {}
        for item in records:
            groups.setdefault(item.logical_key, []).append(item)
        for chain in groups.values():
            chain.sort(key=lambda item: (item.version, item.entry_id))
            versions = [item.version for item in chain]
            if versions != list(range(1, len(chain) + 1)):
                conflict_ids.update(item.entry_id for item in chain)
                continue
            for index, item in enumerate(chain):
                if index and item.supersedes != chain[index - 1].entry_id:
                    conflict_ids.update(row.entry_id for row in chain)
                    break
            else:
                terminal.append(chain[-1])
                superseded_ids.update(item.entry_id for item in chain[:-1])

    current_keys = {
        (item.category, item.semantic_key) for item in context.current_turn
    }
    task_keys = {
        (item.category, item.semantic_key) for item in context.task_state
    }
    decisions: list[MemoryApplicabilityDecision] = []
    applied: list[ShoppingPreference] = []
    for item in sorted(records, key=lambda row: row.entry_id):
        if item.entry_id in conflict_ids:
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", "version_chain_invalid"
            ))
            continue
        if item.entry_id in superseded_ids:
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", "superseded_by_newer_version"
            ))
            continue
        if item not in terminal:
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", "non_terminal_chain_member"
            ))
            continue
        if item.status != "active":
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", f"status_{item.status}"
            ))
            continue
        if item.expires_at <= context.now:
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", "expired_at_runtime"
            ))
            continue
        if item.product_category != context.product_category:
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", "category_mismatch"
            ))
            continue
        if context.recipient_scope != "self":
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", "recipient_not_self"
            ))
            continue
        semantic_key = (item.preference.category, item.preference.semantic_key)
        if semantic_key in current_keys:
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", "current_turn_override"
            ))
            continue
        if semantic_key in task_keys:
            decisions.append(MemoryApplicabilityDecision(
                item.entry_id, "suppressed", "task_state_override"
            ))
            continue
        applied.append(_preference(item.preference))
        decisions.append(MemoryApplicabilityDecision(item.entry_id, "applied", "applicable"))

    applied.sort(key=lambda item: (item.category, item.semantic_key, item.value))
    return _finalize_effective(EffectivePreferenceSnapshot(
        context.authenticated_owner_user_id,
        snapshot.revision,
        tuple(applied),
        tuple(decisions),
    ), v2_authoritative=v2_authoritative)


def projection_result_from_effective(
    effective: EffectivePreferenceSnapshot,
) -> MemoryProjectionResult:
    """Bridge governed preferences into the existing no-ID Context projection."""

    if type(effective) is not EffectivePreferenceSnapshot:
        raise ValueError("unvalidated effective preference snapshot")
    try:
        issued = _has_authority(_ISSUED_EFFECTIVE,effective,_effective_seal(effective),b"effective")
    except (TypeError,ValueError,AttributeError):
        issued = False
    if not issued:
        raise ValueError("effective preference snapshot is not V2-issued")
    if not effective.preferences:
        result=MemoryProjectionResult(
            effective.memory_revision, (), MemoryProjectionReason.AVAILABLE
        )
    else:
        entries = tuple(
            MemoryProjectionEntry(
                f"effective-{index}",
                item.category,
                item.semantic_key,
                item.value,
                1,
                "ACTIVE",
            )
            for index, item in enumerate(effective.preferences, 1)
        )
        result=MemoryProjectionResult(
            effective.memory_revision,
            entries,
            MemoryProjectionReason.AVAILABLE,
        )
    _register_authority(_ISSUED_PROJECTIONS,result,_projection_seal(result),b"projection")
    return result


def memory_snapshot_from_projection_v2(
    result: object,
    *,
    authenticated_owner_user_id: str,
) -> MemorySnapshot:
    """Convert only a strict, chain-verified v2 server projection."""
    owner = _identifier(authenticated_owner_user_id, "ownerUserId")
    issued_result=_validated_issued_v2_projection(result)
    if issued_result is None:
        return MemorySnapshot(owner, 0, (), True)
    result=issued_result
    if result.owner_binding != hashlib.sha256(owner.encode("utf-8")).hexdigest():
        return MemorySnapshot(owner, result.revision, (), True)
    return _snapshot_from_issued_v2_projection(result, owner)


def memory_snapshot_from_authenticated_projection_v2(result: object) -> MemorySnapshot:
    """Use the Java-issued owner binding as an opaque request-local identity.

    This is only for a projection fetched with an already-issued
    ``RequestAuthContext``.  It deliberately never accepts an owner value from
    a browser, model, TaskState, or request body.
    """
    issued_result = _validated_issued_v2_projection(result)
    if issued_result is None:
        return MemorySnapshot("unavailable", 0, (), True)
    try:
        owner_binding = _identifier(issued_result.owner_binding, "ownerBinding")
    except ValueError:
        return MemorySnapshot("unavailable", 0, (), True)
    return _snapshot_from_issued_v2_projection(issued_result, owner_binding)


def _snapshot_from_issued_v2_projection(
    result: MemoryProjectionV2Result,
    owner: str,
) -> MemorySnapshot:
    records: list[ScopedMemoryRecord] = []
    for item in result.entries:
        if type(item) is not MemoryProjectionV2Entry or item.chain_verified is not True:
            return MemorySnapshot(owner, result.revision, (), True)
        try:
            records.append(ScopedMemoryRecord(
                entry_id=item.entry_id,
                owner_user_id=owner,
                product_category=item.product_category,
                recipient_scope="self",
                preference=ShoppingPreference(item.memory_category, item.semantic_key, item.value),
                source=item.source,
                status="active",
                version=item.version,
                created_at=item.created_at,
                updated_at=item.updated_at,
                expires_at=item.expires_at,
                supersedes=item.supersedes,
            ))
        except ValueError:
            return MemorySnapshot(owner, result.revision, (), True)
    try:
        snapshot=MemorySnapshot(owner, result.revision, tuple(records), True)
        _register_authority(_ISSUED_V2_SNAPSHOTS,snapshot,_snapshot_seal(snapshot),b"v2-snapshot")
        return snapshot
    except ValueError:
        return MemorySnapshot(owner, result.revision, (), True)


__all__ = [
    "EffectivePreferenceSnapshot",
    "MemoryApplicationContext",
    "MemoryApplicabilityDecision",
    "MemoryDataClass",
    "MemorySnapshot",
    "memory_snapshot_from_authenticated_projection_v2",
    "memory_snapshot_from_projection_v2",
    "ScopedMemoryRecord",
    "projection_result_from_effective",
    "resolve_effective_preferences",
]
