"""Authoritative Redis inbox for durable read-only Agent tool calls.

TaskState is deliberately absent from this module: it may mirror a receipt,
but it cannot decide whether an external call is safe to repeat.  The logical
slot survives a restarted worker; the execution attempt and fencing token do
not.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ..schemas import ToolTrace

_NAMESPACE = "agent-tool-inbox:v2"
_READ_ONLY = frozenset({"search_products", "get_product_details", "compare_products", "rerank_products_in_scope"})
_HEX = frozenset("0123456789abcdef")


class InboxStatus(StrEnum):
    ABSENT = "ABSENT"
    CLAIMED = "CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    IN_FLIGHT = "IN_FLIGHT"
    # This is a ledger-commit state: the external call completed and its
    # immutable trace was recorded.  Business success remains trace.ok and
    # receipt.toolOutcome, never this storage status alone.
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    UNKNOWN = "UNKNOWN"
    TERMINAL_FAILED = "TERMINAL_FAILED"
    CONFLICT = "CONFLICT"
    FENCED_OUT = "FENCED_OUT"
    UNAVAILABLE = "UNAVAILABLE"


class RedisInbox(Protocol):
    async def eval(self, script: str, numkeys: int, *args: object) -> object: ...


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _opaque(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 160:
        raise ValueError(f"{field} must be a bounded server identifier")
    return value


def _hash(value: object, field: str) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in _HEX for c in value):
        raise ValueError(f"{field} must be SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class ToolInboxSlot:
    task_id: str
    plan_id: str
    step_id: str
    state_revision: int
    tool_name: str
    canonical_args_sha256: str

    @classmethod
    def create(cls, **value: object) -> "ToolInboxSlot":
        revision = value.get("state_revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("stateRevision must be positive")
        tool = _opaque(value.get("tool_name"), "toolName")
        if tool not in _READ_ONLY:
            raise PermissionError("only read-only tools are eligible")
        return cls(
            _opaque(value.get("task_id"), "taskId"),
            _opaque(value.get("plan_id"), "planId"),
            _opaque(value.get("step_id"), "stepId"),
            revision,
            tool,
            _hash(value.get("canonical_args_sha256"), "canonicalArgsSha256"),
        )

    def base(self) -> dict[str, object]:
        return {"taskId": self.task_id, "planId": self.plan_id, "stepId": self.step_id, "stateRevision": self.state_revision, "toolName": self.tool_name}

    def logical_slot_key(self) -> str:
        return hashlib.sha256(canonical_bytes(self.base())).hexdigest()

    def execution_id(self, *, run_id: str, thread_id: str) -> str:
        return hashlib.sha256(canonical_bytes({**self.base(), "runId": _opaque(run_id, "runId"), "threadId": _opaque(thread_id, "threadId"), "canonicalArgsSha256": self.canonical_args_sha256})).hexdigest()


@dataclass(frozen=True, slots=True)
class InboxResponse:
    status: InboxStatus
    fence: int | None = None
    execution_id: str | None = None
    trace: ToolTrace | None = None
    receipt: dict[str, object] | None = None
    error_code: str | None = None


def _keys(slot: ToolInboxSlot) -> tuple[str, str, str]:
    """Return per-slot state plus the namespace-wide non-recycling fence.

    The slot/index records are deliberately allowed to expire.  A fence is not:
    once a worker has observed a token, recreating a slot after retention TTL
    must never make that token current again.  Consequently the third key is a
    *single namespace sequence*, rather than a per-slot key with a TTL.
    """
    logical = slot.logical_slot_key()
    return (
        f"{_NAMESPACE}:slot:{logical}",
        f"{_NAMESPACE}:record:{logical}",
        f"{_NAMESPACE}:fence-sequence",
    )


class ToolInbox:
    def __init__(self, client: RedisInbox, *, lease_ms: int = 30_000, ttl_ms: int = 7 * 24 * 3600 * 1000) -> None:
        if type(lease_ms) is not int or not 100 <= lease_ms <= 300_000:
            raise ValueError("invalid lease")
        if type(ttl_ms) is not int or ttl_ms < lease_ms or ttl_ms > 7 * 24 * 3600 * 1000:
            raise ValueError("invalid ttl")
        self._client, self._lease, self._ttl = client, lease_ms, ttl_ms

    async def claim(self, slot: ToolInboxSlot, *, run_id: str, thread_id: str) -> InboxResponse:
        slot = ToolInboxSlot.create(**{"task_id": slot.task_id, "plan_id": slot.plan_id, "step_id": slot.step_id, "state_revision": slot.state_revision, "tool_name": slot.tool_name, "canonical_args_sha256": slot.canonical_args_sha256})
        execution_id = slot.execution_id(run_id=run_id, thread_id=thread_id)
        now = int(time.time() * 1000)
        a, b, fence_key = _keys(slot)
        try:
            raw = await self._client.eval(_CLAIM_LUA, 3, a, b, fence_key, json.dumps(slot.base(), sort_keys=True), slot.canonical_args_sha256, execution_id, str(now), str(now + self._lease), str(self._ttl), slot.logical_slot_key())
            return _decode(raw, slot=slot)
        except Exception as exc:
            return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_claim_unavailable")

    async def enter_in_flight(self, slot: ToolInboxSlot, *, execution_id: str, fence: int) -> InboxResponse:
        a, b, fence_key = _keys(slot)
        try:
            return _decode(await self._client.eval(_IN_FLIGHT_LUA, 3, a, b, fence_key, execution_id, str(fence), json.dumps(slot.base(), sort_keys=True), slot.canonical_args_sha256, slot.logical_slot_key(), str(self._ttl)), slot=slot)
        except Exception:
            return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_inflight_unavailable")

    async def inspect(self, slot: ToolInboxSlot) -> InboxResponse:
        """Read runner-owned state for recovery; this never grants a claim."""
        a, b, fence_key = _keys(slot)
        try:
            return _decode(await self._client.eval(_INSPECT_LUA, 3, a, b, fence_key, json.dumps(slot.base(), sort_keys=True), slot.canonical_args_sha256, slot.logical_slot_key()), slot=slot)
        except Exception:
            return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_inspect_unavailable")

    async def complete(self, slot: ToolInboxSlot, *, execution_id: str, fence: int, trace: ToolTrace, receipt: dict[str, object]) -> InboxResponse:
        if trace.tool != slot.tool_name:
            raise ValueError("trace tool mismatch")
        expected_outcome = "tool_succeeded" if trace.ok else "tool_failed"
        if receipt.get("toolOutcome") != expected_outcome:
            raise ValueError("receipt toolOutcome does not match trace")
        trace_raw = trace.model_dump_json(by_alias=True)
        receipt_raw = canonical_bytes(receipt).decode("utf-8")
        a, b, fence_key = _keys(slot)
        try:
            return _decode(await self._client.eval(_COMPLETE_LUA, 3, a, b, fence_key, execution_id, str(fence), trace_raw, receipt_raw, sha256(json.loads(trace_raw)), sha256(receipt), str(int(time.time() * 1000)), str(self._ttl), json.dumps(slot.base(), sort_keys=True), slot.canonical_args_sha256, slot.logical_slot_key()), slot=slot)
        except Exception:
            return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_complete_unavailable")


def _decode(raw: object, *, slot: ToolInboxSlot) -> InboxResponse:
    if not isinstance(raw, (list, tuple)) or not raw:
        return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_protocol_invalid")
    try:
        status = InboxStatus(raw[0].decode() if isinstance(raw[0], bytes) else raw[0])
    except Exception:
        return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_status_invalid")
    record_raw = raw[1] if len(raw) > 1 else None
    if status not in {InboxStatus.ABSENT, InboxStatus.SUCCEEDED, InboxStatus.CLAIMED, InboxStatus.IN_PROGRESS, InboxStatus.IN_FLIGHT, InboxStatus.UNKNOWN, InboxStatus.CONFLICT, InboxStatus.FENCED_OUT, InboxStatus.RETRYABLE_FAILED, InboxStatus.TERMINAL_FAILED}:
        return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_record_invalid")
    if not record_raw:
        return InboxResponse(status)
    if isinstance(record_raw, bytes): record_raw = record_raw.decode("utf-8")
    try:
        record = json.loads(record_raw)
        if (
            record["base"] != slot.base()
            or record["identityDigest"] != slot.logical_slot_key()
            or record["inputHash"] != slot.canonical_args_sha256
            or not isinstance(record.get("executionId"), str)
            or not isinstance(record.get("fence"), int)
            or type(record.get("fence")) is not int
            or record["fence"] < 1
        ):
            return InboxResponse(InboxStatus.CONFLICT)
        trace = None
        receipt = None
        if status is InboxStatus.SUCCEEDED:
            trace_raw = record.get("trace")
            receipt_raw = record.get("receipt")
            if not isinstance(trace_raw, str) or not isinstance(receipt_raw, str):
                return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_record_invalid")
            trace = ToolTrace.model_validate_json(trace_raw)
            receipt = json.loads(receipt_raw)
            trace_hash = sha256(trace.model_dump(by_alias=True, mode="json"))
            receipt_hash = sha256(receipt)
            tool_outcome = "tool_succeeded" if trace.ok else "tool_failed"
            if (
                trace.tool != slot.tool_name
                or record.get("resultHash") != trace_hash
                or record.get("receiptHash") != receipt_hash
                or receipt.get("taskId") != slot.task_id
                or receipt.get("planId") != slot.plan_id
                or receipt.get("stepId") != slot.step_id
                or receipt.get("toolName") != slot.tool_name
                or receipt.get("stateRevision") != slot.state_revision
                or receipt.get("inputHash") != slot.canonical_args_sha256
                or receipt.get("resultHash") != trace_hash
                or receipt.get("executionId") != record["executionId"]
                or receipt.get("logicalSlotKey") != slot.logical_slot_key()
                or receipt.get("fence") != record["fence"]
                or receipt.get("inboxStatus") != InboxStatus.SUCCEEDED.value
                or receipt.get("toolOutcome") != tool_outcome
            ):
                return InboxResponse(InboxStatus.CONFLICT)
        return InboxResponse(status, int(record["fence"]), record["executionId"], trace, receipt)
    except Exception:
        return InboxResponse(InboxStatus.UNAVAILABLE, error_code="redis_record_invalid")


_CLAIM_LUA = r'''
local index=redis.call('get',KEYS[1]); local record=redis.call('get',KEYS[2]); local base=cjson.decode(ARGV[1]); local now=tonumber(ARGV[4])
local function matches(i,r)
 return type(i)=='table' and type(r)=='table' and type(r.base)=='table'
  and i.identityDigest==ARGV[7] and i.inputHash==ARGV[2]
  and r.identityDigest==ARGV[7] and r.inputHash==ARGV[2]
  and r.base.taskId==base.taskId and r.base.planId==base.planId and r.base.stepId==base.stepId
  and r.base.stateRevision==base.stateRevision and r.base.toolName==base.toolName
  and type(r.executionId)=='string' and type(r.fence)=='number' and r.fence>=1
end
if not index and not record then
 local fence=redis.call('incr',KEYS[3]); local r={base=base,identityDigest=ARGV[7],inputHash=ARGV[2],executionId=ARGV[3],fence=fence,status='CLAIMED',leaseExpiresMs=tonumber(ARGV[5]),trace=nil,receipt=nil}
 local encoded=cjson.encode(r); local idx=cjson.encode({identityDigest=ARGV[7],inputHash=ARGV[2]})
 redis.call('set',KEYS[1],idx,'PX',ARGV[6]); redis.call('set',KEYS[2],encoded,'PX',ARGV[6]); return {'CLAIMED',encoded}
end
if not index or not record then return {'UNKNOWN',''} end
local i=cjson.decode(index); local r=cjson.decode(record); if not matches(i,r) then return {'CONFLICT',''} end
if r.status=='SUCCEEDED' then return {'SUCCEEDED',record} end
if r.status=='CLAIMED' and now>r.leaseExpiresMs then
 local fence=redis.call('incr',KEYS[3]); r.executionId=ARGV[3]; r.fence=fence; r.status='CLAIMED'; r.leaseExpiresMs=tonumber(ARGV[5]); r.trace=nil; r.receipt=nil; r.resultHash=nil; r.receiptHash=nil
 local encoded=cjson.encode(r); local idx=cjson.encode({identityDigest=ARGV[7],inputHash=ARGV[2]})
 redis.call('set',KEYS[1],idx,'PX',ARGV[6]); redis.call('set',KEYS[2],encoded,'PX',ARGV[6]); return {'CLAIMED',encoded}
end
if r.status=='IN_FLIGHT' and now>r.leaseExpiresMs then
 r.status='UNKNOWN'; local encoded=cjson.encode(r); local idx=cjson.encode({identityDigest=ARGV[7],inputHash=ARGV[2]})
 redis.call('set',KEYS[1],idx,'PX',ARGV[6]); redis.call('set',KEYS[2],encoded,'PX',ARGV[6]); return {'UNKNOWN',encoded}
end
if r.status=='CLAIMED' or r.status=='IN_FLIGHT' then return {'IN_PROGRESS',record} end
return {r.status,record}
'''
_INSPECT_LUA = r'''
local index=redis.call('get',KEYS[1]); local record=redis.call('get',KEYS[2]); local base=cjson.decode(ARGV[1])
if not index and not record then return {'ABSENT',''} end
if not index or not record then return {'CONFLICT',''} end
local i=cjson.decode(index); local r=cjson.decode(record)
if type(i)~='table' or type(r)~='table' or type(r.base)~='table'
 or i.identityDigest~=ARGV[3] or i.inputHash~=ARGV[2]
 or r.identityDigest~=ARGV[3] or r.inputHash~=ARGV[2]
 or r.base.taskId~=base.taskId or r.base.planId~=base.planId or r.base.stepId~=base.stepId
 or r.base.stateRevision~=base.stateRevision or r.base.toolName~=base.toolName then return {'CONFLICT',''} end
return {r.status,record}
'''
_IN_FLIGHT_LUA = r'''
local index=redis.call('get',KEYS[1]); local raw=redis.call('get',KEYS[2]); local base=cjson.decode(ARGV[3])
if not index or not raw then return {'UNKNOWN',''} end
local i=cjson.decode(index); local r=cjson.decode(raw)
if type(i)~='table' or type(r)~='table' or type(r.base)~='table'
 or i.identityDigest~=ARGV[5] or i.inputHash~=ARGV[4]
 or r.identityDigest~=ARGV[5] or r.inputHash~=ARGV[4]
 or r.base.taskId~=base.taskId or r.base.planId~=base.planId or r.base.stepId~=base.stepId
 or r.base.stateRevision~=base.stateRevision or r.base.toolName~=base.toolName then return {'CONFLICT',''} end
if r.executionId~=ARGV[1] or r.fence~=tonumber(ARGV[2]) then return {'FENCED_OUT',raw} end
if r.status~='CLAIMED' then return {r.status,raw} end
r.status='IN_FLIGHT'; local encoded=cjson.encode(r); local idx=cjson.encode({identityDigest=ARGV[5],inputHash=ARGV[4]})
redis.call('set',KEYS[1],idx,'PX',ARGV[6]); redis.call('set',KEYS[2],encoded,'PX',ARGV[6]); return {'IN_FLIGHT',encoded}
'''
_COMPLETE_LUA = r'''
local index=redis.call('get',KEYS[1]); local raw=redis.call('get',KEYS[2]); local base=cjson.decode(ARGV[9])
if not index or not raw then return {'UNKNOWN',''} end
local i=cjson.decode(index); local r=cjson.decode(raw)
if type(i)~='table' or type(r)~='table' or type(r.base)~='table'
 or i.identityDigest~=ARGV[11] or i.inputHash~=ARGV[10]
 or r.identityDigest~=ARGV[11] or r.inputHash~=ARGV[10]
 or r.base.taskId~=base.taskId or r.base.planId~=base.planId or r.base.stepId~=base.stepId
 or r.base.stateRevision~=base.stateRevision or r.base.toolName~=base.toolName then return {'CONFLICT',''} end
if r.executionId~=ARGV[1] or r.fence~=tonumber(ARGV[2]) then return {'FENCED_OUT',raw} end
if r.status=='SUCCEEDED' then return {'SUCCEEDED',raw} end
if r.status~='IN_FLIGHT' then return {r.status,raw} end
r.status='SUCCEEDED'; r.trace=ARGV[3]; r.receipt=ARGV[4]; r.resultHash=ARGV[5]; r.receiptHash=ARGV[6]; r.completedAtMs=tonumber(ARGV[7])
local encoded=cjson.encode(r); local idx=cjson.encode({identityDigest=ARGV[11],inputHash=ARGV[10]})
redis.call('set',KEYS[1],idx,'PX',ARGV[8]); redis.call('set',KEYS[2],encoded,'PX',ARGV[8]); return {'SUCCEEDED',encoded}
'''

__all__ = ["InboxResponse", "InboxStatus", "ToolInbox", "ToolInboxSlot", "canonical_bytes", "sha256"]
