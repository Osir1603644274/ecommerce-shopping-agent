"""Pure shopping-memory domain contracts; production authorization is absent."""
from __future__ import annotations
import hashlib, json, re, unicodedata, weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable, Literal, Protocol

MemoryOperation = Literal["write", "update", "revoke", "suppress", "do_not_write"]
MemoryStatus = Literal["active", "revoked", "suppressed", "expired", "superseded"]
MemoryCategory = Literal["shopping_preference", "health", "allergy"]
PRODUCTION_AUTHORIZATION_UNAVAILABLE = "PRODUCTION_AUTHORIZATION_UNAVAILABLE"
_MAX_ENTRIES, _MAX_BYTES, _MAX_ENTRY_BYTES = 8, 4096, 512
_TERMINAL = frozenset({"revoked", "suppressed", "expired", "superseded"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_FORBIDDEN_ID_TERMS = ("diagnosis", "authorization", "bearer", "token", "secret")
_TOKENS = {
    "shopping_preference": {
        # Existing generic contract values remain for backwards-compatible
        # safety tests.  The field-shaped values below are the bounded used-
        # phone slice: every value comes from the controlled catalog contract.
        "avoid_brand": frozenset({
            "apple", "huawei", "xiaomi", "oppo", "vivo", "honor",
            "samsung", "brand-a", "brand-b", "brand-x", "brand-y",
        }),
        "brand": frozenset({
            "apple", "huawei", "xiaomi", "oppo", "vivo", "honor", "samsung",
        }),
        "os": frozenset({"ios", "android"}),
        "battery_health": frozenset({"lt70", "70_80", "80_90", "90_plus"}),
        "screen_originality": frozenset({"original", "non_original"}),
        "motherboard_repair": frozenset({"not_repaired", "repaired"}),
        "battery_originality": frozenset({"original", "non_original"}),
        "scratch_level": frozenset({"none", "light", "obvious"}),
        "shell_condition": frozenset({"normal", "damaged"}),
        "avoid_material": frozenset({"latex", "nickel"}),
        "avoid_ingredient": frozenset({"dairy", "gluten", "peanut", "shellfish"}),
        "avoid_product_type": frozenset({"fragrance", "supplement"}),
        "prefer_attribute": frozenset({"budget", "compact", "waterproof"}),
        "prefer_brand": frozenset({"apple", "samsung"}),
        "prefer_category": frozenset({"phone", "tablet"}),
        "prefer_price": frozenset({"budget", "premium"}),
    },
    "health": {
        "avoid_ingredient": frozenset({"dairy", "gluten", "peanut", "shellfish"}),
        "avoid_material": frozenset({"latex", "nickel"}),
        "avoid_product_type": frozenset({"fragrance", "supplement"}),
    },
    "allergy": {
        "avoid_ingredient": frozenset({"dairy", "gluten", "peanut", "shellfish"}),
        "avoid_material": frozenset({"latex", "nickel"}),
        "avoid_product_type": frozenset({"fragrance", "supplement"}),
    },
}

class DurableConsentLedger(Protocol):
    """Future durable-server port; this module provides no implementation."""
    def consume_if_absent(self, *, owner_user_id: str, event_id: str, command_digest: str, content_digest: str) -> bool: ...

def _text(value: object, field: str, maximum: int = 128) -> str:
    if type(value) is not str or not value or value.strip() != value or len(value.encode()) > maximum: raise ValueError(f"invalid {field}")
    return value
def _oid(value: object, field: str) -> str:
    value = _text(value, field, 64)
    if not _ID.fullmatch(value) or any(x in value.casefold() for x in _FORBIDDEN_ID_TERMS): raise ValueError(f"invalid {field}")
    return value
def _digest(value: object, field: str) -> str:
    value = _text(value, field, 64)
    if len(value) != 64 or any(x not in "0123456789abcdef" for x in value): raise ValueError(f"invalid {field}")
    return value
def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None: raise ValueError(f"invalid {field}")
    return value.astimezone(UTC)
def _plain(value: object) -> object:
    if value is None or type(value) in {bool, int}: return value
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}: raise ValueError("invalid JSON number")
        return value
    if type(value) is str:
        if len(value.encode()) > 256: raise ValueError("JSON string exceeds budget")
        return value
    if type(value) in {tuple, list}: return [_plain(x) for x in value]
    if type(value) is dict:
        out = {}
        for key, nested in value.items():
            if type(key) is not str or not key.isascii() or unicodedata.normalize("NFKC", key) != key or key in out: raise ValueError("invalid JSON key")
            out[key] = _plain(nested)
        return out
    raise ValueError("non-plain value is forbidden")
def _bytes(value: object) -> bytes: return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
def _load(raw: bytes) -> dict[str, object]:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise ValueError("duplicate JSON key")
            out[key] = value
        return out
    try: result = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, json.JSONDecodeError) as exc: raise ValueError("invalid canonical projection JSON") from exc
    if type(result) is not dict or _bytes(result) != raw: raise ValueError("non-canonical projection JSON")
    return result
