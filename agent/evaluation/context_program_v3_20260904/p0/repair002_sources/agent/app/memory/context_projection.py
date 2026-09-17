"""Bounded, low-priority model context from an authenticated projection.

This is deliberately separate from ContextPack wiring.  It holds no owner,
entry id, credential, expiry, consent, health or allergy data.
"""
from __future__ import annotations

import json
import hashlib
import secrets
import weakref
from dataclasses import dataclass, field
from typing import Iterable, Literal

from .long_term_memory import ShoppingPreference

MemoryContextPhase = Literal["planner", "replanner", "final_answer", "executor", "validator"]
_VISIBLE_PHASES = frozenset({"planner", "replanner", "final_answer"})
_ALL_PHASES = _VISIBLE_PHASES | frozenset({"executor", "validator"})
_MAX_ENTRIES = 8
_MAX_ENTRY_BYTES = 512
_MAX_BYTES = 4096
_PROVENANCE = "governed_projection_v2"
_EMPTY_PROVENANCE = "none"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True, slots=True, repr=False)
class LongTermShoppingPreference:
    category: str
    semantic_key: str
    value: str

    def __post_init__(self) -> None:
        if any(type(field) is not str for field in (self.category, self.semantic_key, self.value)):
            raise ValueError("invalid long-term shopping preference")
        preference = ShoppingPreference.from_plain(
            {"category": self.category, "semanticKey": self.semantic_key, "value": self.value}
        )
        if preference.category != "shopping_preference":
            raise ValueError("only shopping preferences are model-visible")
        if len(_canonical(self.plain())) > _MAX_ENTRY_BYTES:
            raise ValueError("long-term shopping preference exceeds byte budget")

    def plain(self) -> dict[str, str]:
        # Re-validate at every boundary; frozen dataclasses are not a security
        # boundary (object.__setattr__ can still mutate them).
        ShoppingPreference.from_plain(
            {"category": self.category, "semanticKey": self.semantic_key, "value": self.value}
        )
        return {"category": self.category, "semanticKey": self.semantic_key, "value": self.value}


@dataclass(frozen=True, slots=True, weakref_slot=True)
class LongTermMemoryContext:
    """Ephemeral context only; safe for planner/replanner/final-answer views."""

    memory_revision: int = 0
    provenance: Literal["governed_projection_v2", "none"] = _EMPTY_PROVENANCE
    preferences: tuple[LongTermShoppingPreference, ...] = field(default=(), repr=False)
    _sealed: bytes = field(default=b"", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.memory_revision) is not int or not 0 <= self.memory_revision <= 2**63 - 1:
            raise ValueError("invalid memory revision")
        if type(self.provenance) is not str or self.provenance not in {_PROVENANCE, _EMPTY_PROVENANCE}:
            raise ValueError("invalid memory provenance")
        if type(self.preferences) is not tuple or any(type(item) is not LongTermShoppingPreference for item in self.preferences):
            raise ValueError("invalid long-term preference collection")
        if len(self.preferences) > _MAX_ENTRIES:
            raise ValueError("too many long-term preferences")
        keys = [item.semantic_key for item in self.preferences]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate long-term semantic key")
        if tuple(sorted(self.preferences, key=lambda item: (item.category, item.semantic_key, item.value))) != self.preferences:
            raise ValueError("long-term preferences are not stable sorted")
        if self.provenance == _EMPTY_PROVENANCE and (self.memory_revision != 0 or self.preferences):
            raise ValueError("empty long-term context cannot carry projection data")
        payload = {
            "memoryRevision": self.memory_revision,
            "provenance": self.provenance,
            "preferences": [item.plain() for item in self.preferences],
        }
        sealed = _canonical(payload)
        if len(sealed) > _MAX_BYTES:
            raise ValueError("long-term context exceeds byte budget")
        # Public construction validates a value object only.  It does not
        # grant permission to expose memory to a model phase.
        object.__setattr__(self, "_sealed", sealed)

    def plain(self) -> dict[str, object]:
        # Always recover from the independently held canonical bytes.  This
        # prevents a mutated category/value/container from reaching planner.
        sealed = _context_seal(self)
        parsed = json.loads(sealed)
        if _canonical(parsed) != sealed or type(parsed) is not dict:
            raise ValueError("invalid sealed memory context")
        _validate_plain_context(parsed)
        return parsed

    def payload_for_phase(self, phase: MemoryContextPhase) -> dict[str, object] | None:
        if type(phase) is not str or phase not in _ALL_PHASES:
            raise ValueError("unknown context phase")
        if phase not in _VISIBLE_PHASES:
            return None
        # Empty/unavailable contexts intentionally carry no model payload.
        if not self.preferences:
            return None
        payload = self.plain()
        return payload if payload["preferences"] else None


def empty_long_term_memory_context() -> LongTermMemoryContext:
    return LongTermMemoryContext()


@dataclass(frozen=True, slots=True)
class _IssuedContext:
    reference: weakref.ReferenceType[LongTermMemoryContext]
    sealed: bytes
    digest: bytes


_ISSUED_CONTEXTS: dict[int, _IssuedContext] = {}
_ISSUANCE_DIGESTS: dict[int, tuple[weakref.ReferenceType[LongTermMemoryContext], bytes]] = {}
_ISSUER_BINDING = secrets.token_bytes(32)


