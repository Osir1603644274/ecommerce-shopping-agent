import asyncio
import hashlib
import json
import os
import uuid

import pytest

from app.domains.ecommerce.strategy_routing import serialize_receipt
from app.graph.strategy_receipt_ledger import LedgerStatus, StrategyReceiptLedger, _key
from tests.test_strategy_receipt_ledger import Clock, _foreign_receipt, _receipt, _slot


def _redis_test(function):
    def wrapped():
        return asyncio.run(function())
    return wrapped


@_redis_test
async def test_real_redis_lua_claim_commit_replay_and_expired_commit():
    url = os.getenv("STRATEGY_LEDGER_REDIS_URL")
    if not url:
        pytest.skip("set STRATEGY_LEDGER_REDIS_URL to run real Redis integration")
    redis = pytest.importorskip("redis.asyncio").from_url(url, decode_responses=False)
    try:
        clock = Clock()
        first = StrategyReceiptLedger(redis, clock=clock)
        second = StrategyReceiptLedger(redis, clock=clock)
        run_suffix = uuid.uuid4().hex
        slot, receipt = _receipt(clock, task_id=f"task-real-{run_suffix}")
        claimed, other = await asyncio.gather(first.claim(slot), second.claim(slot))
        assert {claimed.status, other.status} == {LedgerStatus.CLAIMED, LedgerStatus.IN_PROGRESS}
        winner = claimed if claimed.status is LedgerStatus.CLAIMED else other
        assert (await first.commit(slot, lease_token=winner.lease_token, receipt=receipt)).status is LedgerStatus.COMMITTED
        assert (await second.claim(slot)).status is LedgerStatus.REPLAY
        record_key = _key(slot)[1]
        record = json.loads(await redis.get(record_key))
        foreign = _foreign_receipt(clock)
        foreign_snapshot = serialize_receipt(foreign).decode("utf-8")
        record["receiptSnapshot"] = foreign_snapshot
        record["receiptDigest"] = hashlib.sha256(foreign_snapshot.encode()).hexdigest()
        await redis.set(record_key, json.dumps(record, separators=(",", ":")))
        corrupt_replay = await second.claim(slot)
        assert corrupt_replay.status is LedgerStatus.UNKNOWN
        assert corrupt_replay.error_code == "CORRUPT_RECORD"
        record["padding"] = "x" * 100_000
        await redis.set(record_key, json.dumps(record, separators=(",", ":")))
        oversized_replay = await second.claim(slot)
        assert oversized_replay.status is LedgerStatus.UNKNOWN
        assert oversized_replay.error_code == "CORRUPT_RECORD"

        slot_attack_clock = Clock()
        slot_attack_ledger = StrategyReceiptLedger(redis, clock=slot_attack_clock)
        slot_attack, slot_attack_receipt = _receipt(slot_attack_clock, task_id=f"task-real-slot-{uuid.uuid4().hex}")
        assert (await slot_attack_ledger.claim(slot_attack)).status is LedgerStatus.CLAIMED
        slot_key, slot_record_key = _key(slot_attack)
        oversized_slot = json.dumps({"padding": "x" * 100_000}, separators=(",", ":"))
        await redis.set(slot_key, oversized_slot, px=5000)
        slot_before = await redis.get(slot_key)
        slot_pttl_before = await redis.pttl(slot_key)
        slot_response = await slot_attack_ledger.claim(slot_attack)
        assert slot_response.status is LedgerStatus.UNKNOWN
        assert slot_response.error_code == "CORRUPT_RECORD"
        assert await redis.get(slot_key) == slot_before
        assert await redis.pttl(slot_key) <= slot_pttl_before

        record_attack_clock = Clock()
        record_attack_ledger = StrategyReceiptLedger(redis, clock=record_attack_clock)
        record_attack, record_attack_receipt = _receipt(record_attack_clock, task_id=f"task-real-record-{uuid.uuid4().hex}")
        claimed_record_attack = await record_attack_ledger.claim(record_attack)
        assert claimed_record_attack.status is LedgerStatus.CLAIMED
        record_key = _key(record_attack)[1]
        record_before = json.loads(await redis.get(record_key))
        record_before["padding"] = "x" * 100_000
        oversized_record = json.dumps(record_before, separators=(",", ":"))
        await redis.set(record_key, oversized_record, px=5000)
        record_bytes_before = await redis.get(record_key)
        record_pttl_before = await redis.pttl(record_key)
        record_response = await record_attack_ledger.commit(record_attack, lease_token=claimed_record_attack.lease_token, receipt=record_attack_receipt)
        assert record_response.status is LedgerStatus.UNKNOWN
        assert record_response.error_code == "CORRUPT_RECORD"
        assert await redis.get(record_key) == record_bytes_before
        assert await redis.pttl(record_key) <= record_pttl_before

        conflict_clock = Clock()
        conflict_ledger = StrategyReceiptLedger(redis, clock=conflict_clock)
        conflict_slot, conflict_receipt = _receipt(conflict_clock, task_id=f"task-real-conflict-{uuid.uuid4().hex}")
        conflict_claimed = await conflict_ledger.claim(conflict_slot)
        assert conflict_claimed.status is LedgerStatus.CLAIMED
        conflict_key = _key(conflict_slot)[1]
        conflict_original = json.loads(await redis.get(conflict_key))
        for mutation in (
            {"identityDigest": "b" * 64},
            {"recordKey": "strategy-receipt-ledger:v1:record:" + "f" * 64},
        ):
            conflict_mutated = dict(conflict_original, **mutation)
            conflict_payload = json.dumps(conflict_mutated, separators=(",", ":"))
            await redis.set(conflict_key, conflict_payload, px=5000)
            conflict_before = await redis.get(conflict_key)
            conflict_pttl_before = await redis.pttl(conflict_key)
            conflict_response = await conflict_ledger.commit(conflict_slot, lease_token=conflict_claimed.lease_token, receipt=conflict_receipt)
            assert conflict_response.status is LedgerStatus.REVISION_CONFLICT
            assert await redis.get(conflict_key) == conflict_before
            assert await redis.pttl(conflict_key) <= conflict_pttl_before
        await redis.set(conflict_key, json.dumps(conflict_original, separators=(",", ":")), px=5000)
        assert (await conflict_ledger.commit(conflict_slot, lease_token=conflict_claimed.lease_token, receipt=conflict_receipt)).status is LedgerStatus.COMMITTED

        epoch_clock = Clock()
        epoch_ledger = StrategyReceiptLedger(redis, clock=epoch_clock)
        epoch_slot, early_receipt = _receipt(epoch_clock, task_id=f"task-real-epoch-{uuid.uuid4().hex}")
        epoch_clock.advance(1)
        epoch_claimed = await epoch_ledger.claim(epoch_slot)
        assert epoch_claimed.status is LedgerStatus.CLAIMED
        epoch_key = _key(epoch_slot)[1]
        epoch_before = await redis.get(epoch_key)
        early_commit = await epoch_ledger.commit(epoch_slot, lease_token=epoch_claimed.lease_token, receipt=early_receipt)
        assert early_commit.status is LedgerStatus.REVISION_CONFLICT
        epoch_record = await redis.get(epoch_key)
        assert epoch_record == epoch_before
        assert json.loads(epoch_record)["status"] == "CLAIMED"
        _, legal_receipt = _receipt(epoch_clock, task_id=epoch_slot.task_id)
        assert (await epoch_ledger.commit(epoch_slot, lease_token=epoch_claimed.lease_token, receipt=legal_receipt)).status is LedgerStatus.COMMITTED

        expired_clock = Clock()
        expired_ledger = StrategyReceiptLedger(redis, clock=expired_clock, lease_seconds=1, max_recovery_count=0)
        expired_slot, expired_receipt = _receipt(
            expired_clock, task_id=f"task-real-expired-{uuid.uuid4().hex}"
        )
        claimed_expired = await expired_ledger.claim(expired_slot)
        assert claimed_expired.status is LedgerStatus.CLAIMED
        expired_clock.advance(2)
        assert (await expired_ledger.commit(expired_slot, lease_token=claimed_expired.lease_token, receipt=expired_receipt)).status is LedgerStatus.REVISION_CONFLICT

        ttl_clock = Clock()
        ttl_ledger = StrategyReceiptLedger(redis, clock=ttl_clock, ttl_seconds=1)
        ttl_slot, ttl_receipt = _receipt(ttl_clock, task_id=f"task-real-ttl-{uuid.uuid4().hex}")
        assert (await ttl_ledger.claim(ttl_slot)).status is LedgerStatus.CLAIMED
        await asyncio.sleep(1.2)
        assert (await ttl_ledger.claim(ttl_slot)).status is LedgerStatus.CLAIMED
    finally:
        await redis.aclose()
