"""Clarification node — the Day-2 real ``interrupt()``/resume() boundary.

Reached from the durable Planner (``ask_user``) or the durable entry router when
a parked pending question exists.  It parks on a dynamic ``interrupt()`` whose
payload carries ONLY the visible question plus server-owned task/thread/revision
identity and a proposal hash (requirement #4/#5) — never prompts, history, raw
evidence or credentials.  ``interrupt()`` is the FIRST operation: nothing
stateful or non-idempotent runs before it, so restarting the node re-executes
nothing (requirement #7).

On ``Command(resume=answer)`` the node re-executes from the top, validates the
answer, applies it through the existing ``update_task_state`` OCC contract, and
routes back to the Planner.  An empty / too-long answer re-parks (a second
interrupt at a fresh id) — the graph never guesses.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from ...harness import HarnessStepResult
from ...task_state import TaskFact, TaskState, TaskStatePatchRequest, update_task_state
from ..runtime import CONTROL_POLICY_REVISIONS, GraphV2Runtime
from ..state import GraphV2State
from . import (
    _finish_node,
    _hydrate_task_state,
    _node_event,
    _reconcile_revision,
    _record_error_end_event,
    _react_scope_eligible,
    _state_diverged_updates,
)

__all__ = ["clarification_node"]

# Bounded answer size: a clarification answer is a short user reply, never a
# payload dump.  Oversized answers are re-asked, not persisted.
_MAX_ANSWER_CHARS = 2000
_FALLBACK_QUESTION = "请补充完成当前任务所需的关键信息。"


def _proposal_hash(task_id: str, question: str, revision: int) -> str:
    """Stable short hash over the visible question + server-bound identity.

    The client must echo the exact ``proposalHash`` back on resume; a forged or
    stale answer carrying a different hash is rejected (requirement #5).
    """
    canonical = json.dumps(
        {
            "taskId": task_id,
            "question": question,
            "revision": revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _clarification_payload(
    deps: GraphV2Runtime,
    live: TaskState,
    *,
    re_ask_reason: str | None = None,
) -> dict[str, Any]:
    question = (
        live.pending_questions[0]
        if live.pending_questions
        else _FALLBACK_QUESTION
    )
    payload: dict[str, Any] = {
        "type": "clarification",
        "taskId": live.task_id,
        "threadId": deps.thread_id,
        "revision": live.revision,
        "question": question,
        "proposalHash": _proposal_hash(live.task_id, question, live.revision),
        "askedAt": datetime.now(timezone.utc).isoformat(),
    }
    if re_ask_reason:
        payload["reAskReason"] = re_ask_reason
    return payload


def _validate_answer(answer: Any) -> str | None:
    """Return a re-ask reason when the answer is not acceptable, else None."""
    if answer is None:
        return "回答为空，请补充关键信息后再继续。"
    text = str(answer).strip()
    if not text:
        return "回答为空，请补充关键信息后再继续。"
    if len(text) > _MAX_ANSWER_CHARS:
        return "回答过长，请用一句简洁的关键信息补充。"
    return None


async def _apply_answer(deps: GraphV2Runtime, live: TaskState, answer: Any) -> TaskState:
    """Deterministically apply one user answer through the TaskState contract."""
    answer_text = str(answer).strip()
    question = (
        live.pending_questions[0] if live.pending_questions else _FALLBACK_QUESTION
    )
    next_rev = live.revision + 1
    receipt: dict[str, Any] = {
        "status": "resolved",
        "taskId": live.task_id,
        "runId": deps.run_id,
        "threadId": deps.thread_id,
        "controlPolicy": deps.control_policy,
        "policyRevision": CONTROL_POLICY_REVISIONS[deps.control_policy],
        "proposalHash": _proposal_hash(live.task_id, question, live.revision),
        "answerHash": hashlib.sha256(answer_text.encode("utf-8")).hexdigest()[:16],
        "appliedAt": datetime.now(timezone.utc).isoformat(),
        "appliedRevision": next_rev,
        "sessionOwnerHash": deps.session_owner_hash,
    }
    patch = TaskStatePatchRequest(
        expected_revision=live.revision,
        actor="user",
        status="ready",
        pending_questions=[],
        upsert_facts=[
            TaskFact(
                key="user_clarification",
                value=answer_text,
                source="user",
            )
        ],
        domain_state_patch={"v2PendingClarification": receipt},
    )
    updated = await update_task_state(deps.task_id, patch)
    if deps.clarification_answer_applier is not None:
        applied = await deps.clarification_answer_applier(updated, answer_text)
        if (
            applied.task_id != updated.task_id
            or applied.session_id != updated.session_id
            or applied.revision <= updated.revision
        ):
            raise RuntimeError(
                "clarification answer applier returned an unowned or stale TaskState"
            )
        updated = applied
    return updated


async def clarification_node(
    state: GraphV2State,
    runtime: Runtime[GraphV2Runtime],
) -> dict[str, Any]:
    deps = runtime.context
    entered_because = state.get("last_route_decision") or "ask_user"
    start_ts = time.perf_counter()
    # Rehydrate the live TaskState first — a pure read, no side effect.  The
    # interrupt payload must carry the REAL revision/question at park time.
    live = await _hydrate_task_state(state, deps)
    # Requirement #7: interrupt() is the very first effect of this node.  On the
    # first entry it raises GraphInterrupt and parks; the code below only runs
    # after Command(resume=...) re-executes the node from the top.
    answer = interrupt(_clarification_payload(deps, live))

    events = [
        _node_event(
            "clarification",
            "start",
            current_state=live,
            entered_because=entered_because,
        )
    ]
    trace_started = False
    try:
        deps.trace_builder.start_phase("clarification")
        trace_started = True
        re_ask_reason = _validate_answer(answer)
        if re_ask_reason is not None:
            # Still uncertain: park AGAIN at a fresh interrupt id carrying the
            # re-ask reason.  The graph never guesses the user's intent.
            deps.trace_builder.end_phase("re_ask")
            trace_started = False
            interrupt(_clarification_payload(deps, live, re_ask_reason=re_ask_reason))

        # Rehydrate once more so revision reconciliation sees the freshest live
        # TaskState (the answer arrives after the interrupt was parked).
        live = await _hydrate_task_state(state, deps)
        reconcile = await _reconcile_revision(state, deps, live)
        if reconcile == "diverged":
            return _state_diverged_updates(
                state=state,
                deps=deps,
                node_name="clarification",
                entered_because=entered_because,
                live=live,
                events=events,
            )
        updated = await _apply_answer(deps, live, answer)
        deps.trace_builder.end_phase("resolved")
        trace_started = False
        scope_rejected = (
            deps.control_policy == "react_v1"
            and not _react_scope_eligible(updated)
        )
        result = HarnessStepResult(
            action="stop_turn" if scope_rejected else "continue_to_executor",
            task_state=updated,
        )
        return await _finish_node(
            state,
            deps,
            node_name="clarification",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action=result.action,
            start_ts=start_ts,
            error_code="react_scope_ineligible" if scope_rejected else None,
            extra=(
                {"degraded_reason": "react_scope_ineligible"}
                if scope_rejected
                else None
            ),
        )
    except GraphInterrupt:
        # The re-ask park is a normal graph boundary, not a node error.
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised after event recording
        if trace_started:
            deps.trace_builder.end_phase("exception")
        _record_error_end_event(
            deps,
            node_name="clarification",
            entered_because=entered_because,
            current=live,
            start_ts=start_ts,
            exc=exc,
        )
        raise
