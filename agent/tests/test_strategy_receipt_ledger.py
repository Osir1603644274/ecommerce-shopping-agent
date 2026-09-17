import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.ecommerce.strategy_routing import (
    BoundedToolRequest,
    ErrorCode,
    ReceiptIssuer,
    ToolReceipt,
    serialize_receipt,
)
from app.graph.strategy_receipt_ledger import (
    LedgerStatus,
    StrategyLedgerSlot,
    StrategyReceiptLedger,
)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 22, tzinfo=UTC)

    def now_utc(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _async_test(function):
    def wrapped():
        return asyncio.run(function())
    return wrapped


class AtomicLedgerRedis:
    """Tiny atomic fake implementing the ledger's server-side seam."""

    def __init__(self):
        self.slots = {}
        self.records = {}
        self.expires = {}
        self.lock = asyncio.Lock()
        self.fail = False

    def _purge(self, now_ms):
        for key, expiry in list(self.expires.items()):
            if now_ms >= expiry:
                self.expires.pop(key, None)
                self.slots.pop(key, None)
                self.records.pop(key, None)

    @staticmethod
    def _exact_json(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    @staticmethod
    def _valid_index(value):
        return type(value) is dict and set(value) == {"identityDigest", "recordKey"} and type(value["identityDigest"]) is str and len(value["identityDigest"]) == 64 and type(value["recordKey"]) is str

    @classmethod
    def _valid_record(cls, value):
        fields = {"status", "identityDigest", "recordKey", "leaseToken", "issuedAt", "leaseIssuedAtMs", "leaseExpiresAt", "leaseExpiresMs", "recoveryCount", "receiptSnapshot", "receiptDigest", "committedAt"}
        if type(value) is not dict or set(value) != fields:
            return False
        if value["status"] not in {"CLAIMED", "COMMITTED", "UNKNOWN"} or type(value["identityDigest"]) is not str or len(value["identityDigest"]) != 64:
            return False
        if type(value["recordKey"]) is not str or type(value["leaseToken"]) is not str or len(value["leaseToken"]) != 38:
            return False
        if type(value["issuedAt"]) is not str or type(value["leaseIssuedAtMs"]) is not int or type(value["leaseExpiresAt"]) is not str or type(value["leaseExpiresMs"]) is not int:
            return False
        if type(value["recoveryCount"]) is not int or not 0 <= value["recoveryCount"] <= 1:
            return False
        nullable = (value["receiptSnapshot"] is None or type(value["receiptSnapshot"]) is str) and (value["receiptDigest"] is None or type(value["receiptDigest"]) is str) and (value["committedAt"] is None or type(value["committedAt"]) is str)
        if not nullable:
            return False
        if value["status"] == "COMMITTED":
            return type(value["receiptSnapshot"]) is str and type(value["receiptDigest"]) is str and len(value["receiptSnapshot"].encode()) <= 4096 and value["committedAt"] is not None
        return value["receiptSnapshot"] is None and value["receiptDigest"] is None and value["committedAt"] is None

    async def strategy_ledger_claim(self, slot_key, record_key, slot_raw, record_raw, identity, now_ms, expiry_ms, max_recovery, ttl_ms):
        if self.fail:
            raise ConnectionError("redis unavailable")
        async with self.lock:
            if len(slot_raw.encode("utf-8")) > 1024 or len(record_raw.encode("utf-8")) > 8192:
                return ["POLICY_DENIED", ""]
            self._purge(now_ms)
            index = self.slots.get(slot_key)
            if index is None:
                if not self._valid_index(json.loads(slot_raw)) or not self._valid_record(json.loads(record_raw)):
                    return ["POLICY_DENIED", ""]
                self.slots[slot_key] = json.loads(slot_raw)
                self.records[record_key] = json.loads(record_raw)
                self.expires[slot_key] = now_ms + ttl_ms
                self.expires[record_key] = now_ms + ttl_ms
                return ["CLAIMED", record_raw]
            if not self._valid_index(index) or len(self._exact_json(index)) > 1024:
                return ["CORRUPT", ""]
            if index["identityDigest"] != identity or index["recordKey"] != record_key:
                return ["REVISION_CONFLICT", ""]
            record = self.records.get(record_key)
            if record is None:
                return ["UNKNOWN", ""]
            if not self._valid_record(record) or len(self._exact_json(record)) > 8192:
                return ["CORRUPT", ""]
            if record["identityDigest"] != identity or record["recordKey"] != record_key:
                return ["REVISION_CONFLICT", ""]
            if record["status"] == "COMMITTED":
                return ["REPLAY", json.dumps(record, sort_keys=True, separators=(",", ":"))]
            if record["status"] == "CLAIMED":
                if now_ms <= record["leaseExpiresMs"]:
                    return ["IN_PROGRESS", json.dumps(record, sort_keys=True, separators=(",", ":"))]
                if record["recoveryCount"] >= max_recovery:
                    record = dict(record, status="UNKNOWN")
                    self.records[record_key] = record
                    self.expires[record_key] = now_ms + ttl_ms
                    return ["UNKNOWN", json.dumps(record, sort_keys=True, separators=(",", ":"))]
                next_record = json.loads(record_raw)
                next_record["recoveryCount"] = record["recoveryCount"] + 1
                self.records[record_key] = next_record
                self.expires[record_key] = now_ms + ttl_ms
                return ["CLAIMED", json.dumps(next_record, sort_keys=True, separators=(",", ":"))]
            return [record["status"], json.dumps(record, sort_keys=True, separators=(",", ":"))]

    async def strategy_ledger_commit(self, slot_key, record_key, identity, token, receipt_raw, receipt_digest, now_ms, committed_at, ttl_ms, receipt_issued_ms):
        if self.fail:
            raise ConnectionError("redis unavailable")
        async with self.lock:
            self._purge(now_ms)
            index = self.slots.get(slot_key)
            if index is None:
                return ["REVISION_CONFLICT", ""]
            if not self._valid_index(index) or len(self._exact_json(index)) > 1024:
                return ["CORRUPT", ""]
            if index["identityDigest"] != identity or index["recordKey"] != record_key:
                return ["REVISION_CONFLICT", ""]
            record = self.records.get(record_key)
            if record is None:
                return ["UNKNOWN", ""]
            if not self._valid_record(record) or len(self._exact_json(record)) > 8192:
                return ["CORRUPT", ""]
            if record["identityDigest"] != identity or record["recordKey"] != record_key:
                return ["REVISION_CONFLICT", ""]
            if record["status"] == "COMMITTED":
                if record["receiptDigest"] == receipt_digest and record["receiptSnapshot"] == receipt_raw:
                    return ["REPLAY", json.dumps(record, sort_keys=True, separators=(",", ":"))]
                return ["REVISION_CONFLICT", json.dumps(record, sort_keys=True, separators=(",", ":"))]
            if record["status"] != "CLAIMED" or record["leaseToken"] != token:
                return ["REVISION_CONFLICT", json.dumps(record, sort_keys=True, separators=(",", ":"))]
            if now_ms > record["leaseExpiresMs"]:
                return ["REVISION_CONFLICT", json.dumps(record, sort_keys=True, separators=(",", ":"))]
            if receipt_issued_ms > now_ms or receipt_issued_ms < record["leaseIssuedAtMs"] or receipt_issued_ms > record["leaseExpiresMs"]:
                return ["REVISION_CONFLICT", json.dumps(record, sort_keys=True, separators=(",", ":"))]
            record = dict(record, status="COMMITTED", receiptSnapshot=receipt_raw, receiptDigest=receipt_digest, committedAt=committed_at)
            self.records[record_key] = record
            self.expires[record_key] = now_ms + ttl_ms
            self.expires[slot_key] = now_ms + ttl_ms
            return ["COMMITTED", json.dumps(record, sort_keys=True, separators=(",", ":"))]


def _slot(**changes):
    values = {
        "task_id": "task-1", "run_id": "run-1", "thread_id": "thread-1",
        "plan_id": "plan-1", "step_id": "step-1", "state_revision": 3,
        "tool_name": "search_products", "canonical_args_digest": "a" * 64,
    }
    values.update(changes)
    return StrategyLedgerSlot.create(**values)


def _receipt(clock, *, task_id="task-1", step_id="step-1"):
    request = BoundedToolRequest.create(
        tool_name="search_products", arguments={"query": "phone", "limit": 2},
        state_revision=3, step_id=step_id,
    )
    slot = _slot(task_id=task_id, step_id=step_id, canonical_args_digest=request.args_digest)
    receipt = ReceiptIssuer(clock=clock).issue(
        request, receipt_id="receipt-1", outcome="succeeded"
    )
    return slot, receipt


def _foreign_receipt(clock):
    request = BoundedToolRequest.create(
        tool_name="get_product_details", arguments={"productId": 1},
        state_revision=3, step_id="step-1",
    )
    return ReceiptIssuer(clock=clock).issue(
        request, receipt_id="receipt-foreign", outcome="succeeded"
    )


@_async_test
async def test_two_ledger_instances_have_one_atomic_claim_and_exact_replay():
    clock = Clock()
    redis = AtomicLedgerRedis()
    first = StrategyReceiptLedger(redis, clock=clock)
    second = StrategyReceiptLedger(redis, clock=clock)
    slot, receipt = _receipt(clock)
    claimed, in_progress = await asyncio.gather(first.claim(slot), second.claim(slot))
    assert {claimed.status, in_progress.status} == {LedgerStatus.CLAIMED, LedgerStatus.IN_PROGRESS}
    winner = claimed if claimed.status is LedgerStatus.CLAIMED else in_progress
    committed = await first.commit(slot, lease_token=winner.lease_token, receipt=receipt)
    assert committed.status is LedgerStatus.COMMITTED
    duplicate = await first.commit(slot, lease_token=winner.lease_token, receipt=receipt)
    assert duplicate.status is LedgerStatus.REPLAY
    request = BoundedToolRequest.create(
        tool_name="search_products", arguments={"query": "phone", "limit": 2},
        state_revision=3, step_id="step-1",
    )
    different = ReceiptIssuer(clock=clock).issue(
        request, receipt_id="receipt-2", outcome="failed", error_code=ErrorCode.TOOL_FAILED
    )
    assert (await first.commit(slot, lease_token=winner.lease_token, receipt=different)).status is LedgerStatus.REVISION_CONFLICT
    replay = await second.claim(slot)
    assert replay.status is LedgerStatus.REPLAY
    assert replay.receipt_snapshot == committed.receipt_snapshot
    assert replay.receipt_snapshot is not committed.receipt_snapshot


@_async_test
async def test_same_slot_digest_conflict_and_receipt_mismatch_fail_closed():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock)
    slot, receipt = _receipt(clock)
    assert (await ledger.claim(slot)).status is LedgerStatus.CLAIMED
    foreign = _slot(canonical_args_digest="b" * 64)
    assert (await ledger.claim(foreign)).status is LedgerStatus.REVISION_CONFLICT
    assert (await ledger.commit(slot, lease_token="lease-forged", receipt=object())).status is LedgerStatus.POLICY_DENIED


@_async_test
async def test_expired_read_only_lease_allows_one_recovery_then_unknown():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock, lease_seconds=1, max_recovery_count=1)
    slot, receipt = _receipt(clock)
    first = await ledger.claim(slot)
    assert first.status is LedgerStatus.CLAIMED
    clock.advance(2)
    recovered = await ledger.claim(slot)
    assert recovered.status is LedgerStatus.CLAIMED and recovered.recovery_count == 1
    clock.advance(2)
    held = await ledger.claim(slot)
    assert held.status is LedgerStatus.UNKNOWN
    assert (await ledger.commit(slot, lease_token=recovered.lease_token, receipt=receipt)).status is LedgerStatus.REVISION_CONFLICT


