"""Minimal LangGraph V2 control-plane state.

``GraphV2State`` carries ONLY the routing-relevant fields: task identity and
revision, the most recent phase action, the bounded transition log, the
terminal outcome, and a redacted node-event log.  No API keys, no prompts, no
raw ToolTrace payloads, no dependency containers, and no user history ever
enter graph state — static dependencies live in ``GraphV2Runtime`` (injected
via LangGraph's runtime context).

TaskState remains the single durable business truth.  The Day-1 graph is
compiled without a checkpointer; the Day-2 durable graph persists only the
bounded routing/identity channels through ``GraphV2CheckpointSaver``, while
``task_state`` and ``transitions`` are ``UntrackedValue`` channels that never
reach a checkpoint (verified by probe).
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, TypedDict

from langgraph.channels import UntrackedValue
from pydantic import BaseModel, ConfigDict, Field

from ..harness import HarnessStepResult
from ..task_state import TaskState

__all__ = [
    "CONTINUE_ACTIONS",
    "GraphV2State",
    "GraphV2NodeEvent",
    "redacted_state_snapshot",
    "redacted_state_hash",
    "append_node_events",
    "MAX_NODE_EVENTS",
]

# Actions that route the graph to another business node (as opposed to a
# user-facing terminal boundary).  When the transition budget is exhausted the
# nodes normalise these to the V1 "continue_to_executor" + limit flag contract
# so the surrounding harness boundary block behaves identically.
CONTINUE_ACTIONS = {
    "continue_to_executor",
    "ready_for_validation",
    "ready_for_replanning",
}

# Bounded redacted node-event summary persisted inside each durable checkpoint
# (requirement #9 keeps the checkpoint payload small and allowlisted).
MAX_NODE_EVENTS = 64


def append_node_events(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Accumulate redacted node events, keeping only the most recent bounded set."""
    merged = existing + new
    if len(merged) > MAX_NODE_EVENTS:
        return merged[-MAX_NODE_EVENTS:]
    return merged


class GraphV2State(TypedDict, total=False):
    """Mutable state for one LangGraph V2 invocation."""

    # UntrackedValue: the live durable TaskState is rehydrated from the real
    # store by every node; it must never enter a checkpoint (requirement #3).
    task_state: Annotated[TaskState, UntrackedValue]
    # Server-bound identity channels (persisted in durable checkpoints).
    task_id: str
    thread_id: str | None
    session_owner_hash: str | None
    graph_revision: int
    plan_id: str | None
    # Most recent node's HarnessAction (drives the conditional routers).
    action: str
    # Number of graph-node executions performed so far (safety budget).
    transition_count: int
    # Number of times the replanner node has run (safety budget).
    replan_count: int
    transition_limit_reached: bool
    # Set only when the graph reaches a user-facing terminal boundary.
    terminal_outcome: str | None
    degraded_reason: str | None
    # routeDecision of the previous node — becomes the next node's
    # ``enteredBecause``, keeping the event source self-describing.
    last_route_decision: str | None
    # UntrackedValue: full per-step transitions never enter a checkpoint.
    transitions: Annotated[list[HarnessStepResult], UntrackedValue]
    # Bounded, accumulated observable transitions (one per node execution).
    node_events: Annotated[list[dict[str, Any]], append_node_events]
    # Bounded ReAct V1 receipts. No prompts, history, tool payloads, or raw
    # evidence are persisted in these channels.
    control_policy: str
    policy_revision: str
    react_model_decision_count: int
    react_action_id: str | None
    react_action_kind: str | None
    react_answer_context_ref: str | None
    react_last_outcome: dict[str, Any] | None


class GraphV2NodeEvent(BaseModel):
    """One redacted node-level event emitted by a real V2 graph execution.

    This is the Day-1 unified event source for the eventual single-step graph
    debugger.  It never carries prompts, credentials, user messages, raw
    ToolTrace payloads, or raw evidence big objects.  Day-2 adds the durable
    identity/checkpoint fields (threadId/taskId/checkpointHash) so restart
    recovery evidence keeps the same redacted event source (requirement #9).
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    node_name: str = Field(alias="nodeName")
    phase: str  # "start" | "end"
    code_source: str = Field(alias="codeSource")
    entered_because: str | None = Field(default=None, alias="enteredBecause")
    route_decision: str | None = Field(default=None, alias="routeDecision")
    revision_before: int | None = Field(default=None, alias="revisionBefore")
    revision_after: int | None = Field(default=None, alias="revisionAfter")
    duration_ms: float | None = Field(default=None, alias="durationMs")
    tool_name: str | None = Field(default=None, alias="toolName")
    model_name: str | None = Field(default=None, alias="modelName")
    model_call_id: str | None = Field(default=None, alias="modelCallId")
    decision_binding_hash: str | None = Field(
        default=None, alias="decisionBindingHash"
    )
    decision_view_hash: str | None = Field(
        default=None, alias="decisionViewHash"
    )
    decision_task_revision: int | None = Field(
        default=None, alias="decisionTaskRevision"
    )
    decision_action_id: str | None = Field(
        default=None, alias="decisionActionId"
    )
    decision_action_kind: str | None = Field(
        default=None, alias="decisionActionKind"
    )
    decision_error_code: str | None = Field(
        default=None, alias="decisionErrorCode"
    )
    redacted_state_hash: str | None = Field(default=None, alias="redactedStateHash")
    error_code: str | None = Field(default=None, alias="errorCode")
    thread_id: str | None = Field(default=None, alias="threadId")
    task_id: str | None = Field(default=None, alias="taskId")
    checkpoint_hash: str | None = Field(default=None, alias="checkpointHash")


def redacted_state_snapshot(state: TaskState) -> dict[str, Any]:
    """Compact, identity-only projection of TaskState for event hashing.

    Never includes raw tool outputs, user messages, fact values, or PII — only
    routing- and ownership-relevant identifiers and statuses.
    """
    plan = state.active_plan
    validation = state.domain_state.get("validationResult")
    compound = state.domain_state.get("compoundComparison")
    return {
        "taskId": state.task_id,
        "revision": state.revision,
        "status": state.status,
        "plan": (
            {
                "planId": plan.plan_id,
                "status": plan.status,
                "stepStatuses": [step.status for step in plan.steps],
            }
            if plan is not None
            else None
        ),
        "replanAttemptCount": state.domain_state.get("replanAttemptCount"),
        "validationOutcome": (
            validation.get("outcome") if isinstance(validation, dict) else None
        ),
        "compoundComparisonStatus": (
            compound.get("status") if isinstance(compound, dict) else None
        ),
    }


def redacted_state_hash(state: TaskState) -> str:
    """SHA-256 prefix over the redacted state snapshot."""
    payload = json.dumps(
        redacted_state_snapshot(state),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
