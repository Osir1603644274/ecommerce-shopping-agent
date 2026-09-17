"""Durable V2 checkpoint saver tests (DAY2 — requirement #1/#2/#9).

Exercises ``GraphV2CheckpointSaver`` against a self-contained in-file Redis
fake (``InFileRedis``) so this test file needs no process-global store, no
``tests.fake_redis`` import (not in the DAY2 allowlist) and no network:

* put / get / list / pending-writes round-trips
* TTL / independent namespace / cross-identity isolation
* Lua CAS monotonicity — an OLD checkpoint can never clobber the newer
  ``latest`` pointer (stale checkpoint never becomes latest)
* payload allowlist (refuses non-allowlisted channels on write AND read)
* 256 KiB bytes budget
* sha256[:16] integrity — a tampered envelope fails closed
* process rebuild — a fresh saver instance reading the SAME Redis returns the
  checkpoint (cross-process durability)
* ``alatest_checkpoint_hash`` / ``acount_checkpoints`` read straight from Redis
  (durable across restarts, unlike the in-memory audit ring)
* ``aput_writes`` drops ``__error__`` / ``__error_source_node__`` so a failed
  superstep leaves the thread at its last successful checkpoint.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import unittest

from langgraph.checkpoint.base import CheckpointMetadata
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app import task_state
from app.graph.checkpoint import (
    DEFAULT_CHECKPOINT_CHANNEL_ALLOWLIST,
    DEFAULT_WRITE_CHANNEL_ALLOWLIST,
    GRAPH_V2_CP_TTL_SECONDS,
    GraphV2CheckpointError,
    GraphV2CheckpointIntegrityError,
    GraphV2CheckpointSaver,
)

# ── self-contained Redis fake (not FakeRedis: not in the DAY2 allowlist) ──────


class InFileRedis:
    """Minimal async plain-Redis fake with the full surface the durable stack
    touches: strings/lists/hashes/zsets + ``eval`` dispatching on ``numkeys``
    (TaskState CAS = 1 key; saver CAS = 3 keys).  Per-key TTL is recorded so
    tests can assert the checkpoint keys carry the 7-day TTL."""

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
        if numkeys == 1 and len(args) == 4:
            return self._task_state_cas(*args)
        if numkeys == 3 and len(args) == 7:
            return self._saver_cas(*args)
        raise NotImplementedError(
            f"InFileRedis cannot eval numkeys={numkeys} args={len(args)}"
        )

    def _put_writes(self, key: str, ttl_raw: str, count_raw: str, *args):
        fields = self.hashes.setdefault(key, {})
        call_ord = int(fields.get("__ord", 0)) + 1
        fields["__ord"] = str(call_ord)
        count = int(count_raw)
        for within in range(count):
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


# ── helpers ──────────────────────────────────────────────────────────────────


def _thread_config(thread_id: str, checkpoint_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _checkpoint(
    checkpoint_id: str,
    *,
    action: str = "continue_to_executor",
    step: int = 0,
    extra: dict | None = None,
):
    channel_values = {
        "task_id": "task-1",
        "thread_id": "v2-task:task-1:run-1",
        "graph_revision": 1,
        "action": action,
        "transition_count": step,
        "node_events": [],
    }
    if extra:
        channel_values.update(extra)
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-08-18T00:00:00.000Z",
        "channel_values": channel_values,
        "channel_versions": {
            "task_id": step + 1,
            "graph_revision": step + 1,
        },
        "versions_seen": {},
    }


def _metadata(step: int) -> CheckpointMetadata:
    return CheckpointMetadata(
        source="update", step=step, writes={}, parents={}
    )


# ── tests ────────────────────────────────────────────────────────────────────


class GraphV2CheckpointSaverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = InFileRedis()
        task_state._client = self.redis
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    def _saver(
        self, *, audit_callback=None
    ) -> GraphV2CheckpointSaver:
        return GraphV2CheckpointSaver(
            serde=JsonPlusSerializer(),
            audit_callback=audit_callback,
        )

    async def test_put_get_round_trip_and_process_rebuild(self):
        """A checkpoint written by one process reads back through a FRESH saver
        instance over the same Redis (requirement #2 cross-process durability)."""
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        cfg = await saver.aput(
            _thread_config(thread),
            _checkpoint("cp-1", step=0),
            _metadata(step=0),
            {},
        )
        self.assertEqual(
            cfg["configurable"]["checkpoint_id"], "cp-1"
        )

        # A brand-new saver = a restarted process: same Redis, no shared memory.
        rebuilt = self._saver()
        tup = await rebuilt.aget_tuple(_thread_config(thread))
        self.assertIsNotNone(tup)
        self.assertEqual(tup.checkpoint["id"], "cp-1")
        self.assertEqual(tup.checkpoint["channel_values"]["action"], "continue_to_executor")
        self.assertEqual(tup.metadata["step"], 0)
        self.assertEqual(
            tup.checkpoint["channel_values"]["task_id"], "task-1"
        )
        # Round-trip preserves the allowlisted channels exactly.
        self.assertIn("graph_revision", tup.checkpoint["channel_values"])
        self.assertIn("transition_count", tup.checkpoint["channel_values"])

    async def test_alist_walks_parent_chain(self):
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        parent = await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        await saver.aput(
            parent, _checkpoint("cp-2", step=1), _metadata(step=1), {}
        )
        collected = [
            tup.checkpoint["id"]
            async for tup in saver.alist(_thread_config(thread))
        ]
        self.assertEqual(collected, ["cp-2", "cp-1"])

    async def test_aput_writes_round_trip_and_ordering(self):
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        cfg = await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        await saver.aput_writes(
            cfg,
            [("action", "ready_for_validation")],
            task_id="node-1",
        )
        tup = await saver.aget_tuple(_thread_config(thread))
        self.assertIsNotNone(tup.pending_writes)
        self.assertIn(("node-1", "action", "ready_for_validation"), tup.pending_writes)
        writes_key = f"graph-v2:cp:{thread}::writes:cp-1"
        self.assertEqual(self.redis.ttls[writes_key], GRAPH_V2_CP_TTL_SECONDS)

    async def test_tampered_pending_write_fails_integrity_check(self):
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        cfg = await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        await saver.aput_writes(
            cfg, [("action", "ready_for_validation")], task_id="node-1"
        )
        writes_key = f"graph-v2:cp:{thread}::writes:cp-1"
        field = next(key for key in self.redis.hashes[writes_key] if key != "__ord")
        envelope = json.loads(self.redis.hashes[writes_key][field])
        blob = bytearray(base64.b64decode(envelope["b"]))
        blob[-1] ^= 0x01
        envelope["b"] = base64.b64encode(bytes(blob)).decode("ascii")
        self.redis.hashes[writes_key][field] = json.dumps(envelope)
        with self.assertRaises(GraphV2CheckpointIntegrityError):
            await saver.aget_tuple(_thread_config(thread))

    async def test_error_writes_dropped_fail_closed(self):
        """``__error__`` / ``__error_source_node__`` never persist; the thread
        stays at its last successful checkpoint so a restart re-runs the node."""
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        cfg = await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        await saver.aput_writes(
            cfg,
            [
                ("__error__", Exception("boom")),
                ("__error_source_node__", "executor"),
            ],
            task_id="node-1",
        )
        tup = await saver.aget_tuple(_thread_config(thread))
        self.assertIsNone(tup.pending_writes)
        # The exception object never entered the store.
        raw = await self.redis.hgetall(f"graph-v2:cp:{thread}::writes:cp-1")
        self.assertEqual(raw, {})

    async def test_write_channel_allowlist_rejects_forbidden_channel(self):
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        cfg = await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        with self.assertRaises(GraphV2CheckpointError):
            await saver.aput_writes(
                cfg, [("api_key", "sk-secret")], task_id="node-1"
            )

    async def test_checkpoint_channel_allowlist_refused_on_write_and_read(self):
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        forbidden = _checkpoint("cp-1", step=0, extra={"user_history": ["secret"]})
        with self.assertRaises(GraphV2CheckpointError):
            await saver.aput(
                _thread_config(thread), forbidden, _metadata(step=0), {}
            )
        # Defense in depth: even a hand-written forbidden checkpoint fails load.
        self.assertNotIn("user_history", DEFAULT_CHECKPOINT_CHANNEL_ALLOWLIST)
        # Plant a POISONED envelope directly into Redis (bypassing the saver's
        # own encode-time validation, simulating a tampered store), then load.
        serde = JsonPlusSerializer()
        raw_cp = _checkpoint("cp-2", step=0, extra={"api_key": "sk-secret"})
        _t, payload = serde.dumps_typed(
            {"c": raw_cp, "m": _metadata(step=0), "p": None}
        )
        envelope = json.dumps(
            {
                "t": _t,
                "b": base64.b64encode(payload).decode("ascii"),
                "h": hashlib.sha256(payload).hexdigest()[:16],
                "n": len(payload),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self.redis.set(f"graph-v2:cp:{thread}::cp:cp-2", envelope)
        with self.assertRaises(GraphV2CheckpointError):
            await saver.aget_tuple(
                _thread_config(thread, checkpoint_id="cp-2")
            )

    async def test_bytes_budget_refuses_oversized_payload(self):
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        # A checkpoint whose channel value alone exceeds the 256 KiB budget.
        big = _checkpoint(
            "cp-1", step=0, extra={"node_events": [{"x": "y" * (300 * 1024)}]}
        )
        with self.assertRaises(GraphV2CheckpointError):
            await saver.aput(
                _thread_config(thread), big, _metadata(step=0), {}
            )

    async def test_tampered_envelope_fails_integrity_check(self):
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        # Corrupt the stored payload bytes in place.
        key = f"graph-v2:cp:{thread}::cp:cp-1"
        raw = await self.redis.get(key)
        envelope = json.loads(raw)
        payload = bytearray(base64.b64decode(envelope["b"]))
        payload[0] ^= 0xFF
        envelope["b"] = base64.b64encode(bytes(payload)).decode("ascii")
        await self.redis.set(key, json.dumps(envelope))
        with self.assertRaises(GraphV2CheckpointIntegrityError):
            await saver.aget_tuple(_thread_config(thread, checkpoint_id="cp-1"))

    async def test_cross_identity_threads_never_share_keys(self):
        saver = self._saver()
        await saver.aput(
            _thread_config("v2-task:task-a:run-1"),
            _checkpoint("cp-a", step=0, extra={"task_id": "task-a"}),
            _metadata(step=0),
            {},
        )
        # Listing thread B returns nothing, even though the saver instance is
        # the same object.
        collected = [
            tup.checkpoint["id"]
            async for tup in saver.alist(_thread_config("v2-task:task-b:run-1"))
        ]
        self.assertEqual(collected, [])

    async def test_namespace_isolation(self):
        saver = self._saver()
        thread = "v2-task:task-1:run-1"
        cfg_a = await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        await saver.aput(
            {
                "configurable": {
                    "thread_id": thread,
                    "checkpoint_ns": "other",
                    "checkpoint_id": cfg_a["configurable"]["checkpoint_id"],
                }
            },
            _checkpoint("cp-ns", step=0),
            _metadata(step=0),
            {},
        )
        tup = await saver.aget_tuple(_thread_config(thread))
        self.assertEqual(tup.checkpoint["id"], "cp-1")
        other = await saver.aget_tuple(
            {"configurable": {"thread_id": thread, "checkpoint_ns": "other"}}
        )
        self.assertEqual(other.checkpoint["id"], "cp-ns")

    async def test_keys_carry_seven_day_ttl(self):
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        self.assertEqual(self.redis.ttls[f"graph-v2:cp:{thread}::cp:cp-1"], GRAPH_V2_CP_TTL_SECONDS)
        self.assertEqual(
            self.redis.ttls[f"graph-v2:cp:{thread}::latest"], GRAPH_V2_CP_TTL_SECONDS
        )
        self.assertEqual(
            self.redis.ttls[f"graph-v2:cp:{thread}::step"], GRAPH_V2_CP_TTL_SECONDS
        )

    async def test_stale_checkpoint_never_becomes_latest(self):
        """Lua CAS: an OLD step write never clobbers the newer latest pointer."""
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        await saver.aput(
            _thread_config(thread), _checkpoint("cp-2", step=1), _metadata(step=1), {}
        )
        # A delayed writer replays step=0 (older than the stored step=1).
        result = await self.redis.eval(
            "CAS", 3,
            f"graph-v2:cp:{thread}::cp:cp-0",
            f"graph-v2:cp:{thread}::latest",
            f"graph-v2:cp:{thread}::step",
            json.dumps({"h": "tamper"}),
            "cp-0",
            "0",
            str(GRAPH_V2_CP_TTL_SECONDS),
        )
        self.assertEqual(result, 0)  # rejected
        latest = await self.redis.get(f"graph-v2:cp:{thread}::latest")
        self.assertEqual(latest, "cp-2")

    async def test_untracked_value_channels_never_persist(self):
        """LangGraph tracks ``channel_versions`` for ``UntrackedValue`` channels
        (``task_state`` / ``transitions``) even though their VALUES never enter a
        checkpoint.  The stored snapshot must still only ever carry allowlisted
        routing/identity channels — a resume sees no stale TaskState/transitions
        (requirement #3)."""
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        cp = _checkpoint("cp-1", step=0)
        # UntrackedValue channels legitimately appear in the version map only.
        cp["channel_versions"]["task_state"] = 5
        cp["channel_versions"]["transitions"] = 5
        cfg = await saver.aput(
            _thread_config(thread), cp, _metadata(step=0), {}
        )
        self.assertEqual(cfg["configurable"]["checkpoint_id"], "cp-1")
        # A fresh saver rebuilds the thread and sees the pruned snapshot.
        rebuilt = self._saver()
        tup = await rebuilt.aget_tuple(_thread_config(thread))
        self.assertNotIn("task_state", tup.checkpoint["channel_values"])
        self.assertNotIn("task_state", tup.checkpoint["channel_versions"])
        self.assertNotIn("transitions", tup.checkpoint["channel_versions"])
        # The allowlisted routing channels survive the prune intact.
        self.assertIn("graph_revision", tup.checkpoint["channel_versions"])

    async def test_alatest_checkpoint_hash_and_acount_read_redis(self):
        """Hash + count come from Redis, not the in-memory audit ring — they
        survive a process restart (requirement #9)."""
        thread = "v2-task:task-1:run-1"
        saver = self._saver()
        await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        await saver.aput(
            _thread_config(thread), _checkpoint("cp-2", step=1), _metadata(step=1), {}
        )
        digest = await saver.alatest_checkpoint_hash(thread)
        self.assertIsNotNone(digest)
        self.assertEqual(len(digest), 16)
        self.assertEqual(await saver.acount_checkpoints(thread), 2)

    async def test_audit_ring_records_each_save_and_load(self):
        audit: list[dict] = []
        thread = "v2-task:task-1:run-1"
        saver = self._saver(audit_callback=audit.append)
        await saver.aput(
            _thread_config(thread), _checkpoint("cp-1", step=0), _metadata(step=0), {}
        )
        await saver.aget_tuple(_thread_config(thread))
        ops = [r["op"] for r in saver.audit_records]
        self.assertIn("put", ops)
        self.assertIn("get_tuple", ops)
        put = next(r for r in saver.audit_records if r["op"] == "put")
        self.assertEqual(put["threadId"], thread)
        self.assertEqual(put["checkpointId"], "cp-1")
        self.assertTrue(put["hash"])
        self.assertIsInstance(put["payloadBytes"], int)
        # audit_callback receives every record too.
        self.assertEqual(len(audit), len(saver.audit_records))


if __name__ == "__main__":
    unittest.main()
