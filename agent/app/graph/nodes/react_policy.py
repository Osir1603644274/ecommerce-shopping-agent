"""ReAct V1 policy node inside the durable LangGraph control plane.

The model never emits tool arguments or free-form execution instructions. It
may only select one server-published option from a bounded DecisionContextView.
The node materializes a one-step Plan; the existing durable executor and
validator remain the only production action/evidence boundaries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

from langgraph.runtime import Runtime

from ...control.react_actions import (
    ActionOutcome,
    NextAction,
    react_action_anchor_key,
    react_plan_contract_sha256,
)
from ...control.react_context import (
    build_decision_context_view,
    decision_view_token_count,
)
from ...control.react_decision import (
    ReactDecisionError,
    decide_next_action,
    deterministic_next_action,
)
from ...control.react_runtime import materialize_react_execution_plan
from ...harness import HarnessStepResult
from ...task_state import (
    TASK_STATE_TTL_SECONDS,
    TaskStatePatchRequest,
    _get_client,
    update_task_state,
)
from ..runtime import CONTROL_POLICY_REVISIONS, GraphV2Runtime
from ..state import GraphV2State
from . import (
    _finish_node,
    _hydrate_task_state,
    _node_event,
    _reconcile_revision,
    _record_error_end_event,
    _state_diverged_updates,
)

__all__ = [
    "ReactPolicyFaultInjected",
    "react_policy_node",
    "set_react_policy_fault_hook",
]


class ReactPolicyFaultInjected(RuntimeError):
    code = "react_policy_fault_injected"


_REACT_POLICY_FAULT_HOOK: Any = None


def set_react_policy_fault_hook(hook: Any) -> None:
    global _REACT_POLICY_FAULT_HOOK
    _REACT_POLICY_FAULT_HOOK = hook


def _maybe_fire_fault(point: str) -> None:
    if _REACT_POLICY_FAULT_HOOK is not None:
        _REACT_POLICY_FAULT_HOOK(point)


def _safe_code(value: str | None, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    return normalized[:128] or fallback


def _selected_option_id(action: NextAction, view: Any) -> str | None:
    for option in view.allowed_action_options:
        if (
            option.kind == action.kind
            and option.reason_code == action.reason_code
            and option.tool_name == action.tool_name
            and option.argument_refs == action.argument_refs
            and option.answer_context_ref == action.answer_context_ref
            and option.question == action.question
            and option.review_code == action.review_code
        ):
            return option.option_id
    return None


def _record_decision(
    deps: GraphV2Runtime,
    *,
    status: str,
    source: str | None,
    current: Any,
    view: Any | None,
    action: NextAction | None,
    error_code: str | None,
    duration_ms: float,
    model_name: str | None = None,
    model_call_id: str | None = None,
    decision_binding_hash: str | None = None,
) -> None:
    view_hash = view.decision_view_hash if view is not None else None
    token_count = decision_view_token_count(view) if view is not None else None
    if view is not None:
        deps.trace_builder.record_context_view(
            "react_decision", view.decision_view_hash, token_count
        )
    deps.trace_builder.record_react_decision(
        status=status,
        decision_source=source,
        task_revision=current.revision,
        view_hash=view_hash,
        view_token_count=token_count,
        adaptive_trigger=(
            view.observation_summary.adaptive_trigger
            if view is not None
            else None
        ),
        action_id=action.action_id if action is not None else None,
        action_kind=action.kind if action is not None else None,
        option_id=(
            _selected_option_id(action, view)
            if action is not None and view is not None
            else None
        ),
        published_option_ids=(
            [item.option_id for item in view.allowed_action_options]
            if view is not None
            else []
        ),
        reason_code=action.reason_code if action is not None else None,
        tool_name=action.tool_name if action is not None else None,
        model_name=model_name,
        model_call_id=model_call_id,
        decision_binding_hash=decision_binding_hash,
        error_code=error_code,
        duration_ms=duration_ms,
    )


def _record_outcome(deps: GraphV2Runtime, outcome: ActionOutcome) -> None:
    deps.trace_builder.record_react_outcome(
        action_id=outcome.action_id,
        status=outcome.status,
        validator_outcome=outcome.validator_outcome,
        state_revision_after=outcome.state_revision_after,
        retryable=outcome.retryable,
        error_code=outcome.error_code,
        observation_ref=outcome.observation_ref,
    )


def _same_run_receipt(receipt: Any, deps: GraphV2Runtime) -> bool:
    return bool(
        isinstance(receipt, dict)
        and receipt.get("runId") == deps.run_id
        and receipt.get("threadId") == deps.thread_id
        and receipt.get("sessionOwnerHash") == deps.session_owner_hash
        and receipt.get("controlPolicy") == "react_v1"
        and receipt.get("policyRevision")
        == CONTROL_POLICY_REVISIONS["react_v1"]
    )


def _journal_prefix(deps: GraphV2Runtime) -> str:
    identity = "\x00".join(
        (str(deps.task_id), str(deps.run_id), str(deps.thread_id))
    ).encode("utf-8")
    return f"graph-v2:react-policy:{hashlib.sha256(identity).hexdigest()}"


def _journal_payload(deps: GraphV2Runtime) -> dict[str, Any]:
    return {
        "taskId": deps.task_id,
        "runId": deps.run_id,
        "threadId": deps.thread_id,
        "sessionOwnerHash": deps.session_owner_hash,
        "controlPolicy": "react_v1",
        "policyRevision": CONTROL_POLICY_REVISIONS["react_v1"],
    }


def _receipt_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


async def _store_immutable_journal(
    key: str,
    receipt: dict[str, Any],
    *,
    conflict_code: str,
) -> None:
    client = _get_client()
    encoded = json.dumps(receipt, ensure_ascii=True, sort_keys=True)
    stored = await client.set(
        key,
        encoded,
        nx=True,
        ex=TASK_STATE_TTL_SECONDS,
    )
    if stored:
        return
    existing = await client.get(key)
    try:
        parsed = json.loads(existing) if existing else None
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if parsed != receipt:
        raise ReactDecisionError(conflict_code, "react durable journal conflict")


async def _decision_budget_count(current: Any, deps: GraphV2Runtime) -> int:
    count = 0
    client = _get_client()
    for index in range(1, deps.react_max_model_decisions + 1):
        raw = await client.get(f"{_journal_prefix(deps)}:decision:{index}")
        if not raw:
            continue
        try:
            receipt = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(receipt, dict)
            and all(
                receipt.get(key) == value
                for key, value in _journal_payload(deps).items()
            )
            and receipt.get("slot") == index
        ):
            count += 1
    return count


async def _reserve_model_decision(
    current: Any,
    deps: GraphV2Runtime,
    *,
    view_hash: str,
) -> tuple[Any, int, bool, int | None]:
    if not deps.durable:
        return current, 1, True, 1
    count = await _decision_budget_count(current, deps)
    if count >= deps.react_max_model_decisions:
        return current, count, False, None
    client = _get_client()
    for index in range(1, deps.react_max_model_decisions + 1):
        key = f"{_journal_prefix(deps)}:decision:{index}"
        payload = {
            **_journal_payload(deps),
            "slot": index,
            "viewHash": view_hash,
        }
        stored = await client.set(
            key,
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            nx=True,
            ex=TASK_STATE_TTL_SECONDS,
        )
        if stored:
            return (
                current,
                await _decision_budget_count(current, deps),
                True,
                index,
            )
    return current, await _decision_budget_count(current, deps), False, None


def _model_call_id(
    deps: GraphV2Runtime,
    *,
    slot: int,
    view_hash: str,
) -> str:
    """Bind one redacted audit identity to the exact ReAct model call."""
    identity = "\x00".join((
        str(deps.task_id),
        str(deps.run_id),
        str(deps.thread_id),
        "react_decision",
        str(slot),
        view_hash,
        deps.model,
    )).encode("utf-8")
    return f"rmc-{hashlib.sha256(identity).hexdigest()[:24]}"


def _decision_binding_hash(
    *,
    model_call_id: str,
    view: Any,
    action: NextAction | None,
    error_code: str | None,
) -> str:
    payload = {
        "modelCallId": model_call_id,
        "taskRevision": view.task_revision,
        "viewHash": view.decision_view_hash,
        "actionId": action.action_id if action is not None else None,
        "actionKind": action.kind if action is not None else None,
        "toolName": action.tool_name if action is not None else None,
        "errorCode": error_code,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _action_receipt(
    deps: GraphV2Runtime,
    current: Any,
    action: NextAction,
    *,
    plan: Any | None = None,
) -> dict:
    receipt = {
        "runId": deps.run_id,
        "threadId": deps.thread_id,
        "sessionOwnerHash": deps.session_owner_hash,
        "controlPolicy": "react_v1",
        "policyRevision": CONTROL_POLICY_REVISIONS["react_v1"],
        "stateRevision": current.revision + 1,
        "action": action.model_dump(by_alias=True, mode="json"),
        "actionSha256": hashlib.sha256(
            json.dumps(
                action.model_dump(by_alias=True, mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    if plan is not None:
        receipt["planSha256"] = react_plan_contract_sha256(plan)
    return receipt


async def _persist_action(
    current: Any,
    deps: GraphV2Runtime,
    action: NextAction,
    *,
    plan: Any | None = None,
) -> Any:
    if action.kind in {"ANSWER", "NEEDS_REVIEW"}:
        receipt = {
            **_journal_payload(deps),
            "taskRevision": current.revision,
            "action": action.model_dump(by_alias=True, mode="json"),
        }
        receipt["actionSha256"] = _receipt_sha256(receipt["action"])
        await _store_immutable_journal(
            f"{_journal_prefix(deps)}:latest-action",
            receipt,
            conflict_code="react_action_journal_conflict",
        )
        return current
    domain_patch: dict[str, Any] = {
        "reactV1ActionReceipt": _action_receipt(
            deps, current, action, plan=plan
        )
    }
    patch_data: dict[str, Any] = {
        "expectedRevision": current.revision,
        "actor": "agent",
        "domainStatePatch": domain_patch,
    }
    if plan is not None:
        patch_data.update({
            "activePlan": plan,
            "planningFailure": None,
        })
        domain_patch.update({
            "stepOutputs": {},
            "executorBlock": None,
            "validationResult": None,
        })
    elif action.kind == "ASK_CLARIFICATION":
        patch_data.update({
            "status": "collecting_information",
            "pendingQuestions": [action.question],
            "planningFailure": None,
        })
    anchor = {
        **_journal_payload(deps),
        "taskRevision": current.revision + 1,
        "action": action.model_dump(by_alias=True, mode="json"),
        "actionSha256": _receipt_sha256(
            action.model_dump(by_alias=True, mode="json")
        ),
    }
    if plan is not None:
        anchor["planSha256"] = react_plan_contract_sha256(plan)
    anchor_key = react_action_anchor_key(
        str(deps.task_id),
        str(deps.run_id),
        str(deps.thread_id),
        action.action_id,
    )
    anchor_payload = json.dumps(anchor, ensure_ascii=True, sort_keys=True)
    _maybe_fire_fault("before_action_atomic_commit")
    updated = await update_task_state(
        current.task_id,
        TaskStatePatchRequest(**patch_data),
        immutable_side_record=(anchor_key, anchor_payload),
    )
    _maybe_fire_fault("after_action_atomic_commit")
    return updated


async def _validate_action_anchor(
    receipt: dict[str, Any],
    deps: GraphV2Runtime,
) -> None:
    raw_action = receipt.get("action")
    action_id = raw_action.get("actionId") if isinstance(raw_action, dict) else None
    raw_anchor = await _get_client().get(
        react_action_anchor_key(
            str(deps.task_id),
            str(deps.run_id),
            str(deps.thread_id),
            str(action_id),
        )
    )
    try:
        anchor = json.loads(raw_anchor) if raw_anchor else None
    except (TypeError, json.JSONDecodeError):
        anchor = None
    valid = bool(
        isinstance(anchor, dict)
        and all(
            anchor.get(key) == value
            for key, value in _journal_payload(deps).items()
        )
        and anchor.get("taskRevision") == receipt.get("stateRevision")
        and anchor.get("action") == raw_action
        and anchor.get("actionSha256") == receipt.get("actionSha256")
        and anchor.get("planSha256") == receipt.get("planSha256")
    )
    if not valid:
        raise ReactDecisionError(
            "react_action_journal_invalid", "react action journal is invalid"
        )


async def _persist_terminal_outcome(
    current: Any,
    deps: GraphV2Runtime,
    outcome: ActionOutcome,
) -> None:
    raw_outcome = outcome.model_dump(by_alias=True, mode="json")
    receipt = {
        **_journal_payload(deps),
        "taskRevision": current.revision,
        "outcome": raw_outcome,
        "outcomeSha256": _receipt_sha256(raw_outcome),
    }
    await _store_immutable_journal(
        f"{_journal_prefix(deps)}:latest-outcome",
        receipt,
        conflict_code="react_outcome_journal_conflict",
    )


async def _validate_terminal_outcome_journal(
    current: Any,
    deps: GraphV2Runtime,
    action: NextAction,
) -> None:
    raw_receipt = await _get_client().get(f"{_journal_prefix(deps)}:latest-outcome")
    if not raw_receipt:
        return
    try:
        receipt = json.loads(raw_receipt)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReactDecisionError(
            "react_outcome_journal_invalid", "react outcome journal is invalid"
        ) from exc
    raw_outcome = receipt.get("outcome") if isinstance(receipt, dict) else None
    valid = bool(
        isinstance(receipt, dict)
        and all(
            receipt.get(key) == value
            for key, value in _journal_payload(deps).items()
        )
        and receipt.get("taskRevision") == current.revision
        and isinstance(raw_outcome, dict)
        and receipt.get("outcomeSha256") == _receipt_sha256(raw_outcome)
    )
    try:
        outcome = ActionOutcome.model_validate(raw_outcome) if valid else None
    except Exception:
        outcome = None
    if outcome is None or outcome.action_id != action.action_id:
        raise ReactDecisionError(
            "react_outcome_journal_invalid", "react outcome journal is invalid"
        )


async def _replay_action(current: Any, deps: GraphV2Runtime) -> NextAction | None:
    receipt = (current.domain_state or {}).get("reactV1ActionReceipt")
    task_state_receipt = _same_run_receipt(receipt, deps) and (
        receipt.get("stateRevision") == current.revision
    )
    if not task_state_receipt:
        raw_receipt = await _get_client().get(
            f"{_journal_prefix(deps)}:latest-action"
        )
        if not raw_receipt:
            return None
        try:
            receipt = json.loads(raw_receipt)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReactDecisionError(
                "react_action_journal_invalid", "react action journal is invalid"
            ) from exc
        if not (
            isinstance(receipt, dict)
            and all(
                receipt.get(key) == value
                for key, value in _journal_payload(deps).items()
            )
            and receipt.get("taskRevision") == current.revision
        ):
            raise ReactDecisionError(
                "react_action_journal_invalid", "react action journal is invalid"
            )
    raw = receipt.get("action")
    if not isinstance(raw, dict):
        raise ReactDecisionError(
            "react_action_journal_invalid", "react action journal is invalid"
        )
    expected_hash = hashlib.sha256(
        json.dumps(
            raw,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if receipt.get("actionSha256") != expected_hash:
        raise ReactDecisionError(
            "react_action_journal_invalid", "react action journal is invalid"
        )
    try:
        action = NextAction.model_validate(raw)
    except Exception as exc:
        raise ReactDecisionError(
            "react_action_journal_invalid", "react action journal is invalid"
        ) from exc
    if action.task_id != current.task_id:
        raise ReactDecisionError(
            "react_action_journal_invalid", "react action journal is invalid"
        )
    if task_state_receipt:
        await _validate_action_anchor(receipt, deps)
    if task_state_receipt and (
        action.based_on_revision + 1 != receipt.get("stateRevision")
    ):
        raise ReactDecisionError(
            "react_action_journal_revision_mismatch",
            "react action journal revision mismatch",
        )
    if not task_state_receipt and action.based_on_revision != current.revision:
        raise ReactDecisionError(
            "react_action_journal_revision_mismatch",
            "react action journal revision mismatch",
        )
    if action.kind == "CALL_TOOL":
        plan = current.active_plan
        if (
            plan is None
            or plan.plan_id != f"react-{action.action_id}"[:64]
            or receipt.get("planSha256") != react_plan_contract_sha256(plan)
        ):
            raise ReactDecisionError(
                "react_action_journal_invalid", "react action journal is invalid"
            )
    if action.kind == "ASK_CLARIFICATION" and (
        not current.pending_questions
        or current.pending_questions[0] != action.question
    ):
        raise ReactDecisionError(
            "react_action_journal_invalid", "react action journal is invalid"
        )
    if action.kind in {"ANSWER", "NEEDS_REVIEW"}:
        await _validate_terminal_outcome_journal(current, deps, action)
    return action


async def react_policy_node(
    state: GraphV2State,
    runtime: Runtime[GraphV2Runtime],
) -> dict[str, Any]:
    deps = runtime.context
    entered_because = state.get("last_route_decision") or "entry"
    started = time.perf_counter()
    current = state.get("task_state")
    if deps.durable:
        current = await _hydrate_task_state(state, deps)
        reconcile = await _reconcile_revision(state, deps, current)
        if reconcile == "diverged":
            return _state_diverged_updates(
                state=state,
                deps=deps,
                node_name="react_policy",
                entered_because=entered_because,
                live=current,
                events=[
                    _node_event(
                        "react_policy",
                        "start",
                        current_state=current,
                        entered_because=entered_because,
                    )
                ],
            )
    if current is None:
        raise RuntimeError("react_policy_node missing task_state")

    events = [
        _node_event(
            "react_policy",
            "start",
            current_state=current,
            entered_because=entered_because,
        )
    ]
    schemas = deps.resolve_tool_schemas(current)
    allowed_names = [
        str(schema.get("function", schema).get("name", ""))
        for schema in schemas
    ]
    raw_outcome = state.get("react_last_outcome")
    model_count = max(
        int(state.get("react_model_decision_count", 0)),
        await _decision_budget_count(current, deps) if deps.durable else 0,
    )
    model_call_id: str | None = None
    decision_binding_hash: str | None = None
    last_outcome = None
    if isinstance(raw_outcome, dict):
        try:
            last_outcome = ActionOutcome.model_validate(raw_outcome)
        except Exception:
            last_outcome = None

    trace_started = False
    try:
        replayed_action = (
            await _replay_action(current, deps) if deps.durable else None
        )
    except ReactDecisionError as exc:
        error_code = _safe_code(exc.code, "react_action_journal_invalid")
        result = HarnessStepResult(action="stop_turn", task_state=current)
        return await _finish_node(
            state,
            deps,
            node_name="react_policy",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action="stop_turn",
            start_ts=started,
            error_code=error_code,
            extra={
                "control_policy": "react_v1",
                "react_model_decision_count": model_count,
                "degraded_reason": error_code,
            },
        )
    if replayed_action is not None:
        deps.trace_builder.start_phase("react_decision")
        deps.trace_builder.end_phase("durable_replay")
        _record_decision(
            deps,
            status="accepted",
            source="durable_replay",
            current=current,
            view=None,
            action=replayed_action,
            error_code=None,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        common = {
            "control_policy": "react_v1",
            "react_model_decision_count": model_count,
            "react_action_id": replayed_action.action_id,
            "react_action_kind": replayed_action.kind,
            "react_answer_context_ref": replayed_action.answer_context_ref,
        }
        if replayed_action.kind == "CALL_TOOL":
            result = HarnessStepResult(
                action="continue_to_executor", task_state=current
            )
            return await _finish_node(
                state,
                deps,
                node_name="react_policy",
                entered_because=entered_because,
                events=events,
                result=result,
                natural_action="continue_to_executor",
                start_ts=started,
                tool_name=replayed_action.tool_name,
                extra={**common, "react_last_outcome": None},
            )
        if replayed_action.kind == "ASK_CLARIFICATION":
            outcome = ActionOutcome(
                actionId=replayed_action.action_id,
                status="INTERRUPTED",
                observationRef=None,
                validatorOutcome="NOT_RUN",
                stateRevisionAfter=current.revision,
                retryable=False,
                errorCode=None,
            )
            _record_outcome(deps, outcome)
            result = HarnessStepResult(action="ask_user", task_state=current)
            return await _finish_node(
                state,
                deps,
                node_name="react_policy",
                entered_because=entered_because,
                events=events,
                result=result,
                natural_action="ask_user",
                start_ts=started,
                extra={
                    **common,
                    "react_last_outcome": outcome.model_dump(
                        by_alias=True, mode="json"
                    ),
                },
            )
        if replayed_action.kind == "ANSWER":
            outcome = ActionOutcome(
                actionId=replayed_action.action_id,
                status="SUCCEEDED",
                observationRef=replayed_action.answer_context_ref,
                validatorOutcome="PASSED",
                stateRevisionAfter=current.revision,
                retryable=False,
                errorCode=None,
            )
            await _persist_terminal_outcome(current, deps, outcome)
            _record_outcome(deps, outcome)
            result = HarnessStepResult(action="task_completed", task_state=current)
            return await _finish_node(
                state,
                deps,
                node_name="react_policy",
                entered_because=entered_because,
                events=events,
                result=result,
                natural_action="task_completed",
                start_ts=started,
                extra={
                    **common,
                    "react_last_outcome": outcome.model_dump(
                        by_alias=True, mode="json"
                    ),
                },
            )
        error_code = _safe_code(replayed_action.review_code, "needs_review")
        outcome = ActionOutcome(
            actionId=replayed_action.action_id,
            status="REJECTED",
            observationRef=None,
            validatorOutcome="NOT_RUN",
            stateRevisionAfter=current.revision,
            retryable=False,
            errorCode=error_code,
        )
        await _persist_terminal_outcome(current, deps, outcome)
        _record_outcome(deps, outcome)
        result = HarnessStepResult(action="stop_turn", task_state=current)
        return await _finish_node(
            state,
            deps,
            node_name="react_policy",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action="stop_turn",
            start_ts=started,
            error_code=error_code,
            extra={
                **common,
                "react_last_outcome": outcome.model_dump(
                    by_alias=True, mode="json"
                ),
                "degraded_reason": error_code,
            },
        )

    try:
        deps.trace_builder.start_phase("react_decision")
        trace_started = True
        view = build_decision_context_view(
            current,
            user_message=deps.user_message,
            allowed_tool_names=allowed_names,
            last_outcome=last_outcome,
            memory_run_binding=deps.memory_run_binding,
        )
        action = deterministic_next_action(view)
        source = "deterministic_policy"
        if action is None:
            adaptive_required = bool(
                view.server_signals.get("adaptiveDecisionRequired", False)
            )
            if not adaptive_required or len(view.allowed_action_options) <= 1:
                duration_ms = (time.perf_counter() - started) * 1000.0
                deps.trace_builder.end_phase("rejected")
                trace_started = False
                _record_decision(
                    deps,
                    status="rejected",
                    source="deterministic_policy",
                    current=current,
                    view=view,
                    action=None,
                    error_code="react_model_not_eligible",
                    duration_ms=duration_ms,
                )
                result = HarnessStepResult(action="stop_turn", task_state=current)
                return await _finish_node(
                    state,
                    deps,
                    node_name="react_policy",
                    entered_because=entered_because,
                    events=events,
                    result=result,
                    natural_action="stop_turn",
                    start_ts=started,
                    error_code="react_model_not_eligible",
                    extra={
                        "control_policy": "react_v1",
                        "react_model_decision_count": model_count,
                        "degraded_reason": "react_model_not_eligible",
                    },
                )
            if model_count >= deps.react_max_model_decisions:
                duration_ms = (time.perf_counter() - started) * 1000.0
                deps.trace_builder.end_phase("rejected")
                trace_started = False
                _record_decision(
                    deps,
                    status="rejected",
                    source=None,
                    current=current,
                    view=view,
                    action=None,
                    error_code="react_model_decision_budget_exhausted",
                    duration_ms=duration_ms,
                )
                result = HarnessStepResult(action="stop_turn", task_state=current)
                return await _finish_node(
                    state,
                    deps,
                    node_name="react_policy",
                    entered_because=entered_because,
                    events=events,
                    result=result,
                    natural_action="stop_turn",
                    start_ts=started,
                    error_code="react_model_decision_budget_exhausted",
                    extra={"degraded_reason": "react_model_decision_budget_exhausted"},
                )
            (
                current,
                model_count,
                reservation_acquired,
                model_slot,
            ) = await _reserve_model_decision(
                current, deps, view_hash=view.decision_view_hash
            )
            if not reservation_acquired:
                duration_ms = (time.perf_counter() - started) * 1000.0
                deps.trace_builder.end_phase("rejected")
                trace_started = False
                error_code = "react_model_decision_reservation_failed"
                _record_decision(
                    deps,
                    status="rejected",
                    source=None,
                    current=current,
                    view=view,
                    action=None,
                    error_code=error_code,
                    duration_ms=duration_ms,
                )
                result = HarnessStepResult(action="stop_turn", task_state=current)
                return await _finish_node(
                    state,
                    deps,
                    node_name="react_policy",
                    entered_because=entered_because,
                    events=events,
                    result=result,
                    natural_action="stop_turn",
                    start_ts=started,
                    error_code=error_code,
                    extra={
                        "control_policy": "react_v1",
                        "react_model_decision_count": model_count,
                        "degraded_reason": error_code,
                    },
                )
            view = build_decision_context_view(
                current,
                user_message=deps.user_message,
                allowed_tool_names=allowed_names,
                last_outcome=last_outcome,
                memory_run_binding=deps.memory_run_binding,
            )
            source = "model"
            if model_slot is None:
                raise ReactDecisionError(
                    "react_model_call_identity_missing",
                    "react model call identity is missing",
                )
            model_call_id = _model_call_id(
                deps,
                slot=model_slot,
                view_hash=view.decision_view_hash,
            )
            model_started = time.perf_counter()
            model_call_failed = True
            provider_response = None

            def capture_provider_response(response: Any) -> None:
                nonlocal provider_response
                provider_response = response

            try:
                action = await asyncio.wait_for(
                    decide_next_action(
                        view,
                        client=deps.client,
                        model=deps.model,
                        on_response=capture_provider_response,
                    ),
                    timeout=max(float(deps.react_decision_timeout_seconds), 0.01),
                )
                model_call_failed = False
            except (asyncio.TimeoutError, ReactDecisionError):
                raise
            except Exception as exc:
                raise ReactDecisionError(
                    "decision_model_failed", "react decision model call failed"
                ) from exc
            finally:
                model_duration_ms = (
                    time.perf_counter() - model_started
                ) * 1000.0
                if deps.on_model_call is not None:
                    deps.on_model_call(
                        "react_decision",
                        model_duration_ms,
                        failed=model_call_failed,
                    )
                if deps.on_model_call_receipt is not None:
                    deps.on_model_call_receipt(
                        "react_decision",
                        model_duration_ms,
                        failed=model_call_failed,
                        response=provider_response,
                        model_call_id=model_call_id,
                        context_binding_hash=view.decision_view_hash,
                    )

            _maybe_fire_fault("after_model_decision")

            if deps.durable:
                live_after_model = await _hydrate_task_state(state, deps)
                if live_after_model.revision != view.task_revision:
                    decision_binding_hash = _decision_binding_hash(
                        model_call_id=model_call_id,
                        view=view,
                        action=action,
                        error_code="stale_task_revision",
                    )
                    outcome = ActionOutcome(
                        actionId=action.action_id,
                        status="REJECTED",
                        observationRef=None,
                        validatorOutcome="NOT_RUN",
                        stateRevisionAfter=live_after_model.revision,
                        retryable=False,
                        errorCode="stale_task_revision",
                    )
                    _record_outcome(deps, outcome)
                    updates = _state_diverged_updates(
                        state=state,
                        deps=deps,
                        node_name="react_policy",
                        entered_because=entered_because,
                        live=live_after_model,
                        events=events,
                        model_name=deps.model,
                        model_call_id=model_call_id,
                        decision_binding_hash=decision_binding_hash,
                        decision_view_hash=view.decision_view_hash,
                        decision_task_revision=view.task_revision,
                        decision_action_id=action.action_id,
                        decision_action_kind=action.kind,
                        decision_error_code="stale_task_revision",
                    )
                    updates.update({
                        "control_policy": "react_v1",
                        "react_model_decision_count": model_count,
                        "react_action_id": action.action_id,
                        "react_action_kind": action.kind,
                        "react_answer_context_ref": action.answer_context_ref,
                        "react_last_outcome": outcome.model_dump(
                            by_alias=True, mode="json"
                        ),
                    })
                    deps.trace_builder.end_phase("rejected")
                    trace_started = False
                    _record_decision(
                        deps,
                        status="rejected",
                        source=source,
                        current=current,
                        view=view,
                        action=action,
                        error_code="stale_task_revision",
                        duration_ms=(time.perf_counter() - started) * 1000.0,
                        model_name=deps.model,
                        model_call_id=model_call_id,
                        decision_binding_hash=decision_binding_hash,
                    )
                    return updates

        duration_ms = (time.perf_counter() - started) * 1000.0
        deps.trace_builder.end_phase("accepted")
        trace_started = False
        if source == "model":
            decision_binding_hash = _decision_binding_hash(
                model_call_id=model_call_id,
                view=view,
                action=action,
                error_code=None,
            )
            decision_view_hash = view.decision_view_hash
            decision_task_revision = view.task_revision
            decision_action_id = action.action_id
            decision_action_kind = action.kind
        _record_decision(
            deps,
            status="accepted",
            source=source,
            current=current,
            view=view,
            action=action,
            error_code=None,
            duration_ms=duration_ms,
            model_name=deps.model if source == "model" else None,
            model_call_id=model_call_id if source == "model" else None,
            decision_binding_hash=(
                decision_binding_hash if source == "model" else None
            ),
        )

        common = {
            "control_policy": "react_v1",
            "react_model_decision_count": model_count,
            "react_action_id": action.action_id,
            "react_action_kind": action.kind,
            "react_answer_context_ref": action.answer_context_ref,
        }
        if action.kind == "CALL_TOOL":
            plan = materialize_react_execution_plan(current, action)
            current = await _persist_action(current, deps, action, plan=plan)
            _maybe_fire_fault("after_action_persist")
            result = HarnessStepResult(
                action="continue_to_executor", task_state=current
            )
            return await _finish_node(
                state,
                deps,
                node_name="react_policy",
                entered_because=entered_because,
                events=events,
                result=result,
                natural_action="continue_to_executor",
                start_ts=started,
                tool_name=action.tool_name,
                model_name=deps.model if source == "model" else None,
                model_call_id=model_call_id if source == "model" else None,
                decision_binding_hash=(
                    decision_binding_hash if source == "model" else None
                ),
                decision_view_hash=(
                    decision_view_hash if source == "model" else None
                ),
                decision_task_revision=(
                    decision_task_revision if source == "model" else None
                ),
                decision_action_id=(
                    decision_action_id if source == "model" else None
                ),
                decision_action_kind=(
                    decision_action_kind if source == "model" else None
                ),
                extra={**common, "react_last_outcome": None},
            )

        if action.kind == "ANSWER":
            current = await _persist_action(current, deps, action)
            _maybe_fire_fault("after_action_persist")
            outcome = ActionOutcome(
                actionId=action.action_id,
                status="SUCCEEDED",
                observationRef=action.answer_context_ref,
                validatorOutcome="PASSED",
                stateRevisionAfter=current.revision,
                retryable=False,
                errorCode=None,
            )
            await _persist_terminal_outcome(current, deps, outcome)
            _record_outcome(deps, outcome)
            result = HarnessStepResult(action="task_completed", task_state=current)
            return await _finish_node(
                state,
                deps,
                node_name="react_policy",
                entered_because=entered_because,
                events=events,
                result=result,
                natural_action="task_completed",
                start_ts=started,
                model_name=deps.model if source == "model" else None,
                model_call_id=model_call_id if source == "model" else None,
                decision_binding_hash=(
                    decision_binding_hash if source == "model" else None
                ),
                decision_view_hash=(
                    decision_view_hash if source == "model" else None
                ),
                decision_task_revision=(
                    decision_task_revision if source == "model" else None
                ),
                decision_action_id=(
                    decision_action_id if source == "model" else None
                ),
                decision_action_kind=(
                    decision_action_kind if source == "model" else None
                ),
                extra={
                    **common,
                    "react_last_outcome": outcome.model_dump(
                        by_alias=True, mode="json"
                    ),
                },
            )

        if action.kind == "ASK_CLARIFICATION":
            current = await _persist_action(current, deps, action)
            _maybe_fire_fault("after_action_persist")
            outcome = ActionOutcome(
                actionId=action.action_id,
                status="INTERRUPTED",
                observationRef=None,
                validatorOutcome="NOT_RUN",
                stateRevisionAfter=current.revision,
                retryable=False,
                errorCode=None,
            )
            _record_outcome(deps, outcome)
            result = HarnessStepResult(action="ask_user", task_state=current)
            return await _finish_node(
                state,
                deps,
                node_name="react_policy",
                entered_because=entered_because,
                events=events,
                result=result,
                natural_action="ask_user",
                start_ts=started,
                model_name=deps.model if source == "model" else None,
                model_call_id=model_call_id if source == "model" else None,
                decision_binding_hash=(
                    decision_binding_hash if source == "model" else None
                ),
                decision_view_hash=(
                    decision_view_hash if source == "model" else None
                ),
                decision_task_revision=(
                    decision_task_revision if source == "model" else None
                ),
                decision_action_id=(
                    decision_action_id if source == "model" else None
                ),
                decision_action_kind=(
                    decision_action_kind if source == "model" else None
                ),
                extra={
                    **common,
                    "react_last_outcome": outcome.model_dump(
                        by_alias=True, mode="json"
                    ),
                },
            )

        error_code = _safe_code(action.review_code, "needs_review")
        current = await _persist_action(current, deps, action)
        _maybe_fire_fault("after_action_persist")
        outcome = ActionOutcome(
            actionId=action.action_id,
            status="REJECTED",
            observationRef=None,
            validatorOutcome="NOT_RUN",
            stateRevisionAfter=current.revision,
            retryable=False,
            errorCode=error_code,
        )
        await _persist_terminal_outcome(current, deps, outcome)
        _record_outcome(deps, outcome)
        result = HarnessStepResult(action="stop_turn", task_state=current)
        return await _finish_node(
            state,
            deps,
            node_name="react_policy",
            entered_because=entered_because,
            events=events,
            result=result,
            natural_action="stop_turn",
            start_ts=started,
            model_name=deps.model if source == "model" else None,
            model_call_id=model_call_id if source == "model" else None,
            decision_binding_hash=(
                decision_binding_hash if source == "model" else None
            ),
            decision_view_hash=(
                decision_view_hash if source == "model" else None
            ),
            decision_task_revision=(
                decision_task_revision if source == "model" else None
            ),
            decision_action_id=(
                decision_action_id if source == "model" else None
            ),
            decision_action_kind=(
                decision_action_kind if source == "model" else None
            ),
            decision_error_code=None,
            error_code=error_code,
            extra={
                **common,
                "react_last_outcome": outcome.model_dump(
                    by_alias=True, mode="json"
                ),
                "degraded_reason": error_code,
            },
        )
    except asyncio.TimeoutError:
        error_code = "react_decision_timeout"
    except ReactDecisionError as exc:
        error_code = _safe_code(exc.code, "react_decision_rejected")
    except Exception as exc:
        if model_call_id is not None and "view" in locals():
            decision_binding_hash = _decision_binding_hash(
                model_call_id=model_call_id,
                view=view,
                action=locals().get("action"),
                error_code=_safe_code(
                    getattr(exc, "code", None), type(exc).__name__.lower()
                ),
            )
        if trace_started:
            deps.trace_builder.end_phase("exception")
            trace_started = False
        _record_error_end_event(
            deps,
            node_name="react_policy",
            entered_because=entered_because,
            current=current,
            start_ts=started,
            exc=exc,
            model_name=deps.model if model_call_id is not None else None,
            model_call_id=model_call_id,
            decision_binding_hash=decision_binding_hash,
            decision_view_hash=(
                view.decision_view_hash if model_call_id is not None else None
            ),
            decision_task_revision=(
                view.task_revision if model_call_id is not None else None
            ),
            decision_action_id=(
                action.action_id
                if model_call_id is not None and "action" in locals()
                and action is not None
                else None
            ),
            decision_action_kind=(
                action.kind
                if model_call_id is not None and "action" in locals()
                and action is not None
                else None
            ),
            decision_error_code=(
                _safe_code(getattr(exc, "code", None), type(exc).__name__.lower())
                if model_call_id is not None
                else None
            ),
        )
        raise

    if trace_started:
        deps.trace_builder.end_phase("rejected")
    duration_ms = (time.perf_counter() - started) * 1000.0
    if model_call_id is not None:
        decision_binding_hash = _decision_binding_hash(
            model_call_id=model_call_id,
            view=view,
            action=None,
            error_code=error_code,
        )
    _record_decision(
        deps,
        status="rejected",
        source="model",
        current=current,
        view=locals().get("view"),
        action=None,
        error_code=error_code,
        duration_ms=duration_ms,
        model_name=deps.model if model_call_id is not None else None,
        model_call_id=model_call_id,
        decision_binding_hash=decision_binding_hash,
    )
    result = HarnessStepResult(action="stop_turn", task_state=current)
    return await _finish_node(
        state,
        deps,
        node_name="react_policy",
        entered_because=entered_because,
        events=events,
        result=result,
        natural_action="stop_turn",
        start_ts=started,
        model_name=deps.model if model_call_id is not None else None,
        model_call_id=model_call_id,
        decision_binding_hash=decision_binding_hash,
        decision_view_hash=(
            view.decision_view_hash if model_call_id is not None else None
        ),
        decision_task_revision=(
            view.task_revision if model_call_id is not None else None
        ),
        decision_error_code=error_code if model_call_id is not None else None,
        error_code=error_code,
        extra={
            "control_policy": "react_v1",
            "react_model_decision_count": model_count,
            "degraded_reason": error_code,
        },
    )
