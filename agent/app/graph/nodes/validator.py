"""Validator node — one explicit graph-visible Check phase.

Reached once a fully executed active Plan has no pending step.  It projects the
ValidatorContextView (context_pack mode), pre-validates it against the current
TaskState, runs the deterministic production ``run_validator_phase`` gate, and
emits redacted node events.
"""

from __future__ import annotations

import re
import time
from typing import Any

from langgraph.runtime import Runtime

from ...harness import (
    HarnessStepResult,
    _build_executed_steps,
    _record_view,
    _validate_view_and_record,
    decide_after_validation,
)
from ...validator import run_validator_phase
from ...control.react_actions import ActionOutcome
from ..runtime import CONTROL_POLICY_REVISIONS, GraphV2Runtime
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

__all__ = ["ValidatorFaultInjected", "set_validator_fault_hook", "validator_node"]


class ValidatorFaultInjected(RuntimeError):
    code = "validator_fault_injected"


_VALIDATOR_FAULT_HOOK: Any = None


def set_validator_fault_hook(hook: Any) -> None:
    global _VALIDATOR_FAULT_HOOK
    _VALIDATOR_FAULT_HOOK = hook


def _maybe_fire_validator_fault() -> None:
    if _VALIDATOR_FAULT_HOOK is not None:
        _VALIDATOR_FAULT_HOOK()


def _current_react_outcome(
    current: Any,
    deps: GraphV2Runtime,
    action_id: str | None,
) -> ActionOutcome | None:
    receipt = (current.domain_state or {}).get("reactV1OutcomeReceipt")
    if not (
        action_id
        and isinstance(receipt, dict)
        and receipt.get("runId") == deps.run_id
        and receipt.get("threadId") == deps.thread_id
        and receipt.get("sessionOwnerHash") == deps.session_owner_hash
        and receipt.get("controlPolicy") == "react_v1"
        and receipt.get("policyRevision") == CONTROL_POLICY_REVISIONS["react_v1"]
        and receipt.get("stateRevision") == current.revision
        and isinstance(receipt.get("outcome"), dict)
    ):
        return None
    try:
        outcome = ActionOutcome.model_validate(receipt["outcome"])
    except Exception:
        return None
    return outcome if outcome.action_id == action_id else None


