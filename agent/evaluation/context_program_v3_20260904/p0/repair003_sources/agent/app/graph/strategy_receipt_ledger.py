"""Redis-backed durable claim/commit seam for read-only Strategy receipts.

This module is intentionally not wired into graph dispatch in Batch 2a.  The
Redis record is the authority; no process-local capability or in-memory replay
table is used as a substitute for it.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from ..domains.ecommerce.strategy_routing import (
    READ_ONLY_TOOL_ALLOWLIST,
    ToolReceipt,
    serialize_receipt,
)

_NAMESPACE = "strategy-receipt-ledger:v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LEASE_TOKEN_RE = re.compile(r"^lease-[A-Za-z0-9_-]{32}$")
_SENSITIVE_RE = re.compile(
    r"(?:bearer|authorization|password|token|prompt|diagnosis|anaphylaxis|session)",
    re.I,
)
_MAX_LEASE_SECONDS = 300
_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_RECOVERY_COUNT = 1
_MAX_SLOT_JSON_BYTES = 1024
_MAX_RECORD_JSON_BYTES = 8192


class LedgerStatus(StrEnum):
    CLAIMED = "CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    REPLAY = "REPLAY"
    COMMITTED = "COMMITTED"
    RECOVERY_RETRY_ALLOWED = "RECOVERY_RETRY_ALLOWED"
    UNKNOWN = "UNKNOWN"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    POLICY_DENIED = "POLICY_DENIED"
    LEDGER_UNAVAILABLE = "LEDGER_UNAVAILABLE"
    CORRUPT = "CORRUPT"


class LedgerClock(Protocol):
    def now_utc(self) -> datetime: ...


class RedisLike(Protocol):
    async def get(self, key: str) -> str | bytes | None: ...


def _id(value: object, field: str) -> str:
    if type(value) is not str or not _ID_RE.fullmatch(value) or _SENSITIVE_RE.search(value):
        raise ValueError(f"{field} must be an opaque server identifier")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return value


def _utc(value: object, field: str = "clock") -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


@dataclass(frozen=True, slots=True)
class StrategyLedgerSlot:
    task_id: str
    run_id: str
    thread_id: str
    plan_id: str
    step_id: str
    state_revision: int
    tool_name: str
    canonical_args_digest: str

    @classmethod
    def create(cls, **values: object) -> "StrategyLedgerSlot":
        if type(values.get("state_revision")) is not int or not 1 <= values["state_revision"] <= 2**31 - 1:
            raise ValueError("stateRevision must be a positive bounded integer")
        tool = values.get("tool_name")
        if type(tool) is not str or tool not in READ_ONLY_TOOL_ALLOWLIST:
            raise PermissionError("only Strategy read-only tools are eligible")
        return cls(
            _id(values.get("task_id"), "taskId"),
            _id(values.get("run_id"), "runId"),
            _id(values.get("thread_id"), "threadId"),
            _id(values.get("plan_id"), "planId"),
            _id(values.get("step_id"), "stepId"),
            values["state_revision"],
            tool,
            _digest(values.get("canonical_args_digest"), "canonicalArgsDigest"),
        )

    def plain(self) -> dict[str, object]:
        return {
            "taskId": self.task_id,
            "runId": self.run_id,
            "threadId": self.thread_id,
            "planId": self.plan_id,
            "stepId": self.step_id,
            "stateRevision": self.state_revision,
            "toolName": self.tool_name,
            "canonicalArgsDigest": self.canonical_args_digest,
        }

    def identity_digest(self) -> str:
        return hashlib.sha256(_json_bytes(self.plain())).hexdigest()

    def slot_digest(self) -> str:
        return hashlib.sha256(
            _json_bytes({key: value for key, value in self.plain().items() if key not in {"toolName", "canonicalArgsDigest"}})
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class LedgerResponse:
    status: LedgerStatus
    lease_token: str | None = None
    issued_at: datetime | None = None
    lease_expires_at: datetime | None = None
    recovery_count: int = 0
    receipt_snapshot: bytes | None = None
    error_code: str | None = None


def _now(clock: LedgerClock) -> datetime:
    return _utc(clock.now_utc(), "authority clock")


def _token() -> str:
    return "lease-" + secrets.token_urlsafe(24)


def _key(slot: StrategyLedgerSlot) -> tuple[str, str]:
    return (
        f"{_NAMESPACE}:slot:{slot.slot_digest()}",
        f"{_NAMESPACE}:record:{slot.identity_digest()}",
    )


def _raw(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if type(value) is str:
        return value
    raise ValueError("ledger response must be UTF-8 JSON")


def _strict_loads(raw: str) -> object:
    def pairs(items: list[tuple[object, object]]) -> dict[object, object]:
        result: dict[object, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError("non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)


def _response(status: object, raw: object = "", *, slot: StrategyLedgerSlot | None = None, now: datetime | None = None, lease_seconds: int = _MAX_LEASE_SECONDS) -> LedgerResponse:
    if not raw:
        try:
            if status == LedgerStatus.CORRUPT:
                return LedgerResponse(LedgerStatus.UNKNOWN, error_code="CORRUPT_RECORD")
            return LedgerResponse(LedgerStatus(status))
        except Exception:
            return LedgerResponse(LedgerStatus.CORRUPT, error_code="ledger_corrupt")
    try:
        raw_text = _raw(raw)
        if len(raw_text.encode("utf-8")) > _MAX_RECORD_JSON_BYTES:
            raise ValueError
        parsed = _strict_loads(raw_text) if raw else {}
        if type(parsed) is not dict or set(parsed) != {
            "status", "identityDigest", "recordKey", "leaseToken", "issuedAt",
            "leaseIssuedAtMs", "leaseExpiresAt", "leaseExpiresMs", "recoveryCount", "receiptSnapshot", "receiptDigest",
            "committedAt",
        }:
            raise ValueError
        if parsed["status"] not in {item.value for item in LedgerStatus}:
            raise ValueError
        if type(parsed["identityDigest"]) is not str or not _DIGEST_RE.fullmatch(parsed["identityDigest"]):
            raise ValueError
        if slot is not None and parsed["identityDigest"] != slot.identity_digest():
            raise ValueError
        if type(parsed["recordKey"]) is not str or not parsed["recordKey"].startswith(f"{_NAMESPACE}:record:"):
            raise ValueError
        if slot is not None and parsed["recordKey"] != _key(slot)[1]:
            raise ValueError
        if type(parsed["recoveryCount"]) is not int or not 0 <= parsed["recoveryCount"] <= _MAX_RECOVERY_COUNT:
            raise ValueError
        if type(parsed["leaseExpiresMs"]) is not int or parsed["leaseExpiresMs"] < 0:
            raise ValueError
        if type(parsed["leaseIssuedAtMs"]) is not int or parsed["leaseIssuedAtMs"] < 0:
            raise ValueError
        issued_at = datetime.fromisoformat(parsed["issuedAt"]) if parsed["issuedAt"] else None
        expires_at = datetime.fromisoformat(parsed["leaseExpiresAt"]) if parsed["leaseExpiresAt"] else None
        if issued_at is None or expires_at is None or issued_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError
        issued_at = _utc(issued_at, "issuedAt")
        expires_at = _utc(expires_at, "leaseExpiresAt")
        if parsed["leaseIssuedAtMs"] != int(issued_at.timestamp() * 1000):
            raise ValueError
        if expires_at < issued_at or expires_at - issued_at > timedelta(seconds=lease_seconds):
            raise ValueError
        if parsed["leaseExpiresMs"] != int(expires_at.timestamp() * 1000):
            raise ValueError
        if now is not None and issued_at > now:
            raise ValueError
        snapshot = parsed["receiptSnapshot"]
        if snapshot is not None and type(snapshot) is not str:
            raise ValueError
        if snapshot is not None and len(snapshot.encode("utf-8")) > 4096:
            raise ValueError
        if parsed["receiptDigest"] is not None and (type(parsed["receiptDigest"]) is not str or not _DIGEST_RE.fullmatch(parsed["receiptDigest"])):
            raise ValueError
        snapshot_bytes = snapshot.encode("utf-8") if type(snapshot) is str else None
        committed_at = parsed["committedAt"]
        if parsed["status"] == "COMMITTED":
            if snapshot_bytes is None or parsed["receiptDigest"] != hashlib.sha256(snapshot_bytes).hexdigest() or not committed_at:
                raise ValueError
            committed_dt = _utc(datetime.fromisoformat(committed_at), "committedAt")
            if committed_dt < issued_at or committed_dt > expires_at:
                raise ValueError
            if now is not None and committed_dt > now:
                raise ValueError
            from ..domains.ecommerce.strategy_routing import _restore_receipt
            restored = _restore_receipt(snapshot_bytes)
            if serialize_receipt(restored) != snapshot_bytes:
                raise ValueError
            if slot is not None and (
                restored.tool_name != slot.tool_name
                or restored.step_id != slot.step_id
                or restored.state_revision != slot.state_revision
                or restored.args_digest != slot.canonical_args_digest
            ):
                raise ValueError
            receipt_issued_at = _utc(restored.issued_at, "receipt issuedAt")
            if receipt_issued_at < issued_at or receipt_issued_at > expires_at:
                raise ValueError
            if now is not None and receipt_issued_at > now:
                raise ValueError
        elif parsed["status"] in {"CLAIMED", "UNKNOWN"}:
            if snapshot is not None or parsed["receiptDigest"] is not None or committed_at is not None:
                raise ValueError
        else:
            raise ValueError
        if type(parsed["leaseToken"]) is not str or not _LEASE_TOKEN_RE.fullmatch(parsed["leaseToken"]):
            raise ValueError
        return LedgerResponse(
            LedgerStatus(status),
            parsed["leaseToken"],
            issued_at,
            expires_at,
            parsed["recoveryCount"],
            snapshot_bytes,
        )
    except Exception:
        return LedgerResponse(LedgerStatus.UNKNOWN, error_code="CORRUPT_RECORD")


class StrategyReceiptLedger:
    def __init__(
        self,
        client: RedisLike,
        *,
        clock: LedgerClock,
        ttl_seconds: int = 24 * 60 * 60,
        lease_seconds: int = 30,
        max_recovery_count: int = _MAX_RECOVERY_COUNT,
    ) -> None:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise ValueError("ttl_seconds exceeds safe bound")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError("lease_seconds exceeds safe bound")
        if type(max_recovery_count) is not int or not 0 <= max_recovery_count <= _MAX_RECOVERY_COUNT:
            raise ValueError("max_recovery_count exceeds safe bound")
        self._client = client
        self._clock = clock
        self._ttl = ttl_seconds
        self._lease = lease_seconds
        self._max_recovery = max_recovery_count

    @staticmethod
    def _validate_receipt(slot: StrategyLedgerSlot, receipt: object) -> bytes:
        if type(receipt) is not ToolReceipt:
            raise PermissionError("receipt must be runner-issued Strategy ToolReceipt")
        raw = serialize_receipt(receipt)
        if (
            receipt.tool_name != slot.tool_name
            or receipt.step_id != slot.step_id
            or receipt.state_revision != slot.state_revision
            or receipt.args_digest != slot.canonical_args_digest
        ):
            raise ValueError("receipt does not match complete ledger slot")
        return bytes(raw)

    async def _claim_remote(self, slot_key: str, record_key: str, slot_raw: str, record_raw: str, identity: str, now_ms: int, expiry_ms: int) -> object:
        custom = getattr(self._client, "strategy_ledger_claim", None)
        if callable(custom):
            return await custom(
                slot_key, record_key, slot_raw, record_raw, identity,
                now_ms, expiry_ms, self._max_recovery, self._ttl * 1000,
            )
        return await self._client.eval(
            _CLAIM_LUA, 2, slot_key, record_key, slot_raw, record_raw,
            identity, str(now_ms), str(expiry_ms), str(self._max_recovery), str(self._ttl * 1000),
        )

    async def _commit_remote(self, slot_key: str, record_key: str, identity: str, token: str, receipt_raw: str, receipt_digest: str, now_ms: int, committed_at: str, receipt_issued_ms: int) -> object:
        custom = getattr(self._client, "strategy_ledger_commit", None)
        if callable(custom):
            return await custom(slot_key, record_key, identity, token, receipt_raw, receipt_digest, now_ms, committed_at, self._ttl * 1000, receipt_issued_ms)
        return await self._client.eval(
            # ARGV1 identityDigest, 2 leaseToken, 3 receiptSnapshot,
            # 4 receiptDigest, 5 nowMs, 6 committedAtUTC, 7 ttlMs,
            # 8 receiptIssuedMs, checked against persisted leaseIssuedAtMs.
            _COMMIT_LUA, 2, slot_key, record_key, identity, token,
            receipt_raw, receipt_digest, str(now_ms), committed_at, str(self._ttl * 1000),
            str(receipt_issued_ms),
        )

    async def claim(self, slot: StrategyLedgerSlot) -> LedgerResponse:
        try:
            if type(slot) is not StrategyLedgerSlot:
                raise PermissionError("slot must be server-validated StrategyLedgerSlot")
            slot = StrategyLedgerSlot.create(
                task_id=slot.task_id, run_id=slot.run_id, thread_id=slot.thread_id,
                plan_id=slot.plan_id, step_id=slot.step_id,
                state_revision=slot.state_revision, tool_name=slot.tool_name,
                canonical_args_digest=slot.canonical_args_digest,
            )
            now = _now(self._clock)
            issued = now
            expires = now + timedelta(seconds=self._lease)
            identity = slot.identity_digest()
            slot_key, record_key = _key(slot)
            lease = _token()
            slot_raw = _json_bytes({"identityDigest": identity, "recordKey": record_key}).decode("ascii")
            record = {
                "status": LedgerStatus.CLAIMED.value,
                "identityDigest": identity,
                "recordKey": record_key,
                "leaseToken": lease,
                "issuedAt": issued.isoformat(),
                "leaseIssuedAtMs": int(issued.timestamp() * 1000),
                "leaseExpiresAt": expires.isoformat(),
                "leaseExpiresMs": int(expires.timestamp() * 1000),
                "recoveryCount": 0,
                "receiptSnapshot": None,
                "receiptDigest": None,
                "committedAt": None,
            }
            record_raw = _json_bytes(record).decode("ascii")
            if len(slot_raw.encode("utf-8")) > _MAX_SLOT_JSON_BYTES or len(record_raw.encode("utf-8")) > _MAX_RECORD_JSON_BYTES:
                raise ValueError("ledger canonical JSON budget exceeded")
            result = await self._claim_remote(
                slot_key, record_key, slot_raw, record_raw,
                identity, int(now.timestamp() * 1000), int(expires.timestamp() * 1000),
            )
            status = result[0].decode("ascii") if isinstance(result[0], bytes) else result[0]
            raw = result[1]
            if status == LedgerStatus.CLAIMED:
                return _response(status, raw, slot=slot, now=now, lease_seconds=self._lease)
            if status in {LedgerStatus.REPLAY, LedgerStatus.COMMITTED, LedgerStatus.IN_PROGRESS, LedgerStatus.UNKNOWN, LedgerStatus.NEEDS_REVIEW, LedgerStatus.REVISION_CONFLICT, LedgerStatus.CORRUPT}:
                return _response(status, raw, slot=slot, now=now, lease_seconds=self._lease)
            if status == "RECOVERY_RETRY_ALLOWED":
                return LedgerResponse(LedgerStatus.RECOVERY_RETRY_ALLOWED)
            return LedgerResponse(LedgerStatus.CORRUPT, error_code="ledger_corrupt")
        except (PermissionError, ValueError):
            return LedgerResponse(LedgerStatus.POLICY_DENIED, error_code="policy_denied")
        except Exception:
            return LedgerResponse(LedgerStatus.LEDGER_UNAVAILABLE, error_code="ledger_unavailable")

    async def commit(self, slot: StrategyLedgerSlot, *, lease_token: str, receipt: object) -> LedgerResponse:
        try:
            if type(slot) is not StrategyLedgerSlot:
                raise PermissionError("slot must be server-validated StrategyLedgerSlot")
            slot = StrategyLedgerSlot.create(
                task_id=slot.task_id, run_id=slot.run_id, thread_id=slot.thread_id,
                plan_id=slot.plan_id, step_id=slot.step_id,
                state_revision=slot.state_revision, tool_name=slot.tool_name,
                canonical_args_digest=slot.canonical_args_digest,
            )
            if type(lease_token) is not str or not _LEASE_TOKEN_RE.fullmatch(lease_token):
                raise PermissionError("invalid lease token")
            now = _now(self._clock)
            raw = self._validate_receipt(slot, receipt)
            # Strategy ToolReceipt v1 exposes one server-issued `issuedAt`
            # instant rather than separate startedAt/finishedAt fields.  The
            # contract therefore treats that instant as both boundaries:
            # it must be UTC, not future-dated, and the Lua/Fake CAS below
            # rejects it outside the authority lease window.
            receipt_issued_at = _utc(receipt.issued_at, "receipt issuedAt")
            if receipt_issued_at > now:
                raise ValueError("receipt time is after the authority commit time")
            slot_key, record_key = _key(slot)
            identity = slot.identity_digest()
            result = await self._commit_remote(
                slot_key, record_key, identity, lease_token,
                raw.decode("utf-8"), hashlib.sha256(raw).hexdigest(),
                int(now.timestamp() * 1000), now.isoformat(),
                int(receipt_issued_at.timestamp() * 1000),
            )
            status = result[0].decode("ascii") if isinstance(result[0], bytes) else result[0]
            response_raw = result[1]
            if status in {LedgerStatus.REPLAY, LedgerStatus.COMMITTED, LedgerStatus.UNKNOWN, LedgerStatus.REVISION_CONFLICT, LedgerStatus.CORRUPT}:
                return _response(status, response_raw, slot=slot, now=now, lease_seconds=self._lease)
            return LedgerResponse(LedgerStatus.CORRUPT, error_code="ledger_corrupt")
        except (PermissionError, ValueError):
            return LedgerResponse(LedgerStatus.POLICY_DENIED, error_code="policy_denied")
        except Exception:
            return LedgerResponse(LedgerStatus.LEDGER_UNAVAILABLE, error_code="ledger_unavailable")


_CLAIM_LUA = r"""
local index_fields = {identityDigest=true, recordKey=true}
local record_fields = {status=true, identityDigest=true, recordKey=true, leaseToken=true, issuedAt=true, leaseIssuedAtMs=true, leaseExpiresAt=true, leaseExpiresMs=true, recoveryCount=true, receiptSnapshot=true, receiptDigest=true, committedAt=true}
local function exact_keys(value, expected, total)
  if type(value) ~= 'table' then return false end
  local count = 0
  for key, _ in pairs(value) do
    if not expected[key] then return false end
    count = count + 1
  end
  return count == total
