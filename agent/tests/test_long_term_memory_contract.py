from __future__ import annotations

from datetime import UTC, datetime, timedelta
import copy

import pytest

from app.memory import (
    LongTermMemoryEntry,
    LongTermMemoryProjection,
    MemoryCommandDraft,
    PRODUCTION_AUTHORIZATION_UNAVAILABLE,
    ShoppingPreference,
    applicable_preferences,
    serialize_projection,
)

NOW = datetime(2026, 8, 22, tzinfo=UTC)


def pref(category="shopping_preference", key="avoid_brand", value="brand-x"):
    return ShoppingPreference.from_plain({"category": category, "semanticKey": key, "value": value})


def entry(index, preference=None, *, version=1, supersedes=None, status="active", owner="user-1", expires=None):
    return LongTermMemoryEntry.from_plain({
        "entryId": f"entry-{index}", "ownerUserId": owner, "version": version, "status": status,
        "preference": (preference or pref()).plain(), "expiresAt": expires or NOW + timedelta(days=30),
        "supersedes": supersedes,
    })


def test_public_contract_never_issues_owner_or_authorized_write():
    import app.memory as memory
    assert memory.PRODUCTION_AUTHORIZATION_UNAVAILABLE == "PRODUCTION_AUTHORIZATION_UNAVAILABLE"
    forbidden = {"issue_server_principal", "ServerAuthenticatedOwner", "LongTermMemoryPolicy", "LongTermMemoryDecision", "ExplicitConsent"}
    assert not (forbidden & set(memory.__all__))
    assert not hasattr(memory, "issue_server_principal")
    assert "authorized" not in MemoryCommandDraft.__doc__.lower()


def test_command_draft_is_structural_not_an_authorization_conclusion():
    draft = MemoryCommandDraft.from_plain({
        "commandId": "draft-1", "operation": "write", "ownerUserId": "claimed-user",
        "preference": pref().plain(), "consentEventId": "event-1",
        "commandDigest": "a" * 64, "contentDigest": "b" * 64,
    })
    assert draft.owner_user_id_claim == "claimed-user"
    assert not hasattr(draft, "allowed") and not hasattr(draft, "authorize")
    with pytest.raises(ValueError):
        MemoryCommandDraft.from_plain({"commandId": "x", "operation": "write", "ownerUserId": "u", "preference": pref().plain(), "consentEventId": "e", "commandDigest": "a" * 64, "contentDigest": "b" * 64, "authorized": True})


@pytest.mark.parametrize("category,key,value", [
    ("allergy", "avoid_ingredient", "diagnosedwithanaphylaxis"),
    ("allergy", "avoid_ingredient", "unknownsecret"),
    ("allergy", "avoid_ingredient", "花生"),
    ("allergy", "avoid_ingredient", "ｐｅａｎｕｔ"),
    ("allergy", "avoid_ingredient", "реanut"),
])
def test_category_semantic_key_whitelist_rejects_sensitive_free_text_without_echo(category, key, value):
    with pytest.raises(ValueError) as raised:
        pref(category, key, value)
    assert value not in str(raised.value)
    with pytest.raises(ValueError):
        pref("allergy", "avoid_brand", "apple")


def test_preference_is_validated_at_current_task_entry_and_projection_layers():
    projection = LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[entry(1)], now=NOW)
    assert applicable_preferences(current_turn=[pref("shopping_preference", "avoid_brand", "brand-a")], task=[pref()], projection=projection)[0].value == "brand-a"
    bad = object.__new__(ShoppingPreference)
    object.__setattr__(bad, "category", "allergy")
    object.__setattr__(bad, "semantic_key", "avoid_ingredient")
    object.__setattr__(bad, "value", "unknownsecret")
    with pytest.raises(ValueError):
        applicable_preferences(current_turn=[bad], projection=projection)
    with pytest.raises(ValueError):
        LongTermMemoryEntry.from_plain({"entryId": "x", "ownerUserId": "u", "version": 1, "status": "active", "preference": {"category": "allergy", "semanticKey": "avoid_ingredient", "value": "unknownsecret"}, "expiresAt": NOW, "supersedes": None})


@pytest.mark.parametrize("status", ["revoked", "suppressed", "expired", "superseded"])
def test_explicit_later_version_can_reopen_a_terminal_logical_key(status):
    first = entry(1)
    terminal = entry(2, version=2, supersedes=first.entry_id, status=status)
    successor = entry(3, version=3, supersedes=terminal.entry_id)
    projection = LongTermMemoryProjection.from_entries(
        owner_user_id_claim="user-1",
        entries=[first, terminal, successor],
        now=NOW,
    )
    assert projection.entries == (successor,)


