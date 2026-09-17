"""Server-owned cooperative pause control for durable Graph V2 runs.

A pause request never interrupts a tool halfway through.  Durable graph nodes
check the request immediately before their next effect; the runner then returns
the latest confirmed LangGraph checkpoint as the restart boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..task_state import TASK_STATE_TTL_SECONDS, _get_client
from .runtime import CONTROL_POLICY_REVISIONS, GraphV2Runtime

PAUSE_KEY_PREFIX = "graph-v2:pause"

_REQUEST_LUA = r"""
-- PAUSE_REQUEST_CAS
local raw = redis.call('GET', KEYS[1])
if raw then
  local current = cjson.decode(raw)
  if current['runId'] == ARGV[1]
    and current['threadId'] == ARGV[2]
    and current['sessionOwnerHash'] == ARGV[3]
    and current['controlPolicy'] == ARGV[4]
    and current['policyRevision'] == ARGV[5] then
    return raw
  end
  -- A request can arrive after its run has already crossed the final node.
  -- Such an unconfirmed request cannot own a checkpoint and must not block a
  -- later run for the same task.  Confirmed paused/resuming receipts remain
  -- immutable and therefore still fail closed below.
  if current['state'] == 'pause_requested' then
    redis.call('SET', KEYS[1], ARGV[6], 'EX', ARGV[7])
    return ARGV[6]
  end
  return ''
end
redis.call('SET', KEYS[1], ARGV[6], 'EX', ARGV[7])
return ARGV[6]
"""

_TRANSITION_LUA = r"""
-- PAUSE_TRANSITION_CAS
local raw = redis.call('GET', KEYS[1])
if not raw then return '' end
if raw == ARGV[2] then return raw end
if raw ~= ARGV[1] then return '' end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return ARGV[2]
"""

_CLEAR_LUA = r"""
-- PAUSE_CLEAR_CAS
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local current = cjson.decode(raw)
if current['requestId'] ~= ARGV[1] or current['state'] ~= ARGV[2] then
  return 0
