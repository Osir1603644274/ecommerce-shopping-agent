"""Strict, server-owned observation snapshots for read-only Strategy dispatch.

This is a process-local contract only.  It deliberately stores a compact,
validated projection of a tool result; raw tool traces and caller supplied
arguments never become part of the snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ..control.validation_contracts import validate_normalized_output_values
from ..domains.ecommerce.models import CANONICAL_ECOMMERCE_ATTRIBUTE_CODES
from ..domains.ecommerce.strategy_routing import (
    BoundedToolRequest,
    ErrorCode,
    ToolReceipt,
    _request,
    serialize_receipt,
)
from .strategy_receipt_ledger import StrategyLedgerSlot


MAX_SNAPSHOT_BYTES = 32 * 1024
MAX_STRING_BYTES = 4096
MAX_EVIDENCE_REFS = 20
MAX_PRODUCT_IDS = 50
MAX_DEPTH = 6
MAX_SEARCH_DEPTH = 10
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_RE = re.compile(r"^product:[1-9][0-9]*:[A-Za-z0-9_:-]{1,96}$")
_SEARCH_SOURCE_CODES = frozenset({
    "title", "brand", "snapshotPriceMinor", "syntheticReferencePriceMinor",
    "currency", "description", "source", "provenanceUrl",
    "categoryL1", "categoryL2", "categoryL3",
})
_DETAIL_SOURCE_CODES = frozenset({"details"})
_COMPARE_SOURCE_CODES = frozenset({"comparison"})
_CANONICAL_ATTRIBUTE_CODES = CANONICAL_ECOMMERCE_ATTRIBUTE_CODES
_ATTRIBUTE_SOURCE_CODES = _CANONICAL_ATTRIBUTE_CODES
_SENSITIVE_RE = re.compile(
    r"(?:bearer|authorization|password|secret|access[_ -]?token|raw[_ -]?prompt|"
    r"session(?:id|_id)?|diagnosis|anaphylaxis)", re.IGNORECASE,
)
_FORBIDDEN_KEYS = frozenset({
    "arguments", "authorization", "owner", "ownerId", "password", "prompt",
    "query", "raw", "rawOutput", "session", "sessionId", "token", "trace",
})
_SAFE_UNKNOWN_CODES = _CANONICAL_ATTRIBUTE_CODES


class ObservationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_CAPABILITY = object()
_REGISTRY: dict[int, tuple[object, bytes]] = {}


def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _strict_digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return value


def _plain(
    value: object, *, depth: int = 0, key: str | None = None,
    max_depth: int = MAX_DEPTH,
) -> object:
    if depth > max_depth:
        raise ValueError("observation nesting exceeds the safe depth")
    if key is not None and key in _FORBIDDEN_KEYS:
        raise ValueError("observation contains a forbidden field")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        raise ValueError("observation numbers must use exact integers")
    if type(value) is str:
        if not value or len(value.encode("utf-8")) > MAX_STRING_BYTES or _SENSITIVE_RE.search(value):
            raise ValueError("observation contains unsafe text")
        return value
    if type(value) is list:
        if len(value) > MAX_PRODUCT_IDS:
            raise ValueError("observation list exceeds the safe bound")
        return [_plain(item, depth=depth + 1, max_depth=max_depth) for item in value]
    if type(value) is dict:
        result: dict[str, object] = {}
        for nested_key, nested in value.items():
            if type(nested_key) is not str or not nested_key.isascii() or nested_key != nested_key.strip():
                raise ValueError("observation keys must be canonical ASCII strings")
            if nested_key in result:
                raise ValueError("observation contains duplicate keys")
            result[nested_key] = _plain(nested, depth=depth + 1, key=nested_key, max_depth=max_depth)
        return result
    raise ValueError("observation must contain only exact plain JSON values")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _strict_loads(raw: bytes, *, max_depth: int = MAX_DEPTH) -> dict[str, object]:
    if type(raw) is not bytes or len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("observation snapshot exceeds its byte budget")

    def pairs(items: list[tuple[object, object]]) -> dict[object, object]:
        result: dict[object, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("observation snapshot contains duplicate keys")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("observation snapshot is not JSON") from exc
    if type(parsed) is not dict or _json_bytes(_plain(parsed, max_depth=max_depth)) != raw:
        raise ValueError("observation snapshot is not canonical JSON")
    return parsed


def _slot_copy(slot: object) -> StrategyLedgerSlot:
    if type(slot) is not StrategyLedgerSlot:
        raise PermissionError("slot must be an exact server-owned StrategyLedgerSlot")
    return StrategyLedgerSlot.create(**{
        "task_id": slot.task_id, "run_id": slot.run_id, "thread_id": slot.thread_id,
        "plan_id": slot.plan_id, "step_id": slot.step_id, "state_revision": slot.state_revision,
        "tool_name": slot.tool_name, "canonical_args_digest": slot.canonical_args_digest,
    })


def _validate_ids(value: object, *, maximum: int) -> list[int]:
    if type(value) is not list or not value or len(value) > maximum:
        raise ValueError("productIds must be a bounded non-empty list")
    if any(type(item) is not int or item <= 0 for item in value) or len(set(value)) != len(value):
        raise ValueError("productIds must contain unique positive integer IDs")
    return list(value)


def _valid_evidence_ref(value: object, *, product_id: int, tool_name: str) -> bool:
    if type(value) is not str or _EVIDENCE_RE.fullmatch(value) is None:
        return False
    parts = value.split(":")
    if len(parts) == 3:
        allowed = {
            "search_products": _SEARCH_SOURCE_CODES,
            "get_product_details": _DETAIL_SOURCE_CODES,
            "compare_products": _COMPARE_SOURCE_CODES,
        }.get(tool_name, frozenset())
        return int(parts[1]) == product_id and parts[2] in allowed
    return (
        tool_name == "search_products"
        and len(parts) == 4
        and parts[2] == "attribute"
        and parts[3] in _ATTRIBUTE_SOURCE_CODES
        and int(parts[1]) == product_id
    )


def _normalize(
    tool_name: str, observation: object, *, request: BoundedToolRequest | None = None,
) -> dict[str, object]:
    if type(observation) is not dict:
        raise PermissionError("observation must be a server-normalized plain dict")
    if tool_name == "search_products":
        validate_normalized_output_values(tool_name, observation)
        values = dict(observation)
        refs = values.get("evidenceRefs")
        if type(refs) is not list or len(refs) > MAX_EVIDENCE_REFS:
            raise ValueError("search evidenceRefs exceed the safe bound")
        if any(
            not _valid_evidence_ref(ref, product_id=product_id, tool_name=tool_name)
            for product_id, ref in zip(values["rankedItemIds"], refs, strict=True)
        ):
            raise ValueError("search evidenceRefs must be canonical product references")
        support = values.get("candidateSupport")
        if type(support) is dict and "hardUnknownsByProduct" in support:
            unknowns = support["hardUnknownsByProduct"]
            if type(unknowns) is not dict:
                raise ValueError("hardUnknownsByProduct must be a plain object")
            for codes in unknowns.values():
                if type(codes) is not list or any(
                    type(code) is not str or code not in _SAFE_UNKNOWN_CODES for code in codes
                ):
                    raise ValueError("hardUnknownsByProduct contains an uncontrolled code")
        return _plain(values, max_depth=MAX_SEARCH_DEPTH)  # type: ignore[return-value]
    if tool_name in {"get_product_details", "compare_products"}:
        if set(observation) != {"productIds", "evidenceRefs"}:
            raise ValueError("product observation fields are not allowlisted")
        if request is None:
            raise ValueError("product observations require the canonical tool request")
        args = json.loads(request.canonical_args_text())
        product_ids = _validate_ids(observation["productIds"], maximum=MAX_PRODUCT_IDS)
        expected = [args["productId"]] if tool_name == "get_product_details" else list(args["productIds"])
        if product_ids != expected:
            raise ValueError("productIds do not match the canonical tool request")
        refs = observation["evidenceRefs"]
        if type(refs) is not list or len(refs) != len(product_ids) or len(refs) > MAX_EVIDENCE_REFS:
            raise ValueError("product evidenceRefs do not cover the requested products")
        if any(
            not _valid_evidence_ref(ref, product_id=product_id, tool_name=tool_name)
            for product_id, ref in zip(product_ids, refs, strict=True)
        ):
            raise ValueError("product evidenceRefs must be canonical product references")
        if [int(ref.split(":", 2)[1]) for ref in refs] != product_ids:
            raise ValueError("product evidenceRefs are not bound to productIds")
        return _plain({"productIds": product_ids, "evidenceRefs": refs})  # type: ignore[return-value]
    raise PermissionError("only the three read-only Strategy tools are supported")


def _body(
    *, slot: StrategyLedgerSlot, receipt: ToolReceipt, normalized: dict[str, object],
    observed_at: datetime, receipt_digest: str,
) -> dict[str, object]:
    return {
        "version": 1,
        "slotDigest": slot.slot_digest(),
        "taskId": slot.task_id,
        "runId": slot.run_id,
        "planId": slot.plan_id,
        "stepId": slot.step_id,
        "receiptId": receipt.receipt_id,
        "receiptDigest": receipt_digest,
        "baseRevision": slot.state_revision,
        "toolName": slot.tool_name,
        "argsDigest": slot.canonical_args_digest,
        "outcome": receipt.outcome,
        "errorCode": None if receipt.error_code is None else receipt.error_code.value,
        "observedAt": observed_at.isoformat(),
        "normalized": normalized,
    }


def _validate_binding(slot: StrategyLedgerSlot, receipt: ToolReceipt) -> bytes:
    receipt_raw = serialize_receipt(receipt)
    if (
        receipt.tool_name != slot.tool_name
        or receipt.step_id != slot.step_id
        or receipt.state_revision != slot.state_revision
        or receipt.args_digest != slot.canonical_args_digest
    ):
        raise ValueError("receipt does not match the complete dispatch slot")
    if type(receipt.outcome) is not str or receipt.outcome not in {"succeeded", "failed"}:
        raise ValueError("receipt outcome is invalid")
    if (receipt.outcome == "succeeded") != (receipt.error_code is None):
        raise ValueError("receipt outcome/errorCode mismatch")
    return receipt_raw


@dataclass(frozen=True, slots=True, init=False, repr=False)
class ObservationSnapshot:
    version: int
    slot_digest: str
    task_id: str
    run_id: str
    plan_id: str
    step_id: str
    receipt_id: str
    receipt_digest: str
    base_revision: int
    tool_name: str
    args_digest: str
    outcome: ObservationOutcome
    error_code: ErrorCode | None
    observed_at: datetime
    observation_digest: str
    _normalized_json: bytes
    _body_json: bytes
    _nonce: object

    def __init__(self, *, _capability: object, **values: object) -> None:
        if _capability is not _CAPABILITY:
            raise TypeError("ObservationSnapshot must be created by its server factory")
        for field, value in values.items():
            object.__setattr__(self, field, value)

    @classmethod
    def create(
        cls, *, slot: object, receipt: object, observation: object,
        request: object | None = None,
        observed_at: object | None = None,
    ) -> "ObservationSnapshot":
        checked_slot = _slot_copy(slot)
        if type(receipt) is not ToolReceipt:
            raise PermissionError("receipt must be an exact runner-issued ToolReceipt")
        receipt_raw = _validate_binding(checked_slot, receipt)
        checked_request: BoundedToolRequest | None = None
        if request is not None:
            if type(request) is not BoundedToolRequest:
                raise PermissionError("request must be an exact runner-issued BoundedToolRequest")
            checked_request = _request(request)
            if (
                checked_request.tool_name != checked_slot.tool_name
                or checked_request.step_id != checked_slot.step_id
                or checked_request.state_revision != checked_slot.state_revision
                or checked_request.args_digest != checked_slot.canonical_args_digest
            ):
                raise ValueError("request does not match the dispatch slot")
        elif checked_slot.tool_name in {"get_product_details", "compare_products"}:
            raise ValueError("product observations require the canonical tool request")
        if observed_at is None:
            observed_at = receipt.issued_at
        checked_at = _utc(observed_at, "observedAt")
        issued_at = _utc(receipt.issued_at, "receipt issuedAt")
        if checked_at < issued_at or checked_at > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("observedAt is outside the authoritative time window")
        outcome = ObservationOutcome(receipt.outcome)
        normalized = {} if outcome is ObservationOutcome.FAILED else _normalize(
            checked_slot.tool_name, observation, request=checked_request,
        )
        if outcome is ObservationOutcome.FAILED and observation not in ({}, None):
            raise ValueError("failed observations cannot carry tool output")
        body = _body(
            slot=checked_slot, receipt=receipt, normalized=normalized,
            observed_at=checked_at, receipt_digest=hashlib.sha256(receipt_raw).hexdigest(),
        )
        body_json = _json_bytes(_plain(body, max_depth=MAX_SEARCH_DEPTH))
        if len(body_json) > MAX_SNAPSHOT_BYTES:
            raise ValueError("observation snapshot exceeds its byte budget")
        digest = hashlib.sha256(body_json).hexdigest()
        if len(_json_bytes({**body, "observationDigest": digest})) > MAX_SNAPSHOT_BYTES:
            raise ValueError("complete observation snapshot exceeds its byte budget")
        value = cls(
            _capability=_CAPABILITY, version=1, slot_digest=checked_slot.slot_digest(),
            task_id=checked_slot.task_id, run_id=checked_slot.run_id,
            plan_id=checked_slot.plan_id, step_id=checked_slot.step_id,
            receipt_id=receipt.receipt_id,
            receipt_digest=hashlib.sha256(receipt_raw).hexdigest(),
            base_revision=checked_slot.state_revision, tool_name=checked_slot.tool_name,
            args_digest=checked_slot.canonical_args_digest, outcome=outcome,
            error_code=receipt.error_code, observed_at=checked_at,
            observation_digest=digest, _normalized_json=_json_bytes(normalized),
            _body_json=body_json, _nonce=_CAPABILITY,
        )
        _REGISTRY[id(value)] = (_CAPABILITY, body_json)
        return value

    @property
    def normalized(self) -> dict[str, object]:
        return json.loads(self._normalized_json.decode("utf-8"))

    def __repr__(self) -> str:
        return (
            "ObservationSnapshot(version=1, "
            f"slot_digest={self.slot_digest!r}, step_id={self.step_id!r}, "
            f"base_revision={self.base_revision}, tool_name={self.tool_name!r}, "
            f"observation_digest={self.observation_digest!r})"
        )

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("ObservationSnapshot cannot be copied or pickled")


def _issued(value: object) -> bytes:
    if type(value) is not ObservationSnapshot:
        raise PermissionError("observation snapshot must be an exact issued object")
    record = _REGISTRY.get(id(value))
    if record is None or record[0] is not _CAPABILITY:
        raise PermissionError("observation snapshot was not issued by the server factory")
    body = _body_from_value(value)
    body_json = _json_bytes(_plain(body, max_depth=MAX_SEARCH_DEPTH))
    if body_json != record[1] or body_json != value._body_json:
        raise ValueError("observation snapshot was mutated")
    if hashlib.sha256(body_json).hexdigest() != value.observation_digest:
        raise ValueError("observation digest mismatch")
    return body_json


def _body_from_value(value: ObservationSnapshot) -> dict[str, object]:
    return {
        "version": value.version, "slotDigest": value.slot_digest,
        "taskId": value.task_id, "runId": value.run_id, "planId": value.plan_id,
        "stepId": value.step_id, "receiptId": value.receipt_id,
        "receiptDigest": value.receipt_digest,
        "baseRevision": value.base_revision,
        "toolName": value.tool_name, "argsDigest": value.args_digest,
        "outcome": value.outcome.value if type(value.outcome) is ObservationOutcome else value.outcome,
        "errorCode": None if value.error_code is None else value.error_code.value,
        "observedAt": value.observed_at.isoformat(), "normalized": value.normalized,
    }


def serialize_observation(value: object) -> bytes:
    body = _issued(value)
    full = {**_strict_loads(body, max_depth=MAX_SEARCH_DEPTH), "observationDigest": value.observation_digest}
    raw = _json_bytes(_plain(full, max_depth=MAX_SEARCH_DEPTH))
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("observation snapshot exceeds its byte budget")
    return raw


def restore_observation(
    raw: object, *, slot: object, receipt: object, request: object | None = None,
) -> ObservationSnapshot:
    if type(raw) is not bytes:
        raise TypeError("observation snapshot must be bytes")
    parsed = _strict_loads(raw, max_depth=MAX_SEARCH_DEPTH)
    expected = {
        "version", "slotDigest", "taskId", "runId", "planId", "stepId", "receiptId", "receiptDigest", "baseRevision",
        "toolName", "argsDigest", "outcome", "errorCode", "observedAt", "normalized",
        "observationDigest",
    }
    if set(parsed) != expected:
        raise ValueError("observation snapshot fields are not exact")
    digest = parsed.pop("observationDigest")
    body = _json_bytes(_plain(parsed, max_depth=MAX_SEARCH_DEPTH))
    if type(digest) is not str or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("observation digest mismatch")
    checked_slot = _slot_copy(slot)
    if parsed["slotDigest"] != checked_slot.slot_digest():
        raise ValueError("observation slot digest mismatch")
    if parsed["taskId"] != checked_slot.task_id or parsed["runId"] != checked_slot.run_id or parsed["planId"] != checked_slot.plan_id or parsed["stepId"] != checked_slot.step_id:
        raise ValueError("observation identity mismatch")
    if parsed["baseRevision"] != checked_slot.state_revision or parsed["toolName"] != checked_slot.tool_name or parsed["argsDigest"] != checked_slot.canonical_args_digest:
        raise ValueError("observation slot binding mismatch")
    checked_receipt = receipt if type(receipt) is ToolReceipt else None
    if checked_receipt is None:
        raise PermissionError("receipt must be an exact runner-issued ToolReceipt")
    receipt_raw = _validate_binding(checked_slot, checked_receipt)
    if parsed["receiptId"] != checked_receipt.receipt_id:
        raise ValueError("observation receipt identity mismatch")
    if parsed["receiptDigest"] != hashlib.sha256(receipt_raw).hexdigest():
        raise ValueError("observation receipt digest mismatch")
    if parsed["outcome"] != checked_receipt.outcome or parsed["errorCode"] != (None if checked_receipt.error_code is None else checked_receipt.error_code.value):
        raise ValueError("observation receipt binding mismatch")
    observed_at = _utc(datetime.fromisoformat(parsed["observedAt"]), "observedAt")
    checked_request: BoundedToolRequest | None = None
    if request is not None:
        if type(request) is not BoundedToolRequest:
            raise PermissionError("request must be an exact runner-issued BoundedToolRequest")
        checked_request = _request(request)
        if (
            checked_request.tool_name != checked_slot.tool_name
            or checked_request.step_id != checked_slot.step_id
            or checked_request.state_revision != checked_slot.state_revision
            or checked_request.args_digest != checked_slot.canonical_args_digest
        ):
            raise ValueError("request does not match the dispatch slot")
    elif checked_slot.tool_name in {"get_product_details", "compare_products"}:
        raise ValueError("product observations require the canonical tool request")
    normalized = _normalize(
        checked_slot.tool_name, parsed["normalized"], request=checked_request,
    ) if parsed["outcome"] == "succeeded" else {}
    if parsed["outcome"] == "failed" and parsed["normalized"] != {}:
        raise ValueError("failed observations cannot carry tool output")
    rebuilt = ObservationSnapshot.create(
        slot=checked_slot, receipt=checked_receipt, observation=normalized,
        request=checked_request, observed_at=observed_at,
    )
    if rebuilt.observation_digest != digest or serialize_observation(rebuilt) != raw:
        raise ValueError("observation snapshot is not canonical")
    return rebuilt


serialize_snapshot = serialize_observation
restore_snapshot = restore_observation

__all__ = [
    "MAX_SNAPSHOT_BYTES", "ObservationOutcome", "ObservationSnapshot",
    "restore_observation", "restore_snapshot", "serialize_observation", "serialize_snapshot",
]
