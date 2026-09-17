"""Agent run tracing with swappable TraceStore (default Redis, TTL 24h).

Each Agent request gets a runId.  The trace records every observable transition:
Planner, Executor, Validator, Replanner, tool calls, ContextPack hash, RecommendationDraft,
Critic state, and final outcomes.

Full traces are ONLY available via a gated debug interface — not in chat responses.
"""

from __future__ import annotations

import json
import hashlib
import logging
import time
import sys
from datetime import datetime, timezone
from typing import Any, Literal

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field

from .settings import settings

logger = logging.getLogger(__name__)

DEFAULT_TRACE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_TRACE_KEY_PREFIX = "agent-run-trace"
TRACE_DEBUG_HEADER = "X-Agent-Debug-Key"

# ── Trace domain models ──────────────────────────────────────────────────────


class PhaseTrace(BaseModel):
    """Timing and outcome for one lifecycle phase."""

    phase: str  # planner, executor, validator, replanner, final_answer
    code_location: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None
    outcome: str | None = None  # planned, step_executed, passed, etc.
    detail: dict[str, Any] | None = None
    view_hash: str | None = Field(default=None)  # hash of the ContextView used
    view_token_count: int | None = Field(default=None)  # token count of the ContextView
    task_revision: int | None = Field(default=None, alias="taskRevision")  # TaskState revision at this phase


class ToolCallTrace(BaseModel):
    """One tool invocation recorded during a run."""

    tool_name: str
    arguments_summary: str | None = None  # NEVER full request body; hash or key list
    ok: bool
    duration_ms: float | None = None
    degraded: bool = False
    error_code: str | None = None


class RecommendationDraftTrace(BaseModel):
    """Summary of a RecommendationDraft for the trace."""

    selected_product_ids: list[int] = Field(default_factory=list)
    claims_count: int = 0
    unknown_count: int = 0
    evidence_refs_count: int = 0


class CriticTrace(BaseModel):
    """Summary of EvidenceCritic output for the trace."""

    approved: bool | None = None
    issue_count: int = 0
    issue_codes: list[str] = Field(default_factory=list)
    critic_version: str | None = None
    duration_ms: float | None = None
    skipped: bool = False
    skip_reason: str | None = None


class AgentRunTrace(BaseModel):
    """Full trace of one Agent request from start to final response."""

    model_config = ConfigDict(populate_by_name=True)

    # Identity
    run_id: str = Field(alias="runId")
    task_id: str | None = Field(default=None, alias="taskId")
    session_id: str | None = Field(default=None, alias="sessionId")
    started_at: str = Field(alias="startedAt")
    finished_at: str | None = Field(default=None, alias="finishedAt")
    total_duration_ms: float | None = Field(default=None, alias="totalDurationMs")

    # Mode
    mode: str = "legacy"  # legacy | context_pack | context_pack+view
    strategy_version: str = "1.0"
    # The actual control runtime entered by this request. ``None`` means the
    # request stopped or answered before any Harness runtime was entered.
    entered_runtime: str | None = Field(default=None, alias="enteredRuntime")
    # Keep the durable execution substrate distinct from the selected policy.
    # Paired evidence must never infer policy from the route or runtime label.
    control_policy: str | None = Field(default=None, alias="controlPolicy")
    policy_revision: str | None = Field(default=None, alias="policyRevision")

    # Context
    context_pack_hash: str | None = Field(default=None, alias="contextPackHash")
    context_token_count: int | None = Field(default=None, alias="contextTokenCount")

    # ContextViews
    context_views: list[dict[str, Any]] = Field(default_factory=list, alias="contextViews")

    # TaskState snapshots
    task_revision_before: int | None = Field(default=None, alias="taskRevisionBefore")
    task_revision_after: int | None = Field(default=None, alias="taskRevisionAfter")

    # Context revision tracking
    base_context_revision: int | None = Field(default=None, alias="baseContextRevision")
    phase_task_revisions: dict[str, int] = Field(default_factory=dict, alias="phaseTaskRevisions")

    # Boundary gate failures (fail-closed: phase, tool, and Validator counts are all 0)
    context_boundary_mismatches: list[dict[str, Any]] = Field(default_factory=list, alias="contextBoundaryMismatches")

    # Phase timeline
    phases: list[PhaseTrace] = Field(default_factory=list)

    # Redacted node-level event source from the V2 control-plane graph
    # (one start/end pair per explicit node).  Same redaction rules as the
    # GraphV2NodeEvent model — never prompts, credentials, or raw payloads.
    graph_v2_events: list[dict[str, Any]] = Field(
        default_factory=list, alias="graphV2Events"
    )

    # Redacted ReAct decision audit. Never stores prompts, user text, questions,
    # answer bodies, raw candidates, or concrete tool arguments.
    react_decisions: list[dict[str, Any]] = Field(
        default_factory=list, alias="reactDecisions"
    )

    # Redacted outcomes from executable ReAct actions. Observation references
    # are hashed so scope identities and answer bodies never enter the trace.
    react_outcomes: list[dict[str, Any]] = Field(
        default_factory=list, alias="reactOutcomes"
    )

    # Public-safe continuation identity for a real durable clarification.
    # It contains no prompt, answer, credentials, evidence, or session hash.
    durable_resume: dict[str, Any] | None = Field(
        default=None, alias="durableResume"
    )
    checkpoint_pause: dict[str, Any] | None = Field(
        default=None, alias="checkpointPause"
    )

    # Tool calls
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)

    # Domain results
    recommendation: RecommendationDraftTrace | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    critic: CriticTrace | None = None

    # Final state
    final_action: str | None = Field(default=None, alias="finalAction")
    error: str | None = None
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)