end
local function digest(value)
  return type(value) == 'string' and string.len(value) == 64 and string.match(value, '^[0-9a-f]+$') ~= nil
end
local function valid_index(value)
  return exact_keys(value, index_fields, 2)
    and digest(value.identityDigest)
    and type(value.recordKey) == 'string'
    and string.match(value.recordKey, '^strategy%-receipt%-ledger:v1:record:[0-9a-f]+$') ~= nil
end
local function nullable_string(value)
  return value == cjson.null or type(value) == 'string'
end
local function integer_number(value)
  return type(value) == 'number' and value >= 0 and math.floor(value) == value
end
local function valid_record(value)
  if not exact_keys(value, record_fields, 12) then return false end
  if value.status ~= 'CLAIMED' and value.status ~= 'COMMITTED' and value.status ~= 'UNKNOWN' then return false end
  if not digest(value.identityDigest) or type(value.recordKey) ~= 'string' or string.match(value.recordKey, '^strategy%-receipt%-ledger:v1:record:[0-9a-f]+$') == nil then return false end
  if type(value.leaseToken) ~= 'string' or string.len(value.leaseToken) ~= 38 or string.match(value.leaseToken, '^lease%-[A-Za-z0-9_-]+$') == nil then return false end
  if type(value.issuedAt) ~= 'string' or type(value.leaseExpiresAt) ~= 'string' then return false end
  if not integer_number(value.leaseIssuedAtMs) or not integer_number(value.leaseExpiresMs) or not integer_number(value.recoveryCount) or value.recoveryCount > 1 then return false end
  if not nullable_string(value.receiptSnapshot) or not nullable_string(value.receiptDigest) or not nullable_string(value.committedAt) then return false end
  if value.status == 'COMMITTED' then
    if value.receiptSnapshot == cjson.null or value.receiptDigest == cjson.null or value.committedAt == cjson.null then return false end
    if string.len(value.receiptSnapshot) > 4096 or not digest(value.receiptDigest) then return false end
  elseif value.receiptSnapshot ~= cjson.null or value.receiptDigest ~= cjson.null or value.committedAt ~= cjson.null then
    return false
  end
  return true
