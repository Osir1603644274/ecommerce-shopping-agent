from __future__ import annotations

from datetime import UTC, datetime, timedelta
import copy
import pickle
from dataclasses import replace

import pytest

from app.memory import (
    MemoryApplicationContext,
    MemorySnapshot,
    ScopedMemoryRecord,
    ShoppingPreference,
    derive_long_term_memory_context,
    projection_result_from_effective,
    resolve_effective_preferences,
    memory_snapshot_from_projection_v2,
)
from app.memory.projection_client import (
    MemoryProjectionEntry,
    MemoryProjectionReason,
    MemoryProjectionResult,
    MemoryProjectionV2Entry,
    MemoryProjectionV2Result,
)
from tests.memory_v2_fetch_support import fetched_v2_result


NOW = datetime(2026, 8, 29, tzinfo=UTC)


def preference(key: str = "os", value: str = "android") -> ShoppingPreference:
    return ShoppingPreference("shopping_preference", key, value)


def record(
    index: int,
    *,
    owner: str = "user-1",
    category: str = "phone",
    pref: ShoppingPreference | None = None,
    source: str = "explicit_user",
    status: str = "active",
    version: int = 1,
    supersedes: str | None = None,
    expires_at: datetime | None = None,
) -> ScopedMemoryRecord:
    return ScopedMemoryRecord(
        entry_id=f"entry-{index}",
        owner_user_id=owner,
        product_category=category,  # type: ignore[arg-type]
        recipient_scope="self",
        preference=pref or preference(),
        source=source,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        version=version,
        created_at=NOW - timedelta(days=5),
        updated_at=NOW - timedelta(days=1),
        expires_at=expires_at or NOW + timedelta(days=30),
        supersedes=supersedes,
    )


def context(
    *,
    owner: str = "user-1",
    category: str = "phone",
    recipient: str = "self",
    current_turn: tuple[ShoppingPreference, ...] = (),
    task_state: tuple[ShoppingPreference, ...] = (),
    enabled: bool = True,
) -> MemoryApplicationContext:
    return MemoryApplicationContext(
        authenticated_owner_user_id=owner,
        product_category=category,  # type: ignore[arg-type]
        recipient_scope=recipient,  # type: ignore[arg-type]
        now=NOW,
        current_turn=current_turn,
        task_state=task_state,
        memory_enabled=enabled,
    )


def snapshot(*records: ScopedMemoryRecord, owner: str = "user-1") -> MemorySnapshot:
    return MemorySnapshot(owner, 7, tuple(records))


def reasons(effective) -> dict[str, str]:
    return {item.entry_id: item.reason for item in effective.decisions}


def test_same_owner_self_and_category_applies_and_bridges_to_contextpack_channel():
    effective = resolve_effective_preferences(snapshot(record(1)), context())
    assert effective.preferences == (preference(),)
    assert reasons(effective) == {"entry-1": "applicable"}
    assert len(effective.snapshot_hash) == 64

    with pytest.raises(ValueError, match="not V2-issued"):
        projection_result_from_effective(effective)


@pytest.mark.parametrize(
    ("layer", "reason"),
    [
        ("current_turn", "current_turn_override"),
        ("task_state", "task_state_override"),
    ],
)
def test_current_authority_layers_override_long_term_memory(layer: str, reason: str):
    values = {layer: (preference("os", "ios"),)}
    effective = resolve_effective_preferences(snapshot(record(1)), context(**values))
    assert effective.preferences == ()
    assert reasons(effective) == {"entry-1": reason}


@pytest.mark.parametrize("recipient", ["other", "unknown"])
def test_self_memory_is_suppressed_for_other_or_unknown_recipient(recipient: str):
    effective = resolve_effective_preferences(
        snapshot(record(1)), context(recipient=recipient)
    )
    assert effective.preferences == ()
    assert reasons(effective) == {"entry-1": "recipient_not_self"}


def test_cross_user_snapshot_and_injected_foreign_record_fail_closed():
    mismatched = resolve_effective_preferences(
        snapshot(record(1), owner="user-1"), context(owner="user-2")
    )
    assert mismatched.preferences == ()
    assert reasons(mismatched) == {"entry-1": "snapshot_owner_mismatch"}

    poisoned = resolve_effective_preferences(
        snapshot(record(1), record(2, owner="user-2")), context()
    )
    assert poisoned.preferences == ()
    assert set(reasons(poisoned).values()) == {"foreign_owner_in_snapshot"}


def test_category_disabled_revoked_expired_and_runtime_expiry_are_suppressed():
    rows = (
        record(1, category="laptop"),
        record(2, pref=preference("battery_health", "90_plus"), status="revoked"),
        record(3, pref=preference("scratch_level", "none"), status="disabled"),
        record(4, pref=preference("screen_originality", "original"), expires_at=NOW),
    )
    effective = resolve_effective_preferences(snapshot(*rows), context())
    assert effective.preferences == ()
    assert reasons(effective) == {
        "entry-1": "category_mismatch",
        "entry-2": "status_revoked",
        "entry-3": "status_disabled",
        "entry-4": "expired_at_runtime",
    }


def test_revision_chain_only_applies_latest_and_revoke_tombstone_stops_influence():
    first = record(1, pref=preference("os", "android"))
    second = record(
        2,
        pref=preference("os", "ios"),
        version=2,
        supersedes="entry-1",
    )
    updated = resolve_effective_preferences(snapshot(first, second), context())
    assert updated.preferences == (preference("os", "ios"),)
    assert reasons(updated) == {
        "entry-1": "superseded_by_newer_version",
        "entry-2": "applicable",
    }

    revoked = record(
        2,
        pref=preference("os", "android"),
        version=2,
        supersedes="entry-1",
        status="revoked",
    )
    terminal = resolve_effective_preferences(snapshot(first, revoked), context())
    assert terminal.preferences == ()
    assert reasons(terminal)["entry-2"] == "status_revoked"