class TracePhaseSummary(BaseModel):
    """Public, detail-free timing for one Agent lifecycle phase."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    phase: str
    outcome: str | None = None
    duration_ms: float | None = Field(default=None, alias="durationMs", ge=0)


class TraceSummary(BaseModel):
    """Lightweight trace summary safe to include in every chat response."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(alias="runId")
    phase_count: int = 0
    tool_call_count: int = 0
    total_duration_ms: float | None = Field(default=None, alias="totalDurationMs")
    degraded: bool = False
    agent_status: Literal["ok", "failed"] = Field(default="ok", alias="agentStatus")
    final_action: str | None = Field(default=None, alias="finalAction")
    failure_code: str | None = Field(default=None, alias="failureCode")
    entered_runtime: str | None = Field(default=None, alias="enteredRuntime")
    control_policy: str | None = Field(default=None, alias="controlPolicy")
    policy_revision: str | None = Field(default=None, alias="policyRevision")
    durable_resume: dict[str, Any] | None = Field(
        default=None, alias="durableResume"
    )
    checkpoint_pause: dict[str, Any] | None = Field(
        default=None, alias="checkpointPause"
    )
    phases: list[TracePhaseSummary] = Field(default_factory=list)


# ── Trace store interface ────────────────────────────────────────────────────


class TraceStore:
    """Default Redis-backed trace store. Swappable for testing."""

    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client: redis.Redis | None = client

    def _get_client(self) -> redis.Redis:
        if self._client is not None:
            return self._client
        self._client = redis.from_url(settings.redis_url, decode_responses=True)
        return self._client

    def _key(self, run_id: str) -> str:
        return f"{_TRACE_KEY_PREFIX}:{run_id}"

    async def save(self, trace: AgentRunTrace) -> bool:
        """Save a trace to Redis. Returns True on success, False on failure."""
        try:
            client = self._get_client()
            key = self._key(trace.run_id)
            payload = trace.model_dump_json(by_alias=True)
            await client.set(key, payload)
            ttl = int(settings.agent_trace_ttl_seconds)
            if ttl > 0:
                await client.expire(key, ttl)
            return True
        except Exception:
            logger.exception("Failed to save trace %s to Redis", trace.run_id)
            return False

    async def get(self, run_id: str) -> AgentRunTrace | None:
        client = self._get_client()
        raw = await client.get(self._key(run_id))
        if raw is None:
            return None
        return AgentRunTrace.model_validate_json(raw)

    async def delete(self, run_id: str) -> None:
        await self._get_client().delete(self._key(run_id))


