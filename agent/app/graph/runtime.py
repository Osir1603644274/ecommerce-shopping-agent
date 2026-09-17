"""Static dependencies injected into V2 graph nodes via LangGraph runtime.

Nodes receive ``runtime: Runtime[GraphV2Runtime]`` and read ``runtime.context``.
The model cannot modify these dependencies — the dataclass is frozen per
invocation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from ..agent_trace import TraceBuilder
from ..context_view import ContextProjector
from ..executor import ToolCaller
from ..harness import HarnessStepResult
from ..task_state import TaskState

CONTROL_POLICY_REVISIONS = {
    "fixed_v1": "fixed-v1",
    "react_v1": "react-v1-2026-08-27",
}

__all__ = ["CONTROL_POLICY_REVISIONS", "GraphV2Runtime", "ProjectorFactory"]

ToolSchemaResolver = Callable[[TaskState], list[dict[str, Any]]]
TransitionCallback = Callable[[HarnessStepResult], Awaitable[None]]
ModelCallCallback = Callable[..., None]
ModelCallReceiptCallback = Callable[..., None]
# Day-2: rebuild a ContextProjector from the CURRENT TaskState revision.  Used
# by durable nodes so a resumed graph projects views from post-answer / post-plan
# live state instead of a stale pre-run pack (requirement #4).
ProjectorFactory = Callable[[TaskState], Awaitable[ContextProjector]]
ClarificationAnswerApplier = Callable[[TaskState, str], Awaitable[TaskState]]


@dataclass(frozen=True)
class GraphV2Runtime:
    """Static dependencies injected into graph nodes for one request."""

    user_message: str
    client: AsyncOpenAI
    model: str
    resolve_tool_schemas: ToolSchemaResolver
    tool_caller: ToolCaller
    trace_builder: TraceBuilder
    projector: ContextProjector | None
    max_transitions: int
    system_policies: dict[str, Any] | None = None
    on_transition: TransitionCallback | None = None
    # Fail-closed backstop on top of the Replanner contract's own
    # maxReplanAttempts gate — the graph must never loop on replans.
    max_replans: int = 3
    # ── Day-2 durable fields (all server-owned; never model-specified) ──
    # Server-bound identity assigned by the durable runner / caller.
    task_id: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    # Non-reversible binding for the server-owned TaskState session.  This can
    # safely travel through a checkpoint without exposing raw session data.
    session_owner_hash: str | None = None
    # Rebuild a fresh ContextProjector from the live TaskState revision at
    # node-execution time.  Durable nodes use this (when set) instead of the
    # request-scoped ``projector`` so a resumed/restarted graph always projects
    # from the CURRENT state — the clarification answer, a persisted plan, etc.
    projector_factory: ProjectorFactory | None = None
    # Server-owned semantic application after the durable receipt is written.
    # It reuses the ordinary TaskState extraction/validation boundary so a
    # clarification answer changes structured constraints, not only an audit
    # hash. Direct graph callers may omit it.
    clarification_answer_applier: ClarificationAnswerApplier | None = None
    # The durable plain-Redis checkpointer (GraphV2CheckpointSaver).  When set
    # the graph compiles ``build_graph_v2_durable`` and nodes may consult the
    # live checkpoint hash for durable events.
    checkpointer: Any | None = None
    # Set once the durable runner has persisted a checkpoint for the current
    # superstep; nodes/events embed it as ``checkpointHash``.
    checkpoint_hash: str | None = None
    # True when running through the durable runner (drives the executor's
    # durable skip guard and the clarification interrupt node).
    durable: bool = False
    # True only for an explicit process-restart invocation. It permits the
    # executor to close an IN_FLIGHT Inbox ambiguity immediately instead of
    # waiting for the dead worker's lease deadline.
    recovery_mode: bool = False
    # The durable path never invokes ``tool_caller`` directly.  This is the
    # explicit V2 Inbox/ledger boundary carrying ToolExecutionContext.
    durable_tool_boundary: Any | None = None
    # Explicit policy selection. react_v1 replaces only the decision nodes;
    # executor, validator, ToolInbox, OCC, and interrupt boundaries stay shared.
    control_policy: str = "fixed_v1"
    react_max_model_decisions: int = 2
    react_decision_timeout_seconds: float = 15.0
    on_model_call: ModelCallCallback | None = None
    on_model_call_receipt: ModelCallReceiptCallback | None = None
    # Request-only issued memory capability and before-effect revocation check.
    memory_run_binding: Any | None = None
    memory_guard: Callable[..., Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        from ..settings import settings
        if settings.product_knowledge_enabled:
            # One request-owned value shared by Planner and Executor views.
            # Do not add it only in Planner: execution must resolve the same source.
            object.__setattr__(self, 'system_policies', {
                **(self.system_policies or {}),
                'productKnowledgeUserQuery': self.user_message,
            })