def _slots(value: object, expected: tuple[str, ...], kind: type) -> None:
    if type(value) is not kind or hasattr(value, "__dict__") or tuple(type(value).__slots__) != expected: raise ValueError("unvalidated contract object")
def _pref(category: object, key: object, value: object) -> tuple[str, str, str]:
    if type(category) is not str or type(key) is not str or type(value) is not str or category not in _TOKENS: raise ValueError("invalid shopping preference")
    if not value.isascii() or value.lower() != value or unicodedata.normalize("NFKC", value) != value or value not in _TOKENS[category].get(key, frozenset()): raise ValueError("invalid controlled shopping preference")
    return category, key, value

@dataclass(frozen=True, slots=True)
class ShoppingPreference:
    category: MemoryCategory; semantic_key: str; value: str
    def __post_init__(self): _pref(self.category, self.semantic_key, self.value)
    @classmethod
    def from_plain(cls, raw: object) -> "ShoppingPreference":
        if type(raw) is not dict or set(raw) != {"category", "semanticKey", "value"}: raise ValueError("invalid shopping preference")
        return cls(*_pref(raw["category"], raw["semanticKey"], raw["value"]))
    def plain(self) -> dict[str, str]:
        _slots(self, ("category", "semantic_key", "value"), ShoppingPreference); _pref(self.category, self.semantic_key, self.value)
        return {"category": self.category, "semanticKey": self.semantic_key, "value": self.value}

@dataclass(frozen=True, slots=True)
class MemoryCommandDraft:
    """Untrusted intent only; no Python construction authorizes a write."""
    command_id: str; operation: MemoryOperation; owner_user_id_claim: str; preference: ShoppingPreference | None; consent_event_id: str; command_digest: str; content_digest: str
    def __post_init__(self):
        _oid(self.command_id, "commandId"); _oid(self.owner_user_id_claim, "ownerUserId"); _oid(self.consent_event_id, "consentEventId"); _digest(self.command_digest, "commandDigest"); _digest(self.content_digest, "contentDigest")
        if type(self.operation) is not str or self.operation not in {"write", "update", "revoke", "suppress", "do_not_write"}: raise ValueError("invalid memory command draft")
        if self.preference is not None: _slots(self.preference, ("category", "semantic_key", "value"), ShoppingPreference); self.preference.plain()
        if self.operation in {"write", "update"} and self.preference is None: raise ValueError("memory command draft requires preference")
    @classmethod
    def from_plain(cls, raw: object) -> "MemoryCommandDraft":
        fields = {"commandId", "operation", "ownerUserId", "preference", "consentEventId", "commandDigest", "contentDigest"}
        if type(raw) is not dict or set(raw) != fields: raise ValueError("invalid memory command draft")
        return cls(_oid(raw["commandId"], "commandId"), raw["operation"], _oid(raw["ownerUserId"], "ownerUserId"), None if raw["preference"] is None else ShoppingPreference.from_plain(raw["preference"]), _oid(raw["consentEventId"], "consentEventId"), _digest(raw["commandDigest"], "commandDigest"), _digest(raw["contentDigest"], "contentDigest"))

