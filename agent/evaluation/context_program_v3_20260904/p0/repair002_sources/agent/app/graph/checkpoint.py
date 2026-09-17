"""Durable V2 checkpoint saver on plain Redis.

This is the Day-2 persistence boundary.  It is a real LangGraph
``BaseCheckpointSaver`` (so ``compile(checkpointer=...)`` / ``ainvoke`` /
``interrupt`` / ``Command(resume=...)`` all work unchanged) that stores each
checkpoint as a **full snapshot** under an independent plain-Redis namespace.

Design contract (TASK_PACKET — AGENT-ADVANCED-ARCH-DAY2-DURABLE-INTERRUPT-001):

* **Plain Redis, independent namespace/TTL.**  Keys live under
  ``graph-v2:cp:<thread>:<ns>:...`` with the same 7-day TTL as TaskState.  No
  Redis Stack, no dedicated DB, no shadow-scratch DB, no separate process.
* **Server-bound identity.**  ``thread_id`` is ``v2-task:<task_id>:<run_id>``
  (assigned by the durable runner); the saver only ever keys on what the graph
  passes in ``config``.  Cross-task threads are rejected upstream in
  ``resume.py``, not here.
* **Payload allowlist.**  Only the bounded routing/identity channels survive a
  checkpoint: task/thread identity, ``graph_revision``, ``plan_id``, the phase
  ``action``, the transition/replan budgets, the bounded redacted node-event
  summary, the ``__start__`` input stub (the durable runner never puts a full
  TaskState in graph input), and LangGraph's internal ``branch:to:*`` routing
  channels.  ``task_state`` / ``transitions`` are ``UntrackedValue`` channels
  and never reach the saver (verified by probe).  Any channel outside the
  allowlist fails the write — and a loaded checkpoint whose channels are not
  all allowlisted fails the read (defense in depth).
* **Bytes budget.**  Each checkpoint snapshot and the total pending-writes for
  a checkpoint are bounded (256 KiB raw payload).  A larger payload is a
  contract violation and is refused.
* **Hash + integrity.**  Every snapshot stores ``sha256(payload)[:16]``; a
  load recomputes and verifies it, so tampered or corrupt blobs fail closed.
  Every save/load also appends a bounded audit record (in-memory ring + an
  optional async callback) for the Day-2 evidence bundle.
* **Stale checkpoint never becomes latest.**  A per-thread Lua CAS updates the
  ``latest`` pointer only when the new checkpoint's ``step`` is not behind the
  stored one, so an old snapshot can never clobber a newer thread cursor.

The graph is single-writer per thread (server-bound run), so a full-snapshot
serde (``JsonPlusSerializer``) round-trips the whole ``Checkpoint`` without
per-channel blobs — no reconstruction walk, no missing-parent failure mode.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from ..task_state import TASK_STATE_TTL_SECONDS, _get_client

__all__ = [
    "GRAPH_V2_CP_NAMESPACE",
    "GRAPH_V2_CP_TTL_SECONDS",
    "GRAPH_V2_CP_MAX_PAYLOAD_BYTES",
    "DEFAULT_CHECKPOINT_CHANNEL_ALLOWLIST",
    "DEFAULT_WRITE_CHANNEL_ALLOWLIST",
    "GraphV2CheckpointError",
    "GraphV2CheckpointIntegrityError",
    "GraphV2CheckpointSaver",
]

# Independent key namespace under the existing plain Redis (requirement #2).
GRAPH_V2_CP_NAMESPACE = "graph-v2:cp"
GRAPH_V2_CP_TTL_SECONDS = TASK_STATE_TTL_SECONDS
# 256 KiB raw-payload budget per checkpoint snapshot and per pending-writes set.
GRAPH_V2_CP_MAX_PAYLOAD_BYTES = 256 * 1024
_GRAPH_V2_CP_MAX_WRITES_BYTES = 256 * 1024
_AUDIT_RING_LIMIT = 2000


# The only channels a durable checkpoint may carry.  Everything else (API
# keys, prompts, user history, raw ToolTrace/StepOutput, full candidates,
# evidence, dependency containers) is refused on write and on load.
DEFAULT_CHECKPOINT_CHANNEL_ALLOWLIST = frozenset(
    {
        "__start__",  # bounded initial-input stub; durable runner omits task_state
        "task_id",
        "thread_id",
        "session_owner_hash",
        "graph_revision",
        "plan_id",
        "action",
        "transition_count",
        "replan_count",
        "transition_limit_reached",
        "terminal_outcome",
        "degraded_reason",
        "last_route_decision",
        "node_events",  # bounded redacted node-event summary
        "control_policy",
        "policy_revision",
        "react_model_decision_count",
        "react_action_id",
        "react_action_kind",
        "react_answer_context_ref",
        "react_last_outcome",
    }
)
# Pending writes may additionally carry the interrupt-receipt refs LangGraph
# persists on an interrupt/resume boundary (requirement #1/#3).  ``__no_writes__``
# is LangGraph's harmless empty-marker for a node that produced no updates.
# ``__error__``/``__error_source_node__`` are NEVER stored: an exception object
# may embed non-allowlisted data, and a durable checkpoint must not serialize it.
# Dropping the error write also means a failed superstep leaves the thread at its
# last successful checkpoint, so a restart re-executes the pending node
# idempotently (requirement #8) instead of replaying a poisoned task queue.
DEFAULT_WRITE_CHANNEL_ALLOWLIST = DEFAULT_CHECKPOINT_CHANNEL_ALLOWLIST | {
    "__interrupt__",
    "__resume__",
    "__no_writes__",
}
# Channels whose pending writes the saver silently drops instead of persisting.
# ``__error__`` carries the exception object; ``__error_source_node__`` names the
# failed node — both belong to a failed superstep that restart must re-run.
_DROPPED_WRITE_CHANNELS = frozenset({"__error__", "__error_source_node__"})
_ALLOWED_CHANNEL_PREFIXES = ("branch:",)


class GraphV2CheckpointError(RuntimeError):
    """Raised when a checkpoint write or read violates the durable contract."""


class GraphV2CheckpointIntegrityError(GraphV2CheckpointError):
    """Raised when a stored checkpoint fails hash or allowlist verification."""


def _channel_allowed(
    channel: str,
    allowlist: frozenset[str],
    prefixes: tuple[str, ...] = _ALLOWED_CHANNEL_PREFIXES,
) -> bool:
    if channel in allowlist:
        return True
    return any(channel.startswith(prefix) for prefix in prefixes)


# Atomic write of the snapshot blob + monotonic ``latest`` pointer.  numkeys=3
# (the TaskState CAS script uses numkeys=1, so dispatch is unambiguous).
_CAS_CP_SCRIPT = """
-- KEYS[1]=cpkey KEYS[2]=latestkey KEYS[3]=stepkey
-- ARGV[1]=envelope JSON  ARGV[2]=checkpoint_id  ARGV[3]=step  ARGV[4]=ttl_seconds
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
local cur = redis.call('GET', KEYS[3])
if not cur or tonumber(ARGV[3]) >= tonumber(cur) then
  redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
  redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[4])
  return 1
