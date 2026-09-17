"""Disabled-by-default authenticated, read-only memory projection adapter."""
from __future__ import annotations
import json
import hashlib
import re
import secrets
import weakref
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import httpx
from .long_term_memory import ShoppingPreference

_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+={0,2}\Z")
# Java's Instant JSON representation is deliberately narrower than generic
# ISO-8601: it is ASCII, UTC, and carries a literal trailing ``Z``.
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z\Z")
_MAX_RESPONSE_BYTES = 8192
_MAX_V2_RESPONSE_BYTES = 16384
_MAX_JSON_DEPTH = 32
class MemoryProjectionReason(str, Enum): AVAILABLE="available"; DISABLED="disabled"; NO_CREDENTIAL="no_credential"; UNAVAILABLE="unavailable"; INVALID="invalid"
class MemoryAccessCredential:
    """Opaque process-local credential; the bearer is held outside the object.

    This is a provider-neutral seam, not a cross-process security boundary.
    The token is never a field, property, repr value, or pickle payload.
    """
    __slots__ = ("__weakref__",)
    def __init__(self, token: str):
        if type(token) is not str or token.strip()!=token or not 1<=len(token)<=2048 or not token.isascii() or not _BEARER.fullmatch(token): raise ValueError("invalid credential")
        _CREDENTIALS[id(self)] = (weakref.ref(self, _forget_credential), token)
    def __repr__(self): return "MemoryAccessCredential(<opaque>)"
    def __copy__(self): raise TypeError("credential cannot be copied")
    __deepcopy__ = __copy__
    def __reduce_ex__(self, protocol): raise TypeError("credential cannot be pickled")

_CREDENTIALS: dict[int, tuple[weakref.ReferenceType[MemoryAccessCredential], str]] = {}
def _forget_credential(reference):
    for key, item in tuple(_CREDENTIALS.items()):
        if item[0] is reference: _CREDENTIALS.pop(key, None)
def _credential_token(credential: object) -> str:
    if type(credential) is not MemoryAccessCredential: raise ValueError("invalid credential")
    item = _CREDENTIALS.get(id(credential))
    if item is None or item[0]() is not credential: raise ValueError("invalid credential")
    return item[1]
@dataclass(frozen=True, slots=True, repr=False)
class MemoryProjectionEntry:
    entry_id: str; category: str; semantic_key: str; value: str; version: int; status: str
    def __post_init__(self):
        if any(type(field) is not str for field in (self.entry_id, self.category, self.semantic_key, self.value, self.status)):
            raise ValueError("invalid entry")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",self.entry_id) or type(self.version) is not int or not 1<=self.version<=2**31-1 or self.status!="ACTIVE": raise ValueError("invalid entry")
        ShoppingPreference.from_plain({"category":self.category,"semanticKey":self.semantic_key,"value":self.value})
@dataclass(frozen=True, slots=True, weakref_slot=True)
class MemoryProjectionResult:
    revision: int = 0; entries: tuple[MemoryProjectionEntry, ...] = field(default=(), repr=False); reason: MemoryProjectionReason = MemoryProjectionReason.DISABLED
    def __post_init__(self):
        if type(self.revision) is not int or not 0 <= self.revision <= 2**63 - 1:
            raise ValueError("invalid projection revision")
        if type(self.entries) is not tuple or any(type(item) is not MemoryProjectionEntry for item in self.entries):
            raise ValueError("invalid projection entries")
        if type(self.reason) is not MemoryProjectionReason:
            raise ValueError("invalid projection reason")