@dataclass(frozen=True, slots=True)
class LongTermMemoryEntry:
    entry_id: str; owner_user_id: str; version: int; status: MemoryStatus; preference: ShoppingPreference; expires_at: datetime; supersedes: str | None = None
    def __post_init__(self):
        _oid(self.entry_id, "entryId"); _oid(self.owner_user_id, "ownerUserId")
        if type(self.version) is not int or self.version < 1 or type(self.status) is not str or self.status not in {"active", "revoked", "suppressed", "expired", "superseded"}: raise ValueError("invalid memory entry")
        if (self.version == 1 and self.supersedes is not None) or (self.version > 1 and self.supersedes is None): raise ValueError("invalid memory entry version chain")
        if self.supersedes is not None: _oid(self.supersedes, "supersedes")
        _slots(self.preference, ("category", "semantic_key", "value"), ShoppingPreference); self.preference.plain(); _utc(self.expires_at, "expiresAt")
    @classmethod
    def from_plain(cls, raw: object) -> "LongTermMemoryEntry":
        fields = {"entryId", "ownerUserId", "version", "status", "preference", "expiresAt", "supersedes"}
        if type(raw) is not dict or set(raw) != fields: raise ValueError("invalid memory entry")
        return cls(_oid(raw["entryId"], "entryId"), _oid(raw["ownerUserId"], "ownerUserId"), raw["version"], raw["status"], ShoppingPreference.from_plain(raw["preference"]), _utc(raw["expiresAt"], "expiresAt"), None if raw["supersedes"] is None else _oid(raw["supersedes"], "supersedes"))
    def plain(self) -> dict[str, object]:
        _slots(self, ("entry_id", "owner_user_id", "version", "status", "preference", "expires_at", "supersedes"), LongTermMemoryEntry); self.__post_init__()
        return {"entryId": self.entry_id, "ownerUserId": self.owner_user_id, "version": self.version, "status": self.status, "preference": self.preference.plain(), "expiresAt": _utc(self.expires_at, "expiresAt").isoformat(), "supersedes": self.supersedes}
    @property
    def logical_key(self): return self.owner_user_id, self.preference.category, self.preference.semantic_key

def _entry(value: object) -> LongTermMemoryEntry:
    _slots(value, ("entry_id", "owner_user_id", "version", "status", "preference", "expires_at", "supersedes"), LongTermMemoryEntry)
    return LongTermMemoryEntry.from_plain({**value.plain(), "expiresAt": value.expires_at})

@dataclass(frozen=True, slots=True, weakref_slot=True)
class LongTermMemoryProjection:
    owner_user_id_claim: str; entries: tuple[LongTermMemoryEntry, ...]
    def __post_init__(self):
        _oid(self.owner_user_id_claim, "ownerUserId")
        if type(self.entries) is not tuple: raise ValueError("invalid projection contract")
        for item in self.entries: _entry(item)
    @classmethod
    def from_entries(cls, *, owner_user_id_claim: object, entries: Iterable[LongTermMemoryEntry], now: datetime) -> "LongTermMemoryProjection":
        owner, present = _oid(owner_user_id_claim, "ownerUserId"), _utc(now, "now")
        source = tuple(_entry(x) for x in entries); projection = cls(owner, _select(owner, source, present)); output = _output(projection)
        source_bytes = _bytes({"ownerUserId": owner, "authoritativeNow": present.isoformat(), "entries": [x.plain() for x in source]})
        _issue(projection, output, source_bytes, present); return projection

def _select(owner: str, source: tuple[LongTermMemoryEntry, ...], present: datetime) -> tuple[LongTermMemoryEntry, ...]:
    if any(x.owner_user_id != owner for x in source): raise ValueError("projection contains another owner claim")
    ids = [x.entry_id for x in source]
    if len(ids) != len(set(ids)): raise ValueError("memory entry IDs must be globally unique")
    groups = {}
    for item in source: groups.setdefault(item.logical_key, []).append(item)
    selected = []
    for chain in groups.values():
        chain.sort(key=lambda x: x.version)
        if [x.version for x in chain] != list(range(1, len(chain) + 1)): raise ValueError("memory version chain is not continuous")
        for index, item in enumerate(chain):
            if index and item.supersedes != chain[index - 1].entry_id: raise ValueError("memory supersedes chain is broken")
        # A revoke/suppress/expiry is terminal for the projection at that time,
        # not a lifetime ban on the logical key.  A later explicit user consent
        # may append a new active version; only the newest version is projected.
        if chain[-1].status == "active" and chain[-1].expires_at > present: selected.append(chain[-1])
    selected.sort(key=lambda x: (x.preference.category, x.preference.semantic_key, x.entry_id))
    if len(selected) > _MAX_ENTRIES: raise ValueError("projection has too many entries")
    return tuple(selected)