@_async_test
async def test_write_transaction_and_sensitive_slots_are_denied_without_redis():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock)
    with pytest.raises((PermissionError, ValueError)):
        StrategyLedgerSlot.create(**{**_slot().plain(), "tool_name": "create_order"})
    with pytest.raises((PermissionError, ValueError)):
        StrategyLedgerSlot.create(**{**_slot().plain(), "task_id": "Bearer-token"})
    assert not redis.slots


@_async_test
async def test_redis_outage_is_fail_closed_and_never_dispatches():
    clock = Clock(); redis = AtomicLedgerRedis(); redis.fail = True
    ledger = StrategyReceiptLedger(redis, clock=clock)
    response = await ledger.claim(_slot())
    assert response.status is LedgerStatus.LEDGER_UNAVAILABLE
    assert not redis.slots


@_async_test
async def test_corrupt_redis_records_are_unknown_without_echoing_raw_data():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock)
    slot, receipt = _receipt(clock)
    claimed = await ledger.claim(slot)
    assert claimed.status is LedgerStatus.CLAIMED
    key = next(iter(redis.records))
    original = dict(redis.records[key])
    for mutation in (
        {"unknownField": "x"},
        {"padding": "x" * 100_000},
        {"leaseExpiresAt": "2026-08-22T00:00:00"},
        {"issuedAt": "2027-08-22T00:00:00+00:00"},
    ):
        redis.records[key] = {**original, **mutation}
        response = await ledger.claim(slot)
        assert response.status is LedgerStatus.UNKNOWN
        assert response.error_code == "CORRUPT_RECORD"
        assert response.receipt_snapshot is None
    redis.records[key] = original
    committed = await ledger.commit(slot, lease_token=claimed.lease_token, receipt=receipt)
    assert committed.status is LedgerStatus.COMMITTED
    redis.records[key] = dict(redis.records[key], receiptDigest="0" * 64)
    response = await ledger.claim(slot)
    assert response.status is LedgerStatus.UNKNOWN
    assert response.error_code == "CORRUPT_RECORD"
    assert response.receipt_snapshot is None
    redis.records[key] = dict(original, status="COMMITTED", receiptSnapshot="x" * 5000,
                              receiptDigest="0" * 64, committedAt=clock.now_utc().isoformat())
    response = await ledger.claim(slot)
    assert response.status is LedgerStatus.UNKNOWN
    assert response.error_code == "CORRUPT_RECORD"
    assert response.receipt_snapshot is None


