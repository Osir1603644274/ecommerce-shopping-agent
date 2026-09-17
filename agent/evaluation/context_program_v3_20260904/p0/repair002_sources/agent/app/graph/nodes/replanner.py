"""Replanner node — one explicit graph-visible Recover phase.

Reached after a Validator rejected the active Plan for insufficient evidence
(``ready_for_replanning``).  It projects the ReplannerContextView (context_pack
mode), pre-validates it, enforces the graph-level ``max_replans`` backstop, and
runs the production ``run_replanner_phase`` gate.  The Replanner's own
maxReplanAttempts contract still applies inside the phase — this node's backstop
is a second, fail-closed bound so the graph can never loop on replans.
"""

from __future__ import annotations

import time
from typing import Any

from langgraph.runtime import Runtime

from ...harness import (
    HarnessStepResult,
    _candidate_tool_names,
    _make_plan_summary,
    _record_view,
    _reusable_step_output_payloads,
    _validate_view_and_record,
    decide_after_replanning,
)
from ...replanner import run_replanner_phase
from ..runtime import GraphV2Runtime
from ..state import GraphV2State
from . import (
    _finish_node,
    _hydrate_task_state,
    _node_event,
    _reconcile_revision,
    _record_error_end_event,
    _resolve_projector,
    _state_diverged_updates,
)

__all__ = ["replanner_node"]


async def replanner_node(
    state: GraphV2State,
    runtime: Runtime[GraphV2Runtime],
) -> dict[str, Any]:
    deps = runtime.context
    current = state.get("task_state")
    entered_because = state.get("last_route_decision") or "ready_for_replanning"
    start_ts = time.perf_counter()
    if deps.durable:
        # TaskState never enters a checkpoint, so a restarted graph has no
        # ``task_state`` — always rehydrate the current revision from Redis.
        current = await _hydrate_task_state(state, deps)
        reconcile = await _reconcile_revision(state, deps, current)
        if reconcile == "diverged":
            return _state_diverged_updates(
                state=state,
                deps=deps,
                node_name="replanner",
                entered_because=entered_because,
                live=current,
                events=[
                    _node_event(
                        "replanner", "start",
                        current_state=current,
                        entered_because=entered_because,
                    )
                ],
            )
    if current is None:
        raise RuntimeError("replanner_node missing task_state")
    events = [
        _node_event(
            "replanner", "start",
            current_state=current,
            entered_because=entered_because,
        )
    ]
    trace_started = False
    try:
        # Fail-closed graph-level backstop: never loop on replans even if the
        # Replanner's own attempt gate were bypassed.
        if state.get("replan_count", 0) >= deps.max_replans:
            result = HarnessStepResult(action="stop_turn", task_state=current)
            return await _finish_node(
                state,
                deps,
                node_name="replanner",
                entered_because=entered_because,
                events=events,
                result=result,
                natural_action="stop_turn",
                start_ts=start_ts,
                model_name=deps.model,
                extra={
                    "replan_count": state.get("replan_count", 0) + 1,
                    "degraded_reason": "max_replans_exceeded",
                },
            )

        schemas = deps.resolve_tool_schemas(current)
        # Project ReplannerContextView BEFORE the phase (context_pack mode),
        # mirroring the V1 entry replanner path.  Durable nodes rebuild from the
        # CURRENT live TaskState revision.
        projector = await _resolve_projector(deps, current)
        replanner_view = None
        if projector is not None:
            raw_failure = current.domain_state.get("validationResult", {})
            max_attempts = int(
                (deps.system_policies or {}).get("maxReplanAttempts", 3)
            )
            replanner_view = projector.replanner_view(
                failed_plan_summary=_make_plan_summary(current),
                failed_plan=current.active_plan,
                failure=raw_failure if isinstance(raw_failure, dict) else {},
                failure_reason="Validator rejected: insufficient evidence",
                remaining_tool_names=_candidate_tool_names(schemas),
                candidate_tool_schemas=schemas,
                system_policies=deps.system_policies,
                replan_attempt=int(
                    current.domain_state.get("replanAttemptCount", 0)
                ) + 1,
                max_replan_attempts=max_attempts,
                user_message=deps.user_message,
                reusable_step_outputs=_reusable_step_output_payloads(current),
                phase_task_revision=current.revision,
            )
            _validate_view_and_record(
                replanner_view,
                current,
                deps.trace_builder,
                "replanner",
                projector=projector,
            )
        deps.trace_builder.start_phase("replanner")
        trace_started = True
        if replanner_view is not None:
            _record_view(deps.trace_builder, "replanner", replanner_view)
            deps.trace_builder.record_phase_task_revision(
                "replanner", replanner_view.phase_task_revision
            )

        replanner_result, replanned_state = await run_replanner_phase(
            current,
            deps.user_message,
            schemas,
            client=deps.client,
            model=deps.model,
            system_policies=deps.system_policies,
            context_view=replanner_view,
        )
        action = decide_after_replanning(replanner_result)
        deps.trace_builder.end_phase(
            replanner_result.outcome if replanner_result is not None else "failed"
        )
        trace_started = False

        result = HarnessStepResult(
            action=action,
            replanner_result=replanner_result,
            task_state=replanned_state,
        )
        return await _finish_node(
            state,
            deps,
            node_name="replanner",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action=action,
            start_ts=start_ts,
            model_name=deps.model,
            extra={"replan_count": state.get("replan_count", 0) + 1},
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after event recording
        if trace_started:
            deps.trace_builder.end_phase("exception")
        _record_error_end_event(
            deps,
            node_name="replanner",
            entered_because=entered_because,
            current=current,
            start_ts=start_ts,
            exc=exc,
        )
        raise