def test_projection_filters_tombstones_expiry_foreign_owner_and_broken_versions():
    first = entry(1)
    terminal = entry(2, version=2, supersedes=first.entry_id, status="revoked")
    assert LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[first, terminal], now=NOW).entries == ()
    expired = entry(3, preference=pref("shopping_preference", "avoid_material", "latex"), expires=NOW - timedelta(seconds=1))
    assert LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[expired], now=NOW).entries == ()
    with pytest.raises(ValueError, match="another owner"):
        LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[entry(4, owner="user-2")], now=NOW)
    with pytest.raises(ValueError, match="continuous"):
        LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[first, entry(3, version=3, supersedes=first.entry_id)], now=NOW)


def test_serializer_is_only_formal_output_and_rebuilds_from_exact_plain_fields():
    entries = [
        entry(1, pref("shopping_preference", "avoid_brand", "brand-x")), entry(2, pref("shopping_preference", "avoid_material", "latex")),
        entry(3, pref("shopping_preference", "avoid_ingredient", "peanut")), entry(4, pref("shopping_preference", "avoid_product_type", "fragrance")),
        entry(5, pref("shopping_preference", "prefer_attribute", "budget")), entry(6, pref("shopping_preference", "prefer_brand", "apple")),
        entry(7, pref("shopping_preference", "prefer_category", "phone")), entry(8, pref("shopping_preference", "prefer_price", "premium")),
    ]
    projection = LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=entries, now=NOW)
    raw = serialize_projection(projection)
    assert len(raw) <= 4096 and not hasattr(projection, "model_dump")
    with pytest.raises(ValueError, match="unvalidated|invalid projection"):
        serialize_projection(type("ProjectionClone", (), {"owner_user_id_claim": "user-1", "entries": ()})())
    object.__setattr__(projection, "entries", tuple(entries + [entries[0]]))
    with pytest.raises(ValueError, match="invalid projection"):
        serialize_projection(projection)


def test_serializer_rejects_poisoned_nested_object_and_public_port_never_claims_durability():
    projection = LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[entry(1)], now=NOW)
    poisoned = object.__new__(LongTermMemoryEntry)
    object.__setattr__(poisoned, "entry_id", "entry-x")
    object.__setattr__(poisoned, "owner_user_id", "user-1")
    object.__setattr__(poisoned, "version", 1)
    object.__setattr__(poisoned, "status", "active")
    object.__setattr__(poisoned, "preference", {"category": "allergy", "semanticKey": "avoid_ingredient", "value": ["diagnosis"]})
    object.__setattr__(poisoned, "expires_at", NOW)
    object.__setattr__(poisoned, "supersedes", None)
    object.__setattr__(projection, "entries", (poisoned,))
    with pytest.raises(ValueError):
        serialize_projection(projection)
    import app.memory as memory
    assert "DurableConsentLedger" in memory.__all__
    assert not any("allow" in name.casefold() for name in memory.__all__)


def test_only_identity_issued_projection_serializes_and_equal_instances_do_not_share_authority():
    issued = LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[entry(1)], now=NOW)
    equal_but_direct = LongTermMemoryProjection("user-1", issued.entries)
    assert serialize_projection(issued)
    for forged in (equal_but_direct, copy.copy(issued), copy.deepcopy(issued)):
        with pytest.raises(ValueError, match="unissued"):
            serialize_projection(forged)
    second = LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[entry(2)], now=NOW)
    object.__setattr__(issued, "entries", second.entries)
    with pytest.raises(ValueError, match="modified"):
        serialize_projection(issued)
    assert serialize_projection(second)


def test_issued_snapshot_binds_authoritative_now_terminal_evidence_and_global_entry_ids():
    expired = entry(1, expires=NOW - timedelta(seconds=1))
    projection = LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[expired], now=NOW)
    assert b'"entries":[]' in serialize_projection(projection)
    first = entry(2)
    terminal = entry(3, version=2, supersedes=first.entry_id, status="suppressed")
    tombstone = LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[first, terminal], now=NOW)
    assert b'"entries":[]' in serialize_projection(tombstone)
    with pytest.raises(ValueError, match="globally unique"):
        LongTermMemoryProjection.from_entries(owner_user_id_claim="user-1", entries=[entry(9), entry(9, preference=pref("shopping_preference", "avoid_material", "latex"))], now=NOW)


@pytest.mark.parametrize("identifier", ["bad id", "owner:1", "Bearer-x", "diagnosis-x", "秘密"])
def test_public_constructors_reject_sensitive_or_nonopaque_ids_without_echo(identifier):
    with pytest.raises(ValueError) as raised:
        MemoryCommandDraft(identifier, "write", "user-1", pref(), "event-1", "a" * 64, "b" * 64)
    assert identifier not in str(raised.value)
    with pytest.raises(ValueError):
        LongTermMemoryEntry(identifier, "user-1", 1, "active", pref(), NOW + timedelta(days=1))