end
if string.len(ARGV[1]) > 1024 or string.len(ARGV[2]) > 8192 then return {'POLICY_DENIED', ''} end
local initial_index_ok, initial_index = pcall(cjson.decode, ARGV[1])
local initial_record_ok, initial_record = pcall(cjson.decode, ARGV[2])
if not initial_index_ok or not initial_record_ok or not valid_index(initial_index) or not valid_record(initial_record) then return {'POLICY_DENIED', ''} end
local index_raw = redis.call('get', KEYS[1])
if not index_raw then
  redis.call('set', KEYS[1], ARGV[1], 'PX', ARGV[7])
  redis.call('set', KEYS[2], ARGV[2], 'PX', ARGV[7])
  return {'CLAIMED', ARGV[2]}
end
if string.len(index_raw) > 1024 then return {'CORRUPT', ''} end
local ok, index = pcall(cjson.decode, index_raw)
if not ok or not valid_index(index) then return {'CORRUPT', ''} end
if index.identityDigest ~= ARGV[3] or index.recordKey ~= KEYS[2] then return {'REVISION_CONFLICT', ''} end
local raw = redis.call('get', KEYS[2])
if not raw then return {'UNKNOWN', ''} end
if string.len(raw) > 8192 then return {'CORRUPT', ''} end
local ok2, rec = pcall(cjson.decode, raw)
if not ok2 or not valid_record(rec) then return {'CORRUPT', ''} end
if rec.identityDigest ~= ARGV[3] or rec.recordKey ~= KEYS[2] then return {'REVISION_CONFLICT', ''} end
if rec.status == 'COMMITTED' then return {'REPLAY', raw} end
if rec.status == 'CLAIMED' then
  if tonumber(ARGV[4]) <= tonumber(rec.leaseExpiresMs) then return {'IN_PROGRESS', raw} end
  if tonumber(rec.recoveryCount) >= tonumber(ARGV[6]) then rec.status = 'UNKNOWN'; redis.call('set', KEYS[2], cjson.encode(rec), 'PX', ARGV[7]); return {'UNKNOWN', cjson.encode(rec)} end
  local next = cjson.decode(ARGV[2]); next.recoveryCount = tonumber(rec.recoveryCount) + 1; redis.call('set', KEYS[2], cjson.encode(next), 'PX', ARGV[7]); return {'CLAIMED', cjson.encode(next)}