def test_broken_version_chain_fails_closed_and_model_inference_cannot_be_recorded():
    first = record(1)
    third = record(3, version=3, supersedes="entry-1")
    effective = resolve_effective_preferences(snapshot(first, third), context())
    assert effective.preferences == ()
    assert set(reasons(effective).values()) == {"version_chain_invalid"}

    with pytest.raises(ValueError, match="model inference"):
        record(4, source="model_inferred")


def test_default_disabled_flag_suppresses_every_record_and_preserves_revision():
    effective = resolve_effective_preferences(
        snapshot(record(1)), context(enabled=False)
    )
    assert effective.preferences == ()
    assert reasons(effective) == {"entry-1": "memory_disabled"}
    with pytest.raises(ValueError, match="not V2-issued"):
        projection_result_from_effective(effective)


def test_strict_v2_terminal_projection_enters_governance_without_bypassing_scope():
    entry = MemoryProjectionV2Entry(
        "entry-v2", "shopping_preference", "phone", "self", "os", "android",
        "explicit_user", "long_term_preference", 2, "ACTIVE",
        NOW - timedelta(days=5), NOW - timedelta(days=1), NOW + timedelta(days=20),
        "entry-v1", True,
    )
    result = fetched_v2_result((entry,),revision=11)
    governed = memory_snapshot_from_projection_v2(result, authenticated_owner_user_id="user-1")
    assert governed.terminal_projection_verified is True
    assert governed.records[0].version == 2
    effective = resolve_effective_preferences(governed, context())
    assert effective.preferences == (preference("os", "android"),)
    assert resolve_effective_preferences(governed, context(category="laptop")).preferences == ()
    issued_projection = projection_result_from_effective(effective)
    visible = derive_long_term_memory_context(issued_projection).plain()
    assert visible == {
        "memoryRevision": 11,
        "provenance": "governed_projection_v2",
        "preferences": [
            {"category": "shopping_preference", "semanticKey": "os", "value": "android"}
        ],
    }
    for forged in (copy.copy(issued_projection),pickle.loads(pickle.dumps(issued_projection))):
        assert derive_long_term_memory_context(forged).payload_for_phase("planner") is None


def test_non_v2_or_unavailable_projection_fails_closed_to_empty_snapshot():
    governed = memory_snapshot_from_projection_v2(object(), authenticated_owner_user_id="user-1")
    assert governed.records == () and governed.revision == 0


def test_direct_v1_and_forged_terminal_snapshot_cannot_issue_model_context():
    direct_v1 = MemoryProjectionResult(
        5,
        (MemoryProjectionEntry("legacy-entry","shopping_preference","os","android",1,"ACTIVE"),),
        MemoryProjectionReason.AVAILABLE,
    )
    assert derive_long_term_memory_context(direct_v1).payload_for_phase("planner") is None

    forged_snapshot = MemorySnapshot("user-1",5,(record(1),),True)
    forged_effective = resolve_effective_preferences(forged_snapshot,context())
    with pytest.raises(ValueError,match="not V2-issued"):
        projection_result_from_effective(forged_effective)


def test_v2_owner_binding_mismatch_cannot_issue_model_context():
    entry = MemoryProjectionV2Entry(
        "entry-v2", "shopping_preference", "phone", "self", "os", "android",
        "explicit_user", "long_term_preference", 1, "ACTIVE",
        NOW - timedelta(days=5), NOW - timedelta(days=1), NOW + timedelta(days=20), None, True,
    )
    mismatched = fetched_v2_result((entry,),owner="other-user",revision=6)
    snapshot = memory_snapshot_from_projection_v2(mismatched,authenticated_owner_user_id="user-1")
    assert snapshot.records == ()
    with pytest.raises(ValueError,match="not V2-issued"):
        projection_result_from_effective(resolve_effective_preferences(snapshot,context()))


def test_only_original_fetch_v2_object_can_cross_governance_issuer():
    entry = MemoryProjectionV2Entry(
        "entry-v2", "shopping_preference", "phone", "self", "os", "android",
        "explicit_user", "long_term_preference", 1, "ACTIVE",
        NOW - timedelta(days=5), NOW - timedelta(days=1), NOW + timedelta(days=20), None, True,
    )
    issued=fetched_v2_result((entry,),revision=9)
    direct=MemoryProjectionV2Result(
        revision=issued.revision,owner_binding=issued.owner_binding,truncated=issued.truncated,
        entries=issued.entries,reason=issued.reason,
    )
    forged=(direct,copy.copy(issued),copy.deepcopy(issued),pickle.loads(pickle.dumps(issued)),replace(issued))
    for candidate in forged:
        snapshot=memory_snapshot_from_projection_v2(candidate,authenticated_owner_user_id="user-1")
        assert snapshot.records == () and snapshot.revision == 0
        with pytest.raises(ValueError,match="not V2-issued"):
            projection_result_from_effective(resolve_effective_preferences(snapshot,context()))

    mutated=fetched_v2_result((entry,),revision=10)
    object.__setattr__(mutated,"revision",11)
    snapshot=memory_snapshot_from_projection_v2(mutated,authenticated_owner_user_id="user-1")
    assert snapshot.records == () and snapshot.revision == 0
