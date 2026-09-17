"""Executor node — one explicit graph-visible Act phase.

Reached for an active Plan with a pending linear step.  It projects the
ExecutorContextView (context_pack mode), pre-validates its identity against the
current TaskState, runs exactly one persisted PlanStep through the production
``run_executor_step`` gate, and emits redacted node events.

Day-2 durable mode rehydrates the LIVE TaskState and applies the revision
reconciliation before acting.  The critical skip guard (requirement #8): when
the step was already executed and its receipt persisted but the next graph
checkpoint was not confirmed, the node skips the tool entirely and routes
straight to the Validator — the live tool count stays exactly 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from langgraph.runtime import Runtime

from ...executor import (
    ExecutorSelectionError,
    recover_expired_executor_claim,
    run_executor_step,
    select_next_plan_step,
)
from ...harness import (
    HarnessStepResult,
    _extract_constraint_keys_for_step,
    _extract_fact_keys_for_step,
    _extract_tool_schema_dict,
    _project_prior_step_outputs_for_step,
    _project_step_arguments,
    _record_view,
    _validate_view_and_record,
    decide_after_execution,
)
from ...settings import settings
from ...task_state import TaskStatePatchRequest, _get_client, update_task_state
from ...control.react_actions import (
    ActionOutcome,
    NextAction,
    react_action_anchor_key,
    react_plan_contract_sha256,
)
from ..runtime import CONTROL_POLICY_REVISIONS, GraphV2Runtime
from ..state import GraphV2State
from . import (
    _finish_node,
    _hydrate_task_state,
    _node_event,
    _reconcile_revision,
    _receipt_is_authentic,
    _record_error_end_event,
    _resolve_projector,
    _state_diverged_updates,
)

__all__ = ["executor_node", "ExecutorFaultInjected"]

# The dangerous-window process-kill exit code (requirement #8 E2E evidence).
EXECUTOR_FAULT_EXIT_CODE = 86


class ExecutorFaultInjected(RuntimeError):
    """Raised at the test-only dangerous-window fault point (in-process mode)."""

    code = "executor_fault_injected"


# In-process tests override this hook; the real E2E subprocess leaves it None so
# the built-in fault path terminates the process (os._exit) at the exact window.
_EXECUTOR_FAULT_HOOK: Any = None


def set_executor_fault_hook(hook: Any) -> None:
    """Install a test-only fault hook (called after the receipt is persisted)."""
    global _EXECUTOR_FAULT_HOOK
    _EXECUTOR_FAULT_HOOK = hook


def _maybe_fire_executor_fault(deps: GraphV2Runtime, step_id: str) -> None:
    """Requirement #8: deterministic dangerous-window fault point.

    Fires ONLY after ``run_executor_step`` returned ``step_executed`` AND the
    v2ExecReceipt was persisted to TaskState — i.e. the live tool already ran
    and its output/receipt are durable, but the NEXT graph checkpoint has NOT
    yet been confirmed.  The process dies here; a restart must rehydrate the
    live TaskState, reconcile fast_forward, and skip the already-executed tool
    (live tool count stays exactly 1).
    """
    if not deps.durable:
        return
    if settings.agent_graph_v2_fault_point != "after_executor_receipt":
        return
    if _EXECUTOR_FAULT_HOOK is not None:
        _EXECUTOR_FAULT_HOOK(step_id)
        return
    # Real-process E2E: terminate immediately at the exact dangerous window.
    os._exit(EXECUTOR_FAULT_EXIT_CODE)


async def _write_executor_receipt(
    deps: GraphV2Runtime,
    current: Any,
    *,
    step_id: str,
    plan_id: str | None,
    tool_name: str,
    claimed_revision: int,
    durable_tool_receipt: dict[str, Any],
) -> Any:
    """Record the same-run Executor receipt in the live TaskState.

    The receipt lets a restart reconcile that ``live.revision`` advanced past the
    stale checkpoint ONLY by this run's own persisted step (requirement #6).  It
    records the revision the write produces (monotonic ``current + 1``); the OCC
    expected_revision guarantees that is exact.
    """
    # Projection must be byte-for-byte the runner-owned ledger receipt.  Adding
    # a TaskState-only revision/timestamp would make TaskState an alternate
    # authority and prevent restart from checking exact ledger equality.
    receipt = dict(durable_tool_receipt)
    if (
        receipt.get("taskId") != deps.task_id
        or receipt.get("runId") != deps.run_id
        or receipt.get("threadId") != deps.thread_id
        or receipt.get("sessionOwnerHash") != deps.session_owner_hash
        or receipt.get("planId") != plan_id
        or receipt.get("stepId") != step_id
        or receipt.get("toolName") != tool_name
        or receipt.get("stateRevision") != claimed_revision
    ):
        raise RuntimeError("durable ledger receipt identity mismatch")
    expected_projection_revision = claimed_revision + 5
    if current.revision != expected_projection_revision - 1:
        raise RuntimeError("durable receipt projection revision chain mismatch")
    receipt_hash = hashlib.sha256(
        json.dumps(
            receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    patch = TaskStatePatchRequest(
        expected_revision=current.revision,
        actor="agent",
        domain_state_patch={
            "v2ExecReceipt": receipt,
            "v2ExecReceiptProjection": {
                "receiptHash": receipt_hash,
                "projectionRevision": expected_projection_revision,
            },
        },
    )
    return await update_task_state(deps.task_id, patch)


def _react_error_code(value: Any) -> str:
    if isinstance(value, BaseException):
        response = getattr(value, "response", None)
        status = getattr(response, "status", None)
        status_value = getattr(status, "value", status)
        value = (
            getattr(response, "error_code", None)
            or (f"tool_inbox_{str(status_value).lower()}" if status_value else None)
            or getattr(value, "code", None)
            or type(value).__name__
        )
    normalized = re.sub(
        r"[^a-z0-9_]+", "_", str(value or "react_executor_failed").lower()
    ).strip("_")
    return normalized[:128] or "react_executor_failed"


async def _persist_react_outcome(
    deps: GraphV2Runtime,
    current: Any,
    outcome: ActionOutcome,
) -> Any:
    receipt = {
        "runId": deps.run_id,
        "threadId": deps.thread_id,
        "sessionOwnerHash": deps.session_owner_hash,
        "controlPolicy": "react_v1",
        "policyRevision": CONTROL_POLICY_REVISIONS["react_v1"],
        "stateRevision": current.revision + 1,
        "outcome": outcome.model_dump(by_alias=True, mode="json"),
    }
    return await update_task_state(
        current.task_id,
        TaskStatePatchRequest(
            expectedRevision=current.revision,
            actor="agent",
            domainStatePatch={"reactV1OutcomeReceipt": receipt},
        ),
    )


async def _react_action_anchor_is_authentic(
    receipt: dict[str, Any],
    deps: GraphV2Runtime,
    action_id: str,
) -> bool:
    raw_anchor = await _get_client().get(
        react_action_anchor_key(
            str(deps.task_id),
            str(deps.run_id),
            str(deps.thread_id),
            action_id,
        )
    )
    try:
        anchor = json.loads(raw_anchor) if raw_anchor else None
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(anchor, dict)
        and anchor.get("taskId") == deps.task_id
        and anchor.get("runId") == deps.run_id
        and anchor.get("threadId") == deps.thread_id
        and anchor.get("sessionOwnerHash") == deps.session_owner_hash
        and anchor.get("controlPolicy") == "react_v1"
        and anchor.get("policyRevision") == CONTROL_POLICY_REVISIONS["react_v1"]
        and anchor.get("taskRevision") == receipt.get("stateRevision")
        and anchor.get("action") == receipt.get("action")
        and anchor.get("actionSha256") == receipt.get("actionSha256")
        and anchor.get("planSha256") == receipt.get("planSha256")
    )


async def _react_execution_contract_is_authentic(
    current: Any,
    deps: GraphV2Runtime,
    action_id: str,
) -> bool:
    receipt = (current.domain_state or {}).get("reactV1ActionReceipt")
    plan = current.active_plan
    if not (
        isinstance(receipt, dict)
        and receipt.get("runId") == deps.run_id
        and receipt.get("threadId") == deps.thread_id
        and receipt.get("sessionOwnerHash") == deps.session_owner_hash
        and receipt.get("controlPolicy") == "react_v1"
        and receipt.get("policyRevision") == CONTROL_POLICY_REVISIONS["react_v1"]
        and plan is not None
        and plan.plan_id == f"react-{action_id}"[:64]
        and len(plan.steps) == 1
        and receipt.get("planSha256") == react_plan_contract_sha256(plan)
    ):
        return False
    raw_action = receipt.get("action")
    if not isinstance(raw_action, dict):
        return False
    expected_hash = hashlib.sha256(
        json.dumps(
            raw_action,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if receipt.get("actionSha256") != expected_hash:
        return False
    try:
        action = NextAction.model_validate(raw_action)
    except Exception:
        return False
    step = plan.steps[0]
    return bool(
        action.action_id == action_id
        and action.task_id == current.task_id
        and action.kind == "CALL_TOOL"
        and action.tool_name == step.tool_name
        and action.based_on_revision + 1 == receipt.get("stateRevision")
    ) and await _react_action_anchor_is_authentic(receipt, deps, action_id)


async def _react_executor_failure_base_is_authentic(
    current: Any,
    deps: GraphV2Runtime,
    action_id: str,
) -> bool:
    domain = current.domain_state or {}
    receipt = domain.get("reactV1ActionReceipt")
    marker = domain.get("v2RunMarker")
    lease = domain.get("executorLease")
    plan = current.active_plan
    if not (
        isinstance(receipt, dict)
        and receipt.get("runId") == deps.run_id
        and receipt.get("threadId") == deps.thread_id
        and receipt.get("sessionOwnerHash") == deps.session_owner_hash
        and receipt.get("controlPolicy") == "react_v1"
        and receipt.get("policyRevision") == CONTROL_POLICY_REVISIONS["react_v1"]
        and type(receipt.get("stateRevision")) is int
        and receipt.get("stateRevision") <= current.revision
        and isinstance(marker, dict)
        and marker.get("runId") == deps.run_id
        and marker.get("threadId") == deps.thread_id
        and marker.get("sessionOwnerHash") == deps.session_owner_hash
        and marker.get("controlPolicy") == "react_v1"
        and marker.get("policyRevision") == CONTROL_POLICY_REVISIONS["react_v1"]
        and plan is not None
        and plan.plan_id == f"react-{action_id}"[:64]
        and receipt.get("planSha256") == react_plan_contract_sha256(plan)
        and isinstance(lease, dict)
        and lease.get("planId") == plan.plan_id
        and lease.get("stepId") == "step-react-action"
        and lease.get("inboxStatus") in {"PREPARED", "IN_FLIGHT"}
    ):
        return False
    expected_revision_delta = (
        2 if lease.get("inboxStatus") == "PREPARED" else 3
    )
    if current.revision != receipt.get("stateRevision") + expected_revision_delta:
        return False
    raw_action = receipt.get("action")
    if not isinstance(raw_action, dict):
        return False
    expected_hash = hashlib.sha256(
        json.dumps(
            raw_action,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if receipt.get("actionSha256") != expected_hash:
        return False
    try:
        action = NextAction.model_validate(raw_action)
    except Exception:
        return False
    return bool(
        action.action_id == action_id
        and action.task_id == current.task_id
        and action.kind == "CALL_TOOL"
        and action.based_on_revision + 1 == receipt.get("stateRevision")
    ) and await _react_action_anchor_is_authentic(receipt, deps, action_id)


async def _react_unknown_recovery_is_authentic(
    current: Any,
    deps: GraphV2Runtime,
    action_id: str,
) -> bool:
    domain = current.domain_state or {}
    receipt = domain.get("reactV1ActionReceipt")
    marker = domain.get("v2RunMarker")
    recovery = domain.get("executorRecovery")
    plan = current.active_plan
    if not (
        isinstance(receipt, dict)
        and receipt.get("runId") == deps.run_id
        and receipt.get("threadId") == deps.thread_id
        and receipt.get("sessionOwnerHash") == deps.session_owner_hash
        and receipt.get("controlPolicy") == "react_v1"
        and receipt.get("policyRevision") == CONTROL_POLICY_REVISIONS["react_v1"]
        and isinstance(marker, dict)
        and marker.get("runId") == deps.run_id
        and marker.get("threadId") == deps.thread_id
        and marker.get("sessionOwnerHash") == deps.session_owner_hash
        and marker.get("controlPolicy") == "react_v1"
        and marker.get("policyRevision") == CONTROL_POLICY_REVISIONS["react_v1"]
        and plan is not None
        and plan.plan_id == f"react-{action_id}"[:64]
        and receipt.get("planSha256") == react_plan_contract_sha256(plan)
        and isinstance(recovery, dict)
        and recovery.get("planId") == plan.plan_id
        and recovery.get("stepId") == "step-react-action"
        and recovery.get("status") == "UNKNOWN"
    ):
        return False
    raw_action = receipt.get("action")
    try:
        action = NextAction.model_validate(raw_action)
    except Exception:
        return False
    return bool(
        action.action_id == action_id
        and action.task_id == current.task_id
        and action.kind == "CALL_TOOL"
        and receipt.get("actionSha256")
        == hashlib.sha256(
            json.dumps(
                raw_action,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    ) and await _react_action_anchor_is_authentic(receipt, deps, action_id)


async def _persist_recovered_unknown_outcome(
    current: Any,
    deps: GraphV2Runtime,
    action_id: str,
) -> Any:
    if not await _react_unknown_recovery_is_authentic(current, deps, action_id):
        return current
    outcome = ActionOutcome(
        actionId=action_id,
        status="FAILED",
        observationRef=None,
        validatorOutcome="NOT_RUN",
        stateRevisionAfter=current.revision + 1,
        retryable=False,
        errorCode="tool_inbox_unknown",
    )
    return await _persist_react_outcome(deps, current, outcome)


async def executor_node(
    state: GraphV2State,
    runtime: Runtime[GraphV2Runtime],
) -> dict[str, Any]:
    deps = runtime.context
    current = state.get("task_state")
    schemas = None
    entered_because = state.get("last_route_decision") or "continue_to_executor"
    start_ts = time.perf_counter()
    events = [
        _node_event(
            "executor", "start",
            current_state=current,
            entered_because=entered_because,
        )
    ]
    if deps.durable:
        # TaskState never enters a checkpoint, so a restarted graph has no
        # ``task_state`` — always rehydrate the current revision from Redis.
        current = await _hydrate_task_state(state, deps)
        if deps.recovery_mode:
            current = await recover_expired_executor_claim(
                current,
                durable_inbox=(
                    deps.durable_tool_boundary.inbox
                    if deps.durable_tool_boundary is not None
                    else None
                ),
                force_unknown=True,
            )
            if (
                state.get("control_policy") == "react_v1"
                and state.get("react_action_id")
            ):
                current = await _persist_recovered_unknown_outcome(
                    current, deps, state["react_action_id"]
                )
        events = [
            _node_event(
                "executor", "start",
                current_state=current,
                entered_because=entered_because,
            )
        ]
        reconcile = await _reconcile_revision(state, deps, current)
        if reconcile == "diverged":
            return _state_diverged_updates(
                state=state,
                deps=deps,
                node_name="executor",
                entered_because=entered_because,
                live=current,
                events=events,
            )
        if state.get("control_policy") == "react_v1" and state.get("react_action_id"):
            if not await _react_execution_contract_is_authentic(
                current, deps, state["react_action_id"]
            ):
                return _state_diverged_updates(
                    state=state,
                    deps=deps,
                    node_name="executor",
                    entered_because=entered_because,
                    live=current,
                    events=events,
                )
            receipt = (current.domain_state or {}).get("reactV1OutcomeReceipt")
            if (
                isinstance(receipt, dict)
                and receipt.get("runId") == deps.run_id
                and receipt.get("threadId") == deps.thread_id
                and receipt.get("sessionOwnerHash") == deps.session_owner_hash
                and receipt.get("controlPolicy") == "react_v1"
                and receipt.get("policyRevision")
                == CONTROL_POLICY_REVISIONS["react_v1"]
                and receipt.get("stateRevision") == current.revision
                and isinstance(receipt.get("outcome"), dict)
            ):
                replayed_outcome = ActionOutcome.model_validate(receipt["outcome"])
                if replayed_outcome.action_id == state["react_action_id"]:
                    deps.trace_builder.record_react_outcome(
                        action_id=replayed_outcome.action_id,
                        status=replayed_outcome.status,
                        validator_outcome=replayed_outcome.validator_outcome,
                        state_revision_after=replayed_outcome.state_revision_after,
                        retryable=replayed_outcome.retryable,
                        error_code=replayed_outcome.error_code,
                        observation_ref=replayed_outcome.observation_ref,
                    )
                    result = HarnessStepResult(
                        action="stop_turn", task_state=current
                    )
                    return await _finish_node(
                        state,
                        deps,
                        node_name="executor",
                        entered_because=entered_because,
                        events=events,
                        result=result,
                        natural_action="stop_turn",
                        start_ts=start_ts,
                        error_code=replayed_outcome.error_code,
                        extra={
                            "react_last_outcome": replayed_outcome.model_dump(
                                by_alias=True, mode="json"
                            ),
                            "degraded_reason": replayed_outcome.error_code,
                        },
                    )
        # Durable skip guard (requirement #8): no pending linear step means the
        # step was already executed and its output/receipt persisted — route to
        # the Validator with ZERO additional tool calls.
        if current.active_plan is not None:
            try:
                select_next_plan_step(current.active_plan)
            except ExecutorSelectionError as exc:
                if exc.code == "plan_has_no_pending_step":
                    receipt = (current.domain_state or {}).get("v2ExecReceipt")
                    if not await _receipt_is_authentic(receipt, deps, current):
                        # A missing receipt means the worker may have crossed
                        # the tool boundary but died before its result became
                        # durable.  Do not turn that ambiguity into synthetic
                        # success or issue a second call.
                        return _state_diverged_updates(
                            state=state,
                            deps=deps,
                            node_name="executor",
                            entered_because=entered_because,
                            live=current,
                            events=events,
                        )
                    result = HarnessStepResult(
                        action="ready_for_validation",
                        task_state=current,
                    )
                    return await _finish_node(
                        state,
                        deps,
                        node_name="executor",
                        entered_because=entered_because,
                        events=events,
                        result=result,
                        natural_action="ready_for_validation",
                        start_ts=start_ts,
                        model_name=deps.model,
                        extra={"skippedDurableReplay": True},
                    )
                raise
    if current is None:
        raise RuntimeError("executor_node missing task_state")
    schemas = deps.resolve_tool_schemas(current)
    trace_started = False
    try:
        # Project ExecutorContextView BEFORE the phase (context_pack mode),
        # mirroring the V1 harness projection exactly.  Durable nodes rebuild
        # the projector from the CURRENT live TaskState so a resumed/restarted
        # graph projects the post-answer / post-plan revision.
        projector = await _resolve_projector(deps, current)
        executor_view = None
        if projector is not None:
            active_plan = current.active_plan
            if active_plan is not None:
                pending = [s for s in active_plan.steps if s.status == "pending"]
                if pending:
                    step = pending[0]
                    tool_schema_dict = _extract_tool_schema_dict(
                        step.tool_name, schemas
                    )
                    prior_outputs = _project_prior_step_outputs_for_step(
                        current, active_plan, step
                    )
                    executor_view = projector.executor_view(
                        plan_id=active_plan.plan_id,
                        step_id=step.step_id,
                        step_description=step.description,
                        tool_name=step.tool_name,
                        tool_schema=tool_schema_dict,
                        resolved_arguments=_project_step_arguments(step, prior_outputs),
                        required_fact_keys=_extract_fact_keys_for_step(step),
                        required_constraint_keys=_extract_constraint_keys_for_step(step),
                        prior_step_outputs=prior_outputs,
                        system_policies=deps.system_policies,
                        phase_task_revision=current.revision,
                    )
                    _validate_view_and_record(
                        executor_view,
                        current,
                        deps.trace_builder,
                        "executor",
                        projector=projector,
                        plan_id=active_plan.plan_id,
                    )
        deps.trace_builder.start_phase("executor")
        trace_started = True
        if executor_view is not None:
            _record_view(deps.trace_builder, "executor", executor_view)
            deps.trace_builder.record_phase_task_revision(
                "executor", executor_view.phase_task_revision
            )

        executor_result = await run_executor_step(
            current,
            schemas,
            system_policies=deps.system_policies,
            tool_caller=deps.tool_caller,
            executor_view=executor_view,
            durable_tool_boundary=(
                deps.durable_tool_boundary if deps.durable else None
            ),
            durable_run_id=deps.run_id if deps.durable else None,
            durable_thread_id=deps.thread_id if deps.durable else None,
            durable_session_owner_hash=(
                deps.session_owner_hash if deps.durable else None
            ),
        )

        # Record the persisted tool call in the trace (same as V1).
        if executor_result.execution_result is not None:
            exec_res = executor_result.execution_result
            trace = exec_res.tool_trace
            if trace is not None:
                deps.trace_builder.record_tool_call(
                    tool_name=exec_res.tool_name,
                    ok=trace.ok,
                    duration_ms=trace.duration_ms,
                    arguments_summary=trace.model_dump_json(
                        include={"tool": True}, exclude_defaults=True
                    ),
                )
        deps.trace_builder.end_phase(
            "step_executed"
            if executor_result.outcome == "step_executed"
            else executor_result.outcome,
            detail={"tool": executor_result.execution_result.tool_name,
                    "toolOk": executor_result.execution_result.tool_trace.ok if executor_result.execution_result.tool_trace else None}
                if executor_result.execution_result else None,
            task_revision=current.revision,
        )
        trace_started = False

        # Day-2: after a genuinely persisted step execution, record the
        # same-run receipt so a restart can reconcile (and skip the tool).
        if deps.durable and executor_result.outcome == "step_executed":
            if not isinstance(executor_result.durable_tool_receipt, dict):
                raise RuntimeError("durable V2 executor result lacks inbox receipt")
            executor_result = executor_result.model_copy(
                update={
                    "task_state": await _write_executor_receipt(
                        deps,
                        executor_result.task_state,
                        step_id=executor_result.context.step.step_id,
                        plan_id=executor_result.context.plan_id,
                        tool_name=executor_result.context.step.tool_name,
                        claimed_revision=executor_result.context.expected_revision,
                        durable_tool_receipt=executor_result.durable_tool_receipt,
                    )
                }
            )
            # Requirement #8 fault point: the live tool has run AND the receipt
            # is durable, but the next graph checkpoint is not yet confirmed.
            _maybe_fire_executor_fault(
                deps, executor_result.context.step.step_id
            )

        action = decide_after_execution(executor_result)
        tool_name = (
            executor_result.execution_result.tool_name
            if executor_result.execution_result is not None
            else None
        )
        result = HarnessStepResult(
            action=action,
            executor_result=executor_result,
            task_state=executor_result.task_state,
        )
        extra = None
        react_action_id = state.get("react_action_id")
        if (
            state.get("control_policy") == "react_v1"
            and react_action_id
            and action != "ready_for_validation"
        ):
            error_code = _react_error_code(
                executor_result.error_code or "react_executor_failed"
            )
            outcome = ActionOutcome(
                actionId=react_action_id,
                status="FAILED",
                observationRef=None,
                validatorOutcome="NOT_RUN",
                stateRevisionAfter=executor_result.task_state.revision + 1,
                retryable=False,
                errorCode=error_code,
            )
            persisted = await _persist_react_outcome(
                deps, executor_result.task_state, outcome
            )
            result = result.model_copy(update={"task_state": persisted})
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
                ),
                "degraded_reason": error_code,
            }
        return await _finish_node(
            state,
            deps,
            node_name="executor",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action=action,
            start_ts=start_ts,
            tool_name=tool_name,
            model_name=deps.model,
            extra=extra,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after event recording
        if trace_started:
            deps.trace_builder.end_phase("exception")
        if state.get("control_policy") == "react_v1" and state.get("react_action_id"):
            error_code = _react_error_code(exc)
            # ``run_executor_step`` may have persisted its claim and Inbox
            # PREPARED/IN_FLIGHT projection before the boundary rejected the
            # call.  Rehydrate that latest server-owned revision before the
            # outcome OCC write; never overwrite it using the node-entry
            # snapshot.
            outcome_base = (
                await _hydrate_task_state(state, deps) if deps.durable else current
            )
            if deps.durable and not await _react_executor_failure_base_is_authentic(
                outcome_base, deps, state["react_action_id"]
            ):
                return _state_diverged_updates(
                    state=state,
                    deps=deps,
                    node_name="executor",
                    entered_because=entered_because,
                    live=outcome_base,
                    events=events,
                )
            outcome = ActionOutcome(
                actionId=state["react_action_id"],
                status="FAILED",
                observationRef=None,
                validatorOutcome="NOT_RUN",
                stateRevisionAfter=outcome_base.revision + 1,
                retryable=False,
                errorCode=error_code,
            )
            persisted = await _persist_react_outcome(deps, outcome_base, outcome)
            deps.trace_builder.record_react_outcome(
                action_id=outcome.action_id,
                status=outcome.status,
                validator_outcome=outcome.validator_outcome,
                state_revision_after=outcome.state_revision_after,
                retryable=outcome.retryable,
                error_code=outcome.error_code,
                observation_ref=outcome.observation_ref,
            )
            result = HarnessStepResult(action="stop_turn", task_state=persisted)
            return await _finish_node(
                state,
                deps,
                node_name="executor",
                entered_because=entered_because,
                events=events,
                result=result,
                natural_action="stop_turn",
                start_ts=start_ts,
                error_code=error_code,
                extra={
                    "react_last_outcome": outcome.model_dump(
                        by_alias=True, mode="json"
                    ),
                    "degraded_reason": error_code,
                },
            )
        _record_error_end_event(
            deps,
            node_name="executor",
            entered_because=entered_because,
            current=current,
            start_ts=start_ts,
            exc=exc,
        )
        raise