def _projection(value: object) -> dict[str, object]:
    _slots(value, ("owner_user_id_claim", "entries", "__weakref__"), LongTermMemoryProjection); value.__post_init__()
    if len(value.entries) > _MAX_ENTRIES: raise ValueError("invalid projection contract")
    entries = [_entry(x).plain() for x in value.entries]
    if any(x["status"] != "active" or x["ownerUserId"] != value.owner_user_id_claim for x in entries): raise ValueError("projection has inactive or foreign entries")
    keys = [(x["preference"]["category"], x["preference"]["semanticKey"]) for x in entries]
    if len(keys) != len(set(keys)): raise ValueError("projection has duplicate semantic keys")
    if any(len(_bytes(x)) > _MAX_ENTRY_BYTES for x in entries): raise ValueError("projection entry exceeds byte budget")
    return {"ownerUserId": _oid(value.owner_user_id_claim, "ownerUserId"), "entries": entries}
def _output(value: object) -> bytes:
    raw = _bytes(_projection(value))
    if len(raw) > _MAX_BYTES: raise ValueError("projection exceeds 4KiB")
    return raw

@dataclass(frozen=True, slots=True)
class _Issued:
    reference: weakref.ReferenceType; nonce: bytes; output: bytes; source: bytes; now: datetime
_ISSUED: dict[int, _Issued] = {}
def _issue(projection, output, source, now):
    key, nonce = id(projection), hashlib.sha256(output + source + str(id(projection)).encode()).digest()
    def cleanup(reference):
        saved = _ISSUED.get(key)
        if saved is not None and saved.reference is reference and saved.nonce == nonce: _ISSUED.pop(key, None)
    reference = weakref.ref(projection, cleanup); _ISSUED[key] = _Issued(reference, nonce, output, source, now)
def _issued_source(raw: bytes):
    parsed = _load(raw)
    if set(parsed) != {"ownerUserId", "authoritativeNow", "entries"} or type(parsed["entries"]) is not list: raise ValueError("invalid issued projection")
    now = _utc(datetime.fromisoformat(parsed["authoritativeNow"]), "authoritativeNow")
    entries = tuple(LongTermMemoryEntry.from_plain({**item, "expiresAt": datetime.fromisoformat(item["expiresAt"])}) for item in parsed["entries"])
    return _oid(parsed["ownerUserId"], "ownerUserId"), now, entries

def serialize_projection(projection: object) -> bytes:
    """The sole formal output; only an issued exact instance is accepted."""
    _slots(projection, ("owner_user_id_claim", "entries", "__weakref__"), LongTermMemoryProjection)
    issued = _ISSUED.get(id(projection))
    if issued is None or issued.reference() is not projection: raise ValueError("unissued projection")
    if _output(projection) != issued.output: raise ValueError("issued projection was modified")
    owner, present, source = _issued_source(issued.source)
    if owner != projection.owner_user_id_claim or present != issued.now: raise ValueError("issued projection authority mismatch")
    if _output(LongTermMemoryProjection(owner, _select(owner, source, present))) != issued.output: raise ValueError("issued terminal evidence mismatch")
    return issued.output

def _pref_object(value: object) -> dict[str, str]:
    _slots(value, ("category", "semantic_key", "value"), ShoppingPreference); return value.plain()
def applicable_preferences(*, current_turn: Iterable[ShoppingPreference] = (), task: Iterable[ShoppingPreference] = (), projection: LongTermMemoryProjection) -> tuple[ShoppingPreference, ...]:
    parsed = _load(serialize_projection(projection)); memory = tuple(ShoppingPreference.from_plain(x["preference"]) for x in parsed["entries"])
    result, occupied = [], set()
    for layer in (tuple(current_turn), tuple(task), memory):
        by_key = {}
        for item in layer:
            checked = ShoppingPreference.from_plain(_pref_object(item)); key = (checked.category, checked.semantic_key)
            if key in by_key and by_key[key] != checked: raise ValueError("same-layer preference conflict")
            by_key[key] = checked
        for key in sorted(by_key):
            if key not in occupied: result.append(by_key[key]); occupied.add(key)
    return tuple(result)

__all__ = ["DurableConsentLedger", "LongTermMemoryEntry", "LongTermMemoryProjection", "MemoryCommandDraft", "PRODUCTION_AUTHORIZATION_UNAVAILABLE", "ShoppingPreference", "applicable_preferences", "serialize_projection"]
