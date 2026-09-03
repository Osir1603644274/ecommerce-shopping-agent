"""Durable V2 interrupt()/resume() tests (DAY2 — requirement #4/#5/#7).

Exercises the real ``interrupt()`` boundary and ``Command(resume=...)`` control
plane against the self-contained ``InFileRedis`` fake (no process-global store,
no ``tests.fake_redis`` import — not in the DAY2 allowlist) and the REAL
production phase functions through the compiled durable graph:

* the durable graph exposes an enumerable ``clarification`` node/edge, and the
  durable routers send ``ask_user`` into it (never a silent END)
* a fresh run on a task with a pending question parks at a REAL interrupt and
  the park is a persisted checkpoint (count >= 1, hash present, rebuildable by
  a fresh saver instance)
* the same-thread ``Command(resume=answer)`` resolves the park, re-enters the
  clarification node, and continues entry → clarification → planner → executor
  → validator with EXACTLY ONE live search dispatch across the whole lifecycle
* empty / too-long / missing-revision / cross-task / malformed-thread /
  revision-mismatched / proposal-hash-mismatched resume payloads fail closed
  with no graph invocation and no tool/model side effects
* a replay of the EXACT already-applied answer is idempotent (no new revision,
  no new checkpoint, no new tool call — requirement #5)
* a blank re-ask on a parked clarification parks AGAIN on the same node
  re-execution carrying ``reAskReason`` — the graph never guesses the user's
  intent.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command

from app import task_state
from app.agent_trace import TraceBuilder
from app.graph.builder import build_graph_v2_durable
from app.graph.checkpoint import GraphV2CheckpointSaver
from app.graph.resume import (
    _initial_graph_input,
    build_thread_id,
    run_graph_v2_durable,
)
from app.graph.pause_control import (
    begin_graph_pause_resume,
    public_pause_receipt,
    read_graph_pause,
    request_graph_pause,
)
from app.graph.routers import route_after_clarification, route_after_planner_durable
from app.graph.runtime import GraphV2Runtime
from app.graph.tool_inbox_v2 import InboxResponse, InboxStatus
from app.schemas import ToolTrace
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

# ── self-contained Redis fake (not FakeRedis: not in the DAY2 allowlist) ──────


class InFileRedis:
    """Minimal async plain-Redis fake with the full surface the durable stack
    touches: strings/lists/hashes/zsets + ``eval`` dispatching on ``numkeys``
    (TaskState CAS = 1 key; saver CAS = 3 keys).  Per-key TTL is recorded so
    tests can assert checkpoint keys carry the 7-day TTL."""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, int]] = {}
        self.ttls: dict[str, int] = {}

    # ── strings ──────────────────────────────────────────────────────────────
    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.strings.pop(key, None)

    async def set(self, key: str, value: str, **kwargs) -> bool:
        if (kwargs.get("nx") or kwargs.get("NX")) and key in self.strings:
            return False
        self.strings[key] = value
        ex = kwargs.get("ex") or kwargs.get("EX")
        if ex:
            self.ttls[key] = int(ex)
        return True

    # ── lists ────────────────────────────────────────────────────────────────
    @staticmethod
    def _slice(values: list[str], start: int, end: int) -> list[str]:
        length = len(values)
        start = max(length + start, 0) if start < 0 else start
        end = length + end if end < 0 else end
        if start >= length or start > end:
            return []
        return values[start : end + 1]

    async def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self._slice(self.lists.get(key, []), start, end)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.lists[key] = self._slice(self.lists.get(key, []), start, end)

    # ── hashes ───────────────────────────────────────────────────────────────
    async def hset(self, key: str, field: str, value: str) -> int:
        created = field not in self.hashes.setdefault(key, {})
        self.hashes[key][field] = value
        return int(created)

    async def hsetnx(self, key: str, field: str, value: str) -> int:
        fields = self.hashes.setdefault(key, {})
        if field in fields:
            return 0
        fields[field] = value
        return 1

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        fields = self.hashes.setdefault(key, {})
        current = int(fields.get(field, 0))
        fields[field] = str(current + amount)
        return current + amount

    # ── sorted sets ──────────────────────────────────────────────────────────
    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        values = self.sorted_sets.setdefault(key, {})
        new_members = sum(member not in values for member in mapping)
        values.update(mapping)
        return new_members

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        members = [
            member
            for member, _ in sorted(
                self.sorted_sets.get(key, {}).items(), key=lambda item: item[1]
            )
        ]
        return self._slice(members, start, end)

    async def zrem(self, key: str, *members: str) -> int:
        values = self.sorted_sets.get(key, {})
        removed = sum(values.pop(member, None) is not None for member in members)
        return removed

    # ── generic ──────────────────────────────────────────────────────────────
    async def expire(self, key: str, seconds: int) -> bool:
        exists = (
            key in self.strings
            or key in self.lists
            or key in self.hashes
            or key in self.sorted_sets
        )
        if exists:
            self.ttls[key] = int(seconds)
        return exists

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.strings.pop(key, None) is not None)
            deleted += int(self.lists.pop(key, None) is not None)
            deleted += int(self.hashes.pop(key, None) is not None)
            deleted += int(self.sorted_sets.pop(key, None) is not None)
            self.ttls.pop(key, None)
        return deleted

    async def scan_iter(self, match: str = "*", count: int = 10):
        import re

        pattern = re.compile("^" + match.replace("*", ".*") + "$")
        for key in (
            set(self.strings)
            | set(self.lists)
            | set(self.hashes)
            | set(self.sorted_sets)
        ):
            if pattern.match(key):
                yield key

    # ── Lua CAS dispatch (numkeys distinguishes TaskState vs saver) ───────────
    async def eval(self, script: str, numkeys: int, *args):
        if "GRAPH_V2_PUT_WRITES" in script and numkeys == 1:
            return self._put_writes(*args)
        if "PAUSE_REQUEST_CAS" in script and numkeys == 1 and len(args) == 8:
            key, run_id, thread_id, owner, policy, policy_revision, payload, ttl = args
            raw = self.strings.get(key)
            if raw is not None:
                current = json.loads(raw)
                matches = (
                    current.get("runId") == run_id
                    and current.get("threadId") == thread_id
                    and current.get("sessionOwnerHash") == owner
                    and current.get("controlPolicy") == policy
                    and current.get("policyRevision") == policy_revision
                )
                if matches:
                    return raw
                if current.get("state") == "pause_requested":
                    self.strings[key] = payload
                    self.ttls[key] = int(ttl)
                    return payload
                return ""
            self.strings[key] = payload
            self.ttls[key] = int(ttl)
            return payload
        if "PAUSE_TRANSITION_CAS" in script and numkeys == 1 and len(args) == 4:
            key, expected, payload, ttl = args
            raw = self.strings.get(key)
            if raw == payload:
                return raw
            if raw != expected:
                return ""
            self.strings[key] = payload
            self.ttls[key] = int(ttl)
            return payload
        if "PAUSE_CLEAR_CAS" in script and numkeys == 1 and len(args) == 3:
            key, request_id, expected_state = args
            raw = self.strings.get(key)
            if raw is None:
                return 0
            current = json.loads(raw)
            if current.get("requestId") != request_id or current.get("state") != expected_state:
                return 0
            del self.strings[key]
            self.ttls.pop(key, None)
            return 1
        if numkeys == 1 and len(args) == 4:
            return self._task_state_cas(*args)
        if numkeys == 2 and len(args) == 6:
            return self._task_state_side_record_cas(*args)
        if numkeys == 3 and len(args) == 7:
            return self._saver_cas(*args)
        raise NotImplementedError(
            f"InFileRedis cannot eval numkeys={numkeys} args={len(args)}"
        )

    def _put_writes(self, key: str, ttl_raw: str, count_raw: str, *args):
        fields = self.hashes.setdefault(key, {})
        call_ord = int(fields.get("__ord", 0)) + 1
        fields["__ord"] = str(call_ord)
        for within in range(int(count_raw)):
            mode, field, encoded = args[within * 3 : within * 3 + 3]
            envelope = json.loads(encoded)
            envelope["o"] = f"{call_ord:05d}:{within:05d}"
            rewritten = json.dumps(envelope, separators=(",", ":"))
            if mode != "nx" or field not in fields:
                fields[field] = rewritten
        self.ttls[key] = int(ttl_raw)
        return call_ord

    def _task_state_cas(self, key: str, expected_raw: str, payload: str, ttl: str):
        raw = self.strings.get(key)
        if raw is None:
            return [-1, -1]
        actual = int(json.loads(raw)["revision"])
        expected = int(expected_raw)
        if actual != expected:
            return [0, actual]
        self.strings[key] = payload
        self.ttls[key] = int(ttl)
        return [1, expected + 1]

    def _task_state_side_record_cas(
        self,
        key: str,
        side_key: str,
        expected_raw: str,
        payload: str,
        ttl: str,
        side_payload: str,
    ):
        raw = self.strings.get(key)
        if raw is None:
            return [-1, -1]
        actual = int(json.loads(raw)["revision"])
        expected = int(expected_raw)
        if actual != expected:
            return [0, actual]
        existing = self.strings.get(side_key)
        if existing is not None and existing != side_payload:
            return [-2, actual]
        self.strings[side_key] = side_payload
        self.ttls[side_key] = int(ttl)
        self.strings[key] = payload
        self.ttls[key] = int(ttl)
        return [1, expected + 1]

    def _saver_cas(
        self,
        cp_key: str,
        latest_key: str,
        step_key: str,
        envelope: str,
        checkpoint_id: str,
        step_raw: str,
        ttl_raw: str,
    ):
        self.strings[cp_key] = envelope
        self.ttls[cp_key] = int(ttl_raw)
        cur = self.strings.get(step_key)
        step = int(step_raw)
        if cur is None or step >= int(cur):
            self.strings[latest_key] = checkpoint_id
            self.strings[step_key] = step_raw
            self.ttls[latest_key] = int(ttl_raw)
            self.ttls[step_key] = int(ttl_raw)
            return 1
        return 0


# ── shared fixtures ───────────────────────────────────────────────────────────


class InFileToolInbox:
    """Deterministic inbox double implementing the production async contract."""

    def __init__(self):
        self.records: dict[str, InboxResponse] = {}

    async def claim(self, slot, *, run_id: str, thread_id: str) -> InboxResponse:
        key = slot.logical_slot_key()
        existing = self.records.get(key)
        if existing is not None and existing.status is InboxStatus.SUCCEEDED:
            return existing
        response = InboxResponse(
            InboxStatus.CLAIMED,
            fence=1,
            execution_id=slot.execution_id(run_id=run_id, thread_id=thread_id),
        )
        self.records[key] = response
        return response

    async def enter_in_flight(
        self, slot, *, execution_id: str, fence: int
    ) -> InboxResponse:
        response = InboxResponse(
            InboxStatus.IN_FLIGHT,
            fence=fence,
            execution_id=execution_id,
        )
        self.records[slot.logical_slot_key()] = response
        return response

    async def complete(
        self,
        slot,
        *,
        execution_id: str,
        fence: int,
        trace: ToolTrace,
        receipt: dict,
    ) -> InboxResponse:
        response = InboxResponse(
            InboxStatus.SUCCEEDED,
            fence=fence,
            execution_id=execution_id,
            trace=trace,
            receipt=receipt,
        )
        self.records[slot.logical_slot_key()] = response
        return response

    async def inspect(self, slot) -> InboxResponse:
        return self.records.get(
            slot.logical_slot_key(), InboxResponse(InboxStatus.ABSENT)
        )


_OS_HARD_REQ = {
    "key": "os", "operator": "eq", "value": "ios",
    "unit": "enum", "priority": "hard", "source": "user",
}

_QUESTION = "你希望手机的存储容量是多大？"


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
    """A task that is ALREADY collecting a required clarification."""
    return TaskStateCreateRequest(
        goal="想找 iOS 二手机。",
        task_type="ecommerce_guide",
        session_id="session-a",
        pending_questions=[_QUESTION],
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


class GraphV2InterruptResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = InFileRedis()
        self.tool_inbox = InFileToolInbox()
        task_state._client = self.redis
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    def _saver(self) -> GraphV2CheckpointSaver:
        return GraphV2CheckpointSaver(serde=JsonPlusSerializer())

    async def _clarification_state(self) -> TaskState:
        return await create_task_state(_used_phone_create_request())

    def _durable_kwargs(
        self,
        task_id: str,
        tool_caller: AsyncMock,
        trace: TraceBuilder,
        *,
        resume=None,
        restart=False,
        pause_resume=None,
        run_id=None,
        thread_id=None,
        user_message: str = "想找 iOS 二手机。",
        clarification_answer_applier=None,
    ) -> dict:
        async def tool_caller_v2(
            name: str,
            arguments: dict,
            context: ToolExecutionContext,
        ):
            self.assertIs(type(context), ToolExecutionContext)
            return await tool_caller(name, arguments)

        return dict(
            task_id=task_id,
            session_id="session-a",
            resume=resume,
            restart=restart,
            pause_resume=pause_resume,
            run_id=run_id,
            thread_id=thread_id,
            user_message=user_message,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda s: [
                _search_products_schema(), _get_product_details_schema(),
            ],
            tool_caller=tool_caller,
            tool_caller_v2=tool_caller_v2,
            tool_inbox=self.tool_inbox,
            trace_builder=trace,
            clarification_answer_applier=clarification_answer_applier,
            max_transitions=8,
        )

    @staticmethod
    def _resume_payload(
        fresh, *, answer: str, **overrides: object,
    ) -> dict:
        payload = {
            "taskId": fresh.interrupt_payload["taskId"],
            "runId": fresh.run_id,
            "threadId": fresh.thread_id,
            "revision": fresh.interrupt_payload["revision"],
            "proposalHash": fresh.interrupt_payload["proposalHash"],
            "answer": answer,
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _search_call_count(tool_caller: AsyncMock) -> int:
        return len(
            [c for c in tool_caller.await_args_list if c.args[0] == "search_products"]
        )

    # ── structure: clarification node/edge is enumerable ─────────────────────

    def test_durable_graph_exposes_clarification_node_and_edge(self):
        graph = build_graph_v2_durable(None)
        nodes = set(graph.get_graph().nodes)
        self.assertIn("clarification", nodes)
        edges = {(e.source, e.target) for e in graph.get_graph().edges}
        self.assertIn(("entry", "clarification"), edges)
        self.assertIn(("clarification", "planner"), edges)
        # ask_user routes INTO the clarification interrupt, never a silent END.
        self.assertEqual(
            route_after_planner_durable({"action": "ask_user"}), "clarification"
        )
        self.assertEqual(
            route_after_clarification({"action": "continue_to_executor"}), "planner"
        )

    # ── a real interrupt parks as a persisted checkpoint ─────────────────────

    async def test_fresh_run_parks_real_interrupt_with_checkpoint(self):
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        result = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-park", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )

        self.assertEqual(result.boundary, "clarification")
        self.assertTrue(result.interrupted)
        self.assertEqual(result.question, _QUESTION)
        self.assertIsNotNone(result.proposal_hash)
        self.assertTrue(result.thread_id.startswith("v2-task:"))
        self.assertIsNotNone(result.run_id)
        # The park IS a persisted checkpoint (requirement #1).
        self.assertGreaterEqual(result.checkpoint_count, 1)
        self.assertIsNotNone(result.checkpoint_hash)
        # The parked payload carries ONLY the visible question + server identity.
        payload = result.interrupt_payload
        self.assertEqual(payload["type"], "clarification")
        self.assertEqual(payload["taskId"], state.task_id)
        self.assertEqual(payload["threadId"], result.thread_id)
        self.assertEqual(payload["revision"], result.revision)
        self.assertEqual(payload["question"], _QUESTION)
        # No tool or model work happened while parked.
        tool_caller.assert_not_awaited()
        # A BRAND-NEW saver instance over the same Redis rebuilds the park.
        rebuilt = GraphV2CheckpointSaver(serde=JsonPlusSerializer())
        self.assertIsNotNone(await rebuilt.alatest_checkpoint_hash(result.thread_id))
        self.assertGreaterEqual(await rebuilt.acount_checkpoints(result.thread_id), 1)

    async def test_operator_pause_confirms_checkpoint_and_restart_continues(self):
        state = await self._clarification_state()
        run_id = "run-operator-pause"
        thread_id = build_thread_id(state.task_id, run_id)
        requested = await request_graph_pause(
            task_id=state.task_id,
            session_id="session-a",
            run_id=run_id,
            thread_id=thread_id,
            control_policy="fixed_v1",
        )
        self.assertEqual(requested["state"], "pause_requested")

        paused = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                AsyncMock(side_effect=_fake_tool),
                TraceBuilder(run_id, mode="context_pack"),
                run_id=run_id,
                thread_id=thread_id,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(paused.boundary, "operator_paused")
        self.assertEqual(paused.pause_receipt["state"], "paused")
        self.assertEqual(paused.pause_receipt["pausedBeforeNode"], "entry")
        self.assertEqual(
            paused.pause_receipt["checkpointHash"], paused.checkpoint_hash
        )
        self.assertGreaterEqual(paused.checkpoint_count, 1)

        with self.assertRaisesRegex(RuntimeError, "pause_request_conflict"):
            await request_graph_pause(
                task_id=state.task_id,
                session_id="session-a",
                run_id="run-must-not-replace-confirmed-pause",
                thread_id=build_thread_id(
                    state.task_id, "run-must-not-replace-confirmed-pause"
                ),
                control_policy="fixed_v1",
            )
        self.assertEqual((await read_graph_pause(state.task_id))["state"], "paused")

        client_receipt = public_pause_receipt(paused.pause_receipt)
        forged = {**client_receipt, "checkpointHash": "0" * 64}
        rejected = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                AsyncMock(side_effect=_fake_tool),
                TraceBuilder(run_id, mode="context_pack"),
                restart=True,
                pause_resume=forged,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(rejected.boundary, "resume_rejected")
        self.assertEqual(rejected.rejected_reason, "pause_resume_receipt_mismatch")
        self.assertEqual((await read_graph_pause(state.task_id))["state"], "paused")

        # Simulate a worker dying after the atomic paused -> resuming CAS but
        # before graph restart.  Replaying the same client receipt must remain
        # idempotent and continue the exact checkpoint.
        await begin_graph_pause_resume(
            await read_graph_pause(state.task_id),
            client_receipt=client_receipt,
        )

        restarted = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                AsyncMock(side_effect=_fake_tool),
                TraceBuilder(run_id, mode="context_pack"),
                restart=True,
                pause_resume=client_receipt,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(restarted.boundary, "clarification")
        self.assertIsNone(await read_graph_pause(state.task_id))

    async def test_late_unconfirmed_pause_does_not_block_next_run(self):
        state = await self._clarification_state()
        stale = await request_graph_pause(
            task_id=state.task_id,
            session_id="session-a",
            run_id="run-already-finished",
            thread_id=build_thread_id(state.task_id, "run-already-finished"),
            control_policy="react_v1",
        )
        current = await request_graph_pause(
            task_id=state.task_id,
            session_id="session-a",
            run_id="run-current",
            thread_id=build_thread_id(state.task_id, "run-current"),
            control_policy="react_v1",
        )

        self.assertNotEqual(current["requestId"], stale["requestId"])
        self.assertEqual(current["state"], "pause_requested")
        self.assertEqual(current["runId"], "run-current")
        self.assertEqual((await read_graph_pause(state.task_id))["runId"], "run-current")

    async def test_new_fresh_turn_replaces_previous_run_message(self):
        """A long-lived task may start multiple fresh durable runs.

        The previous run marker is recovery evidence, not permission to replace
        the current HTTP turn.  This is the production-adjacent regression for
        multi-turn candidate-scope reranking: before the fix the second turn was
        persisted and planned as the first turn again.
        """
        state = await self._clarification_state()
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="agent",
                domainStatePatch={"v2UserMessage": "上一轮的检索问题"},
            ),
        )
        current_turn = "这其中哪一个拍照效果最好"
        tool_caller = AsyncMock(side_effect=_fake_tool)

        result = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id,
                tool_caller,
                TraceBuilder("run-new-fresh-turn", mode="context_pack"),
                user_message=current_turn,
            ),
            checkpointer=self._saver(),
        )

        self.assertEqual(result.boundary, "clarification")
        persisted = await get_task_state(state.task_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(
            persisted.domain_state.get("v2UserMessage"),
            current_turn,
        )

    # ── same-thread Command(resume=...) completes the run ────────────────────

    async def test_same_thread_command_resume_completes_the_run(self):
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        applied_answers: list[str] = []

        async def apply_answer(current: TaskState, answer: str) -> TaskState:
            applied_answers.append(answer)
            return await update_task_state(
                current.task_id,
                TaskStatePatchRequest(
                    expectedRevision=current.revision,
                    actor="agent",
                    domainStatePatch={"lastUserMessage": answer},
                ),
            )

        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-resume-fresh", mode="context_pack"),
                clarification_answer_applier=apply_answer,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(fresh.boundary, "clarification")

        resumed = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-resume-next", mode="context_pack"),
                resume=self._resume_payload(fresh, answer="128GB"),
                clarification_answer_applier=apply_answer,
            ),
            checkpointer=self._saver(),
        )

        self.assertEqual(resumed.boundary, "task_completed")
        self.assertEqual(resumed.mode, "resume")
        self.assertEqual(resumed.thread_id, fresh.thread_id)
        self.assertEqual(resumed.run_id, fresh.run_id)
        # The whole park + resume lifecycle ran EXACTLY ONE live search.
        self.assertEqual(self._search_call_count(tool_caller), 1)
        self.assertEqual(applied_answers, ["128GB"])
        # Node sequence: entry (from the parked checkpoint) then the full path.
        sequence = [
            e["nodeName"]
            for e in resumed.graph_state["node_events"]
            if e["phase"] == "start"
        ]
        self.assertEqual(
            sequence, ["entry", "clarification", "planner", "executor", "validator"]
        )
        # The applied answer is recorded in the durable receipt.
        live = await get_task_state(state.task_id)
        receipt = (live.domain_state or {}).get("v2PendingClarification")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["status"], "resolved")
        self.assertEqual(
            receipt["answerHash"],
            hashlib.sha256("128GB".encode("utf-8")).hexdigest()[:16],
        )
        self.assertGreater(live.revision, fresh.revision)
        # The completion persisted its own checkpoint on the same thread.
        rebuilt = GraphV2CheckpointSaver(serde=JsonPlusSerializer())
        self.assertIsNotNone(await rebuilt.alatest_checkpoint_hash(resumed.thread_id))

    # ── fail-closed resume rejects (requirement #5) ─────────────────────────

    async def _parked(self, state: TaskState, tool_caller: AsyncMock):
        return await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-reject", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )

    async def _rejected(self, task_id: str, resume: dict) -> "object":
        return await run_graph_v2_durable(
            **self._durable_kwargs(
                task_id, AsyncMock(side_effect=_fake_tool),
                TraceBuilder("run-reject-2", mode="context_pack"),
                resume=resume,
            ),
            checkpointer=self._saver(),
        )

    async def test_resume_rejects_empty_answer_fail_closed(self):
        state = await self._clarification_state()
        result = await self._rejected(state.task_id, {
            "taskId": state.task_id, "runId": "run-x",
            "threadId": f"v2-task:{state.task_id}:run-x",
            "revision": 1, "proposalHash": "0" * 16, "answer": "",
        })
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "empty_answer")

    async def test_resume_rejects_too_long_answer_fail_closed(self):
        state = await self._clarification_state()
        result = await self._rejected(state.task_id, {
            "taskId": state.task_id, "runId": "run-x",
            "threadId": f"v2-task:{state.task_id}:run-x",
            "revision": 1, "proposalHash": "0" * 16, "answer": "长" * 2001,
        })
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "answer_too_long")

    async def test_resume_rejects_missing_revision_fail_closed(self):
        state = await self._clarification_state()
        result = await self._rejected(state.task_id, {
            "taskId": state.task_id, "runId": "run-x",
            "threadId": f"v2-task:{state.task_id}:run-x",
            "proposalHash": "0" * 16, "answer": "128GB",
        })
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "revision_missing")

    async def test_resume_rejects_cross_task_fail_closed(self):
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await self._parked(state, tool_caller)
        # A payload whose taskId belongs to a DIFFERENT server task.
        forged = self._resume_payload(fresh, answer="128GB", taskId="task-other")
        result = await self._rejected(state.task_id, forged)
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "cross_task")
        tool_caller.assert_not_awaited()

    async def test_resume_rejects_malformed_thread_fail_closed(self):
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await self._parked(state, tool_caller)
        forged = self._resume_payload(fresh, answer="128GB", threadId="not-a-thread")
        result = await self._rejected(state.task_id, forged)
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "thread_malformed")
        tool_caller.assert_not_awaited()

    async def test_resume_rejects_revision_mismatch_fail_closed(self):
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await self._parked(state, tool_caller)
        forged = self._resume_payload(
            fresh, answer="128GB", revision=fresh.interrupt_payload["revision"] + 1
        )
        result = await self._rejected(state.task_id, forged)
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "revision_mismatch")
        tool_caller.assert_not_awaited()

    async def test_resume_rejects_live_revision_drift_with_unchanged_park(self):
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await self._parked(state, tool_caller)
        live = await get_task_state(state.task_id)
        await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=live.revision,
                actor="system",
                domain_state_patch={"externalRevisionDrift": True},
            ),
        )
        result = await self._rejected(
            state.task_id,
            self._resume_payload(fresh, answer="128GB"),
        )
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "revision_mismatch")
        tool_caller.assert_not_awaited()

    async def test_resume_rejects_proposal_hash_mismatch_fail_closed(self):
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await self._parked(state, tool_caller)
        forged = self._resume_payload(fresh, answer="128GB", proposalHash="0" * 16)
        result = await self._rejected(state.task_id, forged)
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "proposal_hash_mismatch")
        tool_caller.assert_not_awaited()

    async def test_resume_no_pending_interrupt_fail_closed(self):
        """A resume on a completed thread with no parked interrupt fails closed."""
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await self._parked(state, tool_caller)
        # Complete the run first.
        completed = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-complete", mode="context_pack"),
                resume=self._resume_payload(fresh, answer="128GB"),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(completed.boundary, "task_completed")
        # A second resume carrying a FORGED proposal hash finds no pending park.
        forged = self._resume_payload(
            fresh, answer="另一答案", proposalHash="0" * 16,
        )
        result = await self._rejected(state.task_id, forged)
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "no_pending_interrupt")
        # Still exactly one live search — nothing was re-run.
        self.assertEqual(self._search_call_count(tool_caller), 1)

    async def test_resume_resolved_interrupt_different_payload_fail_closed(self):
        """The SAME proposalHash but a DIFFERENT answer on a resolved interrupt
        fails closed instead of double-applying."""
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await self._parked(state, tool_caller)
        await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-resolve", mode="context_pack"),
                resume=self._resume_payload(fresh, answer="128GB"),
            ),
            checkpointer=self._saver(),
        )
        different = self._resume_payload(fresh, answer="另一答案")
        result = await self._rejected(state.task_id, different)
        self.assertEqual(result.boundary, "resume_rejected")
        self.assertEqual(result.rejected_reason, "resolved_interrupt_different_payload")
        self.assertEqual(self._search_call_count(tool_caller), 1)

    async def test_replay_exact_answer_is_idempotent(self):
        """Requirement #5: replaying the EXACT applied answer returns the stored
        receipt with no graph invocation — no new revision/checkpoint/tool."""
        state = await self._clarification_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await self._parked(state, tool_caller)
        resolved = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-resolve-1", mode="context_pack"),
                resume=self._resume_payload(fresh, answer="128GB"),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(resolved.boundary, "task_completed")

        replayed = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-replay", mode="context_pack"),
                resume=self._resume_payload(fresh, answer="128GB"),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(replayed.boundary, "task_completed")
        self.assertEqual(replayed.mode, "idempotent_replay")
        self.assertEqual(replayed.proposal_hash, fresh.proposal_hash)
        # No new checkpoint, no new live revision, no new tool dispatch.
        self.assertEqual(replayed.checkpoint_count, 0)
        self.assertEqual(replayed.revision, resolved.revision)
        self.assertEqual(self._search_call_count(tool_caller), 1)
        self.assertEqual(
            (await get_task_state(state.task_id)).revision, resolved.revision
        )

    # ── still-uncertain re-ask parks again, never guesses (requirement #4) ───

    async def test_re_ask_blank_answer_parks_again_no_guess(self):
        """A blank re-ask on a parked clarification parks at a FRESH interrupt id
        carrying ``reAskReason``; the graph never guesses the user's intent."""
        state = await self._clarification_state()
        saver = GraphV2CheckpointSaver(serde=JsonPlusSerializer())
        graph = build_graph_v2_durable(saver)
        run_id = f"run-{state.task_id}-reask"
        thread_id = f"v2-task:{state.task_id}:{run_id}"
        tool_caller = AsyncMock(side_effect=_fake_tool)
        runtime = GraphV2Runtime(
            user_message=state.goal,
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda s: [
                _search_products_schema(), _get_product_details_schema(),
            ],
            tool_caller=tool_caller,
            trace_builder=TraceBuilder("run-reask", mode="context_pack"),
            projector=None,
            max_transitions=8,
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            session_owner_hash="test-owner-hash",
            checkpointer=saver,
            durable=True,
        )
        config = {"configurable": {"thread_id": thread_id}}

        first = await graph.ainvoke(
            _initial_graph_input(state.task_id, thread_id, state, "test-owner-hash"),
            config=config,
            context=runtime,
            recursion_limit=60,
        )
        self.assertIn("__interrupt__", first)
        first_payload = tuple(first["__interrupt__"] or ())[-1].value
        self.assertEqual(first_payload["type"], "clarification")
        self.assertEqual(first_payload["question"], _QUESTION)
        self.assertNotIn("reAskReason", first_payload)

        second = await graph.ainvoke(
            Command(resume="   "),
            config=config,
            context=runtime,
            recursion_limit=60,
        )
        self.assertIn("__interrupt__", second)
        second_payload = tuple(second["__interrupt__"] or ())[-1].value
        # Parks AGAIN carrying the re-ask reason (a fresh interrupt on the same
        # node re-execution) — the graph never guesses the user's intent.
        self.assertEqual(second_payload["type"], "clarification")
        self.assertIn("reAskReason", second_payload)
        self.assertTrue(second_payload["reAskReason"])
        self.assertEqual(second_payload["question"], _QUESTION)
        # The blank re-ask changed nothing and ran no tool.
        live = await get_task_state(state.task_id)
        self.assertEqual(live.status, "collecting_information")
        self.assertEqual(live.pending_questions, [_QUESTION])
        tool_caller.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
