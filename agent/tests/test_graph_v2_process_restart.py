"""Durable V2 process-restart recovery tests (DAY2 — requirement #6/#8/#9).

Exercises the dangerous-window fault (search executed + ``v2ExecReceipt``
persisted + next graph checkpoint NOT confirmed) and process-restart recovery
against a test-owned real Redis instance and the REAL production phase
functions through the compiled durable graph and ToolInbox boundary:

* in-process fault hook: after ``run_executor_step`` returned ``step_executed``
  AND the same-run receipt was persisted, ``ExecutorFaultInjected`` terminates
  the invocation (the E2E subprocess instead ``os._exit``s at the exact window)
* a restart of the SAME thread rehydrates the live TaskState, reconciles
  ``fast_forward`` through the same-run ``v2RunMarker``/``v2ExecReceipt``, and
  the executor's skip guard routes to the Validator with ZERO extra tools — the
  live tool count stays exactly 1 across the whole kill+restart lifecycle
* a restart of a PARKED (clarification) thread re-parks on the same thread with
  no tool/model work
* a restart before the FIRST checkpoint replays the deterministic initial input
  idempotently (the entry node routes from the LIVE TaskState)
* a restart of a COMPLETED thread is a no-op (idempotent replay).
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import socket
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app import task_state
from app.agent_trace import TraceBuilder
from app.graph.checkpoint import GraphV2CheckpointSaver
from app.graph.nodes.executor import ExecutorFaultInjected, set_executor_fault_hook
from app.graph.resume import (
    build_thread_id,
    resolve_durable_identity,
    run_graph_v2_durable,
    session_owner_hash,
    write_task_cursor,
)
from app.graph.tool_inbox_v2 import ToolInbox
from app.schemas import ToolTrace
from app.settings import settings
from app.tool_execution_v2 import ToolExecutionContext
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    get_task_state,
    update_task_state,
)
from app.tools import TOOL_SCHEMAS
from tests.two_stage_ranking_fixtures import two_stage_search_detail

# ── shared fixtures ───────────────────────────────────────────────────────────


_OS_HARD_REQ = {
    "key": "os", "operator": "eq", "value": "ios",
    "unit": "enum", "priority": "hard", "source": "user",
}


def _fake_client(create_mock: AsyncMock):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_mock),
        )
    )


def _search_products_schema() -> dict:
    for spec in TOOL_SCHEMAS:
        if spec["function"]["name"] == "search_products":
            return spec
    raise AssertionError("TOOL_SCHEMAS must ship search_products")


def _get_product_details_schema() -> dict:
    for spec in TOOL_SCHEMAS:
        if spec["function"]["name"] == "get_product_details":
            return spec
    raise AssertionError("TOOL_SCHEMAS must ship get_product_details")


def _fake_tool(name: str, arguments: dict):
    if name == "search_products":
        return ToolTrace(
            tool=name, ok=True, detail=two_stage_search_detail([101, 102])
        )
    if name == "get_product_details":
        return ToolTrace(
            tool=name, ok=True,
            detail={"productIds": [101, 102], "products": [{"id": 101}, {"id": 102}]},
        )
    raise AssertionError(f"unexpected tool dispatch: {name}")


def _used_phone_create_request() -> TaskStateCreateRequest:
    return TaskStateCreateRequest(
        goal="想找 iOS 二手机。",
        task_type="ecommerce_guide",
        session_id="session-a",
        domain_state={
            "shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "useCases": [],
                "requirements": [_OS_HARD_REQ],
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            }
        },
    )


# ── tests ────────────────────────────────────────────────────────────────────


class GraphV2ProcessRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        executable = shutil.which("redis-server")
        if executable is None:
            self.skipTest("redis-server is required for production ToolInbox tests")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self._redis_port = probe.getsockname()[1]
        self._redis_process = subprocess.Popen(
            [executable, "--port", str(self._redis_port), "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import redis.asyncio as redis_async

        self._redis = redis_async.Redis(
            host="127.0.0.1", port=self._redis_port, decode_responses=True
        )
        for _ in range(40):
            try:
                await self._redis.ping()
                break
            except Exception:  # noqa: BLE001 - owned Redis startup polling
                await asyncio.sleep(0.05)
        else:
            self.fail("owned Redis did not start")
        task_state._client = self._redis
        task_state._task_locks.clear()
        task_state._session_locks.clear()
        self._inbox = ToolInbox(self._redis, lease_ms=1_000, ttl_ms=5_000)
        self._business_ledger: list[dict[str, object]] = []

    async def asyncTearDown(self) -> None:
        task_state._client = None
        await self._redis.aclose()
        self._redis_process.terminate()
        try:
            self._redis_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._redis_process.kill()
            self._redis_process.wait(timeout=3)

    def _saver(self) -> GraphV2CheckpointSaver:
        return GraphV2CheckpointSaver(serde=JsonPlusSerializer())

    async def _ready_used_phone_state(self) -> TaskState:
        created = await create_task_state(_used_phone_create_request())
        return await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )

    def _durable_kwargs(
        self,
        task_id: str,
        trace: TraceBuilder,
        *,
        resume=None,
        restart=False,
        run_id=None,
        thread_id=None,
        session_id="session-a",
    ) -> dict:
        async def legacy_tool_caller(_name: str, _arguments: dict) -> ToolTrace:
            raise AssertionError("durable Graph V2 must not call the legacy tool caller")

        async def tool_caller_v2(
            name: str, arguments: dict, context: ToolExecutionContext,
        ) -> ToolTrace:
            self._business_ledger.append(
                {
                    "tool": name,
                    "runId": context.run_id,
                    "threadId": context.thread_id,
                    "executionId": context.execution_id,
                    "fence": context.fence,
                }
            )
            return _fake_tool(name, arguments)

        return dict(
            task_id=task_id,
            session_id=session_id,
            resume=resume,
            restart=restart,
            run_id=run_id,
            thread_id=thread_id,
            user_message="想找 iOS 二手机。",
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda s: [
                _search_products_schema(), _get_product_details_schema(),
            ],
            tool_caller=legacy_tool_caller,
            tool_caller_v2=tool_caller_v2,
            tool_inbox=self._inbox,
            trace_builder=trace,
            max_transitions=8,
        )

    def _search_call_count(self) -> int:
        return len(
            [entry for entry in self._business_ledger if entry["tool"] == "search_products"]
        )

    async def test_restart_cursor_requires_exact_task_and_run_thread_identity(self):
        state = await self._ready_used_phone_state()
        await write_task_cursor(
            state.task_id,
            run_id="run-cursor",
            thread_id=build_thread_id("other-task", "run-cursor"),
            revision=state.revision,
            session_owner_hash_value=session_owner_hash("session-a"),
        )
        self.assertEqual(
            await resolve_durable_identity(
                state.task_id, restart=True, session_id="session-a"
            ),
            (None, None),
        )

    # ── requirement #8: dangerous-window kill + restart, live tools stay 1 ───

    async def test_restart_after_dangerous_window_keeps_live_tool_count_one(self):
        def fault(step_id: str):
            raise ExecutorFaultInjected(step_id)

        previous_fault = settings.agent_graph_v2_fault_point
        set_executor_fault_hook(fault)
        settings.agent_graph_v2_fault_point = "after_executor_receipt"
        try:
            state = await self._ready_used_phone_state()
            fresh = await run_graph_v2_durable(
                **self._durable_kwargs(
                    state.task_id,
                    TraceBuilder("run-fault", mode="context_pack"),
                ),
                checkpointer=self._saver(),
            )
            self.assertEqual(fresh.boundary, "fault_injected")
            self.assertEqual(fresh.terminal_outcome, "executor_fault_injected")
            # The live tool ran exactly once, and the SAME-RUN receipt is durable.
            self.assertEqual(self._search_call_count(), 1)
            live = await get_task_state(state.task_id)
            receipt = (live.domain_state or {}).get("v2ExecReceipt")
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["runId"], fresh.run_id)
            self.assertEqual(receipt["stepId"], "step-shopping-action")
            self.assertEqual(
                self._business_ledger,
                [{
                    "tool": "search_products",
                    "runId": fresh.run_id,
                    "threadId": fresh.thread_id,
                    "executionId": receipt["executionId"],
                    "fence": receipt["fence"],
                }],
            )
            # Dangerous window: the receipt revision is AHEAD of the latest
            # confirmed checkpoint (which still shows the planner revision).
            checkpoint_hash = await self._saver().alatest_checkpoint_hash(
                fresh.thread_id
            )
            self.assertIsNotNone(checkpoint_hash)

            # Restart the SAME thread: skip guard routes to the Validator with
            # zero additional tools — live tool count stays exactly 1.
            restarted = await run_graph_v2_durable(
                **self._durable_kwargs(
                    state.task_id,
                    TraceBuilder("run-restart", mode="context_pack"),
                    restart=True,
                ),
                checkpointer=self._saver(),
            )
            self.assertEqual(restarted.boundary, "task_completed")
            self.assertEqual(restarted.mode, "restart")
            self.assertEqual(restarted.thread_id, fresh.thread_id)
            self.assertEqual(restarted.run_id, fresh.run_id)
            self.assertEqual(self._search_call_count(), 1)
            # The restart reconciled fast_forward and skipped the executed step.
            skipped = [
                e for e in restarted.graph_state.get("node_events", [])
                if e.get("phase") == "start" and e.get("nodeName") == "executor"
            ]
            self.assertEqual(len(skipped), 1)
        finally:
            settings.agent_graph_v2_fault_point = previous_fault
            set_executor_fault_hook(None)

    # ── restart a PARKED clarification thread: re-parks, no work ─────────────

    async def test_restart_of_parked_clarification_reparks_no_tool(self):
        state = await self._ready_used_phone_state()
        # Turn the ready task into a parked clarification (pending question).
        waiting = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="user",
                status="collecting_information",
                pending_questions=["你希望手机的存储容量是多大？"],
            ),
        )
        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                waiting.task_id,
                TraceBuilder("run-park", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(fresh.boundary, "clarification")

        restarted = await run_graph_v2_durable(
            **self._durable_kwargs(
                waiting.task_id,
                TraceBuilder("run-park-restart", mode="context_pack"),
                restart=True,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(restarted.boundary, "clarification")
        self.assertEqual(restarted.thread_id, fresh.thread_id)
        self.assertEqual(restarted.question, fresh.question)
        self.assertEqual(self._business_ledger, [])

    # ── restart before the first checkpoint replays deterministic input ──────

    async def test_restart_before_first_checkpoint_replays_deterministic_input(self):
        """The process died between the cursor write and the first checkpoint:
        no snapshot exists, so the deterministic initial input re-runs and the
        entry node routes from the LIVE TaskState."""
        state = await self._ready_used_phone_state()
        thread_id = f"v2-task:{state.task_id}:run-boot"
        # Production writes the run marker before the cursor, so a legitimate
        # pre-checkpoint crash must carry both exact identities.
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                domainStatePatch={
                    "v2RunMarker": {
                        "runId": "run-boot",
                        "threadId": thread_id,
                        "sessionOwnerHash": session_owner_hash("session-a"),
                        "controlPolicy": "fixed_v1",
                        "policyRevision": "fixed-v1",
                        "stateRevision": state.revision + 1,
                        "planId": None,
                        "payloadSha256": hashlib.sha256(
                            "想找 iOS 二手机。".encode("utf-8")
                        ).hexdigest(),
                    },
                    "v2UserMessage": "想找 iOS 二手机。",
                },
            ),
        )
        await write_task_cursor(
            state.task_id,
            run_id="run-boot",
            thread_id=thread_id,
            revision=state.revision,
            session_owner_hash_value=session_owner_hash("session-a"),
        )
        restarted = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                TraceBuilder("run-boot", mode="context_pack"),
                restart=True,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(restarted.boundary, "task_completed")
        self.assertEqual(restarted.mode, "restart")
        self.assertEqual(self._search_call_count(), 1)
        sequence = [
            e["nodeName"]
            for e in restarted.graph_state["node_events"]
            if e["phase"] == "start"
        ]
        self.assertEqual(sequence, ["entry", "planner", "executor", "validator"])

    async def test_cross_session_restart_rejects_before_checkpoint_or_tool(self):
        state = await self._ready_used_phone_state()
        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                TraceBuilder("run-owner-fresh", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        before = (await get_task_state(state.task_id)).revision
        rejected = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                TraceBuilder("run-cross-session-restart", mode="context_pack"),
                restart=True, session_id="session-b",
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(rejected.boundary, "resume_rejected")
        self.assertEqual(rejected.rejected_reason, "cross_session")
        self.assertEqual(rejected.graph_state, {})
        self.assertIsNone(rejected.task_state)
        self.assertEqual((await get_task_state(state.task_id)).revision, before)
        self.assertEqual(self._search_call_count(), 1)

    # ── restart a completed thread is an idempotent no-op ────────────────────

    async def test_restart_of_completed_thread_is_idempotent_noop(self):
        state = await self._ready_used_phone_state()
        first = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                TraceBuilder("run-done", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(first.boundary, "task_completed")
        before_revision = (await get_task_state(state.task_id)).revision

        again = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                TraceBuilder("run-done-restart", mode="context_pack"),
                restart=True,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(again.boundary, "task_completed")
        self.assertEqual(again.mode, "restart")
        self.assertEqual(again.thread_id, first.thread_id)
        # No new live search, no new revision — the replay is a no-op.
        self.assertEqual(self._search_call_count(), 1)
        self.assertEqual((await get_task_state(state.task_id)).revision, before_revision)


if __name__ == "__main__":
    unittest.main()
