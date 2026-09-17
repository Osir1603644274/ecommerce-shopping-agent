"""Planner node — one explicit graph-visible Reason phase.

Reached from the entry router for a ready task without a recoverable failed
Plan.  It runs the production planning gate and emits the redacted node events.
"""

from __future__ import annotations

import time
from typing import Any

from langgraph.runtime import Runtime

from ...harness import (
    HarnessStepResult,
    _record_view,
    _validate_view_and_record,
    run_planning_step,
)
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

__all__ = ["planner_node"]


async def planner_node(
    state: GraphV2State,
    runtime: Runtime[GraphV2Runtime],
) -> dict[str, Any]:
    deps = runtime.context
    current = state.get("task_state")
    entered_because = state.get("last_route_decision") or "entry"
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
                node_name="planner",
                entered_because=entered_because,
                live=current,
                events=[
                    _node_event(
                        "planner", "start",
                        current_state=current,
                        entered_because=entered_because,
                    )
                ],
            )
    if current is None:
        raise RuntimeError("planner_node missing task_state")
    schemas = deps.resolve_tool_schemas(current)
    events = [
        _node_event(
            "planner", "start",
            current_state=current,
            entered_because=entered_because,
        )
    ]
    trace_started = False
    try:
        # Project PlannerContextView BEFORE the phase (context_pack mode).
        projector = await _resolve_projector(deps, current)
        planner_view = None
        if projector is not None:
            tool_names = [
                str(s.get("function", s).get("name", "")) for s in schemas
            ]
            flat_tools: list[dict[str, Any]] = []
            for schema in schemas:
                fn = schema.get("function", schema)
                flat_tools.append(dict(fn) if isinstance(fn, dict) else fn)
            planner_view = projector.planner_view(
                tool_names=tool_names,
                task_status=current.status,
                user_message=deps.user_message,
                candidate_tool_schemas=flat_tools,
                system_policies=deps.system_policies,
                phase_task_revision=current.revision,
            )
            _validate_view_and_record(
                planner_view, current, deps.trace_builder, "planner",
                projector=projector,
            )
        deps.trace_builder.start_phase("planner")
        trace_started = True
        if planner_view is not None:
            _record_view(deps.trace_builder, "planner", planner_view)
            deps.trace_builder.record_phase_task_revision(
                "planner", planner_view.phase_task_revision
            )

        planning_step = await run_planning_step(
            current,
            deps.user_message,
            schemas,
            client=deps.client,
            model=deps.model,
            system_policies=deps.system_policies,
            planner_view=planner_view,
        )
        deps.trace_builder.end_phase(
            planning_step.planner_result.outcome
            if planning_step.planner_result is not None
            else "skipped",
            detail={"plannedTools": [step.tool_name for step in planning_step.task_state.active_plan.steps]}
                if planning_step.task_state.active_plan else None,
            task_revision=current.revision,
        )
        trace_started = False

        result = HarnessStepResult(
            action=planning_step.action,
            planner_result=planning_step.planner_result,
            task_state=planning_step.task_state,
        )
        return await _finish_node(
            state,
            deps,
            node_name="planner",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action=planning_step.action,
            start_ts=start_ts,
            model_name=deps.model,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after event recording
        if trace_started:
            deps.trace_builder.end_phase("exception")
        _record_error_end_event(
            deps,
            node_name="planner",
            entered_because=entered_because,
            current=current,
            start_ts=start_ts,
            exc=exc,
        )
        raise