@_async_test
async def test_committed_receipt_is_rebound_to_the_complete_slot():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock)
    slot, receipt = _receipt(clock)
    claimed = await ledger.claim(slot)
    assert claimed.status is LedgerStatus.CLAIMED
    assert (await ledger.commit(slot, lease_token=claimed.lease_token, receipt=receipt)).status is LedgerStatus.COMMITTED
    key = next(iter(redis.records))
    foreign = _foreign_receipt(clock)
    snapshot = serialize_receipt(foreign).decode("utf-8")
    redis.records[key] = dict(redis.records[key], receiptSnapshot=snapshot,
                              receiptDigest=hashlib.sha256(snapshot.encode()).hexdigest())
    response = await ledger.claim(slot)
    assert response.status is LedgerStatus.UNKNOWN
    assert response.error_code == "CORRUPT_RECORD"


@_async_test
async def test_fake_redis_px_ttl_expires_slot_and_record_independently():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock, ttl_seconds=1)
    slot, receipt = _receipt(clock)
    claimed = await ledger.claim(slot)
    assert claimed.status is LedgerStatus.CLAIMED
    clock.advance(1.2)
    assert (await ledger.commit(slot, lease_token=claimed.lease_token, receipt=receipt)).status is LedgerStatus.REVISION_CONFLICT
    assert (await ledger.claim(slot)).status is LedgerStatus.CLAIMED