end
return {'UNKNOWN', raw}
"""

_COMMIT_LUA = r"""
-- ARGV1 identityDigest, 2 leaseToken, 3 receiptSnapshot, 4 receiptDigest,
-- 5 nowMs, 6 committedAtUTC, 7 ttlMs, 8 receiptIssuedMs.
local index_fields = {identityDigest=true, recordKey=true}
local record_fields = {status=true, identityDigest=true, recordKey=true, leaseToken=true, issuedAt=true, leaseIssuedAtMs=true, leaseExpiresAt=true, leaseExpiresMs=true, recoveryCount=true, receiptSnapshot=true, receiptDigest=true, committedAt=true}
local function exact_keys(value, expected, total)
  if type(value) ~= 'table' then return false end
  local count = 0
  for key, _ in pairs(value) do if not expected[key] then return false end; count = count + 1 end
  return count == total
end
local function digest(value) return type(value) == 'string' and string.len(value) == 64 and string.match(value, '^[0-9a-f]+$') ~= nil end
local function valid_index(value)
  return exact_keys(value, index_fields, 2) and digest(value.identityDigest) and type(value.recordKey) == 'string' and string.match(value.recordKey, '^strategy%-receipt%-ledger:v1:record:[0-9a-f]+$') ~= nil
end
local function nullable_string(value) return value == cjson.null or type(value) == 'string' end
local function integer_number(value) return type(value) == 'number' and value >= 0 and math.floor(value) == value end
local function valid_record(value)
  if not exact_keys(value, record_fields, 12) then return false end
  if value.status ~= 'CLAIMED' and value.status ~= 'COMMITTED' and value.status ~= 'UNKNOWN' then return false end
  if not digest(value.identityDigest) or type(value.recordKey) ~= 'string' or string.match(value.recordKey, '^strategy%-receipt%-ledger:v1:record:[0-9a-f]+$') == nil then return false end
  if type(value.leaseToken) ~= 'string' or string.len(value.leaseToken) ~= 38 or string.match(value.leaseToken, '^lease%-[A-Za-z0-9_-]+$') == nil then return false end
  if type(value.issuedAt) ~= 'string' or type(value.leaseExpiresAt) ~= 'string' then return false end
  if not integer_number(value.leaseIssuedAtMs) or not integer_number(value.leaseExpiresMs) or not integer_number(value.recoveryCount) or value.recoveryCount > 1 then return false end
  if not nullable_string(value.receiptSnapshot) or not nullable_string(value.receiptDigest) or not nullable_string(value.committedAt) then return false end
  if value.status == 'COMMITTED' then
    if value.receiptSnapshot == cjson.null or value.receiptDigest == cjson.null or value.committedAt == cjson.null then return false end
    if string.len(value.receiptSnapshot) > 4096 or not digest(value.receiptDigest) then return false end
  elseif value.receiptSnapshot ~= cjson.null or value.receiptDigest ~= cjson.null or value.committedAt ~= cjson.null then return false end
  return true