end
return 0
"""

# Atomic pending-write append + retention.  A hard worker termination must not
# land between HSET/HINCRBY and EXPIRE, otherwise a partially written hash can
# survive forever.  The script also allocates the stable call ordinal inside
# the same Redis operation.
_PUT_WRITES_SCRIPT = """
-- GRAPH_V2_PUT_WRITES
-- KEYS[1]=writes hash
-- ARGV[1]=ttl_seconds ARGV[2]=entry_count
-- then triples: mode ('nx'|'set'), field, envelope_without_order JSON
local call_ord = redis.call('HINCRBY', KEYS[1], '__ord', 1)
local count = tonumber(ARGV[2])
for i = 0, count - 1 do
  local base = 3 + (i * 3)
  local mode = ARGV[base]
  local field = ARGV[base + 1]
  local envelope = cjson.decode(ARGV[base + 2])
  envelope['o'] = string.format('%05d:%05d', call_ord, i)
  local encoded = cjson.encode(envelope)
  if mode == 'nx' then
    redis.call('HSETNX', KEYS[1], field, encoded)
  else
    redis.call('HSET', KEYS[1], field, encoded)
  end
end
redis.call('EXPIRE', KEYS[1], ARGV[1])
return call_ord
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


class GraphV2CheckpointSaver(BaseCheckpointSaver[str]):
    """Async-native plain-Redis full-snapshot checkpointer for the V2 graph.

    The Redis client is resolved per call through ``task_state._get_client()``,
    so the durable saver shares the existing store AND honours the coroutine-
    local TaskState client override (the V2 shadow isolation boundary) without
    ever owning a process-global connection.
    """

    def __init__(
        self,
        *,
        serde: SerializerProtocol | None = None,
        namespace: str = GRAPH_V2_CP_NAMESPACE,
        ttl_seconds: int = GRAPH_V2_CP_TTL_SECONDS,
        max_payload_bytes: int = GRAPH_V2_CP_MAX_PAYLOAD_BYTES,
        channel_allowlist: frozenset[str] = DEFAULT_CHECKPOINT_CHANNEL_ALLOWLIST,
        write_allowlist: frozenset[str] = DEFAULT_WRITE_CHANNEL_ALLOWLIST,
        audit_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self.namespace = namespace
        self.ttl_seconds = int(ttl_seconds)
        self.max_payload_bytes = int(max_payload_bytes)
        self.channel_allowlist = frozenset(channel_allowlist)
        self.write_allowlist = frozenset(write_allowlist)
        self._audit_callback = audit_callback
        self._audit_ring: deque[dict[str, Any]] = deque(maxlen=_AUDIT_RING_LIMIT)

    # ── key helpers ──────────────────────────────────────────────────────────

    def _cp_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return f"{self.namespace}:{thread_id}:{ns}:cp:{checkpoint_id}"

    def _writes_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return f"{self.namespace}:{thread_id}:{ns}:writes:{checkpoint_id}"

    def _latest_key(self, thread_id: str, ns: str) -> str:
        return f"{self.namespace}:{thread_id}:{ns}:latest"

    def _step_key(self, thread_id: str, ns: str) -> str:
        return f"{self.namespace}:{thread_id}:{ns}:step"

    def _audit(
        self,
        *,
        op: str,
        thread_id: str,
        ns: str,
        checkpoint_id: str | None,
        step: int | None,
        digest: str | None,
        payload_bytes: int | None,
        channel_count: int | None,
    ) -> None:
        record = {
            "op": op,
            "threadId": thread_id,
            "ns": ns,
            "checkpointId": checkpoint_id,
            "step": step,
            "hash": digest,
            "payloadBytes": payload_bytes,
            "channelCount": channel_count,
            "ts": _utc_now(),
        }
        self._audit_ring.append(record)
        if self._audit_callback is not None:
            self._audit_callback(record)

    @property
    def audit_records(self) -> list[dict[str, Any]]:
        """Bounded in-memory checkpoint audit ring, newest last."""
        return list(self._audit_ring)

    async def alatest_checkpoint_hash(
        self, thread_id: str, ns: str = ""
    ) -> str | None:
        """Read the digest of the latest stored envelope directly from Redis.

        Unlike ``audit_records`` (in-memory, per-process), this survives a
        process restart — the evidence layer uses it to stamp restart-recovery
        node events with the real checkpoint hash (requirement #9).
        """
        client = _get_client()
        latest_id = await client.get(self._latest_key(thread_id, ns))
        if not latest_id:
            return None
        raw = await client.get(self._cp_key(thread_id, ns, latest_id))
        if not raw:
            return None
        loaded = self._decode_envelope(raw, thread_id=thread_id, ns=ns)
        digest = loaded.get("h")
        return digest if isinstance(digest, str) else None

    async def acount_checkpoints(self, thread_id: str, ns: str = "") -> int:
        """Count stored snapshot envelopes for a thread (durable across restarts)."""
        client = _get_client()
        pattern = f"{self.namespace}:{thread_id}:{ns}:cp:*"
        count = 0
        async for _ in client.scan_iter(match=pattern, count=200):
            count += 1
        return count

    # ── allowlist / budget / hash enforcement ────────────────────────────────

    def _validate_checkpoint(self, checkpoint: Checkpoint) -> None:
        for channel in checkpoint.get("channel_values", {}):
            if not _channel_allowed(channel, self.channel_allowlist):
                raise GraphV2CheckpointError(
                    f"channel {channel!r} not in durable checkpoint allowlist"
                )
        for channel in checkpoint.get("channel_versions", {}):
            if not _channel_allowed(channel, self.channel_allowlist):
                raise GraphV2CheckpointError(
                    f"versioned channel {channel!r} not in durable checkpoint allowlist"
                )

    def _validate_writes(self, writes: Sequence[tuple[str, Any]]) -> None:
        for channel, _value in writes:
            if not _channel_allowed(channel, self.write_allowlist):
                raise GraphV2CheckpointError(
                    f"pending-write channel {channel!r} not in durable allowlist"
                )

    def _hash_payload(self, payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()[:16]

    # ── envelope (str-safe under decode_responses=True clients) ──────────────

    def _sanitize_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        """Strip version bookkeeping the durable contract never persists.

        ``channel_values`` is STRICT: a genuinely persisted non-allowlisted
        channel is refused (requirement #3).  ``channel_versions`` however is
        pruned to the allowlist: LangGraph tracks a version entry for EVERY
        written channel — including ``UntrackedValue`` channels (``task_state``,
        ``transitions``) whose VALUES never enter a checkpoint.  The stored
        snapshot must only ever carry allowlisted routing/identity channels, so
        a resume never sees a stale ``task_state``/``transitions`` and durable
        nodes always rehydrate the LIVE TaskState from the real store.
        """
        for channel in checkpoint.get("channel_values", {}):
            if not _channel_allowed(channel, self.channel_allowlist):
                raise GraphV2CheckpointError(
                    f"channel {channel!r} not in durable checkpoint allowlist"
                )
        return {
            **checkpoint,
            "channel_versions": {
                k: v for k, v in checkpoint.get("channel_versions", {}).items()
                if _channel_allowed(k, self.channel_allowlist)
            },
        }

    def _encode_envelope(
        self, *, checkpoint: Checkpoint, metadata: CheckpointMetadata, parent_id: str | None
    ) -> tuple[str, int, str]:
        """Serialize a full-snapshot envelope to an allowlist-checked JSON str.

        Returns ``(envelope_json, payload_bytes, digest)``.  ``payload_bytes``
        is the RAW serde payload size used for the bytes budget; the envelope
        itself base64-encodes it so the shared ``decode_responses=True`` client
        round-trips binary safely.
        """
        checkpoint = self._sanitize_checkpoint(checkpoint)
        self._validate_checkpoint(checkpoint)
        type_name, payload = self.serde.dumps_typed(
            {"c": checkpoint, "m": metadata, "p": parent_id}
        )
        if len(payload) > self.max_payload_bytes:
            raise GraphV2CheckpointError(
                f"checkpoint payload {len(payload)}B exceeds budget "
                f"{self.max_payload_bytes}B"
            )
        digest = self._hash_payload(payload)
        envelope = {
            "t": type_name,
            "b": _b64(payload),
            "h": digest,
            "n": len(payload),
        }
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), len(payload), digest

    def _decode_envelope(self, raw: str, *, thread_id: str, ns: str) -> dict[str, Any]:
        """Decode + integrity-verify one stored envelope, returning ``{c,m,p}``."""
        try:
            envelope = json.loads(raw)
            payload = _b64decode(envelope["b"])
            digest = self._hash_payload(payload)
            if digest != envelope["h"]:
                raise GraphV2CheckpointIntegrityError(
                    f"checkpoint hash mismatch for thread={thread_id} ns={ns!r}"
                )
            loaded = self.serde.loads_typed((envelope["t"], payload))
        except GraphV2CheckpointIntegrityError:
            raise
        except Exception as exc:  # noqa: BLE001 - any decode failure is fatal
            raise GraphV2CheckpointIntegrityError(
                f"checkpoint decode failed for thread={thread_id} ns={ns!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        checkpoint = loaded.get("c")
        if not isinstance(checkpoint, dict):
            raise GraphV2CheckpointIntegrityError(
                f"checkpoint envelope missing checkpoint for thread={thread_id}"
            )
        self._validate_checkpoint(checkpoint)
        # Carry the verified digest through so callers can record it.
        loaded["h"] = envelope.get("h")
        return loaded

    # ── config reconstruction ────────────────────────────────────────────────

    @staticmethod
    def _parent_config(thread_id: str, ns: str, parent_id: str | None) -> RunnableConfig | None:
        if not parent_id:
            return None
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": parent_id,
            }
        }

    @staticmethod
    def _thread_ns(config: RunnableConfig) -> tuple[str, str]:
        configurable = config.get("configurable", {})
        return (
            configurable.get("thread_id", ""),
            configurable.get("checkpoint_ns", ""),
        )

    # ── async persistence ────────────────────────────────────────────────────

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, ns = self._thread_ns(config)
        if not thread_id:
            raise GraphV2CheckpointError("aput requires configurable.thread_id")
        parent_id = config.get("configurable", {}).get("checkpoint_id")
        envelope, payload_bytes, digest = self._encode_envelope(
            checkpoint=checkpoint,
            metadata=get_checkpoint_metadata(config, metadata),
            parent_id=parent_id,
        )
        checkpoint_id = checkpoint["id"]
        step = int(metadata.get("step", 0))
        client = _get_client()
        cp_key = self._cp_key(thread_id, ns, checkpoint_id)
        await client.eval(
            _CAS_CP_SCRIPT,
            3,
            cp_key,
            self._latest_key(thread_id, ns),
            self._step_key(thread_id, ns),
            envelope,
            checkpoint_id,
            str(step),
            self.ttl_seconds,
        )
        self._audit(
            op="put",
            thread_id=thread_id,
            ns=ns,
            checkpoint_id=checkpoint_id,
            step=step,
            digest=digest,
            payload_bytes=payload_bytes,
            channel_count=len(checkpoint.get("channel_values", {})),
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, ns = self._thread_ns(config)
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id:
            raise GraphV2CheckpointError("aput_writes requires configurable.checkpoint_id")
        # Drop failed-superstep error markers before validation: an exception
        # object must never enter a durable checkpoint, and a dropped error
        # write leaves the thread at its last successful checkpoint so a
        # restart re-executes the pending node idempotently.  Writes are
        # ``(channel, value)`` pairs, so the channel is ``w[0]``.
        writes = [w for w in writes if w[0] not in _DROPPED_WRITE_CHANNELS]
        if not writes:
            return
        self._validate_writes(writes)
        client = _get_client()
        writes_key = self._writes_key(thread_id, ns, checkpoint_id)
        total_bytes = 0
        script_args: list[str | int] = [self.ttl_seconds, len(writes)]
        for within, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, within)
            type_name, blob = self.serde.dumps_typed(
                (task_id, channel, value, task_path)
            )
            total_bytes += len(blob)
            if total_bytes > _GRAPH_V2_CP_MAX_WRITES_BYTES:
                raise GraphV2CheckpointError(
                    f"pending-writes payload exceeds {_GRAPH_V2_CP_MAX_WRITES_BYTES}B "
                    f"for checkpoint {checkpoint_id}"
                )
            # Field keyed by (task_id, idx) exactly like InMemorySaver: regular
            # writes dedup across calls, interrupt/resume special writes (idx<0)
            # always overwrite.  The value carries the stable order ordinal.
            field = f"{task_id}:{idx}"
            encoded = json.dumps(
                {
                    "v": 1,
                    "t": type_name,
                    "b": _b64(blob),
                    "n": len(blob),
                    "h": hashlib.sha256(blob).hexdigest(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            script_args.extend(("nx" if idx >= 0 else "set", field, encoded))
        # Pending writes belong to the same checkpoint retention domain.  A
        # hash without an expiry would outlive the snapshot/latest pointers and
        # leak stale interrupt/resume material indefinitely.  The Lua boundary
        # is one atomic Redis operation, including ordinal allocation.
        await client.eval(
            _PUT_WRITES_SCRIPT, 1, writes_key, *script_args
        )
        self._audit(
            op="put_writes",
            thread_id=thread_id,
            ns=ns,
            checkpoint_id=checkpoint_id,
            step=None,
            digest=None,
            payload_bytes=total_bytes,
            channel_count=len(writes),
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, ns = self._thread_ns(config)
        if not thread_id:
            return None
        client = _get_client()
        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id is None:
            checkpoint_id = await client.get(self._latest_key(thread_id, ns))
            if checkpoint_id is None:
                return None
        raw = await client.get(self._cp_key(thread_id, ns, checkpoint_id))
        if raw is None:
            return None
        loaded = self._decode_envelope(raw, thread_id=thread_id, ns=ns)
        checkpoint: Checkpoint = loaded["c"]
        metadata: CheckpointMetadata = loaded["m"]
        parent_id: str | None = loaded.get("p")
        if checkpoint.get("id") != checkpoint_id:
            raise GraphV2CheckpointIntegrityError(
                f"checkpoint id mismatch for thread={thread_id} ns={ns!r}"
            )
        pending_writes = await self._load_writes(
            client, thread_id, ns, checkpoint_id
        )
        self._audit(
            op="get_tuple",
            thread_id=thread_id,
            ns=ns,
            checkpoint_id=checkpoint_id,
            step=metadata.get("step"),
            digest=loaded.get("h"),
            payload_bytes=len(raw),
            channel_count=len(checkpoint.get("channel_values", {})),
        )
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=self._parent_config(thread_id, ns, parent_id),
            pending_writes=pending_writes,
        )

    async def _load_writes(
        self,
        client: Any,
        thread_id: str,
        ns: str,
        checkpoint_id: str,
    ) -> list[PendingWrite] | None:
        fields = await client.hgetall(self._writes_key(thread_id, ns, checkpoint_id))
        if not fields:
            return None
        writes: list[PendingWrite] = []
        total_bytes = 0
        for field, encoded in fields.items():
            if field == "__ord":
                continue
            try:
                envelope = json.loads(encoded)
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("v") != 1
                    or type(envelope.get("n")) is not int
                    or not isinstance(envelope.get("h"), str)
                    or len(envelope["h"]) != 64
                    or not isinstance(envelope.get("t"), str)
                    or not isinstance(envelope.get("b"), str)
                    or not isinstance(envelope.get("o"), str)
                ):
                    raise ValueError("pending-write envelope shape")
                blob = _b64decode(envelope["b"])
                if envelope["n"] != len(blob):
                    raise ValueError("pending-write byte count")
                if hashlib.sha256(blob).hexdigest() != envelope["h"]:
                    raise ValueError("pending-write hash")
                total_bytes += len(blob)
                if total_bytes > _GRAPH_V2_CP_MAX_WRITES_BYTES:
                    raise ValueError("pending-write bytes budget")
                tid, channel, value, _task_path = self.serde.loads_typed(
                    (envelope["t"], blob)
                )
                if (
                    not isinstance(tid, str)
                    or not isinstance(channel, str)
                    or not _channel_allowed(channel, self.write_allowlist)
                ):
                    raise ValueError("pending-write identity or channel")
            except Exception as exc:
                raise GraphV2CheckpointIntegrityError(
                    f"pending-write integrity failed for thread={thread_id} "
                    f"ns={ns!r} checkpoint={checkpoint_id}: {type(exc).__name__}"
                ) from exc
            writes.append((envelope["o"], tid, channel, value))
        writes.sort(key=lambda item: item[0])
        return [(tid, channel, value) for _o, tid, channel, value in writes] or None

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            # Global list-all is not supported: thread ids are embedded in the
            # Redis keys, so a config-less scan cannot recover thread/ns without
            # ambiguity.  The graph, the durable runner and the evidence tools
            # always list by thread config.
            raise GraphV2CheckpointError(
                "GraphV2CheckpointSaver.list(config=None) is not supported; "
                "pass a thread config."
            )
        thread_id, ns = self._thread_ns(config)
        before_id = get_checkpoint_id(before) if before else None
        latest_id = await _get_client().get(self._latest_key(thread_id, ns))
        cursor_id = latest_id
        yielded = 0
        while cursor_id is not None:
            if before_id is not None and cursor_id >= before_id:
                cursor_id = None
                continue
            tup = await self.aget_tuple(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": cursor_id,
                    }
                }
            )
            if tup is None:
                break
            if filter is None or all(
                query_value == tup.metadata.get(query_key)
                for query_key, query_value in filter.items()
            ):
                yield tup
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            cursor_id = (
                tup.parent_config.get("configurable", {}).get("checkpoint_id")
                if tup.parent_config
                else None
            )

    async def adelete_thread(self, thread_id: str) -> None:
        client = _get_client()
        pattern = f"{self.namespace}:{thread_id}:*"
        async for key in client.scan_iter(match=pattern, count=200):
            await client.delete(key)
        self._audit(
            op="delete_thread",
            thread_id=thread_id,
            ns="",
            checkpoint_id=None,
            step=None,
            digest=None,
            payload_bytes=None,
            channel_count=None,
        )

    # ── sync delegates (single-shot, used by sync invoke/tooling) ────────────

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        import asyncio

        return asyncio.run(
            self.aput(config, checkpoint, metadata, new_versions)
        )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        import asyncio

        asyncio.run(self.aput_writes(config, writes, task_id, task_path))

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        import asyncio

        return asyncio.run(self.aget_tuple(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        import asyncio

        collected: list[CheckpointTuple] = []
        async def _collect() -> None:
            async for item in self.alist(
                config, filter=filter, before=before, limit=limit
            ):
                collected.append(item)

        asyncio.run(_collect())
        return iter(collected)

    def delete_thread(self, thread_id: str) -> None:
        import asyncio

        asyncio.run(self.adelete_thread(thread_id))

    def get_next_version(self, current: str | None, channel: None) -> str:
        # Match InMemorySaver's monotonically increasing string versions so
        # channel_versions/versions_seen ordering is well-defined.
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        return f"{current_v + 1:032}.0000000000000000"

    def checkpoint_fingerprint(self, checkpoint: Checkpoint) -> str:
        """Stable short digest of one checkpoint's identity-bearing content."""
        if not isinstance(checkpoint, dict):
            return ""
        _t, payload = self.serde.dumps_typed(
            {
                "id": checkpoint.get("id"),
                "v": checkpoint.get("v"),
                "channel_versions": checkpoint.get("channel_versions"),
            }
        )
        return self._hash_payload(payload)