@_async_test
async def test_fake_existing_oversized_slot_and_record_are_not_mutated():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock)
    slot, receipt = _receipt(clock, task_id="task-oversized")
    assert (await ledger.claim(slot)).status is LedgerStatus.CLAIMED
    slot_key, record_key = next(iter(redis.slots)), next(iter(redis.records))
    redis.slots[slot_key] = {"padding": "x" * 100_000}
    bad_slot = dict(redis.slots[slot_key])
    response = await ledger.claim(slot)
    assert response.status is LedgerStatus.UNKNOWN
    assert response.error_code == "CORRUPT_RECORD"
    assert redis.slots[slot_key] == bad_slot

    redis.slots[slot_key] = {"identityDigest": slot.identity_digest(), "recordKey": record_key}
    record = dict(redis.records[record_key], padding="x" * 100_000)
    redis.records[record_key] = record
    response = await ledger.commit(slot, lease_token="lease-" + "x" * 32, receipt=receipt)
    assert response.status is LedgerStatus.UNKNOWN
    assert response.error_code == "CORRUPT_RECORD"
    assert redis.records[record_key] == record


@_async_test
async def test_receipt_before_actual_claim_epoch_is_rejected_without_poisoning_claim():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock)
    slot, early_receipt = _receipt(clock, task_id="task-claim-epoch")
    clock.advance(1)
    claimed = await ledger.claim(slot)
    assert claimed.status is LedgerStatus.CLAIMED
    early = await ledger.commit(slot, lease_token=claimed.lease_token, receipt=early_receipt)
    assert early.status is LedgerStatus.REVISION_CONFLICT
    record_key = next(iter(redis.records))
    assert redis.records[record_key]["status"] == "CLAIMED"
    _, legal_receipt = _receipt(clock, task_id="task-claim-epoch")
    legal = await ledger.commit(slot, lease_token=claimed.lease_token, receipt=legal_receipt)
    assert legal.status is LedgerStatus.COMMITTED


@_async_test
async def test_fake_record_identity_and_key_conflicts_are_pre_cas_and_recoverable():
    clock = Clock(); redis = AtomicLedgerRedis(); ledger = StrategyReceiptLedger(redis, clock=clock)
    slot, receipt = _receipt(clock, task_id="task-fake-identity")
    claimed = await ledger.claim(slot)
    assert claimed.status is LedgerStatus.CLAIMED
    record_key = next(iter(redis.records))
    original = dict(redis.records[record_key])
    original_expiry = redis.expires[record_key]
    for mutation in (
        {"identityDigest": "b" * 64},
        {"recordKey": "strategy-receipt-ledger:v1:record:" + "f" * 64},
    ):
        redis.records[record_key] = dict(original, **mutation)
        mutated = dict(redis.records[record_key])
        response = await ledger.commit(slot, lease_token=claimed.lease_token, receipt=receipt)
        assert response.status is LedgerStatus.REVISION_CONFLICT
        assert redis.records[record_key] == mutated
        assert redis.expires[record_key] == original_expiry
    redis.records[record_key] = original
    assert (await ledger.commit(slot, lease_token=claimed.lease_token, receipt=receipt)).status is LedgerStatus.COMMITTED