def _register_context(context: LongTermMemoryContext, sealed: bytes) -> None:
    key = id(context)
    digest = hashlib.sha256(_ISSUER_BINDING + sealed + str(key).encode("ascii")).digest()
    def cleanup(reference: weakref.ReferenceType[LongTermMemoryContext]) -> None:
        saved = _ISSUED_CONTEXTS.get(key)
        if saved is not None and saved.reference is reference:
            _ISSUED_CONTEXTS.pop(key, None)
        anchored = _ISSUANCE_DIGESTS.get(key)
        if anchored is not None and anchored[0] is reference:
            _ISSUANCE_DIGESTS.pop(key, None)
    reference = weakref.ref(context, cleanup)
    _ISSUED_CONTEXTS[key] = _IssuedContext(reference, sealed, digest)
    _ISSUANCE_DIGESTS[key] = (reference, digest)


def _context_seal(context: object) -> bytes:
    if type(context) is not LongTermMemoryContext:
        raise ValueError("unvalidated memory context")
    issued = _ISSUED_CONTEXTS.get(id(context))
    anchor = _ISSUANCE_DIGESTS.get(id(context))
    if (
        issued is None or issued.reference() is not context
        or anchor is None or anchor[0]() is not context
        or anchor[1] != issued.digest
        or hashlib.sha256(_ISSUER_BINDING + issued.sealed + str(id(context)).encode("ascii")).digest() != issued.digest
    ):
        raise ValueError("unissued memory context")
    if type(issued.sealed) is not bytes or len(issued.sealed) > _MAX_BYTES:
        raise ValueError("invalid sealed memory context")
    return issued.sealed


def _validate_plain_context(raw: object) -> None:
    if type(raw) is not dict or set(raw) != {"memoryRevision", "provenance", "preferences"}:
        raise ValueError("invalid sealed memory context")
    revision = raw["memoryRevision"]
    provenance = raw["provenance"]
    preferences = raw["preferences"]
    pending: list[tuple[object, int]] = [(raw, 1)]
    while pending:
        value, depth = pending.pop()
        if depth > 8:
            raise ValueError("sealed context nesting exceeds budget")
        if type(value) is dict:
            pending.extend((child, depth + 1) for child in value.values())
        elif type(value) is list:
            pending.extend((child, depth + 1) for child in value)
    if type(revision) is not int or not 0 <= revision <= 2**63 - 1:
        raise ValueError("invalid sealed memory revision")
    if type(provenance) is not str or provenance not in {_PROVENANCE, _EMPTY_PROVENANCE}:
        raise ValueError("invalid sealed memory provenance")
    if type(preferences) is not list or len(preferences) > _MAX_ENTRIES:
        raise ValueError("invalid sealed memory preferences")
    checked = []
    if len(_canonical(raw)) > _MAX_BYTES:
        raise ValueError("sealed context exceeds byte budget")
    for item in preferences:
        if type(item) is not dict or set(item) != {"category", "semanticKey", "value"}:
            raise ValueError("invalid sealed preference")
        if item["category"] != "shopping_preference":
            raise ValueError("only shopping preferences are model-visible")
        if len(_canonical(item)) > _MAX_ENTRY_BYTES:
            raise ValueError("sealed preference exceeds byte budget")
        checked.append(ShoppingPreference.from_plain(item))
    keys = [item.semantic_key for item in checked]
    if len(keys) != len(set(keys)) or checked != sorted(checked, key=lambda x: (x.category, x.semantic_key, x.value)):
        raise ValueError("invalid sealed preference ordering")
    if provenance == _EMPTY_PROVENANCE and (revision != 0 or preferences):
        raise ValueError("invalid empty sealed context")


def _issue_context(revision: int, provenance: str, preferences: tuple[LongTermShoppingPreference, ...]) -> LongTermMemoryContext:
    """The sole model-context issuer; callers must come through B2 derive."""
    checked = LongTermMemoryContext(revision, provenance, preferences)
    context = object.__new__(LongTermMemoryContext)
    object.__setattr__(context, "memory_revision", checked.memory_revision)
    object.__setattr__(context, "provenance", checked.provenance)
    object.__setattr__(context, "preferences", checked.preferences)
    object.__setattr__(context, "_sealed", checked._sealed)
    _register_context(context, checked._sealed)
    return context


def derive_long_term_memory_context(
    result: object,
    *,
    current_requirements: Iterable[ShoppingPreference] = (),
) -> LongTermMemoryContext:
    """Expose only a V2-issued snapshot that passed scoped governance."""
    from .governance import _validated_governed_projection

    governed = _validated_governed_projection(result)
    if governed is None:
        return empty_long_term_memory_context()
    requirements = tuple(current_requirements)
    if any(type(item) is not ShoppingPreference for item in requirements):
        raise ValueError("current requirements must be exact ShoppingPreference values")
    current_keys = {item.semantic_key for item in requirements}
    retained: list[LongTermShoppingPreference] = []
    seen: set[str] = set()
    for entry in governed.entries:
        if entry.category != "shopping_preference" or entry.semantic_key in current_keys:
            continue
        if entry.semantic_key in seen:
            return empty_long_term_memory_context()
        try:
            retained.append(LongTermShoppingPreference(entry.category, entry.semantic_key, entry.value))
        except ValueError:
            return empty_long_term_memory_context()
        seen.add(entry.semantic_key)
    retained.sort(key=lambda item: (item.category, item.semantic_key, item.value))
    try:
        return _issue_context(governed.revision, _PROVENANCE, tuple(retained))
    except ValueError:
        return empty_long_term_memory_context()


__all__ = [
    "LongTermMemoryContext",
    "LongTermShoppingPreference",
    "MemoryContextPhase",
    "derive_long_term_memory_context",
    "empty_long_term_memory_context",
]
