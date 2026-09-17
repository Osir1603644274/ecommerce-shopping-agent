"""Durable V2 runner — Day-2 checkpoint / interrupt / resume / restart.

``run_graph_v2_durable(...)`` is the single server-bound entry for the durable
control plane built in DAY2:

* **fresh** — assign a new thread (``v2-task:<task_id>:<run_id>``), persist the
  per-task thread cursor (``graph-v2:cursor:<task_id>``) and the same-run
  ``v2RunMarker`` into TaskState, then ``ainvoke(initial_input)``.  The initial
  input carries ONLY server-bound identity + routing/budget channels — never a
  full TaskState (that stays ``UntrackedValue`` and is rehydrated by every node).
* **resume** — validate the client payload (task / thread / interrupt /
  revision / proposal hash) against the PARKED checkpoint, then
  ``ainvoke(Command(resume=answer))`` on the SAME thread.  Empty, forged,
  cross-task, expired, revision-mismatched and different-payload requests fail
  closed.  Replaying the EXACT already-applied answer returns the stored
  receipt without invoking the graph — no extra revision/model/tool
  (requirement #5).
* **restart** — process-restart recovery: read the per-task thread cursor and
  ``ainvoke(None)`` to continue the pending nodes.  If the process died before
  the first checkpoint (no checkpoint exists yet), re-run the deterministic
  initial input idempotently.  On the dangerous window (search executed + the
  TaskState receipt persisted but the next checkpoint not confirmed) the
  executor's skip guard rehydrates the live TaskState, reconciles
  fast_forward and routes to the Validator with zero extra tools.

Every durable invocation stamps the redacted node events with the server-owned
thread/task identity and the latest confirmed checkpoint hash (requirement #9),
and emits durable control events through the SAME redacted event source.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langgraph.types import Command
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

from ..agent_trace import TraceBuilder
from ..context_view import ContextProjector
from ..executor import ToolCaller
from ..harness import HarnessStepResult
from ..tool_execution_v2 import (
    LegacyToolCallerForbidden,
    ToolCallerV2,
    ToolInboxCallerV2,
)
from ..task_state import (
    TASK_STATE_TTL_SECONDS,
    TaskState,
    TaskStateNotFoundError,
    TaskStatePatchRequest,
    _get_client,
    get_task_state,
    update_task_state,
)
from ..settings import settings
from .builder import build_graph_v2_durable
from .checkpoint import GraphV2CheckpointSaver
from .tool_inbox_v2 import ToolInbox
from .nodes.clarification import _MAX_ANSWER_CHARS, _validate_answer
from .nodes.executor import ExecutorFaultInjected
from .nodes.validator import ValidatorFaultInjected
from .pause_control import (
    GraphPauseRequested,
    begin_graph_pause_resume,
    clear_graph_pause,
    confirm_graph_pause,
    read_graph_pause,
)
from .runtime import CONTROL_POLICY_REVISIONS, GraphV2Runtime, ProjectorFactory
from .state import GraphV2NodeEvent, GraphV2State

__all__ = [
    "THREAD_ID_PREFIX",
    "CURSOR_KEY_PREFIX",
    "TERMINAL_RESPONSE_KEY_PREFIX",
    "ResumePayload",
    "DurableRunResult",
    "DurableResumeRejected",
    "build_thread_id",
    "parse_thread_id",
    "session_owner_hash",
    "read_task_cursor",
    "write_task_cursor",
    "is_exact_resolved_resume",
    "read_terminal_response_receipt",
    "write_terminal_response_receipt",
    "resolve_durable_identity",
    "run_graph_v2_durable",
]

# Server-bound identity (requirement #2): thread/session/task/namespace are
# assigned by the durable runner; the model/client can neither pick nor reuse
# an identity across tasks.
THREAD_ID_PREFIX = "v2-task:"
CURSOR_KEY_PREFIX = "graph-v2:cursor"
# A terminal answer is deliberately kept outside TaskState/checkpoints. It is
# user-visible output, not planning input; storing it in either would make a
# later graph node see historical answer text it is not authorized to use.
TERMINAL_RESPONSE_KEY_PREFIX = "graph-v2:terminal"
_MAX_TERMINAL_RESPONSE_CHARS = 24_000
# The single code source for durable control events in the redacted event source.
_DURABLE_CODE_SOURCE = "agent/app/graph/resume.py"
def _policy_revision(control_policy: str) -> str:
    try:
        return CONTROL_POLICY_REVISIONS[control_policy]
    except KeyError as exc:
        raise ValueError("unknown durable control policy") from exc


# ── exceptions ────────────────────────────────────────────────────────────────


class DurableResumeRejected(RuntimeError):
    """A resume payload failed fail-closed validation (requirement #5).

    ``reason`` is a short machine-readable code; the surrounding request turns
    it into a user-facing message and a rejected observation event.
    """

    code = "resume_rejected"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── identity / cursor ─────────────────────────────────────────────────────────


def build_thread_id(task_id: str, run_id: str) -> str:
    """Thread ids are server-bound: ``v2-task:<task_id>:<run_id>``."""
    return f"{THREAD_ID_PREFIX}{task_id}:{run_id}"


def parse_thread_id(thread_id: str) -> tuple[str, str] | None:
    """Return ``(task_id, run_id)`` when the thread id is well-formed."""
    if not thread_id.startswith(THREAD_ID_PREFIX):
        return None
    rest = thread_id[len(THREAD_ID_PREFIX):]
    task_id, sep, run_id = rest.rpartition(":")
    if not sep or not task_id or not run_id:
        return None
    return task_id, run_id


def cursor_key(task_id: str) -> str:
    return f"{CURSOR_KEY_PREFIX}:{task_id}"


def _terminal_response_key(task_id: str, run_id: str, proposal_hash: str) -> str:
    """Use a bounded opaque Redis key; raw client-provided ids never form it."""
    identity = "\x00".join((task_id, run_id, proposal_hash)).encode("utf-8")
    return f"{TERMINAL_RESPONSE_KEY_PREFIX}:{hashlib.sha256(identity).hexdigest()}"


def session_owner_hash(session_id: str) -> str:
    """Non-reversible owner binding safe for durable metadata."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _require_session_owner(live: TaskState, request_session_id: str | None) -> str:
    """Reject before checkpoint/graph access unless the request owns the task."""
    if not isinstance(request_session_id, str) or not request_session_id:
        raise DurableResumeRejected("session_missing")
    if not isinstance(live.session_id, str) or not live.session_id:
        raise DurableResumeRejected("task_session_missing")
    if live.session_id != request_session_id:
        raise DurableResumeRejected("cross_session")
    return session_owner_hash(request_session_id)


async def read_task_cursor(task_id: str) -> dict[str, Any] | None:
    """Read the per-task durable thread cursor (None when absent/expired)."""
    raw = await _get_client().get(cursor_key(task_id))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not value.get("threadId"):
        return None
    return value


async def write_task_cursor(
    task_id: str, *, run_id: str, thread_id: str, revision: int,
    session_owner_hash_value: str,
    control_policy: str = "fixed_v1",
) -> None:
    """Persist the per-task thread cursor with the same TTL as TaskState."""
    payload = {
        "runId": run_id,
        "threadId": thread_id,
        "revision": revision,
        "sessionOwnerHash": session_owner_hash_value,
        "controlPolicy": control_policy,
        "policyRevision": _policy_revision(control_policy),
        "startedAt": datetime.now(timezone.utc).isoformat(),
    }
    await _get_client().set(
        cursor_key(task_id),
        json.dumps(payload, ensure_ascii=False),
        ex=TASK_STATE_TTL_SECONDS,
    )


async def resolve_durable_identity(
    task_id: str,
    *,
    resume: dict[str, Any] | None = None,
    restart: bool = False,
    session_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve ``(run_id, thread_id)`` for an existing durable run.

    Returns ``(None, None)`` for a brand-new run.  Used by the request layer to
    build the TraceBuilder with the SAME run_id across a resume/restart.
    """
    if resume is not None and isinstance(resume, dict):
        return resume.get("runId"), resume.get("threadId")
    if restart:
        cursor = await read_task_cursor(task_id)
        if (
            cursor is not None
            and isinstance(session_id, str)
            and session_id
            and cursor.get("sessionOwnerHash") == session_owner_hash(session_id)
            and isinstance(cursor.get("runId"), str)
            and isinstance(cursor.get("threadId"), str)
            and parse_thread_id(cursor["threadId"]) == (task_id, cursor["runId"])
        ):
            return cursor.get("runId"), cursor.get("threadId")
    return None, None


# ── resume payload validation ────────────────────────────────────────────────


def _answer_hash(answer: str) -> str:
    return hashlib.sha256(answer.strip().encode("utf-8")).hexdigest()[:16]


def _terminal_result_hash(answer: str) -> str:
    return hashlib.sha256(answer.encode("utf-8")).hexdigest()


def is_exact_resolved_resume(
    state: TaskState,
    raw_payload: dict[str, Any] | None,
    *,
    session_id: str | None,
) -> bool:
    """Side-effect-free preflight for an already-resolved resume.

    This only suppresses request-history loading/writing. The durable runner
    repeats its full cursor/checkpoint validation before it returns a result.
    """
    if not isinstance(raw_payload, dict) or not isinstance(session_id, str) or not session_id:
        return False
    try:
        payload = ResumePayload.from_dict(raw_payload, server_task_id=state.task_id)
    except DurableResumeRejected:
        return False
    parsed = parse_thread_id(payload.thread_id)
    if (
        payload.task_id != state.task_id
        or parsed is None
        or parsed != (state.task_id, payload.run_id)
    ):
        return False
    receipt = (state.domain_state or {}).get("v2PendingClarification")
    return bool(
        isinstance(receipt, dict)
        and receipt.get("taskId") == state.task_id
        and receipt.get("runId") == payload.run_id
        and receipt.get("threadId") == payload.thread_id
        and receipt.get("sessionOwnerHash") == session_owner_hash(session_id)
        and receipt.get("controlPolicy") in CONTROL_POLICY_REVISIONS
        and receipt.get("policyRevision")
        == CONTROL_POLICY_REVISIONS.get(receipt.get("controlPolicy"))
        and receipt.get("proposalHash") == payload.proposal_hash
        and receipt.get("answerHash") == _answer_hash(payload.answer)
    )


async def write_terminal_response_receipt(
    *,
    task_id: str,
    run_id: str,
    thread_id: str,
    proposal_hash: str,
    session_id: str,
    answer: str,
    state_revision: int | None = None,
    base_task_revision: int | None = None,
    control_policy: str | None = None,
) -> bool:
    """Persist the bounded, server-owned final answer for an exact replay."""
    prepared = prepare_terminal_response_receipt(
        task_id=task_id,
        run_id=run_id,
        thread_id=thread_id,
        proposal_hash=proposal_hash,
        session_id=session_id,
        answer=answer,
        state_revision=state_revision,
        base_task_revision=base_task_revision,
        control_policy=control_policy,
    )
    if prepared is None:
        return False
    key, payload = prepared
    try:
        client = _get_client()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        stored = await client.set(
            key,
            encoded,
            nx=True,
            ex=TASK_STATE_TTL_SECONDS,
        )
        if stored:
            return True
        raw_existing = await client.get(key)
        existing = json.loads(raw_existing) if raw_existing else None
        if not isinstance(existing, dict) or any(
            existing.get(field) != value
            for field, value in payload.items()
        ):
            return False
    except Exception:  # noqa: BLE001 - caller handles the fail-closed replay
        logger.warning("unable to persist durable terminal response receipt", exc_info=True)
        return False
    return True


def prepare_terminal_response_receipt(
    *,
    task_id: str,
    run_id: str,
    thread_id: str,
    proposal_hash: str,
    session_id: str,
    answer: str,
    state_revision: int | None = None,
    base_task_revision: int | None = None,
    control_policy: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Build the immutable outbox record used by the atomic TaskState CAS."""
    if (
        not all(isinstance(value, str) and value for value in (
            task_id, run_id, thread_id, proposal_hash, session_id, answer,
        ))
        or not answer.strip()
        or len(answer) > _MAX_TERMINAL_RESPONSE_CHARS
        or parse_thread_id(thread_id) != (task_id, run_id)
    ):
        return False
    payload = {
        "version": 1,
        "taskId": task_id,
        "runId": run_id,
        "threadId": thread_id,
        "proposalHash": proposal_hash,
        "sessionOwnerHash": session_owner_hash(session_id),
        "answerHash": _answer_hash(answer),
        "resultHash": _terminal_result_hash(answer),
        "answer": answer,
    }
    if state_revision is not None:
        if type(state_revision) is not int or state_revision < 1:
            return False
        payload["stateRevision"] = state_revision
    if base_task_revision is not None:
        if type(base_task_revision) is not int or base_task_revision < 1:
            return False
        payload["baseTaskRevision"] = base_task_revision
    if control_policy is not None:
        if control_policy not in CONTROL_POLICY_REVISIONS:
            return False
        payload["controlPolicy"] = control_policy
        payload["policyRevision"] = CONTROL_POLICY_REVISIONS[control_policy]
    return _terminal_response_key(task_id, run_id, proposal_hash), payload


async def read_terminal_response_receipt(
    *,
    task_id: str,
    run_id: str,
    thread_id: str,
    proposal_hash: str,
    session_id: str,
    task_state: TaskState,
) -> dict[str, str] | None:
    """Read an identity-validated final-answer receipt for an exact replay."""
    if (
        not all(isinstance(value, str) and value for value in (
            task_id, run_id, thread_id, proposal_hash, session_id,
        ))
        or parse_thread_id(thread_id) != (task_id, run_id)
    ):
        return None
    try:
        raw = await _get_client().get(_terminal_response_key(task_id, run_id, proposal_hash))
        value = json.loads(raw) if raw else None
    except (TypeError, json.JSONDecodeError):
        return None
    except Exception:  # noqa: BLE001 - fail closed on store failure
        logger.warning("unable to read durable terminal response receipt", exc_info=True)
        return None
    if not isinstance(value, dict):
        return None
    answer = value.get("answer")
    finalization = (task_state.domain_state or {}).get("v2FinalAnswerReceipt")
    expected = {
        "taskId": task_id,
        "runId": run_id,
        "threadId": thread_id,
        "proposalHash": proposal_hash,
        "sessionOwnerHash": session_owner_hash(session_id),
    }
    if (
        value.get("version") != 1
        or not isinstance(answer, str)
        or not answer
        or len(answer) > _MAX_TERMINAL_RESPONSE_CHARS
        or any(value.get(key) != expected_value for key, expected_value in expected.items())
        or value.get("answerHash") != _answer_hash(answer)
        or value.get("resultHash") != _terminal_result_hash(answer)
        or not isinstance(finalization, dict)
        or task_state.task_id != task_id
        or finalization.get("taskId") != task_id
        or finalization.get("runId") != run_id
        or finalization.get("threadId") != thread_id
        or finalization.get("publicationId") != proposal_hash
        or finalization.get("sessionOwnerHash") != session_owner_hash(session_id)
        or value.get("stateRevision") != finalization.get("finalizationRevision")
        or value.get("baseTaskRevision") != finalization.get("baseTaskRevision")
        or value.get("controlPolicy") != finalization.get("controlPolicy")
        or value.get("policyRevision") != finalization.get("policyRevision")
        or value.get("resultHash") != finalization.get("answerSha256")
    ):
        return None
    return {
        "answer": answer,
        "answerHash": value["answerHash"],
        "resultHash": value["resultHash"],
    }


@dataclass(frozen=True)
class ResumePayload:
    """A validated, server-bound resume request (requirement #5).

    The client echoes the server-owned fields from the parked interrupt payload;
    any field the client could forge (task/thread/revision/proposal hash) is
    re-verified against the real store below.
    """

    answer: str
    task_id: str
    run_id: str
    thread_id: str
    revision: int
    proposal_hash: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, server_task_id: str) -> "ResumePayload":
        if not isinstance(raw, dict) or not raw.get("answer"):
            raise DurableResumeRejected("empty_answer")
        answer = str(raw["answer"]).strip()
        if not answer:
            raise DurableResumeRejected("empty_answer")
        if len(answer) > _MAX_ANSWER_CHARS:
            raise DurableResumeRejected("answer_too_long")
        task_id = str(raw.get("taskId") or server_task_id)
        run_id = str(raw.get("runId") or "")
        thread_id = str(raw.get("threadId") or "")
        proposal_hash = str(raw.get("proposalHash") or "")
        try:
            revision = int(raw.get("revision"))
        except (TypeError, ValueError):
            raise DurableResumeRejected("revision_missing") from None
        return cls(
            answer=answer,
            task_id=task_id,
            run_id=run_id,
            thread_id=thread_id,
            revision=revision,
            proposal_hash=proposal_hash,
        )


def _thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _find_clarification_interrupt(
    interrupts: tuple[Any, ...], task_id: str, thread_id: str
) -> dict[str, Any] | None:
    """Return the parked clarification payload matching this task/thread, or None."""
    for interrupt in reversed(interrupts):
        value = getattr(interrupt, "value", None)
        if not isinstance(value, dict):
            continue
        if value.get("type") != "clarification":
            continue
        if value.get("taskId") != task_id or value.get("threadId") != thread_id:
            continue
        return value
    return None


def _checkpoint_identity_matches(
    values: Any,
    *,
    task_id: str,
    thread_id: str,
    session_owner_hash_value: str,
    control_policy: str,
) -> bool:
    """Bind a loaded graph snapshot to the exact server invocation."""
    return bool(
        isinstance(values, dict)
        and values.get("task_id") == task_id
        and values.get("thread_id") == thread_id
        and values.get("session_owner_hash") == session_owner_hash_value
        and values.get("control_policy") == control_policy
        and values.get("policy_revision") == _policy_revision(control_policy)
    )


def _run_marker_matches(
    marker: Any,
    *,
    run_id: str,
    thread_id: str,
    session_owner_hash_value: str,
    control_policy: str,
) -> bool:
    return bool(
        isinstance(marker, dict)
        and marker.get("runId") == run_id
        and marker.get("threadId") == thread_id
        and marker.get("sessionOwnerHash") == session_owner_hash_value
        and marker.get("controlPolicy") == control_policy
        and marker.get("policyRevision") == _policy_revision(control_policy)
    )


# ── durable control events (requirement #9, redacted event source) ───────────


def _durable_event(
    *,
    node_name: str,
    phase: str,
    task_id: str,
    thread_id: str,
    revision: int | None = None,
    route_decision: str | None = None,
    checkpoint_hash: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    event = GraphV2NodeEvent(
        nodeName=node_name,
        phase=phase,
        codeSource=_DURABLE_CODE_SOURCE,
        routeDecision=route_decision,
        revisionBefore=revision,
        revisionAfter=revision,
        errorCode=error_code,
        threadId=thread_id,
        taskId=task_id,
        checkpointHash=checkpoint_hash,
    )
    return event.model_dump(by_alias=True, mode="json")


# ── checkpoint audit helpers ──────────────────────────────────────────────────


def _stamp_events(
    events: list[dict[str, Any]],
    *,
    task_id: str,
    thread_id: str,
    checkpoint_hash: str | None,
) -> list[dict[str, Any]]:
    """Attach the server-bound durable identity to every redacted node event."""
    stamped: list[dict[str, Any]] = []
    for event in events:
        copy = dict(event)
        copy["threadId"] = thread_id
        copy["taskId"] = task_id
        copy["checkpointHash"] = checkpoint_hash
        stamped.append(copy)
    return stamped


# ── run result ────────────────────────────────────────────────────────────────


@dataclass
class DurableRunResult:
    """The structured outcome of one durable V2 invocation.

    ``boundary`` is one of ``clarification`` (parked at a real interrupt),
    ``task_completed``, ``max_transitions_exceeded``, ``state_diverged``,
    ``resume_rejected``, ``fault_injected``, ``durable_error``.
    """

    graph_state: dict[str, Any]
    # Ownership rejection deliberately carries no target TaskState.  This
    # keeps direct runner callers from turning a rejected taskId probe into a
    # cross-session state disclosure.
    task_state: TaskState | None
    boundary: str
    mode: str  # fresh | resume | restart | idempotent_replay
    run_id: str
    thread_id: str
    interrupted: bool = False
    interrupt_payload: dict[str, Any] | None = None
    interrupt_id: str | None = None
    question: str | None = None
    proposal_hash: str | None = None
    terminal_outcome: str | None = None
    degraded_reason: str | None = None
    transition_limit_reached: bool = False
    checkpoint_count: int = 0
    checkpoint_hash: str | None = None
    pause_receipt: dict[str, Any] | None = None
    rejected_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def revision(self) -> int:
        return self.task_state.revision if self.task_state is not None else 0


# ── runtime + initial input ───────────────────────────────────────────────────


def _build_runtime(
    *,
    user_message: str,
    client: AsyncOpenAI,
    model: str,
    resolve_tool_schemas: Callable[[TaskState], list[dict[str, Any]]],
    tool_caller: ToolCaller,
    durable_tool_boundary: ToolInboxCallerV2,
    trace_builder: TraceBuilder,
    projector: ContextProjector | None,
    projector_factory: ProjectorFactory | None,
    clarification_answer_applier: Callable[
        [TaskState, str], Awaitable[TaskState]
    ] | None,
    max_transitions: int,
    max_replans: int,
    system_policies: dict[str, Any] | None,
    on_transition: Callable[[HarnessStepResult], Awaitable[None]] | None,
    task_id: str,
    run_id: str,
    thread_id: str,
    session_owner_hash_value: str,
    checkpointer: GraphV2CheckpointSaver,
    control_policy: str,
    react_max_model_decisions: int,
    react_decision_timeout_seconds: float,
    on_model_call: Callable[..., None] | None,
    on_model_call_receipt: Callable[..., None] | None,
    recovery_mode: bool,
) -> GraphV2Runtime:
    return GraphV2Runtime(
        user_message=user_message,
        client=client,
        model=model,
        resolve_tool_schemas=resolve_tool_schemas,
        tool_caller=tool_caller,
        trace_builder=trace_builder,
        projector=projector,
        max_transitions=max_transitions,
        system_policies=system_policies,
        on_transition=on_transition,
        max_replans=max_replans,
        task_id=task_id,
        run_id=run_id,
        thread_id=thread_id,
        session_owner_hash=session_owner_hash_value,
        checkpointer=checkpointer,
        projector_factory=projector_factory,
        clarification_answer_applier=clarification_answer_applier,
        durable=True,
        recovery_mode=recovery_mode,
        durable_tool_boundary=durable_tool_boundary,
        control_policy=control_policy,
        react_max_model_decisions=react_max_model_decisions,
        react_decision_timeout_seconds=react_decision_timeout_seconds,
        on_model_call=on_model_call,
        on_model_call_receipt=on_model_call_receipt,
    )


def _initial_graph_input(
    task_id: str,
    thread_id: str,
    live: TaskState,
    session_owner_hash_value: str,
    control_policy: str = "fixed_v1",
) -> dict[str, Any]:
    """Deterministic routing stub for a fresh durable run.

    TaskState is ``UntrackedValue`` and never enters graph input or a
    checkpoint (requirement #1/#3); every durable node rehydrates it from the
    real store.  Only server-bound identity + bounded routing/budget channels
    are passed in — no prompts, no history, no raw payloads.
    """
    plan = live.active_plan
    return {
        "task_id": task_id,
        "thread_id": thread_id,
        "session_owner_hash": session_owner_hash_value,
        "graph_revision": live.revision,
        "plan_id": plan.plan_id if plan is not None else None,
        "action": "continue_to_executor",
        "transition_count": 0,
        "replan_count": 0,
        "transition_limit_reached": False,
        "terminal_outcome": None,
        "degraded_reason": None,
        "last_route_decision": None,
        "node_events": [],
        "control_policy": control_policy,
        "policy_revision": _policy_revision(control_policy),
        "react_model_decision_count": 0,
        "react_action_id": None,
        "react_action_kind": None,
        "react_answer_context_ref": None,
        "react_last_outcome": None,
    }


# ── invocation + finalize ─────────────────────────────────────────────────────


def _extract_interrupts(result: Any) -> tuple[bool, tuple[Any, ...], dict[str, Any]]:
    """Return ``(interrupted, interrupts, graph_state)`` from an ainvoke result."""
    if isinstance(result, dict) and "__interrupt__" in result:
        interrupts = tuple(result["__interrupt__"] or ())
        graph_state = {k: v for k, v in result.items() if k != "__interrupt__"}
        return bool(interrupts), interrupts, graph_state
    return False, (), result if isinstance(result, dict) else {}


async def _fire_transitions(runtime: GraphV2Runtime, result: Any) -> None:
    """Surface each node result to the request layer after the run's boundary.

    The durable runner is single-shot ``ainvoke`` (not a stream), so the
    request layer's ``on_transition`` (tool traces + task-state notifications)
    is invoked post-hoc for every ``HarnessStepResult`` the graph produced.
    A failure here never breaks the durable run.
    """
    if runtime.on_transition is None:
        return
    transitions = (
        result.get("transitions", []) if isinstance(result, dict) else []
    )
    for item in transitions:
        if not isinstance(item, HarnessStepResult):
            continue
        try:
            await runtime.on_transition(item)
        except Exception:  # noqa: BLE001 - notifications are best-effort
            logger.warning(
                "durable on_transition failed action=%s", item.action,
                exc_info=True,
            )


def _classify_boundary(graph_state: dict[str, Any], interrupted: bool) -> str:
    if interrupted:
        return "clarification"
    outcome = graph_state.get("terminal_outcome")
    action = graph_state.get("action")
    if outcome == "STATE_DIVERGED":
        return "state_diverged"
    if graph_state.get("transition_limit_reached") or outcome == "max_transitions_exceeded":
        return "max_transitions_exceeded"
    if action == "task_completed" or outcome == "task_completed":
        return "task_completed"
    return "stop_turn"


async def _finalize_run(
    *,
    saver: GraphV2CheckpointSaver,
    result: Any,
    mode: str,
    task_id: str,
    run_id: str,
    thread_id: str,
    trace_builder: TraceBuilder,
    boundary_override: str | None = None,
) -> DurableRunResult:
    """Post-pass: extract interrupts, rehydrate live state, stamp events."""
    interrupted, interrupts, graph_state = _extract_interrupts(result)
    live = await get_task_state(task_id)
    if live is None:
        raise TaskStateNotFoundError(task_id)
    checkpoint_hash = await saver.alatest_checkpoint_hash(thread_id)
    checkpoint_count = await saver.acount_checkpoints(thread_id)
    events = _stamp_events(
        list(graph_state.get("node_events", [])),
        task_id=task_id,
        thread_id=thread_id,
        checkpoint_hash=checkpoint_hash,
    )
    boundary = boundary_override or _classify_boundary(graph_state, interrupted)
    interrupt_payload: dict[str, Any] | None = None
    interrupt_id: str | None = None
    question: str | None = None
    proposal_hash: str | None = None
    if interrupted and interrupts:
        last = interrupts[-1]
        interrupt_payload = getattr(last, "value", None)
        interrupt_id = getattr(last, "id", None)
        if isinstance(interrupt_payload, dict):
            question = interrupt_payload.get("question")
            proposal_hash = interrupt_payload.get("proposalHash")
    # Record the stamped node events + one durable control event (requirement #9).
    trace_builder.record_graph_v2_events([
        *events,
        _durable_event(
            node_name="durable",
            phase="end",
            task_id=task_id,
            thread_id=thread_id,
            revision=live.revision,
            route_decision=boundary,
            checkpoint_hash=checkpoint_hash,
        ),
    ])
    return DurableRunResult(
        graph_state=graph_state,
        task_state=live,
        boundary=boundary,
        mode=mode,
        run_id=run_id,
        thread_id=thread_id,
        interrupted=interrupted,
        interrupt_payload=interrupt_payload,
        interrupt_id=interrupt_id,
        question=question,
        proposal_hash=proposal_hash,
        terminal_outcome=graph_state.get("terminal_outcome"),
        degraded_reason=graph_state.get("degraded_reason"),
        transition_limit_reached=bool(graph_state.get("transition_limit_reached")),
        checkpoint_count=checkpoint_count,
        checkpoint_hash=checkpoint_hash,
        events=events,
    )


def _recursion_limit(max_transitions: int) -> int:
    # Each business node counts one superstep; the budget check lives in the
    # nodes, so the recursion cap only needs generous headroom.
    return max(max_transitions * 4 + 20, 60)


async def _invoke(
    *,
    graph: Any,
    saver: GraphV2CheckpointSaver,
    runtime: GraphV2Runtime,
    input_: Any,
    mode: str,
    task_id: str,
    run_id: str,
    thread_id: str,
    trace_builder: TraceBuilder,
) -> DurableRunResult:
    config = _thread_config(thread_id)
    live_before = await get_task_state(task_id)
    trace_builder.record_graph_v2_events([
        _durable_event(
            node_name="durable",
            phase="start",
            task_id=task_id,
            thread_id=thread_id,
            revision=live_before.revision if live_before is not None else None,
            route_decision=mode,
        )
    ])
    try:
        result = await graph.ainvoke(
            input_,
            config=config,
            context=runtime,
            recursion_limit=_recursion_limit(runtime.max_transitions),
        )
    except GraphPauseRequested as exc:
        # Cooperative pause occurs before the next node effect.  The previous
        # LangGraph checkpoint is therefore the exact restart boundary.
        live = await get_task_state(task_id)
        if live is None:
            raise TaskStateNotFoundError(task_id) from exc
        checkpoint_hash = await saver.alatest_checkpoint_hash(thread_id)
        checkpoint_count = await saver.acount_checkpoints(thread_id)
        if not checkpoint_hash or checkpoint_count < 1:
            raise RuntimeError("pause_checkpoint_unavailable") from exc
        snapshot = await graph.aget_state(config)
        snapshot_values = snapshot.values if snapshot is not None else None
        checkpoint_revision = (
            snapshot_values.get("graph_revision")
            if isinstance(snapshot_values, dict)
            and type(snapshot_values.get("graph_revision")) is int
            else None
        )
        pause_receipt = await confirm_graph_pause(
            exc.receipt,
            live_revision=live.revision,
            checkpoint_revision=checkpoint_revision,
            checkpoint_hash=checkpoint_hash,
            checkpoint_count=checkpoint_count,
        )
        trace_builder.record_graph_v2_events([
            _durable_event(
                node_name="durable",
                phase="end",
                task_id=task_id,
                thread_id=thread_id,
                revision=live.revision,
                route_decision="operator_paused",
                checkpoint_hash=checkpoint_hash,
            )
        ])
        return DurableRunResult(
            graph_state={},
            task_state=live,
            boundary="operator_paused",
            mode=mode,
            run_id=run_id,
            thread_id=thread_id,
            checkpoint_count=checkpoint_count,
            checkpoint_hash=checkpoint_hash,
            pause_receipt=pause_receipt,
            events=[],
        )
    except (ExecutorFaultInjected, ValidatorFaultInjected) as exc:
        # Requirement #8 in-process mode: the dangerous-window fault fired after
        # the receipt was persisted.  Return a fault boundary; the caller may
        # restart the same thread (skip guard keeps the live tool count at 1).
        live = await get_task_state(task_id)
        if live is None:
            raise TaskStateNotFoundError(task_id) from exc
        checkpoint_hash = await saver.alatest_checkpoint_hash(thread_id)
        fault_code = getattr(exc, "code", "durable_fault_injected")
        trace_builder.record_graph_v2_events([
            _durable_event(
                node_name="durable",
                phase="fault",
                task_id=task_id,
                thread_id=thread_id,
                revision=live.revision,
                route_decision=fault_code,
                checkpoint_hash=checkpoint_hash,
                error_code=fault_code,
            )
        ])
        return DurableRunResult(
            graph_state={},
            task_state=live,
            boundary="fault_injected",
            mode=mode,
            run_id=run_id,
            thread_id=thread_id,
            terminal_outcome=fault_code,
            degraded_reason=fault_code,
            checkpoint_count=await saver.acount_checkpoints(thread_id),
            checkpoint_hash=checkpoint_hash,
            events=[],
        )
    # Surface each transition (tool traces + task-state notifications) to the
    # request layer now that the run reached its boundary.
    await _fire_transitions(runtime, result)
    return await _finalize_run(
        saver=saver,
        result=result,
        mode=mode,
        task_id=task_id,
        run_id=run_id,
        thread_id=thread_id,
        trace_builder=trace_builder,
    )


# ── mode implementations ──────────────────────────────────────────────────────


async def _run_fresh(
    *,
    graph: Any,
    saver: GraphV2CheckpointSaver,
    task_id: str,
    run_id: str,
    live: TaskState,
    runtime: GraphV2Runtime,
    trace_builder: TraceBuilder,
    session_owner_hash_value: str,
) -> DurableRunResult:
    thread_id = build_thread_id(task_id, run_id)
    # Same-run lifecycle marker: proves a fast-forward reconcile belongs to THIS
    # run/thread (requirement #6) rather than unrelated drift.  The original user
    # message is persisted server-owned so a later resume/restart rebuilds the
    # planner context from the ORIGINAL request, not the clarification answer.
    patch = TaskStatePatchRequest(
        expected_revision=live.revision,
        actor="agent",
        domain_state_patch={
            "v2RunMarker": {
                "runId": run_id,
                "threadId": thread_id,
                "sessionOwnerHash": session_owner_hash_value,
                "controlPolicy": runtime.control_policy,
                "policyRevision": _policy_revision(runtime.control_policy),
                # The marker may attest only its own OCC write.  It is not a
                # blanket capability to fast-forward arbitrary later state.
                "stateRevision": live.revision + 1,
                "planId": (
                    live.active_plan.plan_id
                    if live.active_plan is not None
                    else None
                ),
                "payloadSha256": hashlib.sha256(
                    runtime.user_message.encode("utf-8")
                ).hexdigest(),
                "startedAt": datetime.now(timezone.utc).isoformat(),
            },
            "v2UserMessage": runtime.user_message,
        },
    )
    live = await update_task_state(task_id, patch)
    await write_task_cursor(
        task_id,
        run_id=run_id,
        thread_id=thread_id,
        revision=live.revision,
        session_owner_hash_value=session_owner_hash_value,
        control_policy=runtime.control_policy,
    )
    return await _invoke(
        graph=graph,
        saver=saver,
        runtime=runtime,
        input_=_initial_graph_input(
            task_id,
            thread_id,
            live,
            session_owner_hash_value,
            runtime.control_policy,
        ),
        mode="fresh",
        task_id=task_id,
        run_id=run_id,
        thread_id=thread_id,
        trace_builder=trace_builder,
    )


async def _run_restart(
    *,
    graph: Any,
    saver: GraphV2CheckpointSaver,
    task_id: str,
    run_id: str | None,
    thread_id: str,
    live: TaskState,
    runtime: GraphV2Runtime,
    trace_builder: TraceBuilder,
    session_owner_hash_value: str,
) -> DurableRunResult:
    # If the process died before the first checkpoint (no snapshot yet), the
    # deterministic initial input re-runs idempotently — the entry node
    # rehydrates the LIVE TaskState and routes from it.
    snapshot = await graph.aget_state(_thread_config(thread_id))
    if snapshot.values and not _checkpoint_identity_matches(
        snapshot.values,
        task_id=task_id,
        thread_id=thread_id,
        session_owner_hash_value=session_owner_hash_value,
        control_policy=runtime.control_policy,
    ):
        raise DurableResumeRejected("checkpoint_identity_mismatch")
    if not snapshot.values:
        return await _invoke(
            graph=graph,
            saver=saver,
            runtime=runtime,
            input_=_initial_graph_input(
                task_id,
                thread_id,
                live,
                session_owner_hash_value,
                runtime.control_policy,
            ),
            mode="restart",
            task_id=task_id,
            run_id=run_id or "",
            thread_id=thread_id,
            trace_builder=trace_builder,
        )
    # Otherwise continue the pending nodes from the last confirmed checkpoint.
    # ainvoke(None) is a no-op on a completed thread (idempotent replay).
    return await _invoke(
        graph=graph,
        saver=saver,
        runtime=runtime,
        input_=None,
        mode="restart",
        task_id=task_id,
        run_id=run_id or "",
        thread_id=thread_id,
        trace_builder=trace_builder,
    )


async def _run_resume(
    *,
    graph: Any,
    saver: GraphV2CheckpointSaver,
    task_id: str,
    payload: ResumePayload,
    live: TaskState,
    runtime: GraphV2Runtime,
    trace_builder: TraceBuilder,
    session_owner_hash_value: str,
) -> DurableRunResult:
    # Fail-closed validation (requirement #5), in order.
    if payload.task_id != task_id:
        raise DurableResumeRejected("cross_task")
    parsed = parse_thread_id(payload.thread_id)
    if parsed is None:
        raise DurableResumeRejected("thread_malformed")
    parsed_task, parsed_run = parsed
    if parsed_task != task_id or payload.run_id != parsed_run:
        raise DurableResumeRejected("cross_task")
    marker = (live.domain_state or {}).get("v2RunMarker")
    if (
        not isinstance(marker, dict)
        or marker.get("runId") != payload.run_id
        or marker.get("threadId") != payload.thread_id
        or marker.get("sessionOwnerHash") != session_owner_hash_value
    ):
        raise DurableResumeRejected("run_session_mismatch")
    if (
        marker.get("controlPolicy") != runtime.control_policy
        or marker.get("policyRevision") != _policy_revision(runtime.control_policy)
    ):
        raise DurableResumeRejected("run_control_policy_mismatch")
    cursor = await read_task_cursor(task_id)
    if (
        not isinstance(cursor, dict)
        or cursor.get("runId") != payload.run_id
        or cursor.get("threadId") != payload.thread_id
        or cursor.get("sessionOwnerHash") != session_owner_hash_value
        or cursor.get("controlPolicy") != runtime.control_policy
        or cursor.get("policyRevision") != _policy_revision(runtime.control_policy)
    ):
        raise DurableResumeRejected("cursor_session_mismatch")
    config = _thread_config(payload.thread_id)
    snapshot = await graph.aget_state(config)
    if snapshot.values and not _checkpoint_identity_matches(
        snapshot.values,
        task_id=task_id,
        thread_id=payload.thread_id,
        session_owner_hash_value=session_owner_hash_value,
        control_policy=runtime.control_policy,
    ):
        raise DurableResumeRejected("checkpoint_identity_mismatch")
    parked = _find_clarification_interrupt(
        tuple(snapshot.interrupts or ()), task_id, payload.thread_id
    )
    receipt = (live.domain_state or {}).get("v2PendingClarification")
    if parked is None:
        # No pending interrupt.  If this exact answer was already applied,
        # replay it idempotently (no new revision/model/tool); otherwise a
        # different/forged payload on a resolved interrupt fails closed.
        if isinstance(receipt, dict) and receipt.get("proposalHash") == payload.proposal_hash:
            if (
                receipt.get("taskId") != task_id
                or receipt.get("runId") != payload.run_id
                or receipt.get("threadId") != payload.thread_id
                or receipt.get("sessionOwnerHash") != session_owner_hash_value
                or receipt.get("controlPolicy") != runtime.control_policy
                or receipt.get("policyRevision")
                != _policy_revision(runtime.control_policy)
            ):
                raise DurableResumeRejected("receipt_session_mismatch")
            if receipt.get("answerHash") == _answer_hash(payload.answer):
                return _idempotent_replay_result(
                    saver=saver,
                    task_id=task_id,
                    run_id=payload.run_id,
                    thread_id=payload.thread_id,
                    live=live,
                    receipt=receipt,
                    trace_builder=trace_builder,
                )
            raise DurableResumeRejected("resolved_interrupt_different_payload")
        raise DurableResumeRejected("no_pending_interrupt")
    # The parked interrupt revision is necessary but not sufficient: an
    # unrelated OCC write may have advanced the live TaskState while leaving
    # the checkpoint payload untouched.  Accepting the old receipt would let a
    # stale continuation overwrite externally changed state.
    if live.revision != payload.revision:
        raise DurableResumeRejected("revision_mismatch")
    if parked.get("revision") != payload.revision:
        raise DurableResumeRejected("revision_mismatch")
    if parked.get("proposalHash") != payload.proposal_hash:
        raise DurableResumeRejected("proposal_hash_mismatch")
    reason = _validate_answer(payload.answer)
    if reason is not None:
        raise DurableResumeRejected("invalid_answer")
    # Same-thread Command(resume=...) — the graph re-executes the clarification
    # node from the top and applies the answer through the TaskState contract.
    completed = await _invoke(
        graph=graph,
        saver=saver,
        runtime=runtime,
        input_=Command(resume=payload.answer),
        mode="resume",
        task_id=task_id,
        run_id=payload.run_id,
        thread_id=payload.thread_id,
        trace_builder=trace_builder,
    )
    # The terminal response is rendered by the request layer, but any receipt
    # must stay bound to the exact answer/proposal that drove this graph run.
    completed.proposal_hash = payload.proposal_hash
    return completed


def _idempotent_replay_result(
    *,
    saver: GraphV2CheckpointSaver,
    task_id: str,
    run_id: str,
    thread_id: str,
    live: TaskState,
    receipt: dict[str, Any],
    trace_builder: TraceBuilder,
) -> DurableRunResult:
    """Requirement #5: same answer replayed returns the SAME receipt.

    No graph invocation, no new checkpoint, no revision/model/tool change.
    """
    trace_builder.record_graph_v2_events([
        _durable_event(
            node_name="durable",
            phase="replay",
            task_id=task_id,
            thread_id=thread_id,
            revision=live.revision,
            route_decision="idempotent_resume_replay",
            error_code="idempotent_replay",
        )
    ])
    return DurableRunResult(
        graph_state={},
        task_state=live,
        boundary="task_completed",
        mode="idempotent_replay",
        run_id=run_id,
        thread_id=thread_id,
        proposal_hash=receipt.get("proposalHash"),
        checkpoint_count=0,
        events=[],
    )


# ── public entry ──────────────────────────────────────────────────────────────


async def run_graph_v2_durable(
    *,
    task_id: str,
    session_id: str | None = None,
    resume: dict[str, Any] | None = None,
    restart: bool = False,
    pause_resume: dict[str, Any] | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    user_message: str,
    client: AsyncOpenAI,
    model: str,
    resolve_tool_schemas: Callable[[TaskState], list[dict[str, Any]]],
    tool_caller: ToolCaller,
    tool_caller_v2: ToolCallerV2 | None = None,
    tool_inbox: ToolInbox | None = None,
    allow_legacy_durable_test_compat: bool = False,
    trace_builder: TraceBuilder,
    projector: ContextProjector | None = None,
    projector_factory: ProjectorFactory | None = None,
    clarification_answer_applier: Callable[
        [TaskState, str], Awaitable[TaskState]
    ] | None = None,
    max_transitions: int,
    max_replans: int = 3,
    system_policies: dict[str, Any] | None = None,
    on_transition: Callable[[HarnessStepResult], Awaitable[None]] | None = None,
    checkpointer: GraphV2CheckpointSaver | None = None,
    control_policy: str = "fixed_v1",
    react_max_model_decisions: int = 2,
    react_decision_timeout_seconds: float = 15.0,
    on_model_call: Callable[..., None] | None = None,
    on_model_call_receipt: Callable[..., None] | None = None,
) -> DurableRunResult:
    """Run the durable V2 graph to one user-facing boundary.

    ``resume`` is a validated client payload (dict); ``restart=True`` continues
    the existing thread for process-restart recovery.  When neither is given a
    new thread is created.  Server-bound identity is always authoritative.
    A cooperative operator pause additionally requires ``pause_resume`` to
    match the persisted paused receipt and latest checkpoint exactly.

    ``projector_factory`` rebuilds a ContextProjector from the CURRENT live
    TaskState at node-execution time, so a resume/restart projects views from
    post-answer / post-plan state instead of a stale pre-run pack.
    """
    if control_policy not in {"fixed_v1", "react_v1"}:
        raise ValueError("durable control_policy must be fixed_v1 or react_v1")
    if react_max_model_decisions != 2:
        raise ValueError("react_v1 model decision budget must equal 2")
    if tool_caller_v2 is None:
        if not allow_legacy_durable_test_compat:
            raise LegacyToolCallerForbidden(
                "durable Graph V2 requires an explicit ToolCallerV2"
            )

        async def tool_caller_v2(
            tool_name: str, arguments: dict[str, Any], _context: Any,
        ) -> Any:
            # Only explicit tests may choose this compatibility adapter.  The
            # durable runner never introspects a two-argument callable.
            return await tool_caller(tool_name, arguments)

    boundary = ToolInboxCallerV2(
        inbox=tool_inbox or ToolInbox(_get_client()), caller=tool_caller_v2,
    )
    saver = checkpointer or GraphV2CheckpointSaver()
    graph = build_graph_v2_durable(saver)
    live = await get_task_state(task_id)
    if live is None:
        raise TaskStateNotFoundError(task_id)
    try:
        owner_hash = _require_session_owner(live, session_id)
    except DurableResumeRejected as exc:
        # Direct callers cannot bypass the request boundary: do not read a
        # cursor/checkpoint or invoke the graph, and do not mutate TaskState.
        trace_builder.record_graph_v2_events([_durable_event(
            node_name="durable",
            phase="end",
            task_id=task_id,
            thread_id="",
            revision=live.revision,
            route_decision="resume_rejected",
            error_code=exc.reason,
        )])
        return DurableRunResult(
            graph_state={},
            task_state=None,
            boundary="resume_rejected",
            mode="resume" if resume is not None else ("restart" if restart else "fresh"),
            run_id="",
            thread_id="",
            rejected_reason=exc.reason,
            events=[],
        )

    # Independent, record-only routing observation.  This is deliberately
    # before any checkpoint/graph work and never changes the live state or
    # runtime inputs.  Fail-open keeps the authoritative V2 path unchanged if
    # the shadow contract encounters malformed server facts.
    if settings.agent_strategy_shadow_enabled:
        try:
            from .strategy_shadow import (
                build_strategy_shadow_report,
                strategy_shadow_event,
            )

            report = build_strategy_shadow_report(
                live,
                runner_receipt=(live.domain_state or {}).get("v2ExecReceipt"),
            )
            trace_builder.record_graph_v2_events(
                [
                    strategy_shadow_event(
                        report,
                        task_id=task_id,
                        revision=live.revision,
                    )
                ]
            )
        except Exception:
            # A diagnostic observer must not turn a valid durable invocation
            # into an error.  The fallback event is fixed/redacted as well.
            try:
                trace_builder.record_graph_v2_events(
                    [
                        _durable_event(
                            node_name="strategy-shadow",
                            phase="end",
                            task_id=task_id,
                            thread_id="",
                            revision=live.revision,
                            route_decision="shadow_error",
                            error_code="strategy_shadow_error",
                        )
                    ]
                )
            except Exception:
                pass

    # Only a process restart recovers its message from server-owned TaskState.
    # A normal fresh invocation is a NEW chat turn on the same long-lived task,
    # so its current HTTP message must replace the prior run's v2UserMessage.
    # Reading the old marker for every fresh run made a correctly recognised
    # candidate-scope follow-up plan from the previous request and silently
    # fall back to a full-catalog search.  Resume supplies its accepted answer
    # explicitly below and therefore does not need this recovery branch.
    if restart:
        stored_message = (live.domain_state or {}).get("v2UserMessage")
        if isinstance(stored_message, str) and stored_message:
            user_message = stored_message
        receipt = (live.domain_state or {}).get("v2PendingClarification")
        latest_applied_message = (live.domain_state or {}).get("lastUserMessage")
        if (
            isinstance(receipt, dict)
            and receipt.get("status") == "resolved"
            and isinstance(latest_applied_message, str)
            and latest_applied_message
        ):
            user_message = latest_applied_message

    def _make_runtime(
        *, run: str, thread: str, message_override: str | None = None,
    ) -> GraphV2Runtime:
        return _build_runtime(
            user_message=message_override or user_message,
            client=client,
            model=model,
            resolve_tool_schemas=resolve_tool_schemas,
            tool_caller=tool_caller,
            durable_tool_boundary=boundary,
            trace_builder=trace_builder,
            projector=projector,
            projector_factory=projector_factory,
            clarification_answer_applier=clarification_answer_applier,
            max_transitions=max_transitions,
            max_replans=max_replans,
            system_policies=system_policies,
            on_transition=on_transition,
            task_id=task_id,
            run_id=run,
            thread_id=thread,
            session_owner_hash_value=owner_hash,
            checkpointer=saver,
            control_policy=control_policy,
            react_max_model_decisions=react_max_model_decisions,
            react_decision_timeout_seconds=react_decision_timeout_seconds,
            on_model_call=on_model_call,
            on_model_call_receipt=on_model_call_receipt,
            recovery_mode=restart,
        )

    if resume is not None:
        try:
            payload = ResumePayload.from_dict(resume, server_task_id=task_id)
            return await _run_resume(
                graph=graph,
                saver=saver,
                task_id=task_id,
                payload=payload,
                live=live,
                runtime=_make_runtime(
                    run=payload.run_id,
                    thread=payload.thread_id,
                    message_override=payload.answer,
                ),
                trace_builder=trace_builder,
                session_owner_hash_value=owner_hash,
            )
        except DurableResumeRejected as exc:
            # Fail-closed boundary (requirement #5): a forged, stale or malformed
            # resume payload never touches the graph.  The rejection is still
            # recorded in the redacted durable event source.
            event = _durable_event(
                node_name="durable",
                phase="end",
                task_id=task_id,
                thread_id=(
                    resume.get("threadId") if isinstance(resume, dict) else None
                ) or "",
                revision=live.revision,
                route_decision="resume_rejected",
                error_code=exc.reason,
            )
            trace_builder.record_graph_v2_events([event])
            return DurableRunResult(
                graph_state={},
                task_state=live,
                boundary="resume_rejected",
                mode="resume",
                run_id=(
                    resume.get("runId") if isinstance(resume, dict) else None
                ) or "",
                thread_id=(
                    resume.get("threadId") if isinstance(resume, dict) else None
                ) or "",
                rejected_reason=exc.reason,
                events=[],
            )
    if restart:
        cursor = await read_task_cursor(task_id)
        if (
            not isinstance(cursor, dict)
            or cursor.get("sessionOwnerHash") != owner_hash
            or cursor.get("controlPolicy") != control_policy
            or cursor.get("policyRevision") != _policy_revision(control_policy)
            or not isinstance(cursor.get("runId"), str)
            or not isinstance(cursor.get("threadId"), str)
            or parse_thread_id(cursor["threadId"]) != (task_id, cursor["runId"])
        ):
            return DurableRunResult(
                graph_state={}, task_state=live, boundary="resume_rejected",
                mode="restart", run_id="", thread_id="",
                rejected_reason="cursor_identity_mismatch", events=[],
            )
        marker = (live.domain_state or {}).get("v2RunMarker")
        if isinstance(marker, dict) and not _run_marker_matches(
            marker,
            run_id=cursor["runId"],
            thread_id=cursor["threadId"],
            session_owner_hash_value=owner_hash,
            control_policy=control_policy,
        ):
            return DurableRunResult(
                graph_state={}, task_state=live, boundary="resume_rejected",
                mode="restart", run_id="", thread_id="",
                rejected_reason="run_marker_identity_mismatch", events=[],
            )
        stored_pause = await read_graph_pause(task_id)
        pause_transition: dict[str, Any] | None = None
        if isinstance(stored_pause, dict) and stored_pause.get("state") in {
            "paused", "resuming",
        }:
            if not isinstance(pause_resume, dict):
                return DurableRunResult(
                    graph_state={}, task_state=live, boundary="resume_rejected",
                    mode="restart", run_id="", thread_id="",
                    rejected_reason="pause_receipt_required", events=[],
                )
            current_hash = await saver.alatest_checkpoint_hash(cursor["threadId"])
            current_count = await saver.acount_checkpoints(cursor["threadId"])
            if (
                stored_pause.get("sessionOwnerHash") != owner_hash
                or stored_pause.get("runId") != cursor.get("runId")
                or stored_pause.get("threadId") != cursor.get("threadId")
                or stored_pause.get("checkpointHash") != current_hash
                or stored_pause.get("checkpointCount") != current_count
            ):
                return DurableRunResult(
                    graph_state={}, task_state=live, boundary="resume_rejected",
                    mode="restart", run_id="", thread_id="",
                    rejected_reason="pause_checkpoint_mismatch", events=[],
                )
            try:
                pause_transition = await begin_graph_pause_resume(
                    stored_pause, client_receipt=pause_resume
                )
            except RuntimeError as exc:
                return DurableRunResult(
                    graph_state={}, task_state=live, boundary="resume_rejected",
                    mode="restart", run_id="", thread_id="",
                    rejected_reason=str(exc), events=[],
                )
        elif pause_resume is not None:
            return DurableRunResult(
                graph_state={}, task_state=live, boundary="resume_rejected",
                mode="restart", run_id="", thread_id="",
                rejected_reason="pause_receipt_not_found", events=[],
            )
        resolved_run, resolved_thread = await resolve_durable_identity(
            task_id, restart=True, session_id=session_id
        )
        thread_id = resolved_thread or thread_id or build_thread_id(task_id, run_id or "")
        run_id = resolved_run or run_id
        result = await _run_restart(
            graph=graph,
            saver=saver,
            task_id=task_id,
            run_id=run_id,
            thread_id=thread_id,
            live=live,
            runtime=_make_runtime(run=run_id or "", thread=thread_id),
            trace_builder=trace_builder,
            session_owner_hash_value=owner_hash,
        )
        if pause_transition is not None:
            cleared = await clear_graph_pause(
                task_id=task_id,
                request_id=str(pause_transition["requestId"]),
            )
            if not cleared:
                raise RuntimeError("pause_clear_conflict")
        return result

    fresh_run_id = run_id or f"run-{uuid_hex()}"
    fresh_thread = thread_id or build_thread_id(task_id, fresh_run_id)
    return await _run_fresh(
        graph=graph,
        saver=saver,
        task_id=task_id,
        run_id=fresh_run_id,
        live=live,
        runtime=_make_runtime(run=fresh_run_id, thread=fresh_thread),
        trace_builder=trace_builder,
        session_owner_hash_value=owner_hash,
    )


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex[:12]