end
local index_raw = redis.call('get', KEYS[1])
if not index_raw then return {'REVISION_CONFLICT', ''} end
if string.len(index_raw) > 1024 then return {'CORRUPT', ''} end
local ok, index = pcall(cjson.decode, index_raw)
if not ok or not valid_index(index) then return {'CORRUPT', ''} end
if index.identityDigest ~= ARGV[1] or index.recordKey ~= KEYS[2] then return {'REVISION_CONFLICT', ''} end
local raw = redis.call('get', KEYS[2]); if not raw then return {'UNKNOWN', ''} end
if string.len(raw) > 8192 then return {'CORRUPT', ''} end
local ok2, rec = pcall(cjson.decode, raw); if not ok2 or not valid_record(rec) then return {'CORRUPT', ''} end
if rec.identityDigest ~= ARGV[1] or rec.recordKey ~= KEYS[2] then return {'REVISION_CONFLICT', ''} end
if rec.status == 'COMMITTED' then if rec.receiptDigest == ARGV[4] and rec.receiptSnapshot == ARGV[3] then return {'REPLAY', raw} else return {'REVISION_CONFLICT', raw} end end
if rec.status ~= 'CLAIMED' or rec.leaseToken ~= ARGV[2] then return {'REVISION_CONFLICT', raw} end
if tonumber(rec.leaseExpiresMs or 0) < tonumber(ARGV[5]) then return {'REVISION_CONFLICT', raw} end
if string.len(ARGV[2]) ~= 38 or string.match(ARGV[2], '^lease%-[A-Za-z0-9_-]+$') == nil then return {'REVISION_CONFLICT', raw} end
if tonumber(ARGV[8]) > tonumber(ARGV[5]) or tonumber(ARGV[8]) < tonumber(rec.leaseIssuedAtMs) or tonumber(ARGV[8]) > tonumber(rec.leaseExpiresMs) then return {'REVISION_CONFLICT', ''} end
if string.len(ARGV[3]) > 4096 or string.len(ARGV[4]) ~= 64 or string.match(ARGV[4], '^[0-9a-f]+$') == nil then return {'REVISION_CONFLICT', raw} end
rec.status = 'COMMITTED'; rec.receiptSnapshot = ARGV[3]; rec.receiptDigest = ARGV[4]; rec.committedAt = ARGV[6];
redis.call('set', KEYS[2], cjson.encode(rec), 'PX', ARGV[7]);
redis.call('pexpire', KEYS[1], ARGV[7]);
return {'COMMITTED', cjson.encode(rec)}
"""


__all__ = ["LedgerResponse", "LedgerStatus", "StrategyLedgerSlot", "StrategyReceiptLedger"]
