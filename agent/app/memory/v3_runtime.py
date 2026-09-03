"""Authenticated V13 projection and immutable per-run shopping memory binding."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import weakref
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Mapping

import httpx

from .projection_client import (
    MemoryAccessCredential,
    MemoryProjectionReason,
    _credential_token,
    _loads,
    _within_depth,
)

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CATEGORY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_ATTRIBUTE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_VALUE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_REVISION = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z\Z")
_VISIBLE_PHASES = frozenset({"planner", "replanner", "final_answer"})
_ALL_PHASES = _VISIBLE_PHASES | frozenset({"executor", "validator"})
_MAX_ENTRIES = 8
_MAX_BYTES = 4096


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _instant(value: object) -> datetime:
    if type(value) is not str or not _INSTANT.fullmatch(value):
        raise ValueError("invalid instant")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid instant")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ProjectionV3Entry:
    entry_id: str
    category_id: str
    recipient_scope: str
    preference_kind: Literal["prefer", "avoid", "indifferent"]
    attribute_key: str
    normalized_value: str
    catalog_revision: str
    version: int
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    supersedes: str | None
    chain_verified: bool

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.entry_id) or not _CATEGORY.fullmatch(self.category_id):
            raise ValueError("invalid v3 projection entry")
        if self.recipient_scope != "self":
            raise ValueError("invalid v3 projection entry")
        if self.preference_kind not in {"prefer", "avoid", "indifferent"}:
            raise ValueError("invalid v3 projection entry")
        if not _ATTRIBUTE.fullmatch(self.attribute_key) or not _VALUE.fullmatch(self.normalized_value):
            raise ValueError("invalid v3 projection entry")
        if not _REVISION.fullmatch(self.catalog_revision):
            raise ValueError("invalid v3 projection entry")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("invalid v3 projection entry")
        if self.status != "ACTIVE" or self.chain_verified is not True:
            raise ValueError("invalid v3 projection entry")
        if (self.version == 1) != (self.supersedes is None):
            raise ValueError("invalid v3 projection entry")
        if self.supersedes is not None and not _ID.fullmatch(self.supersedes):
            raise ValueError("invalid v3 projection entry")
        if self.updated_at < self.created_at or self.expires_at <= self.updated_at:
            raise ValueError("invalid v3 projection entry")

    def sealed_plain(self) -> dict[str, object]:
        return {
            "entryId": self.entry_id,
            "categoryId": self.category_id,
            "recipientScope": self.recipient_scope,
            "preferenceKind": self.preference_kind,
            "attributeKey": self.attribute_key,
            "normalizedValue": self.normalized_value,
            "catalogRevision": self.catalog_revision,
            "version": self.version,
            "status": self.status,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "expiresAt": self.expires_at.isoformat(),
            "supersedes": self.supersedes,
            "chainVerified": self.chain_verified,
        }


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ProjectionV3Result:
    revision: int = 0
    owner_binding: str = ""
    truncated: bool = False
    entries: tuple[ProjectionV3Entry, ...] = field(default=(), repr=False)
    reason: MemoryProjectionReason = MemoryProjectionReason.DISABLED

    def __post_init__(self) -> None:
        if type(self.revision) is not int or not 0 <= self.revision <= 2**63 - 1:
            raise ValueError("invalid v3 projection")
        if self.reason is MemoryProjectionReason.AVAILABLE:
            if not re.fullmatch(r"[0-9a-f]{64}", self.owner_binding):
                raise ValueError("invalid v3 projection")
        elif self.owner_binding != "":
            raise ValueError("invalid v3 projection")
        if type(self.truncated) is not bool or type(self.entries) is not tuple:
            raise ValueError("invalid v3 projection")
        if len(self.entries) > _MAX_ENTRIES or any(type(item) is not ProjectionV3Entry for item in self.entries):
            raise ValueError("invalid v3 projection")


@dataclass(frozen=True, slots=True)
class CatalogRunPreference:
    category_id: str
    preference_kind: Literal["prefer", "avoid", "indifferent"]
    attribute_key: str
    normalized_value: str

    def __post_init__(self) -> None:
        if not _CATEGORY.fullmatch(self.category_id):
            raise ValueError("invalid run preference")
        if self.preference_kind not in {"prefer", "avoid", "indifferent"}:
            raise ValueError("invalid run preference")
        if not _ATTRIBUTE.fullmatch(self.attribute_key) or not _VALUE.fullmatch(self.normalized_value):
            raise ValueError("invalid run preference")

    def plain(self) -> dict[str, str]:
        self.__post_init__()
        return {
            "categoryId": self.category_id,
            "preferenceKind": self.preference_kind,
            "attributeKey": self.attribute_key,
            "normalizedValue": self.normalized_value,
        }


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class MemoryRunBinding:
    memory_revision: int
    category_id: str
    catalog_revision: str
    preferences: tuple[CatalogRunPreference, ...] = field(default=(), repr=False)

    def payload_for_phase(self, phase: str) -> dict[str, object] | None:
        if phase not in _ALL_PHASES:
            raise ValueError("unknown context phase")
        _validated_binding(self)
        if phase not in _VISIBLE_PHASES or not self.preferences:
            return None
        return {"preferences": [item.plain() for item in self.preferences]}


@dataclass(frozen=True, slots=True)
class _Issued:
    reference: weakref.ReferenceType
    sealed: bytes
    digest: bytes


_PROJECTION_BINDING = secrets.token_bytes(32)
_RUN_BINDING = secrets.token_bytes(32)
_ISSUED_PROJECTIONS: dict[int, _Issued] = {}
_ISSUED_BINDINGS: dict[int, _Issued] = {}


def _register(registry: dict[int, _Issued], secret: bytes, value: object, sealed: bytes) -> None:
    key = id(value)
    digest = hashlib.sha256(secret + sealed + str(key).encode("ascii")).digest()

    def cleanup(reference: weakref.ReferenceType) -> None:
        stored = registry.get(key)
        if stored is not None and stored.reference is reference:
            registry.pop(key, None)

    reference = weakref.ref(value, cleanup)
    registry[key] = _Issued(reference, sealed, digest)


def _validated(
    registry: dict[int, _Issued], secret: bytes, value: object,
    current_seal: bytes,
) -> bool:
    issued = registry.get(id(value))
    if issued is None or issued.reference() is not value or issued.sealed != current_seal:
        return False
    expected = hashlib.sha256(
        secret + current_seal + str(id(value)).encode("ascii")
    ).digest()
    return secrets.compare_digest(expected, issued.digest)


def _projection_seal(result: ProjectionV3Result) -> bytes:
    return _canonical({
        "revision": result.revision,
        "ownerBinding": result.owner_binding,
        "truncated": result.truncated,
        "reason": result.reason.value,
        "entries": [item.sealed_plain() for item in result.entries],
    })


def _validated_projection(result: object) -> ProjectionV3Result | None:
    if type(result) is not ProjectionV3Result or result.reason is not MemoryProjectionReason.AVAILABLE:
        return None
    try:
        seal = _projection_seal(result)
    except (TypeError, ValueError, AttributeError):
        return None
    return result if _validated(_ISSUED_PROJECTIONS, _PROJECTION_BINDING, result, seal) else None


def _binding_seal(binding: MemoryRunBinding) -> bytes:
    return _canonical({
        "memoryRevision": binding.memory_revision,
        "categoryId": binding.category_id,
        "catalogRevision": binding.catalog_revision,
        "preferences": [item.plain() for item in binding.preferences],
    })


def _validated_binding(binding: object) -> MemoryRunBinding:
    if type(binding) is not MemoryRunBinding:
        raise ValueError("unissued memory run binding")
    seal = _binding_seal(binding)
    if not _validated(_ISSUED_BINDINGS, _RUN_BINDING, binding, seal):
        raise ValueError("unissued memory run binding")
    return binding


def build_memory_run_binding(
    result: object,
    *,
    category_id: str,
    catalog_revision: str,
    current_requirement_keys: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> MemoryRunBinding:
    projection = _validated_projection(result)
    if (
        projection is None
        or not _CATEGORY.fullmatch(category_id)
        or not _REVISION.fullmatch(catalog_revision)
    ):
        return empty_memory_run_binding(
            category_id if _CATEGORY.fullmatch(category_id or "") else "unknown",
            catalog_revision if _REVISION.fullmatch(catalog_revision or "") else "unknown",
        )
    effective_now = datetime.now(UTC) if now is None else now
    if type(effective_now) is not datetime or effective_now.tzinfo is None:
        raise ValueError("invalid binding time")
    effective_now = effective_now.astimezone(UTC)
    if type(current_requirement_keys) is not frozenset or any(
        type(key) is not str or not _ATTRIBUTE.fullmatch(key)
        for key in current_requirement_keys
    ):
        raise ValueError("invalid current requirement keys")
    retained: list[CatalogRunPreference] = []
    seen: set[str] = set()
    for entry in projection.entries:
        if (
            entry.category_id != category_id
            or entry.catalog_revision != catalog_revision
            or entry.expires_at <= effective_now
            or entry.attribute_key in current_requirement_keys
        ):
            continue
        if entry.attribute_key in seen:
            return empty_memory_run_binding(category_id, catalog_revision)
        retained.append(CatalogRunPreference(
            entry.category_id, entry.preference_kind,
            entry.attribute_key, entry.normalized_value,
        ))
        seen.add(entry.attribute_key)
    retained.sort(key=lambda item: (
        item.category_id, item.attribute_key,
        item.preference_kind, item.normalized_value,
    ))
    binding = MemoryRunBinding(
        projection.revision, category_id, catalog_revision, tuple(retained)
    )
    if len(_binding_seal(binding)) > _MAX_BYTES:
        return empty_memory_run_binding(category_id, catalog_revision)
    _register(_ISSUED_BINDINGS, _RUN_BINDING, binding, _binding_seal(binding))
    return binding


def empty_memory_run_binding(
    category_id: str = "unknown", catalog_revision: str = "unknown"
) -> MemoryRunBinding:
    if not _CATEGORY.fullmatch(category_id):
        category_id = "unknown"
    if not _REVISION.fullmatch(catalog_revision):
        catalog_revision = "unknown"
    binding = MemoryRunBinding(0, category_id, catalog_revision, ())
    _register(_ISSUED_BINDINGS, _RUN_BINDING, binding, _binding_seal(binding))
    return binding


def memory_score(
    binding: object,
    product_attributes: Mapping[str, str],
) -> float:
    checked = _validated_binding(binding)
    if not isinstance(product_attributes, Mapping):
        raise ValueError("invalid product attributes")
    active = [
        item for item in checked.preferences
        if item.preference_kind in {"prefer", "avoid"}
    ]
    total = 0
    for item in active:
        value = product_attributes.get(item.attribute_key)
        if type(value) is not str:
            continue
        if value == item.normalized_value:
            total += 1 if item.preference_kind == "prefer" else -1
    return total / max(1, len(active))


def rerank_product_presentations(
    binding: object,
    presentations: list[dict[str, object]],
    *,
    weight: float,
    suppressed_attribute_keys: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Soft-rerank a validated presentation list without filtering products."""
    checked = _validated_binding(binding)
    if weight not in {0.01, 0.03, 0.05, 0.08}:
        raise ValueError("invalid memory rerank weight")
    if type(presentations) is not list or not 0 <= len(presentations) <= 20:
        raise ValueError("invalid product presentations")
    if type(suppressed_attribute_keys) is not frozenset or any(
        type(key) is not str or not _ATTRIBUTE.fullmatch(key)
        for key in suppressed_attribute_keys
    ):
        raise ValueError("invalid suppressed attribute keys")

    active = tuple(
        item for item in checked.preferences
        if item.attribute_key not in suppressed_attribute_keys
    )
    effective = MemoryRunBinding(
        checked.memory_revision, checked.category_id,
        checked.catalog_revision, active,
    )
    _register(_ISSUED_BINDINGS, _RUN_BINDING, effective, _binding_seal(effective))
    denominator = max(1, len(presentations) - 1)
    ranked: list[tuple[float, int, str, dict[str, object], float]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(presentations):
        if type(raw) is not dict:
            raise ValueError("invalid product presentation")
        product_id = raw.get("productId")
        if type(product_id) not in {int, str}:
            raise ValueError("invalid product presentation")
        product_key = str(product_id)
        if not product_key or product_key in seen_ids:
            raise ValueError("invalid product presentation")
        seen_ids.add(product_key)
        attributes: dict[str, str] = {}
        brand = raw.get("brand")
        if type(brand) is str:
            normalized = re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")
            if _VALUE.fullmatch(normalized):
                attributes["brand"] = normalized
        raw_attributes = raw.get("attributes")
        if type(raw_attributes) is not list:
            raise ValueError("invalid product presentation")
        for item in raw_attributes:
            if type(item) is not dict or item.get("status") != "known":
                continue
            key, value = item.get("key"), item.get("value")
            if type(key) is str and type(value) is str and _ATTRIBUTE.fullmatch(key):
                normalized = re.sub(r"[^a-z0-9._:-]+", "-", value.strip().lower()).strip("-")
                if _VALUE.fullmatch(normalized):
                    attributes[key] = normalized
        score = memory_score(effective, attributes)
        base_score = 1.0 - (index / denominator)
        final_score = base_score + weight * score
        ranked.append((final_score, index, product_key, deepcopy(raw), score))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    output = [item[3] for item in ranked]
    receipt = {
        "schemaVersion": "memory-presentation-rerank-v1",
        "memoryRevision": checked.memory_revision,
        "categoryId": checked.category_id,
        "catalogRevision": checked.catalog_revision,
        "weight": weight,
        "inputProductIds": [str(item.get("productId")) for item in presentations],
        "outputProductIds": [str(item.get("productId")) for item in output],
        "memoryScores": {
            item[2]: round(item[4], 8) for item in ranked
        },
        "filteredProductCount": 0,
    }
    return output, receipt


class MemoryProjectionV3Client:
    def __init__(self, *, enabled: bool, backend_url: str, timeout_seconds: float = 2.0):
        self._enabled = enabled
        self._url = backend_url.rstrip("/") + "/api/memory/projection/v3"
        self._timeout = max(0.1, min(float(timeout_seconds), 5.0))

    async def fetch(self, credential: MemoryAccessCredential | None) -> ProjectionV3Result:
        if not self._enabled:
            return ProjectionV3Result(reason=MemoryProjectionReason.DISABLED)
        if credential is None:
            return ProjectionV3Result(reason=MemoryProjectionReason.NO_CREDENTIAL)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "GET", self._url,
                    headers={"Authorization": "Bearer " + _credential_token(credential)},
                ) as response:
                    if response.status_code != 200:
                        return ProjectionV3Result(reason=MemoryProjectionReason.UNAVAILABLE)
                    encoding = response.headers.get("content-encoding", "")
                    if type(encoding) is not str or encoding not in {"", "identity"}:
                        return ProjectionV3Result(reason=MemoryProjectionReason.INVALID)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if type(chunk) is not bytes:
                            return ProjectionV3Result(reason=MemoryProjectionReason.INVALID)
                        size += len(chunk)
                        if size > 16384:
                            return ProjectionV3Result(reason=MemoryProjectionReason.INVALID)
                        chunks.append(chunk)
            root = _loads(b"".join(chunks))
            if not _within_depth(root):
                raise ValueError("invalid v3 response")
            if type(root) is not dict or set(root) != {"success", "data", "message", "timestamp"}:
                raise ValueError("invalid v3 response")
            if root["success"] is not True or type(root["data"]) is not dict:
                raise ValueError("invalid v3 response")
            _instant(root["timestamp"])
            data = root["data"]
            if set(data) != {"schemaVersion", "revision", "ownerBinding", "truncated", "entries"}:
                raise ValueError("invalid v3 response")
            if data["schemaVersion"] != 3 or type(data["revision"]) is not int:
                raise ValueError("invalid v3 response")
            if type(data["entries"]) is not list or len(data["entries"]) > _MAX_ENTRIES:
                raise ValueError("invalid v3 response")
            fields = {
                "entryId", "categoryId", "recipientScope", "preferenceKind",
                "attributeKey", "normalizedValue", "catalogRevision", "version",
                "status", "createdAt", "updatedAt", "expiresAt", "supersedes",
                "chainVerified",
            }
            entries = []
            for item in data["entries"]:
                if type(item) is not dict or set(item) != fields:
                    raise ValueError("invalid v3 entry")
                entries.append(ProjectionV3Entry(
                    item["entryId"], item["categoryId"], item["recipientScope"],
                    item["preferenceKind"], item["attributeKey"],
                    item["normalizedValue"], item["catalogRevision"],
                    item["version"], item["status"], _instant(item["createdAt"]),
                    _instant(item["updatedAt"]), _instant(item["expiresAt"]),
                    item["supersedes"], item["chainVerified"],
                ))
            result = ProjectionV3Result(
                data["revision"], data["ownerBinding"], data["truncated"],
                tuple(entries), MemoryProjectionReason.AVAILABLE,
            )
            seal = _projection_seal(result)
            _register(_ISSUED_PROJECTIONS, _PROJECTION_BINDING, result, seal)
            return result
        except httpx.DecodingError:
            return ProjectionV3Result(reason=MemoryProjectionReason.INVALID)
        except httpx.HTTPError:
            return ProjectionV3Result(reason=MemoryProjectionReason.UNAVAILABLE)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError, RecursionError):
            return ProjectionV3Result(reason=MemoryProjectionReason.INVALID)


__all__ = [
    "CatalogRunPreference",
    "MemoryProjectionV3Client",
    "MemoryRunBinding",
    "ProjectionV3Entry",
    "ProjectionV3Result",
    "build_memory_run_binding",
    "empty_memory_run_binding",
    "memory_score",
    "rerank_product_presentations",
]
