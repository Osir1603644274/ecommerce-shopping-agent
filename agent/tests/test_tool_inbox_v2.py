"""Real-Redis red tests for the durable V2 tool inbox."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
import unittest
from unittest.mock import AsyncMock

import redis.asyncio as redis

from agent.app.graph.tool_inbox_v2 import InboxStatus, ToolInbox, ToolInboxSlot, _keys, sha256
from agent.app.schemas import ToolTrace
from agent.app.tool_execution_v2 import ToolInboxCallerV2, ToolInboxExecutionRejected
from agent.app import task_state
from agent.app.executor import run_executor_step
from agent.app.task_state import TaskStateCreateRequest, TaskStatePatchRequest, create_task_state, update_task_state
from tests.fake_redis import FakeRedis
from tests.test_executor import _OS_REQ, SingleStepProductOutputPersistenceTests


class ToolInboxV2RedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()
        executable = shutil.which("redis-server")
        if executable is None:
            self.skipTest("redis-server is required for Inbox V2 integration tests")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.process = subprocess.Popen(
            [executable, "--port", str(self.port), "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.client = redis.Redis(host="127.0.0.1", port=self.port)
        for _ in range(40):
            try:
                await self.client.ping()
                return
            except Exception:  # noqa: BLE001 - process start polling
                await asyncio.sleep(0.05)
        self.fail("owned Redis did not start")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)

    @staticmethod
    def _slot() -> ToolInboxSlot:
        return ToolInboxSlot.create(
            task_id="task", plan_id="plan", step_id="step", state_revision=1,
            tool_name="search_products", canonical_args_sha256=sha256({"q": "x"}),
        )

    async def test_concurrent_claim_allows_one_business_call(self) -> None:
        count = 0

        async def caller(name: str, args: dict[str, object], _context: object) -> ToolTrace:
            nonlocal count
            count += 1
            await asyncio.sleep(0.15)
            return ToolTrace(tool=name, ok=True, durationMs=1, detail={"items": []})

        boundary = ToolInboxCallerV2(
            inbox=ToolInbox(self.client, lease_ms=1_000, ttl_ms=5_000), caller=caller,
        )
        kwargs = dict(
            slot=self._slot(), run_id="run", thread_id="thread",
            session_owner_hash="a" * 16, tool_name="search_products", arguments={"q": "x"},
        )
        first, second = await asyncio.gather(
            boundary.execute(**kwargs), boundary.execute(**kwargs), return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(v, Exception) for v in (first, second)), 1)
        self.assertEqual(sum(isinstance(v, ToolInboxExecutionRejected) for v in (first, second)), 1)
        self.assertEqual(count, 1)

    async def test_slot_ttl_rebuild_never_reuses_or_accepts_old_fence(self) -> None:
        inbox = ToolInbox(self.client, lease_ms=100, ttl_ms=100)
        slot = self._slot()
        old = await inbox.claim(slot, run_id="run", thread_id="thread")
        self.assertEqual(old.status, InboxStatus.CLAIMED)
        await asyncio.sleep(0.14)
        rebuilt = await inbox.claim(slot, run_id="run", thread_id="thread")
        self.assertEqual(rebuilt.status, InboxStatus.CLAIMED)
        self.assertGreater(rebuilt.fence, old.fence)
        stale = await inbox.enter_in_flight(
            slot, execution_id=old.execution_id, fence=old.fence,
        )
        self.assertEqual(stale.status, InboxStatus.FENCED_OUT)

    async def test_expired_claim_reissues_fence_but_expired_inflight_is_unknown(self) -> None:
        inbox = ToolInbox(self.client, lease_ms=100, ttl_ms=5_000)
        slot = self._slot()
        old = await inbox.claim(slot, run_id="run", thread_id="thread")
        await asyncio.sleep(0.14)
        reclaimed = await inbox.claim(slot, run_id="run", thread_id="thread")
        self.assertEqual(reclaimed.status, InboxStatus.CLAIMED)
        self.assertGreater(reclaimed.fence, old.fence)
        entered = await inbox.enter_in_flight(
            slot, execution_id=reclaimed.execution_id, fence=reclaimed.fence,
        )
        self.assertEqual(entered.status, InboxStatus.IN_FLIGHT)
        await asyncio.sleep(0.14)
        unknown = await inbox.claim(slot, run_id="run", thread_id="thread")
        self.assertEqual(unknown.status, InboxStatus.UNKNOWN)

    async def test_complete_renews_both_slot_keys_before_returning_terminal_result(self) -> None:
        """A terminal receipt must outlive neither half of its slot identity."""
        inbox = ToolInbox(self.client, lease_ms=100, ttl_ms=2_000)
        slot = self._slot()
        claimed = await inbox.claim(slot, run_id="run", thread_id="thread")
        self.assertEqual(claimed.status, InboxStatus.CLAIMED)
        entered = await inbox.enter_in_flight(
            slot, execution_id=claimed.execution_id, fence=claimed.fence,
        )
        self.assertEqual(entered.status, InboxStatus.IN_FLIGHT)
        index_key, record_key, _ = _keys(slot)
        # Force the index close to expiry while the paired record still has
        # normal retention, exactly the window completion must close.
        # Leave enough headroom for a loaded Windows event loop to enter the
        # atomic Lua completion before the deliberately shortened TTL elapses.
        self.assertTrue(await self.client.pexpire(index_key, 500))
        trace = ToolTrace(tool="search_products", ok=True, durationMs=1, detail={"items": []})
        receipt = {
            "taskId": "task", "planId": "plan", "stepId": "step",
            "toolName": "search_products", "stateRevision": 1,
            "inputHash": slot.canonical_args_sha256,
            "resultHash": sha256(trace.model_dump(by_alias=True, mode="json")),
            "executionId": claimed.execution_id, "logicalSlotKey": slot.logical_slot_key(),
            "fence": claimed.fence, "inboxStatus": "SUCCEEDED",
            "toolOutcome": "tool_succeeded",
        }
        self.assertEqual(
            (await inbox.complete(
                slot, execution_id=claimed.execution_id, fence=claimed.fence,
                trace=trace, receipt=receipt,
            )).status,
            InboxStatus.SUCCEEDED,
        )

        # This crosses the original claim TTL.  The old implementation left
        # index expired while the refreshed SUCCEEDED record still existed.
        self.assertGreater(await self.client.pttl(index_key), 1_000)
        self.assertGreater(await self.client.pttl(record_key), 1_000)
        await asyncio.sleep(0.6)
        self.assertIsNotNone(await self.client.get(index_key))
        self.assertIsNotNone(await self.client.get(record_key))
        replay = await inbox.claim(slot, run_id="other-run", thread_id="other-thread")
        self.assertEqual(replay.status, InboxStatus.SUCCEEDED)
        self.assertEqual(replay.execution_id, claimed.execution_id)

    async def test_single_key_drift_fails_closed_for_inspect_and_claim(self) -> None:
        inbox = ToolInbox(self.client, lease_ms=100, ttl_ms=1_000)
        slot = self._slot()
        claimed = await inbox.claim(slot, run_id="run", thread_id="thread")
        index_key, _record_key, _ = _keys(slot)
        await self.client.delete(index_key)
        self.assertEqual((await inbox.inspect(slot)).status, InboxStatus.CONFLICT)
        self.assertEqual(
            (await inbox.claim(slot, run_id="other-run", thread_id="other-thread")).status,
            InboxStatus.UNKNOWN,
        )

    async def test_index_identity_drift_fails_closed_for_inspect_and_claim(self) -> None:
        inbox = ToolInbox(self.client, lease_ms=100, ttl_ms=1_000)
        slot = self._slot()
        self.assertEqual(
            (await inbox.claim(slot, run_id="run", thread_id="thread")).status,
            InboxStatus.CLAIMED,
        )
        index_key, _record_key, _ = _keys(slot)
        index = json.loads(await self.client.get(index_key))
        index["inputHash"] = "0" * 64
        await self.client.set(index_key, json.dumps(index), px=1_000)
        self.assertEqual((await inbox.inspect(slot)).status, InboxStatus.CONFLICT)
        self.assertEqual(
            (await inbox.claim(slot, run_id="run", thread_id="thread")).status,
            InboxStatus.CONFLICT,
        )

    async def test_missing_index_cannot_overwrite_a_surviving_success_record(self) -> None:
        async def caller(name: str, _args: dict[str, object], _context: object) -> ToolTrace:
            return ToolTrace(tool=name, ok=True, durationMs=1, detail={"items": []})

        inbox = ToolInbox(self.client, lease_ms=1_000, ttl_ms=5_000)
        boundary = ToolInboxCallerV2(inbox=inbox, caller=caller)
        slot = self._slot()
        completed = await boundary.execute(
            slot=slot, run_id="run", thread_id="thread", session_owner_hash="a" * 16,
            tool_name="search_products", arguments={"q": "x"},
        )
        index_key, record_key, _ = _keys(slot)
        raw_before = await self.client.get(record_key)
        await self.client.delete(index_key)
        rejected = await inbox.claim(slot, run_id="other-run", thread_id="other-thread")
        self.assertEqual(rejected.status, InboxStatus.UNKNOWN)
        self.assertEqual(await self.client.get(record_key), raw_before)
        self.assertIn(completed.context.execution_id, raw_before.decode("utf-8"))

    async def test_failed_tool_is_terminal_execution_not_business_success(self) -> None:
        calls = 0

        async def caller(name: str, _args: dict[str, object], _context: object) -> ToolTrace:
            nonlocal calls
            calls += 1
            return ToolTrace(tool=name, ok=False, durationMs=1, detail={"error": "upstream"})

        boundary = ToolInboxCallerV2(
            inbox=ToolInbox(self.client, lease_ms=1_000, ttl_ms=5_000), caller=caller,
        )
        kwargs = dict(
            slot=self._slot(), run_id="run", thread_id="thread",
            session_owner_hash="a" * 16, tool_name="search_products", arguments={"q": "x"},
        )
        first = await boundary.execute(**kwargs)
        replay = await boundary.execute(**kwargs)
        self.assertFalse(first.trace.ok)
        self.assertEqual(first.receipt["toolOutcome"], "tool_failed")
        self.assertEqual(replay.receipt, first.receipt)
        self.assertTrue(replay.replayed)
        self.assertEqual(calls, 1)

    async def test_tampered_frozen_record_never_replays(self) -> None:
        async def caller(name: str, _args: dict[str, object], _context: object) -> ToolTrace:
            return ToolTrace(tool=name, ok=True, durationMs=1, detail={"items": []})

        inbox = ToolInbox(self.client, lease_ms=1_000, ttl_ms=5_000)
        boundary = ToolInboxCallerV2(inbox=inbox, caller=caller)
        slot = self._slot()
        await boundary.execute(
            slot=slot, run_id="run", thread_id="thread", session_owner_hash="a" * 16,
            tool_name="search_products", arguments={"q": "x"},
        )
        _, record_key, _ = _keys(slot)
        raw = await self.client.get(record_key)
        record = json.loads(raw)
        record["resultHash"] = "0" * 64
        await self.client.set(record_key, json.dumps(record))
        replay = await inbox.claim(slot, run_id="run", thread_id="thread")
        self.assertIn(replay.status, {InboxStatus.CONFLICT, InboxStatus.UNAVAILABLE})

    async def test_terminal_receipt_without_tool_outcome_never_replays(self) -> None:
        async def caller(name: str, _args: dict[str, object], _context: object) -> ToolTrace:
            return ToolTrace(tool=name, ok=True, durationMs=1, detail={"items": []})

        inbox = ToolInbox(self.client, lease_ms=1_000, ttl_ms=5_000)
        boundary = ToolInboxCallerV2(inbox=inbox, caller=caller)
        slot = self._slot()
        await boundary.execute(
            slot=slot, run_id="run", thread_id="thread", session_owner_hash="a" * 16,
            tool_name="search_products", arguments={"q": "x"},
        )
        _index_key, record_key, _ = _keys(slot)
        record = json.loads(await self.client.get(record_key))
        receipt = json.loads(record["receipt"])
        receipt.pop("toolOutcome")
        record["receipt"] = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        record["receiptHash"] = sha256(receipt)
        await self.client.set(record_key, json.dumps(record), px=5_000)
        replay = await inbox.claim(slot, run_id="run", thread_id="thread")
        self.assertIn(replay.status, {InboxStatus.CONFLICT, InboxStatus.UNAVAILABLE})

    async def test_durable_executor_uses_ledger_and_not_taskstate_inbox(self) -> None:
        created = await create_task_state(TaskStateCreateRequest(
            goal="想找 iOS 二手机。", task_type="ecommerce_guide",
            domain_state={"shoppingGuide": {"mode": "recommend", "category": "phone", "useCases": [], "requirements": _OS_REQ, "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing"}},
        ))
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(expectedRevision=created.revision, actor="agent", status="ready"),
        )
        planned = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision, actor="agent",
                activePlan={
                    "planId": "plan-products", "basedOnRevision": ready.revision,
                    "status": "active", "steps": [{
                        "stepId": "step-products", "description": "搜索手机",
                        "toolName": "search_products",
                        "arguments": {"query": ready.goal, "category": "手机", "requirements": _OS_REQ},
                        "argumentSources": {"query": {"kind": "task_goal"}, "category": {"kind": "shopping_guide", "reference": "category"}, "requirements": {"kind": "shopping_guide", "reference": "requirements"}},
                        "expectedOutput": {"requiresProductCandidates": True}, "status": "pending",
                    }],
                },
            ),
        )
        contexts = []

        async def caller(name: str, _args: dict[str, object], context: object) -> ToolTrace:
            contexts.append(context)
            return ToolTrace(tool=name, ok=True, durationMs=1, detail=SingleStepProductOutputPersistenceTests._search_detail())

        boundary = ToolInboxCallerV2(
            inbox=ToolInbox(self.client, lease_ms=1_000, ttl_ms=5_000), caller=caller,
        )
        schema = {
            "type": "function",
            "function": {
                "name": "search_products",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": "string"}, "requirements": {"type": "array"}}, "required": ["query", "category", "requirements"]},
            },
        }
        result = await run_executor_step(
            planned, [schema], tool_caller=AsyncMock(), durable_tool_boundary=boundary,
            durable_run_id="run", durable_thread_id="thread", durable_session_owner_hash="a" * 16,
        )
        self.assertEqual(result.outcome, "step_executed")
        self.assertEqual(len(contexts), 1)
        self.assertIsNotNone(result.durable_tool_receipt)
        self.assertNotIn("executorToolInbox", result.task_state.domain_state)
        receipt = result.durable_tool_receipt
        slot = ToolInboxSlot.create(
            task_id=receipt["taskId"], plan_id=receipt["planId"], step_id=receipt["stepId"],
            state_revision=receipt["stateRevision"], tool_name=receipt["toolName"],
            canonical_args_sha256=receipt["inputHash"],
        )
        stored = await boundary.inbox.inspect(slot)
        self.assertEqual(stored.status, InboxStatus.SUCCEEDED)
        self.assertEqual(stored.receipt, receipt)