end
return redis.call('DEL', KEYS[1])
"""


class GraphPauseRequested(RuntimeError):
    """Raised before a durable node starts when its run has a pause request."""

    def __init__(self, receipt: dict[str, Any]):
        super().__init__("durable graph pause requested")
        self.receipt = dict(receipt)


def _pause_key(task_id: str) -> str:
    return f"{PAUSE_KEY_PREFIX}:{task_id}"


def _owner_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def request_graph_pause(
    *,
    task_id: str,
    session_id: str,
    run_id: str,
    thread_id: str,
    control_policy: str,
) -> dict[str, Any]:
    """Create or return the idempotent pause request for the active run."""

    owner = _owner_hash(session_id)
    receipt = {
        "requestId": f"pause-{uuid.uuid4().hex[:12]}",
        "state": "pause_requested",
        "taskId": task_id,
        "runId": run_id,
        "threadId": thread_id,
        "sessionOwnerHash": owner,
        "controlPolicy": control_policy,
        "policyRevision": CONTROL_POLICY_REVISIONS.get(control_policy),
        "requestedAt": _now(),
    }
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    raw = await _get_client().eval(
        _REQUEST_LUA,
        1,
        _pause_key(task_id),
        run_id,
        thread_id,
        owner,
        control_policy,
        str(CONTROL_POLICY_REVISIONS.get(control_policy) or ""),
        encoded,
        str(TASK_STATE_TTL_SECONDS),
    )
    if not raw:
        raise RuntimeError("pause_request_conflict")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("pause_request_invalid_receipt")
    return value


async def read_graph_pause(task_id: str) -> dict[str, Any] | None:
    raw = await _get_client().get(_pause_key(task_id))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def public_pause_receipt(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    return {
        key: receipt.get(key)
        for key in (
            "requestId",
            "state",
            "taskId",
            "runId",
            "threadId",
            "controlPolicy",
            "policyRevision",
            "requestedAt",
            "confirmedAt",
            "liveRevision",
            "checkpointRevision",
            "checkpointHash",
            "checkpointCount",
            "pausedBeforeNode",
        )
        if receipt.get(key) is not None
    }


async def matching_pause_request(
    *, node_name: str, runtime: GraphV2Runtime
) -> dict[str, Any] | None:
    """Return a request only when every server-bound run identity matches."""

    if not runtime.durable or not runtime.task_id:
        return None
    receipt = await read_graph_pause(runtime.task_id)
    if not isinstance(receipt, dict) or receipt.get("state") != "pause_requested":
        return None
    if (
        receipt.get("runId") != runtime.run_id
        or receipt.get("threadId") != runtime.thread_id
        or receipt.get("sessionOwnerHash") != runtime.session_owner_hash
        or receipt.get("controlPolicy") != runtime.control_policy
        or receipt.get("policyRevision")
        != CONTROL_POLICY_REVISIONS.get(runtime.control_policy)
    ):
        return None
    return {**receipt, "pausedBeforeNode": node_name}


async def confirm_graph_pause(
    receipt: dict[str, Any],
    *,
    live_revision: int,
    checkpoint_revision: int | None,
    checkpoint_hash: str,
    checkpoint_count: int,
) -> dict[str, Any]:
    confirmed = {
        **receipt,
        "state": "paused",
        "confirmedAt": _now(),
        "liveRevision": live_revision,
        "checkpointRevision": checkpoint_revision,
        "checkpointHash": checkpoint_hash,
        "checkpointCount": checkpoint_count,
    }
    requested = {key: value for key, value in receipt.items() if key != "pausedBeforeNode"}
    encoded = json.dumps(confirmed, ensure_ascii=False, sort_keys=True)
    raw = await _get_client().eval(
        _TRANSITION_LUA,
        1,
        _pause_key(str(receipt["taskId"])),
        json.dumps(requested, ensure_ascii=False, sort_keys=True),
        encoded,
        str(TASK_STATE_TTL_SECONDS),
    )
    if not raw:
        raise RuntimeError("pause_confirm_conflict")
    return json.loads(raw)


async def begin_graph_pause_resume(
    receipt: dict[str, Any], *, client_receipt: dict[str, Any]
) -> dict[str, Any]:
    """Atomically bind a client-seen paused receipt before graph restart."""

    identity_fields = (
        "requestId", "taskId", "runId", "threadId",
        "controlPolicy", "policyRevision", "checkpointHash",
        "checkpointCount", "liveRevision", "checkpointRevision",
        "requestedAt", "confirmedAt", "pausedBeforeNode",
    )
    if receipt.get("state") not in {"paused", "resuming"} or any(
        receipt.get(key) != client_receipt.get(key) for key in identity_fields
    ):
        raise RuntimeError("pause_resume_receipt_mismatch")
    resuming = (
        dict(receipt)
        if receipt.get("state") == "resuming"
        else {**receipt, "state": "resuming", "resumeStartedAt": _now()}
    )
    encoded = json.dumps(resuming, ensure_ascii=False, sort_keys=True)
    raw = await _get_client().eval(
        _TRANSITION_LUA,
        1,
        _pause_key(str(receipt["taskId"])),
        json.dumps(receipt, ensure_ascii=False, sort_keys=True),
        encoded,
        str(TASK_STATE_TTL_SECONDS),
    )
    if not raw:
        raise RuntimeError("pause_resume_transition_conflict")
    return json.loads(raw)


async def clear_graph_pause(
    *, task_id: str, request_id: str, expected_state: str = "resuming"
) -> bool:
    return bool(await _get_client().eval(
        _CLEAR_LUA,
        1,
        _pause_key(task_id),
        request_id,
        expected_state,
    ))


__all__ = [
    "GraphPauseRequested",
    "begin_graph_pause_resume",
    "clear_graph_pause",
    "confirm_graph_pause",
    "matching_pause_request",
    "public_pause_receipt",
    "read_graph_pause",
    "request_graph_pause",
]