async def validator_node(
    state: GraphV2State,
    runtime: Runtime[GraphV2Runtime],
) -> dict[str, Any]:
    deps = runtime.context
    current = state.get("task_state")
    entered_because = state.get("last_route_decision") or "ready_for_validation"
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
                node_name="validator",
                entered_because=entered_because,
                live=current,
                events=[
                    _node_event(
                        "validator", "start",
                        current_state=current,
                        entered_because=entered_because,
                    )
                ],
            )
    if current is None:
        raise RuntimeError("validator_node missing task_state")
    events = [
        _node_event(
            "validator", "start",
            current_state=current,
            entered_because=entered_because,
        )
    ]
    replayed_outcome = (
        _current_react_outcome(current, deps, state.get("react_action_id"))
        if deps.durable and state.get("control_policy") == "react_v1"
        else None
    )
    if replayed_outcome is not None:
        deps.trace_builder.record_react_outcome(
            action_id=replayed_outcome.action_id,
            status=replayed_outcome.status,
            validator_outcome=replayed_outcome.validator_outcome,
            state_revision_after=replayed_outcome.state_revision_after,
            retryable=replayed_outcome.retryable,
            error_code=replayed_outcome.error_code,
            observation_ref=replayed_outcome.observation_ref,
        )
        action = (
            "task_completed"
            if replayed_outcome.validator_outcome == "PASSED"
            else "ready_for_replanning"
        )
        result = HarnessStepResult(action=action, task_state=current)
        return await _finish_node(
            state,
            deps,
            node_name="validator",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action=action,
            start_ts=start_ts,
            extra={
                "react_last_outcome": replayed_outcome.model_dump(
                    by_alias=True, mode="json"
                ),
                "skippedDurableReplay": True,
            },
        )
    trace_started = False
    try:
        # Project ValidatorContextView BEFORE the phase (context_pack mode).
        # Durable nodes rebuild from the CURRENT live TaskState revision.
        projector = await _resolve_projector(deps, current)
        validator_view = None
        if projector is not None:
            validator_view = projector.validator_view(
                plan=current.active_plan,
                executed_steps=_build_executed_steps(current),
                phase_task_revision=current.revision,
            )
            _validate_view_and_record(
                validator_view,
                current,
                deps.trace_builder,
                "validator",
                projector=projector,
            )
        deps.trace_builder.start_phase("validator")
        trace_started = True
        if validator_view is not None:
            _record_view(deps.trace_builder, "validator", validator_view)
            deps.trace_builder.record_phase_task_revision(
                "validator", validator_view.phase_task_revision
            )

        react_action_id = state.get("react_action_id")

        def react_outcome_patch(result: Any, next_revision: int) -> dict[str, Any]:
            if not (
                state.get("control_policy") == "react_v1" and react_action_id
            ):
                return {}
            if result.outcome == "passed":
                outcome = ActionOutcome(
                    actionId=react_action_id,
                    status="SUCCEEDED",
                    observationRef=(
                        f"validated-task:{current.task_id}:r{next_revision}"
                    ),
                    validatorOutcome="PASSED",
                    stateRevisionAfter=next_revision,
                    retryable=False,
                    errorCode=None,
                )
            else:
                error_code = re.sub(
                    r"[^a-z0-9_]+",
                    "_",
                    str(result.error_code or "react_validation_rejected").lower(),
                ).strip("_")[:128] or "react_validation_rejected"
                outcome = ActionOutcome(
                    actionId=react_action_id,
                    status="REJECTED",
                    observationRef=None,
                    validatorOutcome="REJECTED",
                    stateRevisionAfter=next_revision,
                    retryable=False,
                    errorCode=error_code,
                )
            return {
                "reactV1OutcomeReceipt": {
                    "runId": deps.run_id,
                    "threadId": deps.thread_id,
                    "sessionOwnerHash": deps.session_owner_hash,
                    "controlPolicy": "react_v1",
                    "policyRevision": CONTROL_POLICY_REVISIONS["react_v1"],
                    "stateRevision": next_revision,
                    "outcome": outcome.model_dump(by_alias=True, mode="json"),
                }
            }

        validator_result, validated_state = await run_validator_phase(
            current,
            context_view=validator_view,
            extra_domain_state_patch_factory=react_outcome_patch,
        )
        _maybe_fire_validator_fault()
        action = decide_after_validation(validator_result)
        deps.trace_builder.end_phase(
            "passed"
            if action == "task_completed"
            else "insufficient_evidence"
            if action == "ready_for_replanning"
            else "failed",
            detail={"decision": action},
            task_revision=current.revision,
        )
        trace_started = False

        result = HarnessStepResult(
            action=action,
            validator_result=validator_result,
            task_state=validated_state,
        )
        extra = None
        if state.get("control_policy") == "react_v1" and react_action_id:
            receipt = (validated_state.domain_state or {}).get(
                "reactV1OutcomeReceipt"
            )
            outcome = ActionOutcome.model_validate(receipt["outcome"])
            deps.trace_builder.record_react_outcome(
                action_id=outcome.action_id,
                status=outcome.status,
                validator_outcome=outcome.validator_outcome,
                state_revision_after=outcome.state_revision_after,
                retryable=outcome.retryable,
                error_code=outcome.error_code,
                observation_ref=outcome.observation_ref,
            )
            extra = {
                "react_last_outcome": outcome.model_dump(
                    by_alias=True, mode="json"
                )
            }
        return await _finish_node(
            state,
            deps,
            node_name="validator",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action=action,
            start_ts=start_ts,
            model_name=None,
            extra=extra,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after event recording
        if trace_started:
            deps.trace_builder.end_phase("exception")
        _record_error_end_event(
            deps,
            node_name="validator",
            entered_because=entered_because,
            current=current,
            start_ts=start_ts,
            exc=exc,
        )
        raise