@dataclass(frozen=True, slots=True, repr=False)
class MemoryProjectionV2Entry:
    entry_id: str
    memory_category: str
    product_category: str
    recipient_scope: str
    semantic_key: str
    value: str
    source: str
    data_class: str
    version: int
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    supersedes: str | None
    chain_verified: bool

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.entry_id):
            raise ValueError("invalid v2 entry")
        if self.memory_category != "shopping_preference":
            raise ValueError("invalid v2 entry")
        if self.product_category not in {"phone", "laptop", "headphones"}:
            raise ValueError("invalid v2 entry")
        if self.recipient_scope != "self" or self.source not in {"explicit_user", "user_confirmed"}:
            raise ValueError("invalid v2 entry")
        if self.data_class != "long_term_preference" or self.status != "ACTIVE" or self.chain_verified is not True:
            raise ValueError("invalid v2 entry")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("invalid v2 entry")
        if (self.version == 1) != (self.supersedes is None):
            raise ValueError("invalid v2 entry")
        if self.supersedes is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.supersedes):
            raise ValueError("invalid v2 entry")
        ShoppingPreference.from_plain({"category": self.memory_category, "semanticKey": self.semantic_key, "value": self.value})
        for stamp in (self.created_at, self.updated_at, self.expires_at):
            if type(stamp) is not datetime or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("invalid v2 entry")
        if self.updated_at < self.created_at or self.expires_at <= self.updated_at:
            raise ValueError("invalid v2 entry")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class MemoryProjectionV2Result:
    revision: int = 0
    owner_binding: str = ""
    truncated: bool = False
    entries: tuple[MemoryProjectionV2Entry, ...] = field(default=(), repr=False)
    reason: MemoryProjectionReason = MemoryProjectionReason.DISABLED

    def __post_init__(self) -> None:
        if type(self.revision) is not int or not 0 <= self.revision <= 2**63 - 1:
            raise ValueError("invalid v2 projection revision")
        if self.reason is MemoryProjectionReason.AVAILABLE:
            if type(self.owner_binding) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.owner_binding):
                raise ValueError("invalid v2 owner binding")
        elif self.owner_binding != "":
            raise ValueError("unavailable v2 projection cannot bind an owner")
        if type(self.truncated) is not bool or type(self.entries) is not tuple or len(self.entries) > 8:
            raise ValueError("invalid v2 projection")
        if any(type(item) is not MemoryProjectionV2Entry for item in self.entries):
            raise ValueError("invalid v2 projection")
        if type(self.reason) is not MemoryProjectionReason:
            raise ValueError("invalid v2 projection reason")


@dataclass(frozen=True, slots=True)
class _IssuedV2Projection:
    reference: weakref.ReferenceType[MemoryProjectionV2Result]
    sealed: bytes
    digest: bytes


_V2_ISSUER_BINDING = secrets.token_bytes(32)
_ISSUED_V2_PROJECTIONS: dict[int, _IssuedV2Projection] = {}
_V2_PROJECTION_ANCHORS: dict[int, tuple[weakref.ReferenceType[MemoryProjectionV2Result], bytes]] = {}


def _v2_projection_seal(result: MemoryProjectionV2Result) -> bytes:
    value = {
        "revision": result.revision,
        "ownerBinding": result.owner_binding,
        "truncated": result.truncated,
        "reason": result.reason.value,
        "entries": [
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
                "createdAt": item.created_at.isoformat(),
                "updatedAt": item.updated_at.isoformat(),
                "expiresAt": item.expires_at.isoformat(),
                "supersedes": item.supersedes,
                "chainVerified": item.chain_verified,
            }
            for item in result.entries
        ],
    }
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")


def _register_v2_projection(result: MemoryProjectionV2Result) -> None:
    key=id(result)
    sealed=_v2_projection_seal(result)
    digest=hashlib.sha256(_V2_ISSUER_BINDING+sealed+str(key).encode("ascii")).digest()

    def cleanup(reference: weakref.ReferenceType[MemoryProjectionV2Result]) -> None:
        issued=_ISSUED_V2_PROJECTIONS.get(key)
        if issued is not None and issued.reference is reference:
            _ISSUED_V2_PROJECTIONS.pop(key,None)
        anchor=_V2_PROJECTION_ANCHORS.get(key)
        if anchor is not None and anchor[0] is reference:
            _V2_PROJECTION_ANCHORS.pop(key,None)

    reference=weakref.ref(result,cleanup)
    _ISSUED_V2_PROJECTIONS[key]=_IssuedV2Projection(reference,sealed,digest)
    _V2_PROJECTION_ANCHORS[key]=(reference,digest)


def _validated_issued_v2_projection(result: object) -> MemoryProjectionV2Result | None:
    if type(result) is not MemoryProjectionV2Result or result.reason is not MemoryProjectionReason.AVAILABLE:
        return None
    issued=_ISSUED_V2_PROJECTIONS.get(id(result))
    anchor=_V2_PROJECTION_ANCHORS.get(id(result))
    if issued is None or issued.reference() is not result or anchor is None or anchor[0]() is not result:
        return None
    try:
        sealed=_v2_projection_seal(result)
    except (AttributeError,TypeError,ValueError):
        return None
    expected=hashlib.sha256(_V2_ISSUER_BINDING+sealed+str(id(result)).encode("ascii")).digest()
    if sealed!=issued.sealed or expected!=issued.digest or anchor[1]!=issued.digest:
        return None
    return result