# Global store singleton (replaceable for tests)
_trace_store: TraceStore | None = None


def get_trace_store() -> TraceStore:
    global _trace_store
    if _trace_store is None:
        _trace_store = TraceStore()
    return _trace_store


def set_trace_store(store: TraceStore) -> None:
    global _trace_store
    _trace_store = store


# ── Trace builder (usage inside harness) ────────────────────────────────────


class TraceBuilder:
    """Stateful builder to accumulate trace events during one Agent request.

    Uses a phase STACK so nested phases (e.g., validator inside harness_step)
    are recorded independently — the outer phase does NOT overwrite the inner.
    """

    def __init__(self, run_id: str, *, mode: str = "legacy") -> None:
        self._trace = AgentRunTrace(
            run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            mode=mode,
        )
        self._phase_stack: list[tuple] = []
        self._start_ns = time.perf_counter()

    @property
    def run_id(self) -> str:
        return self._trace.run_id

    def set_context(self, task_id: str | None, session_id: str | None) -> None:
        self._trace.task_id = task_id
        self._trace.session_id = session_id

    def set_context_pack(self, pack_hash: str, token_count: int) -> None:
        self._trace.context_pack_hash = pack_hash
        self._trace.context_token_count = token_count

    def set_entered_runtime(self, runtime: str) -> None:
        self._trace.entered_runtime = runtime

    def set_control_policy(self, policy: str, revision: str) -> None:
        self._trace.control_policy = policy
        self._trace.policy_revision = revision

    def record_context_view(
        self,
        view_type: str,
        view_hash: str,
        token_count: int,
    ) -> None:
        """Record a ContextView that was projected for a lifecycle phase."""
        self._trace.context_views.append({
            "type": view_type,
            "hash": view_hash,
            "tokenCount": token_count,
        })

    def record_react_decision(
        self,
        *,
        status: str,
        decision_source: str | None = None,
        task_revision: int,
        view_hash: str | None,
        view_token_count: int | None = None,
        adaptive_trigger: str | None = None,
        action_id: str | None = None,
        action_kind: str | None = None,
        option_id: str | None = None,
        published_option_ids: list[str] | None = None,
        reason_code: str | None = None,
        tool_name: str | None = None,
        model_name: str | None = None,
        model_call_id: str | None = None,
        decision_binding_hash: str | None = None,
        error_code: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Record one bounded, redacted ReAct decision observation."""

        self._trace.react_decisions.append({
            "status": status,
            "decisionSource": decision_source,
            "taskRevision": task_revision,
            "viewHash": view_hash,
            "viewTokenCount": view_token_count,
            "adaptiveTrigger": adaptive_trigger,
            "actionId": action_id,
            "actionKind": action_kind,
            "optionId": option_id,
            "publishedOptionIds": list(published_option_ids or []),
            "reasonCode": reason_code,
            "toolName": tool_name,
            "modelName": model_name,
            "modelCallId": model_call_id,
            "decisionBindingHash": decision_binding_hash,
            "errorCode": error_code,
            "durationMs": (
                round(duration_ms, 2) if duration_ms is not None else None
            ),
        })

    def record_react_outcome(
        self,
        *,
        action_id: str,
        status: str,
        validator_outcome: str,
        state_revision_after: int | None,
        retryable: bool,
        error_code: str | None,
        observation_ref: str | None,
    ) -> None:
        """Record one redacted executable ReAct action result."""

        observation_hash = (
            hashlib.sha256(observation_ref.encode("utf-8")).hexdigest()
            if observation_ref is not None
            else None
        )
        self._trace.react_outcomes.append({
            "actionId": action_id,
            "status": status,
            "validatorOutcome": validator_outcome,
            "stateRevisionAfter": state_revision_after,
            "retryable": retryable,
            "errorCode": error_code,
            "observationRefHash": observation_hash,
        })

    def set_revision_before(self, revision: int) -> None:
        self._trace.task_revision_before = revision

    def set_revision_after(self, revision: int) -> None:
        self._trace.task_revision_after = revision

    def set_base_context_revision(self, revision: int) -> None:
        """Record the TaskState revision at the moment the ContextPack was frozen."""
        self._trace.base_context_revision = revision

    def record_phase_task_revision(self, phase: str, revision: int) -> None:
        """Record the TaskState revision at the moment a phase BEGAN (entry revision).

        This MUST be called BEFORE the phase executes, with the View's
        phaseTaskRevision — NOT the post-persistence revision.
        """
        self._trace.phase_task_revisions[phase] = revision

    def record_context_boundary_mismatch(
        self,
        phase: str,
        error_code: str,
        detail: str,
        *,
        view_phase_revision: int | None = None,
        state_revision: int | None = None,
    ) -> None:
        """Record a fail-closed boundary gate failure.

        When a View's identity metadata does not match the current TaskState,
        the phase, tool call, and Validator are all blocked.  This record is
        the audit trail that proves the gate fired — no phase ran, no tool was
        called, and counts are zero.
        """
        self._trace.context_boundary_mismatches.append({
            "phase": phase,
            "errorCode": error_code,
            "detail": detail,
            "viewPhaseRevision": view_phase_revision,
            "stateRevision": state_revision,
        })
        self._trace.degraded = True
        reason = f"context_boundary_mismatch:{phase}:{error_code}"
        if reason not in self._trace.degraded_reasons:
            self._trace.degraded_reasons.append(reason)

    def start_phase(self, phase: str) -> None:
        """Push a phase onto the stack — nested phases are independent."""
        caller = sys._getframe(1)
        filename = caller.f_code.co_filename.replace('\\', '/')
        location = filename[filename.rfind('/agent/app/') + 1:] if '/agent/app/' in filename else None
        if location:
            location += f':{caller.f_lineno} ({caller.f_code.co_name})'
        self._phase_stack.append((phase, time.perf_counter(), datetime.now(timezone.utc).isoformat(), location))

    def end_phase(
        self,
        outcome: str,
        detail: dict[str, Any] | None = None,
        *,
        view_hash: str | None = None,
        view_token_count: int | None = None,
        task_revision: int | None = None,
    ) -> None:
        """Pop the most recently started phase and record it."""
        if not self._phase_stack:
            return
        phase_name, start_ts, started_at, location = self._phase_stack.pop()
        duration = (time.perf_counter() - start_ts) * 1000
        now = datetime.now(timezone.utc).isoformat()
        self._trace.phases.append(
            PhaseTrace(
                phase=phase_name,
                started_at=started_at,
                code_location=location,
                finished_at=now,
                duration_ms=round(duration, 2),
                outcome=outcome,
                detail=detail,
                view_hash=view_hash,
                view_token_count=view_token_count,
                taskRevision=task_revision,
            )
        )

    def record_tool_call(
        self,
        tool_name: str,
        *,
        ok: bool,
        duration_ms: float | None = None,
        degraded: bool = False,
        error_code: str | None = None,
        arguments_summary: str | None = None,
    ) -> None:
        self._trace.tool_calls.append(
            ToolCallTrace(
                tool_name=tool_name,
                arguments_summary=arguments_summary,
                ok=ok,
                duration_ms=round(duration_ms, 2) if duration_ms else None,
                degraded=degraded,
                error_code=error_code,
            )
        )

    def set_recommendation(self, draft: RecommendationDraftTrace) -> None:
        self._trace.recommendation = draft

    def set_evidence_refs(self, refs: list[str]) -> None:
        self._trace.evidence_refs = list(refs)

    def set_critic(self, critic: CriticTrace) -> None:
        self._trace.critic = critic

    def record_graph_v2_events(self, events: list[dict[str, Any]]) -> None:
        """Append redacted V2 graph node events to the trace.

        Events are already redacted by the emitting node (see
        ``graph/state.py::GraphV2NodeEvent``); this only persists them so the
        gated debug trace exposes the node-level control flow.
        """
        self._trace.graph_v2_events.extend(list(events))

    def set_durable_resume(
        self,
        *,
        task_id: str,
        run_id: str,
        thread_id: str,
        revision: int,
        proposal_hash: str,
    ) -> None:
        self._trace.durable_resume = {
            "taskId": task_id,
            "runId": run_id,
            "threadId": thread_id,
            "revision": revision,
            "proposalHash": proposal_hash,
        }

    def set_checkpoint_pause(self, receipt: dict[str, Any]) -> None:
        """Expose only the public-safe checkpoint pause receipt."""

        self._trace.checkpoint_pause = dict(receipt)

    def set_final(self, action: str, *, error: str | None = None) -> None:
        self._trace.final_action = action
        if error:
            self._trace.error = error

    def mark_degraded(self, reason: str) -> None:
        self._trace.degraded = True
        if reason not in self._trace.degraded_reasons:
            self._trace.degraded_reasons.append(reason)

    def finish(self) -> AgentRunTrace:
        # Close any unclosed phases (shouldn't happen in normal flow)
        while self._phase_stack:
            phase_name, start_ts, started_at, location = self._phase_stack.pop()
            duration = (time.perf_counter() - start_ts) * 1000
            now = datetime.now(timezone.utc).isoformat()
            self._trace.phases.append(
                PhaseTrace(
                    phase=phase_name,
                    started_at=started_at,
                    code_location=location,
                    finished_at=now,
                    duration_ms=round(duration, 2),
                    outcome="unclosed",
                )
            )
        self._trace.total_duration_ms = round(
            (time.perf_counter() - self._start_ns) * 1000, 2
        )
        self._trace.finished_at = datetime.now(timezone.utc).isoformat()
        return self._trace

    def summary(self) -> TraceSummary:
        failure_code = (
            self._trace.degraded_reasons[0]
            if self._trace.degraded_reasons
            else self._trace.error
        )
        total_duration_ms = self._trace.total_duration_ms
        if total_duration_ms is None:
            total_duration_ms = round(
                (time.perf_counter() - self._start_ns) * 1000,
                2,
            )
        return TraceSummary(
            run_id=self._trace.run_id,
            phase_count=len(self._trace.phases),
            tool_call_count=len(self._trace.tool_calls),
            total_duration_ms=total_duration_ms,
            degraded=self._trace.degraded,
            agent_status=(
                "failed" if self._trace.degraded or self._trace.error else "ok"
            ),
            final_action=self._trace.final_action,
            failure_code=failure_code,
            entered_runtime=self._trace.entered_runtime,
            control_policy=self._trace.control_policy,
            policy_revision=self._trace.policy_revision,
            durable_resume=self._trace.durable_resume,
            checkpoint_pause=self._trace.checkpoint_pause,
            # Public responses expose only the phase name, outcome and timing.
            # Full phase detail, ContextView hashes and revisions remain behind
            # the gated AgentRunTrace debug endpoint.
            phases=[
                {
                    "phase": item.phase,
                    "outcome": item.outcome,
                    "durationMs": item.duration_ms,
                }
                for item in self._trace.phases
            ],
        )


# ── Debug access gate ────────────────────────────────────────────────────────


def trace_debug_enabled() -> bool:
    """Check whether the debug gate is open via environment variable."""
    return settings.agent_trace_debug_enabled


def validate_debug_key(request_key: str | None) -> bool:
    """Validate the debug key against the configured value."""
    if not request_key:
        return False
    configured = settings.agent_trace_debug_key
    if not configured:
        return False
    return request_key == configured


async def get_trace_for_debug(run_id: str, debug_key: str) -> AgentRunTrace | None:
    """Gated trace retrieval. Returns None if debug is disabled or key is wrong."""
    if not trace_debug_enabled():
        return None
    if not validate_debug_key(debug_key):
        return None
    return await get_trace_store().get(run_id)
