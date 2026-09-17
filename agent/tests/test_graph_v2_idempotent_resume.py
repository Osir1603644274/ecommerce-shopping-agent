"""Durable V2 idempotent-resume tests (DAY2 — requirement #5).

The resume contract is fail-closed and idempotent:

* a VALID resume applies the answer once, then the graph completes the run;
* replaying the EXACT already-applied answer returns the STORED receipt without
  invoking the graph — no new revision, no model/tool, no new checkpoint
  (``mode == "idempotent_replay"``, ``checkpoint_count == 0``);
* duplicate replays are identical and revision-stable;
* replaying the exact answer even after the task has advanced stays idempotent
  (the stored receipt attests proposal + answer, not the revision);
* a DIFFERENT answer on an already-resolved interrupt fails closed
  (``resolved_interrupt_different_payload``);
* every rejected resume (empty / too-long / forged / cross-task / malformed /
  revision-mismatch / proposal-hash-mismatch / no-pending-interrupt) NEVER
  mutates the live TaskState — no revision bump, no tool, no checkpoint.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app import task_state
from app.agent_trace import TraceBuilder
from app.graph.checkpoint import GraphV2CheckpointSaver
from app.graph.nodes.clarification import _MAX_ANSWER_CHARS
from app.graph.resume import (
    read_terminal_response_receipt,
    run_graph_v2_durable,
    write_terminal_response_receipt,
)
from app.schemas import ToolTrace
from app.tool_execution_v2 import ToolExecutionContext
from app.task_state import (
    TaskStateCreateRequest,
    create_task_state,
    get_task_state,
    update_task_state,
    TaskStatePatchRequest,
)
from app.tools import TOOL_SCHEMAS
from tests.two_stage_ranking_fixtures import two_stage_search_detail
from tests.test_graph_v2_interrupt_resume import InFileToolInbox

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

    async def eval(self, script: str, numkeys: int, *args):
        if "GRAPH_V2_PUT_WRITES" in script and numkeys == 1:
            return self._put_writes(*args)
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


_OS_HARD_REQ = {
    "key": "os", "operator": "eq", "value": "ios",
    "unit": "enum", "priority": "hard", "source": "user",
}
_QUESTION = "你希望手机的存储容量是多大？"
_ANSWER = "256GB 版本"


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


def _used_phone_create_request(pending_questions: list[str] | None = None):
    return TaskStateCreateRequest(
        goal="想找 iOS 二手机。",
        task_type="ecommerce_guide",
        session_id="session-a",
        pending_questions=pending_questions or [],
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


class GraphV2IdempotentResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = InFileRedis()
        self.tool_inbox = InFileToolInbox()
        task_state._client = self.redis
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    def _saver(self) -> GraphV2CheckpointSaver:
        return GraphV2CheckpointSaver(serde=JsonPlusSerializer())

    async def _parked_state(self):
        """A task that parks at a real clarification interrupt (status
        ``collecting_information`` + a pending question)."""
        return await create_task_state(
            _used_phone_create_request(pending_questions=[_QUESTION])
        )

    def _durable_kwargs(
        self,
        task_id: str,
        tool_caller: AsyncMock,
        trace: TraceBuilder,
        *,
        resume=None,
        session_id="session-a",
    ) -> dict:
        async def tool_caller_v2(
            name: str,
            arguments: dict,
            context: ToolExecutionContext,
        ) -> ToolTrace:
            self.assertIs(type(context), ToolExecutionContext)
            return await tool_caller(name, arguments)

        return dict(
            task_id=task_id,
            session_id=session_id,
            resume=resume,
            user_message="想找 iOS 二手机。",
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda s: [
                _search_products_schema(), _get_product_details_schema(),
            ],
            tool_caller=tool_caller,
            tool_caller_v2=tool_caller_v2,
            tool_inbox=self.tool_inbox,
            trace_builder=trace,
            max_transitions=8,
        )

    def _resume_payload(self, fresh, answer: str, **overrides) -> dict:
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

    # ── exact-answer replay: no graph invocation, no mutation ─────────────────

    async def test_exact_answer_replay_is_idempotent_no_new_revision_tool_or_checkpoint(self):
        state = await self._parked_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-park", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(fresh.boundary, "clarification")

        resolved = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-resume", mode="context_pack"),
                resume=self._resume_payload(fresh, _ANSWER),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(resolved.boundary, "task_completed")
        resolved_revision = resolved.revision
        self.assertEqual(self._search_call_count(tool_caller), 1)

        # Replay the EXACT answer: returns the stored receipt, no graph work.
        replayed = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-replay", mode="context_pack"),
                resume=self._resume_payload(fresh, _ANSWER),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(replayed.boundary, "task_completed")
        self.assertEqual(replayed.mode, "idempotent_replay")
        self.assertEqual(replayed.revision, resolved_revision)
        self.assertEqual(replayed.checkpoint_count, 0)
        self.assertEqual(self._search_call_count(tool_caller), 1)
        live = await get_task_state(state.task_id)
        receipt = (live.domain_state or {}).get("v2PendingClarification")
        self.assertIsNotNone(receipt)
        self.assertEqual(replayed.proposal_hash, receipt["proposalHash"])

    async def test_duplicate_replays_are_identical_and_revision_stable(self):
        state = await self._parked_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-park", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-resume", mode="context_pack"),
                resume=self._resume_payload(fresh, _ANSWER),
            ),
            checkpointer=self._saver(),
        )
        base_revision = (await get_task_state(state.task_id)).revision

        first = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-replay-1", mode="context_pack"),
                resume=self._resume_payload(fresh, _ANSWER),
            ),
            checkpointer=self._saver(),
        )
        second = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-replay-2", mode="context_pack"),
                resume=self._resume_payload(fresh, _ANSWER),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(first.mode, "idempotent_replay")
        self.assertEqual(second.mode, "idempotent_replay")
        self.assertEqual(first.proposal_hash, second.proposal_hash)
        self.assertEqual(first.revision, second.revision)
        self.assertEqual(first.revision, base_revision)
        self.assertEqual((await get_task_state(state.task_id)).revision, base_revision)

    async def test_precommitted_terminal_receipt_is_inert_until_taskstate_cas(self):
        state = await self._parked_state()
        run_id = "run-terminal-precommit"
        thread_id = f"v2-task:{state.task_id}:{run_id}"
        proposal_hash = "proposal-terminal-precommit"
        answer = "这是崩溃窗前预写的固定回答。"
        base_revision = state.revision

        self.assertTrue(await write_terminal_response_receipt(
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            proposal_hash=proposal_hash,
            session_id="session-a",
            answer=answer,
            state_revision=base_revision + 1,
            base_task_revision=base_revision,
            control_policy="react_v1",
        ))
        self.assertIsNone(await read_terminal_response_receipt(
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            proposal_hash=proposal_hash,
            session_id="session-a",
            task_state=state,
        ))

        committed = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=base_revision,
                actor="agent",
                domainStatePatch={"v2FinalAnswerReceipt": {
                    "version": 1,
                    "taskId": state.task_id,
                    "runId": run_id,
                    "threadId": thread_id,
                    "publicationId": proposal_hash,
                    "sessionOwnerHash": hashlib.sha256(
                        b"session-a"
                    ).hexdigest()[:16],
                    "controlPolicy": "react_v1",
                    "policyRevision": "react-v1-2026-08-27",
                    "baseTaskRevision": base_revision,
                    "finalizationRevision": base_revision + 1,
                    "answerSha256": hashlib.sha256(
                        answer.encode("utf-8")
                    ).hexdigest(),
                }},
            ),
        )
        replay = await read_terminal_response_receipt(
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            proposal_hash=proposal_hash,
            session_id="session-a",
            task_state=committed,
        )
        self.assertIsNotNone(replay)
        self.assertEqual(replay["answer"], answer)

    async def test_terminal_response_receipt_is_identity_bound_and_bounded(self):
        state = await self._parked_state()
        run_id = "run-terminal-receipt"
        thread_id = f"v2-task:{state.task_id}:{run_id}"
        proposal_hash = "proposal-terminal-001"
        answer = "这是第一次完成后的固定可见回答。"
        base_revision = state.revision
        state = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=base_revision,
                actor="agent",
                domainStatePatch={"v2FinalAnswerReceipt": {
                    "version": 1,
                    "taskId": state.task_id,
                    "runId": run_id,
                    "threadId": thread_id,
                    "publicationId": proposal_hash,
                    "sessionOwnerHash": hashlib.sha256(
                        b"session-a"
                    ).hexdigest()[:16],
                    "controlPolicy": "react_v1",
                    "policyRevision": "react-v1-2026-08-27",
                    "baseTaskRevision": base_revision,
                    "finalizationRevision": base_revision + 1,
                    "answerSha256": hashlib.sha256(
                        answer.encode("utf-8")
                    ).hexdigest(),
                }},
            ),
        )
        stored = await write_terminal_response_receipt(
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            proposal_hash=proposal_hash,
            session_id="session-a",
            answer=answer,
            state_revision=state.revision,
            base_task_revision=base_revision,
            control_policy="react_v1",
        )
        self.assertTrue(stored)
        self.assertFalse(await write_terminal_response_receipt(
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            proposal_hash=proposal_hash,
            session_id="session-a",
            answer="冲突的第二个回答。",
            state_revision=state.revision,
            base_task_revision=base_revision,
            control_policy="react_v1",
        ))
        replay = await read_terminal_response_receipt(
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            proposal_hash=proposal_hash,
            session_id="session-a",
            task_state=state,
        )
        self.assertIsNotNone(replay)
        self.assertEqual(replay["answer"], "这是第一次完成后的固定可见回答。")
        self.assertIsNone(await read_terminal_response_receipt(
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            proposal_hash=proposal_hash,
            session_id="session-b",
            task_state=state,
        ))
        terminal_key = next(
            key for key in self.redis.strings if key.startswith("graph-v2:terminal:")
        )
        forged = json.loads(self.redis.strings[terminal_key])
        forged["answer"] = "伪造回答"
        forged["answerHash"] = hashlib.sha256(
            forged["answer"].strip().encode("utf-8")
        ).hexdigest()[:16]
        forged["resultHash"] = hashlib.sha256(
            forged["answer"].encode("utf-8")
        ).hexdigest()
        forged.pop("policyRevision", None)
        self.redis.strings[terminal_key] = json.dumps(forged, ensure_ascii=False)
        self.assertIsNone(await read_terminal_response_receipt(
            task_id=state.task_id,
            run_id=run_id,
            thread_id=thread_id,
            proposal_hash=proposal_hash,
            session_id="session-a",
            task_state=state,
        ))

    async def test_cross_session_resume_and_replay_reject_before_graph(self):
        """Session B cannot use A's complete payload, even on the no-graph
        exact-replay branch that was vulnerable in Attempt 001."""
        state = await self._parked_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-owner-park", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        payload = self._resume_payload(fresh, _ANSWER)
        before = (await get_task_state(state.task_id)).revision
        rejected = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-cross-session", mode="context_pack"),
                resume=payload, session_id="session-b",
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(rejected.boundary, "resume_rejected")
        self.assertEqual(rejected.rejected_reason, "cross_session")
        self.assertEqual(rejected.graph_state, {})
        self.assertIsNone(rejected.task_state)
        self.assertEqual((await get_task_state(state.task_id)).revision, before)
        tool_caller.assert_not_awaited()

        resolved = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-owner-resume", mode="context_pack"),
                resume=payload,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(resolved.boundary, "task_completed")
        replay_rejected = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-cross-session-replay", mode="context_pack"),
                resume=payload, session_id="session-b",
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(replay_rejected.rejected_reason, "cross_session")
        self.assertIsNone(replay_rejected.task_state)
        self.assertEqual((await get_task_state(state.task_id)).revision, resolved.revision)
        self.assertEqual(self._search_call_count(tool_caller), 1)

    async def test_missing_request_session_rejects_direct_runner(self):
        state = await self._parked_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        rejected = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-missing-session", mode="context_pack"),
                session_id=None,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(rejected.rejected_reason, "session_missing")
        self.assertIsNone(rejected.task_state)
        self.assertEqual((await get_task_state(state.task_id)).revision, state.revision)
        tool_caller.assert_not_awaited()

    async def test_replay_after_task_advanced_remains_idempotent(self):
        """The stored receipt attests proposal+answer, so a replay after later
        business progress is still an idempotent no-op (no further writes)."""
        state = await self._parked_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-park", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-resume", mode="context_pack"),
                resume=self._resume_payload(fresh, _ANSWER),
            ),
            checkpointer=self._saver(),
        )
        advanced = await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=(await get_task_state(state.task_id)).revision,
                actor="user",
                domain_state_patch={"followUpNote": "预算三千以内"},
            ),
        )
        tool_caller.reset_mock()

        replayed = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-replay", mode="context_pack"),
                resume=self._resume_payload(fresh, _ANSWER),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(replayed.mode, "idempotent_replay")
        self.assertEqual(replayed.revision, advanced.revision)
        tool_caller.assert_not_awaited()

    # ── different payload on a resolved interrupt fails closed ────────────────

    async def test_different_answer_on_resolved_interrupt_fails_closed(self):
        state = await self._parked_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-park", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-resume", mode="context_pack"),
                resume=self._resume_payload(fresh, _ANSWER),
            ),
            checkpointer=self._saver(),
        )
        before = await get_task_state(state.task_id)
        tool_caller.reset_mock()

        rejected = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-replay-other", mode="context_pack"),
                resume=self._resume_payload(fresh, "1TB 版本"),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(rejected.boundary, "resume_rejected")
        self.assertEqual(rejected.rejected_reason, "resolved_interrupt_different_payload")
        after = await get_task_state(state.task_id)
        self.assertEqual(after.revision, before.revision)
        tool_caller.assert_not_awaited()

    # ── every rejected resume never mutates the live TaskState ────────────────

    async def test_rejected_resumes_never_mutate_live_state(self):
        state = await self._parked_state()
        tool_caller = AsyncMock(side_effect=_fake_tool)
        fresh = await run_graph_v2_durable(
            **self._durable_kwargs(
                state.task_id, tool_caller,
                TraceBuilder("run-park", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        before = await get_task_state(state.task_id)
        payload = self._resume_payload(fresh, _ANSWER)

        cases = [
            ("empty_answer", dict(payload, answer="")),
            ("answer_too_long", dict(payload, answer="x" * (_MAX_ANSWER_CHARS + 1))),
            ("revision_missing", {k: v for k, v in payload.items() if k != "revision"}),
            ("cross_task", dict(payload, taskId="task-other")),
            ("thread_malformed", dict(payload, threadId="bogus-thread")),
            ("cross_task", dict(payload, threadId="v2-task:task-other:run-1")),
            ("revision_mismatch", dict(payload, revision=fresh.interrupt_payload["revision"] + 1)),
            ("proposal_hash_mismatch", dict(payload, proposalHash="deadbeefdeadbeef")),
        ]
        for expected_reason, bad_payload in cases:
            with self.subTest(reason=expected_reason):
                rejected = await run_graph_v2_durable(
                    **self._durable_kwargs(
                        state.task_id, tool_caller,
                        TraceBuilder(f"run-reject-{expected_reason}", mode="context_pack"),
                        resume=bad_payload,
                    ),
                    checkpointer=self._saver(),
                )
                self.assertEqual(rejected.boundary, "resume_rejected")
                self.assertEqual(rejected.rejected_reason, expected_reason)
                after = await get_task_state(state.task_id)
                self.assertEqual(after.revision, before.revision)
        tool_caller.assert_not_awaited()

    async def test_resume_with_no_pending_interrupt_fails_closed(self):
        """A fresh task that never parked (no pending question) has neither a
        parked interrupt nor a receipt — any resume is rejected as
        ``no_pending_interrupt``."""
        created = await create_task_state(_used_phone_create_request())
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        tool_caller = AsyncMock(side_effect=_fake_tool)
        done = await run_graph_v2_durable(
            **self._durable_kwargs(
                ready.task_id, tool_caller,
                TraceBuilder("run-done", mode="context_pack"),
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(done.boundary, "task_completed")
        before = await get_task_state(ready.task_id)
        tool_caller.reset_mock()

        forged = {
            "taskId": ready.task_id,
            "runId": done.run_id,
            "threadId": done.thread_id,
            "revision": before.revision,
            "proposalHash": "deadbeefdeadbeef",
            "answer": "256GB",
        }
        rejected = await run_graph_v2_durable(
            **self._durable_kwargs(
                ready.task_id, tool_caller,
                TraceBuilder("run-forge", mode="context_pack"),
                resume=forged,
            ),
            checkpointer=self._saver(),
        )
        self.assertEqual(rejected.boundary, "resume_rejected")
        self.assertEqual(rejected.rejected_reason, "no_pending_interrupt")
        after = await get_task_state(ready.task_id)
        self.assertEqual(after.revision, before.revision)
        tool_caller.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
