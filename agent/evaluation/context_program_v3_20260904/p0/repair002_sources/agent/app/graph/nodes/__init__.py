"""Shared helpers for the four explicit V2 business nodes.

Every V2 node emits a redacted ``GraphV2NodeEvent`` start/end pair, calls a
production phase function through a typed ContextView (context_pack mode) or the
existing contract, applies the bounded transition budget, and awaits the
surrounding ``on_transition`` callback exactly like the V1 Harness did.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from langgraph.runtime import Runtime

from ...context_view import ContextProjector
from ...control.react_actions import (
    react_action_anchor_key,
    react_plan_contract_sha256,
)
from ...executor import recover_expired_executor_claim
from ...harness import HarnessStepResult
from ...replanner import should_run_replanner
from ...task_state import TaskState, TaskStateNotFoundError, _get_client, get_task_state
from ..runtime import CONTROL_POLICY_REVISIONS, GraphV2Runtime
from ..state import CONTINUE_ACTIONS, GraphV2NodeEvent, GraphV2State, redacted_state_hash

__all__ = [
    "entry_node",
    "_node_event",
    "_resolve_action",
    "_finish_node",
    "_error_code_of",
    "_record_error_end_event",
    "_hydrate_task_state",
    "_reconcile_revision",
    "_resolve_projector",
]

# Where each node actually lives on disk.  ``entry`` is implemented here (in
# ``nodes/__init__.py``), not in a per-node file, so its ``codeSource`` must not
# point at a nonexistent ``entry.py``.  The DAY1 event source feeds the eventual
# single-step debugger, so every code location must be a real repository file.
_NODE_SOURCE_FILES = {
    "entry": "agent/app/graph/nodes/__init__.py",
    "planner": "agent/app/graph/nodes/planner.py",
    "executor": "agent/app/graph/nodes/executor.py",
    "validator": "agent/app/graph/nodes/validator.py",
    "replanner": "agent/app/graph/nodes/replanner.py",
    "clarification": "agent/app/graph/nodes/clarification.py",
    "react_policy": "agent/app/graph/nodes/react_policy.py",
}


# ── durable helpers (Day-2) ──────────────────────────────────────────────────


async def _hydrate_task_state(
    state: GraphV2State,
    deps: GraphV2Runtime,
) -> TaskState:
    """Rehydrate the live TaskState from the real store by server-bound task id.

    TaskState is the single durable business truth (requirement #1) and never
    enters a checkpoint, so every durable node rehydrates the CURRENT revision
    from Redis instead of trusting any reconstructed graph state.
    """
    checkpoint_task_id = state.get("task_id")
    if checkpoint_task_id is not None and checkpoint_task_id != deps.task_id:
        raise RuntimeError(
            "durable V2 graph checkpoint task_id differs from runtime task_id"
        )
    task_id = checkpoint_task_id or deps.task_id
    if not task_id:
        raise RuntimeError(
            "durable V2 graph cannot rehydrate TaskState: missing task_id"
        )
    live = await get_task_state(task_id)
    if live is None:
        raise TaskStateNotFoundError(task_id)
    return live


async def _receipt_is_authentic(
    receipt: Any,
    deps: GraphV2Runtime,
    live: TaskState,
) -> bool:
    """Verify the complete runner-owned Inbox receipt projection.

    This is deliberately an exact identity check, not a permissive ``same
    run`` marker.  The result bytes themselves stay in the Inbox ledger; the
    TaskState projection binds its immutable result digest and all ownership
    fields to the live plan/revision.
    """
    if not isinstance(receipt, dict):
        return False
    plan = live.active_plan
    if plan is None or not deps.run_id:
        return False
    hex64 = lambda value: isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    expected_step = next(
        (step for step in plan.steps if step.step_id == receipt.get("stepId")),
        None,
    )
    return (
        receipt.get("taskId") == live.task_id
        and receipt.get("runId") == deps.run_id
        and receipt.get("threadId") == deps.thread_id
        and receipt.get("sessionOwnerHash") == deps.session_owner_hash
        and receipt.get("planId") == plan.plan_id
        and isinstance(receipt.get("stateRevision"), int)
        and type(receipt.get("stateRevision")) is int
        and receipt.get("stateRevision") >= 1
        and expected_step is not None
        and expected_step.status == "executed"
        and live.status == "ready"
        and receipt.get("toolName") == expected_step.tool_name
        and receipt.get("inboxStatus") == "SUCCEEDED"
        and isinstance(receipt.get("fence"), int)
        and type(receipt.get("fence")) is int
        and receipt.get("fence") >= 1
        and hex64(receipt.get("inputHash"))
        and hex64(receipt.get("resultHash"))
        and hex64(receipt.get("executionId"))
        and hex64(receipt.get("logicalSlotKey"))
        and _receipt_projection_is_current(receipt, live)
    ) and await _receipt_matches_ledger(receipt, deps)


def _receipt_projection_is_current(receipt: dict[str, Any], live: TaskState) -> bool:
    """Bind an Inbox receipt to the exact TaskState OCC projection write."""
    projection = (live.domain_state or {}).get("v2ExecReceiptProjection")
    receipt_hash = hashlib.sha256(
        json.dumps(
            receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return (
        isinstance(projection, dict)
        and projection.get("receiptHash") == receipt_hash
        and projection.get("projectionRevision") == live.revision
    )


async def _receipt_matches_ledger(
    receipt: dict[str, Any], deps: GraphV2Runtime,
) -> bool:
    """TaskState is a projection only; Redis Inbox remains authoritative."""
    boundary = deps.durable_tool_boundary
    if boundary is None:
        return False
    try:
        from ..tool_inbox_v2 import InboxStatus, ToolInboxSlot, sha256

        slot = ToolInboxSlot.create(
            task_id=receipt["taskId"],
            plan_id=receipt["planId"],
            step_id=receipt["stepId"],
            state_revision=receipt["stateRevision"],
            tool_name=receipt["toolName"],
            canonical_args_sha256=receipt["inputHash"],
        )
        stored = await boundary.inbox.inspect(slot)
        return (
            stored.status is InboxStatus.SUCCEEDED
            and stored.receipt == receipt
            and stored.trace is not None
            and sha256(stored.trace.model_dump(by_alias=True, mode="json"))
            == receipt.get("resultHash")
            and stored.execution_id == receipt.get("executionId")
            and stored.fence == receipt.get("fence")
        )
    except Exception:
        return False


async def _resolve_projector(
    deps: GraphV2Runtime, current: TaskState
) -> ContextProjector | None:
    """Return the ContextProjector this node must project its view from.

    Durable nodes rebuild from the CURRENT live TaskState revision when a
    factory is wired (so a resumed/restarted graph never projects from a stale
    pre-run pack); otherwise the request-scoped ``projector`` is used.
    """
    if deps.durable and deps.projector_factory is not None:
        return await deps.projector_factory(current)
    return deps.projector


def _react_scope_eligible(current: TaskState) -> bool:
    guide = (current.domain_state or {}).get("shoppingGuide")
    return bool(
        current.task_type == "ecommerce_guide"
        and isinstance(guide, dict)
        and isinstance(guide.get("category"), str)
        and guide.get("category", "").strip()
    )


def _react_adaptive_entry_required(current: TaskState) -> bool:
    """Return true only for the two pre-execution adaptive ReAct triggers.

    Zero-result is discovered after Validator and is routed there.  Unsupported
    evidence and stale-scope references are already classified by the
    server-owned TaskState extractor before graph entry, so react_v1 must see
    them before the generic durable clarification interrupt takes precedence.
    """

    extraction = (current.domain_state or {}).get("taskStateExtraction")
    reason = extraction.get("reason") if isinstance(extraction, dict) else None
    return reason in {
        "unsupported_game_camera_evidence",
        "stale_candidate_reference",
    }


def _react_server_boundary_clarification_allowed(current: TaskState) -> bool:
    """Allow a server-authored ecommerce boundary to bypass ReAct scope entry.

    An explicit unsupported category is deliberately represented as an
    ecommerce clarification with no active category.  It is outside ReAct's
    model/tool scope, but the deterministic server answer remains valid and
    must be shared with fixed_v1 instead of degrading to a generic stop.
    """

    extraction = (current.domain_state or {}).get("taskStateExtraction")
    guide = (current.domain_state or {}).get("shoppingGuide")
    return bool(
        current.task_type == "ecommerce_guide"
        and current.status == "collecting_information"
        and current.pending_questions
        and isinstance(extraction, dict)
        and extraction.get("route") == "deterministic_unsupported_category_boundary"
        and extraction.get("reason") == "unsupported_product_category"
        and isinstance(guide, dict)
        and guide.get("category") is None
    )


async def _reconcile_revision(
    state: GraphV2State,
    deps: GraphV2Runtime,
    live: TaskState,
) -> str:
    """Classify the checkpoint/live revision relationship (requirement #6).

    Returns ``"ok"`` (equal / fresh path), ``"fast_forward"`` (live is ahead of
    the checkpoint ONLY by same-run progress the durable runner can attest:
    the v2RunMarker lifecycle marker or an authentic v2ExecReceipt), or
    ``"diverged"`` (unrelated drift — fail closed, never resume a stale
    checkpoint over a newer TaskState).
    """
    if not deps.durable:
        return "ok"
    if deps.control_policy == "react_v1" and state.get("policy_revision") != (
        CONTROL_POLICY_REVISIONS["react_v1"]
    ):
        return "diverged"
    checkpoint_rev = state.get("graph_revision")
    if checkpoint_rev is None:
        return "ok"
    live_rev = live.revision
    if checkpoint_rev == live_rev:
        return "ok"
    if live_rev < checkpoint_rev:
        # The live TaskState is OLDER than what the checkpoint assumed — a
        # stale checkpoint must never overwrite a newer TaskState.
        return "diverged"
    marker = (live.domain_state or {}).get("v2RunMarker")
    user_message = (live.domain_state or {}).get("v2UserMessage")
    same_run_marker = (
        isinstance(marker, dict)
        and marker.get("runId") == deps.run_id
        and marker.get("threadId") == deps.thread_id
        and marker.get("stateRevision") == live.revision
        and marker.get("planId") == (
            live.active_plan.plan_id if live.active_plan is not None else None
        )
        and marker.get("sessionOwnerHash") == deps.session_owner_hash
        and isinstance(user_message, str)
        and marker.get("payloadSha256") == hashlib.sha256(
            user_message.encode("utf-8")
        ).hexdigest()
    )
    # A run marker proves only its exact lifecycle write.  Every later
    # executor fast-forward requires the complete executor receipt binding.
    react_budget = (live.domain_state or {}).get("reactV1DecisionBudget")
    react_action = (live.domain_state or {}).get("reactV1ActionReceipt")
    react_outcome = (live.domain_state or {}).get("reactV1OutcomeReceipt")

    def same_react_receipt(receipt: Any) -> bool:
        return bool(
            deps.control_policy == "react_v1"
            and isinstance(receipt, dict)
            and receipt.get("runId") == deps.run_id
            and receipt.get("threadId") == deps.thread_id
            and receipt.get("sessionOwnerHash") == deps.session_owner_hash
            and receipt.get("controlPolicy") == "react_v1"
            and receipt.get("policyRevision")
            == CONTROL_POLICY_REVISIONS["react_v1"]
            and receipt.get("stateRevision") == live.revision
        )

    react_budget_current = same_react_receipt(react_budget) and (
        type(react_budget.get("count")) is int
        and 1 <= react_budget.get("count") <= 2
        and isinstance(react_budget.get("viewHash"), str)
        and len(react_budget.get("viewHash")) == 64
    )
    react_action_current = False
    if same_react_receipt(react_action):
        raw_action = react_action.get("action")
        if isinstance(raw_action, dict):
            action_hash = hashlib.sha256(
                json.dumps(
                    raw_action,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            react_action_current = react_action.get("actionSha256") == action_hash
            if react_action_current and raw_action.get("kind") == "CALL_TOOL":
                react_action_current = bool(
                    live.active_plan is not None
                    and react_action.get("planSha256")
                    == react_plan_contract_sha256(live.active_plan)
                )
            if react_action_current:
                raw_anchor = await _get_client().get(
                    react_action_anchor_key(
                        str(deps.task_id),
                        str(deps.run_id),
                        str(deps.thread_id),
                        str(raw_action.get("actionId")),
                    )
                )
                try:
                    anchor = json.loads(raw_anchor) if raw_anchor else None
                except (TypeError, json.JSONDecodeError):
                    anchor = None
                react_action_current = bool(
                    isinstance(anchor, dict)
                    and anchor.get("taskId") == deps.task_id
                    and anchor.get("runId") == deps.run_id
                    and anchor.get("threadId") == deps.thread_id
                    and anchor.get("sessionOwnerHash") == deps.session_owner_hash
                    and anchor.get("controlPolicy") == "react_v1"
                    and anchor.get("policyRevision")
                    == CONTROL_POLICY_REVISIONS["react_v1"]
                    and anchor.get("taskRevision")
                    == react_action.get("stateRevision")
                    and anchor.get("action") == raw_action
                    and anchor.get("actionSha256")
                    == react_action.get("actionSha256")
                    and anchor.get("planSha256")
                    == react_action.get("planSha256")
                )

    react_outcome_current = same_react_receipt(react_outcome) and isinstance(
        react_outcome.get("outcome"), dict
    )

    if same_run_marker or react_budget_current or react_action_current or react_outcome_current or await _receipt_is_authentic(
        (live.domain_state or {}).get("v2ExecReceipt"), deps, live
    ):
        return "fast_forward"
    return "diverged"


def _state_diverged_updates(
    *,
    state: GraphV2State,
    deps: GraphV2Runtime,
    node_name: str,
    entered_because: str,
    live: TaskState,
    events: list[dict[str, Any]],
    model_name: str | None = None,
    model_call_id: str | None = None,
    decision_binding_hash: str | None = None,
    decision_view_hash: str | None = None,
    decision_task_revision: int | None = None,
    decision_action_id: str | None = None,
    decision_action_kind: str | None = None,
    decision_error_code: str | None = None,
) -> dict[str, Any]:
    """Terminal STATE_DIVERGED updates shared by durable nodes.

    Emits a redacted end event, applies the transition budget once, and never
    touches any tool or business write.
    """
    resolved = _resolve_action(state, deps, "stop_turn")
    end_event = _node_event(
        node_name,
        "end",
        current_state=live,
        entered_because=entered_because,
        route_decision="STATE_DIVERGED",
        revision_after=live.revision,
        model_name=model_name,
        model_call_id=model_call_id,
        decision_binding_hash=decision_binding_hash,
        decision_view_hash=decision_view_hash,
        decision_task_revision=decision_task_revision,
        decision_action_id=decision_action_id,
        decision_action_kind=decision_action_kind,
        decision_error_code=decision_error_code,
    )
    updates: dict[str, Any] = {
        "task_state": live,
        "action": "stop_turn",
        "transition_count": resolved["transition_count"],
        "transition_limit_reached": False,
        "terminal_outcome": "STATE_DIVERGED",
        "degraded_reason": "STATE_DIVERGED",
        "last_route_decision": "STATE_DIVERGED",
        "transitions": [
            HarnessStepResult(action="stop_turn", task_state=live)
        ],
        "node_events": [*events, end_event],
        "graph_revision": live.revision,
    }
    return updates


def node_code_source(node_name: str) -> str:
    return _NODE_SOURCE_FILES.get(node_name, f"agent/app/graph/nodes/{node_name}.py")


def _node_event(
    node_name: str,
    phase: str,
    *,
    current_state: TaskState | None,
    entered_because: str,
    route_decision: str | None = None,
    revision_after: int | None = None,
    duration_ms: float | None = None,
    tool_name: str | None = None,
    model_name: str | None = None,
    model_call_id: str | None = None,
    decision_binding_hash: str | None = None,
    decision_view_hash: str | None = None,
    decision_task_revision: int | None = None,
    decision_action_id: str | None = None,
    decision_action_kind: str | None = None,
    decision_error_code: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build one redacted node event dict (never prompts/payloads)."""
    event = GraphV2NodeEvent(
        nodeName=node_name,
        phase=phase,
        codeSource=node_code_source(node_name),
        enteredBecause=entered_because,
        routeDecision=route_decision,
        revisionBefore=current_state.revision if current_state is not None else None,
        revisionAfter=revision_after,
        durationMs=duration_ms,
        toolName=tool_name,
        modelName=model_name,
        modelCallId=model_call_id,
        decisionBindingHash=decision_binding_hash,
        decisionViewHash=decision_view_hash,
        decisionTaskRevision=decision_task_revision,
        decisionActionId=decision_action_id,
        decisionActionKind=decision_action_kind,
        decisionErrorCode=decision_error_code,
        redactedStateHash=(
            redacted_state_hash(current_state) if current_state is not None else None
        ),
        errorCode=error_code,
    )
    return event.model_dump(by_alias=True, mode="json")


def _resolve_action(
    state: GraphV2State,
    deps: GraphV2Runtime,
    natural_action: str,
) -> dict[str, Any]:
    """Apply the bounded transition budget; return routing-field updates.

    Natural 'continue' actions are normalised to the V1 limit contract so the
    surrounding harness boundary block sees ``action == continue_to_executor``
    plus ``transition_limit_reached`` — identical to ``react_graph``.
    """
    next_count = state["transition_count"] + 1
    limit_reached = False
    outcome: str | None = None
    if natural_action in CONTINUE_ACTIONS:
        if next_count >= deps.max_transitions:
            natural_action = "continue_to_executor"
            limit_reached = True
            outcome = "max_transitions_exceeded"
    else:
        outcome = natural_action
    return {
        "action": natural_action,
        "transition_count": next_count,
        "transition_limit_reached": limit_reached,
        "terminal_outcome": outcome,
    }


async def _finish_node(
    state: GraphV2State,
    deps: GraphV2Runtime,
    *,
    node_name: str,
    entered_because: str,
    events: list[dict[str, Any]],
    result: Any,
    natural_action: str,
    start_ts: float,
    tool_name: str | None = None,
    model_name: str | None = None,
    model_call_id: str | None = None,
    decision_binding_hash: str | None = None,
    decision_view_hash: str | None = None,
    decision_task_revision: int | None = None,
    decision_action_id: str | None = None,
    decision_action_kind: str | None = None,
    decision_error_code: str | None = None,
    error_code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Await on_transition, emit the end event, and assemble the return dict."""
    resolved = _resolve_action(state, deps, natural_action)
    if resolved["action"] != natural_action:
        result = result.model_copy(update={"action": resolved["action"]})
    if deps.on_transition is not None:
        await deps.on_transition(result)
    end_event = _node_event(
        node_name,
        "end",
        current_state=result.task_state,
        entered_because=entered_because,
        route_decision=resolved["action"],
        revision_after=result.task_state.revision,
        duration_ms=(time.perf_counter() - start_ts) * 1000,
        tool_name=tool_name,
        model_name=model_name,
        model_call_id=model_call_id,
        decision_binding_hash=decision_binding_hash,
        decision_view_hash=decision_view_hash,
        decision_task_revision=decision_task_revision,
        decision_action_id=decision_action_id,
        decision_action_kind=decision_action_kind,
        decision_error_code=decision_error_code,
        error_code=error_code,
    )
    updates: dict[str, Any] = {
        "task_state": result.task_state,
        "action": resolved["action"],
        "transition_count": resolved["transition_count"],
        "transition_limit_reached": resolved["transition_limit_reached"],
        "terminal_outcome": resolved["terminal_outcome"],
        "last_route_decision": resolved["action"],
        "transitions": [result],
        "node_events": [*events, end_event],
        # Day-2: the durable checkpoint records the live revision this node
        # reached, so a restart reconciles checkpoint vs live TaskState.
        "graph_revision": result.task_state.revision,
    }
    if resolved["terminal_outcome"] == "max_transitions_exceeded":
        updates["degraded_reason"] = "max_transitions_exceeded"
    if extra:
        updates.update(extra)
    return updates


def _error_code_of(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(exc).__name__


def _record_error_end_event(
    deps: GraphV2Runtime,
    *,
    node_name: str,
    entered_because: str,
    current: TaskState | None,
    start_ts: float,
    exc: BaseException,
    model_name: str | None = None,
    model_call_id: str | None = None,
    decision_binding_hash: str | None = None,
    decision_view_hash: str | None = None,
    decision_task_revision: int | None = None,
    decision_action_id: str | None = None,
    decision_action_kind: str | None = None,
    decision_error_code: str | None = None,
) -> None:
    """Record a redacted error end event to the trace before re-raising."""
    event = _node_event(
        node_name,
        "end",
        current_state=current,
        entered_because=entered_because,
        route_decision="error",
        duration_ms=(time.perf_counter() - start_ts) * 1000,
        model_name=model_name,
        model_call_id=model_call_id,
        decision_binding_hash=decision_binding_hash,
        decision_view_hash=decision_view_hash,
        decision_task_revision=decision_task_revision,
        decision_action_id=decision_action_id,
        decision_action_kind=decision_action_kind,
        decision_error_code=decision_error_code,
        error_code=_error_code_of(exc),
    )
    deps.trace_builder.record_graph_v2_events([event])


async def entry_node(
    state: GraphV2State,
    runtime: Runtime[GraphV2Runtime],
) -> dict[str, Any]:
    """Recover an abandoned Executor claim, then route planner vs replanner.

    This node performs no business phase — it only restores the durable claim
    boundary and picks the entry business node, so it is never a single-node
    wrapper around the Harness.

    Day-2 durable mode rehydrates the LIVE TaskState first: the checkpoint never
    carries TaskState (UntrackedValue), so a restarted graph has no
    ``task_state`` key and must reload the current revision from the real store.
    """
    deps = runtime.context
    current = state.get("task_state")
    if deps.durable:
        current = await _hydrate_task_state(state, deps)
    if current is None:
        raise RuntimeError("V2 graph started without a task_state to route on")
    current = await recover_expired_executor_claim(
        current,
        durable_inbox=(
            deps.durable_tool_boundary.inbox
            if deps.durable and deps.durable_tool_boundary is not None
            else None
        ),
        force_unknown=bool(deps.durable and deps.recovery_mode),
    )
    route = "replanner" if should_run_replanner(current) else "planner"
    if deps.control_policy == "react_v1":
        if _react_scope_eligible(current):
            recovery = (current.domain_state or {}).get("executorRecovery")
            route = (
                "executor"
                if isinstance(recovery, dict)
                and recovery.get("status") == "UNKNOWN"
                and state.get("react_action_id")
                else "react_policy"
            )
        else:
            route = "react_scope_ineligible"
    reject_ineligible_clarification = bool(
        deps.control_policy == "react_v1"
        and route == "react_scope_ineligible"
        and not _react_server_boundary_clarification_allowed(current)
    )
    if deps.durable and (
        current.status == "collecting_information" and current.pending_questions
    ) and not reject_ineligible_clarification and not (
        deps.control_policy == "react_v1"
        and route == "react_policy"
        and _react_adaptive_entry_required(current)
    ):
        # A parked clarification from a prior run must be answered before any
        # business phase continues.
        route = "clarification"
    events = [
        _node_event(
            "entry",
            "start",
            current_state=current,
            entered_because="request_start",
        ),
        _node_event(
            "entry",
            "end",
            current_state=current,
            entered_because="request_start",
            route_decision=route,
            revision_after=current.revision,
        ),
    ]
    updates = {
        "task_state": current,
        "last_route_decision": route,
        "node_events": events,
        "graph_revision": current.revision,
        "control_policy": deps.control_policy,
        "policy_revision": CONTROL_POLICY_REVISIONS[deps.control_policy],
    }
    if route == "react_scope_ineligible":
        updates.update({
            "action": "stop_turn",
            "terminal_outcome": "stop_turn",
            "degraded_reason": "react_scope_ineligible",
        })
    return updates