def _loads(raw: bytes) -> Any:
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError("duplicate")
            result[key]=value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def _within_depth(value: object) -> bool:
    """Iterative post-parse gate; do not let deeply nested JSON reach policy."""
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            return False
        if type(current) is dict:
            pending.extend((nested, depth + 1) for nested in current.values())
        elif type(current) is list:
            pending.extend((nested, depth + 1) for nested in current)
    return True
class MemoryProjectionClient:
    def __init__(self, *, enabled: bool, backend_url: str, timeout_seconds: float=2.0): self._enabled=enabled; self._url=backend_url.rstrip("/")+"/api/memory/projection"; self._url_v2=backend_url.rstrip("/")+"/api/memory/projection/v2"; self._timeout=max(0.1,min(float(timeout_seconds),5.0))
    async def fetch(self, credential: MemoryAccessCredential | None) -> MemoryProjectionResult:
        if not self._enabled: return MemoryProjectionResult(reason=MemoryProjectionReason.DISABLED)
        if credential is None: return MemoryProjectionResult(reason=MemoryProjectionReason.NO_CREDENTIAL)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("GET",self._url,headers={"Authorization":"Bearer "+_credential_token(credential)}) as response:
                    # Never pull untrusted response bytes for non-authoritative status.
                    if response.status_code != 200:
                        return MemoryProjectionResult(reason=MemoryProjectionReason.UNAVAILABLE)
                    encoding = response.headers.get("content-encoding", "")
                    if type(encoding) is not str or encoding not in {"", "identity"}:
                        return MemoryProjectionResult(reason=MemoryProjectionReason.INVALID)
                    chunks=[]; size=0
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if type(chunk) is not bytes:
                            return MemoryProjectionResult(reason=MemoryProjectionReason.INVALID)
                        size+=len(chunk)
                        if size > _MAX_RESPONSE_BYTES:
                            return MemoryProjectionResult(reason=MemoryProjectionReason.INVALID)
                        chunks.append(chunk)
                    raw=b"".join(chunks)
            root=_loads(raw)
            if not _within_depth(root): raise ValueError()
            if type(root) is not dict or set(root)!={"success","data","message","timestamp"} or root["success"] is not True or type(root["data"]) is not dict or type(root["message"]) is not str or len(root["message"].encode())>128 or type(root["timestamp"]) is not str or len(root["timestamp"])>64: raise ValueError()
            if not _INSTANT.fullmatch(root["timestamp"]): raise ValueError()
            stamp=datetime.fromisoformat(root["timestamp"].replace("Z","+00:00"));
            if stamp.tzinfo is None: raise ValueError()
            data=root["data"]
            if set(data)!={"schemaVersion","revision","entries"} or type(data["schemaVersion"]) is not int or data["schemaVersion"]!=1 or type(data["revision"]) is not int or not 0<=data["revision"]<=2**63-1 or type(data["entries"]) is not list or len(data["entries"])>8: raise ValueError()
            entries=[]
            for item in data["entries"]:
                if type(item) is not dict or set(item)!={"entryId","category","semanticKey","value","version","status"} or type(item.get("entryId")) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",item["entryId"]) or type(item.get("version")) is not int or not 1<=item["version"]<=2**31-1 or item.get("status")!="ACTIVE": raise ValueError()
                if any(type(item[key]) is not str for key in ("category", "semanticKey", "value", "status")): raise ValueError()
                encoded = json.dumps(item,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
                if len(encoded) > 512: raise ValueError()
                entries.append(MemoryProjectionEntry(item["entryId"],item["category"],item["semanticKey"],item["value"],item["version"],item["status"]))
            if len(json.dumps(data["entries"],ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()) > 4096: raise ValueError()
            return MemoryProjectionResult(data["revision"],tuple(entries),MemoryProjectionReason.AVAILABLE)
        except httpx.HTTPError: return MemoryProjectionResult(reason=MemoryProjectionReason.UNAVAILABLE)
        except (ValueError,TypeError,json.JSONDecodeError,RecursionError): return MemoryProjectionResult(reason=MemoryProjectionReason.INVALID)

    async def fetch_v2(self, credential: MemoryAccessCredential | None) -> MemoryProjectionV2Result:
        """Fetch only schema v2; schema v1 can never become governed memory."""
        if not self._enabled:
            return MemoryProjectionV2Result(reason=MemoryProjectionReason.DISABLED)
        if credential is None:
            return MemoryProjectionV2Result(reason=MemoryProjectionReason.NO_CREDENTIAL)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("GET",self._url_v2,headers={"Authorization":"Bearer "+_credential_token(credential)}) as response:
                    if response.status_code != 200:
                        return MemoryProjectionV2Result(reason=MemoryProjectionReason.UNAVAILABLE)
                    encoding=response.headers.get("content-encoding","")
                    if type(encoding) is not str or encoding not in {"","identity"}:
                        return MemoryProjectionV2Result(reason=MemoryProjectionReason.INVALID)
                    chunks=[]; size=0
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if type(chunk) is not bytes:
                            return MemoryProjectionV2Result(reason=MemoryProjectionReason.INVALID)
                        size+=len(chunk)
                        if size>_MAX_V2_RESPONSE_BYTES:
                            return MemoryProjectionV2Result(reason=MemoryProjectionReason.INVALID)
                        chunks.append(chunk)
            root=_loads(b"".join(chunks))
            if not _within_depth(root) or type(root) is not dict or set(root)!={"success","data","message","timestamp"}:
                raise ValueError()
            if root["success"] is not True or type(root["message"]) is not str or len(root["message"].encode())>128:
                raise ValueError()
            _parse_instant(root["timestamp"])
            data=root["data"]
            if type(data) is not dict or set(data)!={"schemaVersion","revision","ownerBinding","truncated","entries"}:
                raise ValueError()
            if data["schemaVersion"]!=2 or type(data["schemaVersion"]) is not int or type(data["revision"]) is not int or not 0<=data["revision"]<=2**63-1:
                raise ValueError()
            if type(data["truncated"]) is not bool or type(data["entries"]) is not list or len(data["entries"])>8:
                raise ValueError()
            if type(data["ownerBinding"]) is not str or not re.fullmatch(r"[0-9a-f]{64}",data["ownerBinding"]):
                raise ValueError()
            entries=[]
            fields={"entryId","memoryCategory","productCategory","recipientScope","semanticKey","value","source","dataClass","version","status","createdAt","updatedAt","expiresAt","supersedes","chainVerified"}
            for item in data["entries"]:
                if type(item) is not dict or set(item)!=fields or len(json.dumps(item,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode())>1024:
                    raise ValueError()
                if any(type(item[key]) is not str for key in ("entryId","memoryCategory","productCategory","recipientScope","semanticKey","value","source","dataClass","status","createdAt","updatedAt","expiresAt")):
                    raise ValueError()
                if item["supersedes"] is not None and type(item["supersedes"]) is not str:
                    raise ValueError()
                if type(item["version"]) is not int or type(item["chainVerified"]) is not bool:
                    raise ValueError()
                entries.append(MemoryProjectionV2Entry(
                    item["entryId"],item["memoryCategory"],item["productCategory"],item["recipientScope"],
                    item["semanticKey"],item["value"],item["source"],item["dataClass"],item["version"],item["status"],
                    _parse_instant(item["createdAt"]),_parse_instant(item["updatedAt"]),_parse_instant(item["expiresAt"]),
                    item["supersedes"],item["chainVerified"]
                ))
            if len(json.dumps(data["entries"],ensure_ascii=False,separators=(",",":"),allow_nan=False).encode())>8192:
                raise ValueError()
            result=MemoryProjectionV2Result(
                revision=data["revision"],owner_binding=data["ownerBinding"],truncated=data["truncated"],
                entries=tuple(entries),reason=MemoryProjectionReason.AVAILABLE
            )
            _register_v2_projection(result)
            return result
        except httpx.HTTPError:
            return MemoryProjectionV2Result(reason=MemoryProjectionReason.UNAVAILABLE)
        except (ValueError,TypeError,json.JSONDecodeError,RecursionError):
            return MemoryProjectionV2Result(reason=MemoryProjectionReason.INVALID)


def _parse_instant(value: object) -> datetime:
    if type(value) is not str or len(value)>64 or not _INSTANT.fullmatch(value):
        raise ValueError("invalid instant")
    parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid instant")
    return parsed
