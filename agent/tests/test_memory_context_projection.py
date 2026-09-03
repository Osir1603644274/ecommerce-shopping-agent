from dataclasses import FrozenInstanceError
import copy
import pickle
from datetime import UTC, datetime, timedelta

import pytest
import app.memory.context_projection as context_projection

from app.memory.context_projection import (
    LongTermMemoryContext,
    LongTermShoppingPreference,
    derive_long_term_memory_context,
)
from app.memory.long_term_memory import ShoppingPreference
from app.memory.projection_client import (
    MemoryProjectionEntry,
    MemoryProjectionReason,
    MemoryProjectionResult,
    MemoryProjectionV2Entry,
)
from tests.memory_v2_fetch_support import fetched_v2_result
from app.memory.governance import (
    MemoryApplicationContext,
    memory_snapshot_from_projection_v2,
    projection_result_from_effective,
    resolve_effective_preferences,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def _entry(category="shopping_preference", semantic_key="avoid_brand", value="brand-x"):
    return MemoryProjectionEntry("entry-" + semantic_key, category, semantic_key, value, 1, "ACTIVE")


def _governed(*values: tuple[str, str]):
    entries = tuple(
        MemoryProjectionV2Entry(
            f"entry-{index}","shopping_preference","phone","self",key,value,
            "explicit_user","long_term_preference",1,"ACTIVE",
            NOW-timedelta(days=5),NOW-timedelta(days=1),NOW+timedelta(days=20),None,True,
        )
        for index,(key,value) in enumerate(values,1)
    )
    result=fetched_v2_result(entries)
    snapshot=memory_snapshot_from_projection_v2(result,authenticated_owner_user_id="user-1")
    effective=resolve_effective_preferences(snapshot,MemoryApplicationContext(
        "user-1","phone","self",NOW,memory_enabled=True
    ))
    return projection_result_from_effective(effective)


def test_available_projection_filters_health_and_has_no_identifiers_or_value_repr():
    result = _governed(("avoid_brand","brand-x"))
    context = derive_long_term_memory_context(result)
    assert context.memory_revision == 7
    assert context.provenance == "governed_projection_v2"
    assert context.plain() == {
        "memoryRevision": 7,
        "provenance": "governed_projection_v2",
        "preferences": [{"category": "shopping_preference", "semanticKey": "avoid_brand", "value": "brand-x"}],
    }
    assert "entry-avoid_brand" not in str(context.plain())
    assert "brand-x" not in repr(context)
    assert "brand-x" not in repr(context.preferences[0])


def test_current_requirement_by_semantic_key_overrides_long_term_memory():
    result = _governed(("avoid_brand","brand-x"),("prefer_attribute","compact"))
    current = ShoppingPreference("shopping_preference", "avoid_brand", "brand-a")
    context = derive_long_term_memory_context(result, current_requirements=(current,))
    assert context.plain()["preferences"] == [
        {"category": "shopping_preference", "semanticKey": "prefer_attribute", "value": "compact"}
    ]


def test_unavailable_and_duplicate_semantic_projection_fail_closed_to_empty_context():
    unavailable = MemoryProjectionResult(reason=MemoryProjectionReason.UNAVAILABLE)
    assert derive_long_term_memory_context(unavailable) == LongTermMemoryContext()
    duplicate = MemoryProjectionResult(
        1,
        (_entry(), MemoryProjectionEntry("entry-other", "shopping_preference", "avoid_brand", "brand-a", 2, "ACTIVE")),
        MemoryProjectionReason.AVAILABLE,
    )
    assert derive_long_term_memory_context(duplicate) == LongTermMemoryContext()


def test_context_is_bounded_immutable_and_phase_limited():
    context = derive_long_term_memory_context(_governed(("avoid_brand","brand-x")))
    assert len(context.preferences) <= 8
    assert len(str(context.plain()).encode()) <= 4096
    assert context.payload_for_phase("planner") == context.plain()
    assert context.payload_for_phase("replanner") == context.plain()
    assert context.payload_for_phase("final_answer") == context.plain()
    assert context.payload_for_phase("executor") is None
    assert context.payload_for_phase("validator") is None
    with pytest.raises(FrozenInstanceError):
        context.preferences[0].value = "brand-a"
    with pytest.raises(ValueError):
        LongTermShoppingPreference("health", "avoid_material", "latex")
    with pytest.raises(ValueError):
        LongTermMemoryContext(1, "governed_projection_v2", (context.preferences[0],) * 9)
    with pytest.raises(ValueError):
        LongTermMemoryContext(0, True, ())  # type: ignore[arg-type]


def test_context_rejects_invalid_current_requirement_and_unknown_phase():
    result = _governed(("avoid_brand","brand-x"))
    with pytest.raises(ValueError):
        derive_long_term_memory_context(result, current_requirements=(object(),))
    with pytest.raises(ValueError):
        derive_long_term_memory_context(result).payload_for_phase("other")  # type: ignore[arg-type]


def test_public_context_constructor_is_not_a_model_issuer():
    preference = LongTermShoppingPreference("shopping_preference", "avoid_brand", "brand-x")
    context = LongTermMemoryContext(1, "governed_projection_v2", (preference,))
    with pytest.raises(ValueError, match="unissued"):
        context.payload_for_phase("planner")


def test_post_construction_field_and_registry_tampering_fail_closed():
    context = derive_long_term_memory_context(_governed(("avoid_brand","brand-x")))
    object.__setattr__(context.preferences[0], "category", "health")
    object.__setattr__(context, "preferences", (context.preferences[0],))
    assert context.payload_for_phase("planner")["preferences"][0]["category"] == "shopping_preference"
    issued = context_projection._ISSUED_CONTEXTS[id(context)]
    context_projection._ISSUED_CONTEXTS[id(context)] = context_projection._IssuedContext(
        issued.reference, issued.sealed, b"tampered"
    )
    with pytest.raises(ValueError):
        context.plain()


def test_derived_context_copy_pickle_and_forgery_cannot_become_issued():
    context = derive_long_term_memory_context(_governed(("avoid_brand","brand-x")))
    for forged in (copy.copy(context), pickle.loads(pickle.dumps(context))):
        with pytest.raises(ValueError):
            forged.payload_for_phase("planner")
