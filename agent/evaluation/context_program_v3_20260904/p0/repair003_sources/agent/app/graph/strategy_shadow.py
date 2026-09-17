"""Record-only strategy routing adapter for the durable V2 entry point.

This module is deliberately an observation seam, not a runner.  It consumes
only server-owned TaskState/plan facts and returns a redacted candidate report;
it never executes a strategy, tool, model, checkpoint, or TaskState mutation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ..domains.ecommerce.shopping_task_state_v2 import (
    ShoppingTaskStateV2,
    ShoppingTaskStateV2Authoritative,
    parse_shopping_task_state_v2_snapshot,
)
from ..domains.ecommerce.strategy_routing import (
    ReasonCode,
    RunnerRouteAuthority,
    Strategy,
    serialize_route_decision,
)

_SCHEMA_VERSION = "strategy-shadow-v1"
_POLICY_VERSION = "strategy-routing-v1"
_READ_ONLY_TOOLS = frozenset(
    {"search_products", "get_product_details", "compare_products"}
)
_READ_ONLY_TASK_TYPES = frozenset(
    {"shopping", "product_search", "product_compare", "catalog"}
)
_READ_ONLY_ACTIONS = frozenset(
    {"clarify", "search", "compare", "recommend", "answer", "wait"}
)
_KNOWN_RECEIPT_KEYS = frozenset({"runId", "planId", "stepId", "revision", "at"})
_STATUS_RECEIPT_KEYS = _KNOWN_RECEIPT_KEYS | frozenset(
    {"status", "queryRequired"}
)
_RECEIPT_STATUSES = frozenset({"UNKNOWN", "FAILED", "SUCCEEDED"})


class StrategyShadowContractError(ValueError):
    """A server-owned fact was not sufficiently validated for shadowing."""


def _strict_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise StrategyShadowContractError(f"invalid_{field}")
    return value


def _strict_revision(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise StrategyShadowContractError(f"invalid_{field}")
    return value


def _receipt_uncertainty(
    receipt: object,
    *,
    revision: int,
    runner_run_id: object,
    runner_plan_id: object,
    expected_step_id: object,
) -> bool:
    """Read uncertainty only from a current server receipt shape.

    The current V2 executor receipt predates status/query fields; that shape
    is accepted but cannot assert uncertainty.  Any newer shape is exact-key
    validated and revision-bound.  Caller/model strategy fields are ignored.
    """
    if receipt is None:
        return False
    # A receipt is meaningful only when the durable runner supplies all four
    # server-owned bindings.  In particular, do not infer a run from a
    # client resume payload or a receipt's own fields.
    if (
        type(runner_run_id) is not str
        or type(runner_plan_id) is not str
        or type(expected_step_id) is not str
    ):
        raise StrategyShadowContractError("receipt_ignored")
    if type(receipt) is not dict:
        raise StrategyShadowContractError("invalid_runner_receipt")
    keys = frozenset(receipt)
    if keys not in {_KNOWN_RECEIPT_KEYS, _STATUS_RECEIPT_KEYS}:
        raise StrategyShadowContractError("receipt_ignored")
    if _strict_revision(receipt.get("revision"), "receipt_revision") != revision:
        raise StrategyShadowContractError("receipt_revision_mismatch")
    for key in ("runId", "planId", "stepId", "at"):
        _strict_text(receipt.get(key), f"receipt_{key}")
    if (
        receipt["runId"] != runner_run_id
        or receipt["planId"] != runner_plan_id
        or receipt["stepId"] != expected_step_id
    ):
        raise StrategyShadowContractError("receipt_binding_mismatch")
    if keys == _KNOWN_RECEIPT_KEYS:
        return False
    status = receipt.get("status")
    query_required = receipt.get("queryRequired")
    if type(status) is not str or status not in _RECEIPT_STATUSES:
        raise StrategyShadowContractError("receipt_ignored")
    if type(query_required) is not bool:
        raise StrategyShadowContractError("receipt_ignored")
    return status in {"UNKNOWN", "FAILED"} or query_required


def _validated_sts(
    domain_state: Mapping[str, Any],
) -> ShoppingTaskStateV2 | ShoppingTaskStateV2Authoritative | None:
    raw = domain_state.get("shoppingTaskStateV2")
    if raw is None:
        return None
    try:
        return parse_shopping_task_state_v2_snapshot(raw)
    except Exception as exc:  # do not expose Pydantic/user text to the report
        raise StrategyShadowContractError("invalid_shopping_state") from exc


def derive_server_observation(
    live: Any,
    *,
    checkpoint_revision: int | None = None,
    runner_receipt: object = None,
    # Compatibility assertions only.  These are never trust sources: the
    # authoritative values below are always derived from ``live``.
    runner_run_id: str | None = None,
    runner_plan_id: str | None = None,
    expected_step_id: str | None = None,
) -> dict[str, object]:
    """Derive the closed observation consumed by ``RunnerRouteAuthority``.

    No user message, goal, strategy, reason, signal, or free-form domain text
    is read.  Plan/tool names, revision, and validated STS action kind are the
    only inputs used for routing.
    """
    if live is None:
        raise StrategyShadowContractError("missing_task_state")
    revision = _strict_revision(getattr(live, "revision", None), "revision")
    task_type = _strict_text(getattr(live, "task_type", None), "task_type")
    plan = getattr(live, "active_plan", None)
    steps = getattr(plan, "steps", None)
    if (
        type(getattr(plan, "status", None)) is not str
        or getattr(plan, "status", None) != "active"
        or type(steps) is not list
        or not 1 <= len(steps) <= 32
    ):
        raise StrategyShadowContractError("invalid_validated_plan")

    domain_state = getattr(live, "domain_state", None)
    if type(domain_state) is not dict:
        raise StrategyShadowContractError("invalid_domain_state")
    sts = _validated_sts(domain_state)

    tool_names: list[str] = []
    for step in steps:
        tool_name = _strict_text(getattr(step, "tool_name", None), "tool_name")
        tool_names.append(tool_name)

    policy_denied = (
        task_type.casefold() not in _READ_ONLY_TASK_TYPES
        or any(tool_name not in _READ_ONLY_TOOLS for tool_name in tool_names)
    )
    pending = getattr(live, "pending_questions", None)
    unknowns = getattr(live, "unknowns", None)
    if type(pending) is not list or type(unknowns) is not list:
        raise StrategyShadowContractError("invalid_task_state_lists")
    needs_observation = bool(pending or unknowns)
    if sts is not None and sts.current_action.kind in {"clarify", "wait"}:
        needs_observation = True
    if sts is not None and sts.current_action.kind not in _READ_ONLY_ACTIONS:
        policy_denied = True

    dynamic_revision = False
    if checkpoint_revision is not None:
        dynamic_revision = _strict_revision(
            checkpoint_revision, "checkpoint_revision"
        ) != revision
    pending_steps = [step.step_id for step in steps if step.status == "pending"]
    derived_step_id = pending_steps[0] if len(pending_steps) == 1 else None
    marker = domain_state.get("v2RunMarker")
    derived_run_id = (
        marker.get("runId")
        if type(marker) is dict and type(marker.get("runId")) is str
        else None
    )
    derived_plan_id = getattr(plan, "plan_id", None)
    if (
        (runner_run_id is not None and runner_run_id != derived_run_id)
        or (runner_plan_id is not None and runner_plan_id != derived_plan_id)
        or (expected_step_id is not None and expected_step_id != derived_step_id)
    ):
        raise StrategyShadowContractError("receipt_ignored")
    tool_uncertainty = _receipt_uncertainty(
        runner_receipt,
        revision=revision,
        runner_run_id=derived_run_id,
        runner_plan_id=derived_plan_id,
        expected_step_id=derived_step_id,
    )

    return {
        "taskKind": "simple" if len(steps) == 1 else "structured",
        "planSteps": len(steps),
        "needsObservation": needs_observation,
        "dynamicRevision": dynamic_revision,
        "toolUncertainty": tool_uncertainty,
        "policyDenied": policy_denied,
    }


def _digest_observation(observation: Mapping[str, object]) -> str:
    raw = json.dumps(
        dict(observation), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def build_strategy_shadow_report(
    live: Any,
    *,
    checkpoint_revision: int | None = None,
    runner_receipt: object = None,
    runner_run_id: str | None = None,
    runner_plan_id: str | None = None,
    expected_step_id: str | None = None,
) -> dict[str, object]:
    """Return a redacted, candidate-only report; never raises to the caller."""
    revision = getattr(live, "revision", None)
    report: dict[str, object] = {
        "schemaVersion": _SCHEMA_VERSION,
        "candidateOnly": True,
        "strategy": None,
        "reasonCode": None,
        "policyVersion": _POLICY_VERSION,
        "revision": revision if type(revision) is int else None,
        "checkpointRevision": (
            checkpoint_revision if type(checkpoint_revision) is int else None
        ),
        "observationDigest": None,
        "excluded": False,
        "errorCode": None,
    }
    receipt_ignored = False
    try:
        try:
            observation = derive_server_observation(
                live,
                checkpoint_revision=checkpoint_revision,
                runner_receipt=runner_receipt,
                runner_run_id=runner_run_id,
                runner_plan_id=runner_plan_id,
                expected_step_id=expected_step_id,
            )
        except StrategyShadowContractError as exc:
            if not str(exc).startswith("receipt_"):
                raise
            # A stale/foreign/malformed receipt is not a routing signal.  Drop
            # it and route from the remaining validated server facts, while
            # retaining only a fixed ignored marker in the report.
            receipt_ignored = True
            observation = derive_server_observation(
                live,
                checkpoint_revision=checkpoint_revision,
                runner_receipt=None,
                runner_run_id=None,
                runner_plan_id=None,
                expected_step_id=None,
            )
        authority = RunnerRouteAuthority()
        issued = authority.observe_server(observation)
        decision = authority.route(issued)
        # Serialization is part of the authority boundary; a report must not
        # be emitted from an unissued or mutated RouteDecision.
        serialize_route_decision(decision)
        report.update(
            strategy=decision.strategy.value,
            reasonCode=decision.reason_code.value,
            observationDigest=_digest_observation(observation),
            excluded=(decision.reason_code is ReasonCode.POLICY_DENIED),
        )
        if receipt_ignored:
            report["errorCode"] = "receipt_ignored"
    except StrategyShadowContractError as exc:
        # Shadow is fail-open to the authoritative business path, but the
        # failure itself is a fixed code and never an exception/prompt echo.
        report["errorCode"] = (
            "receipt_ignored"
            if str(exc).startswith("receipt_")
            else "shadow_contract_error"
        )
        report["excluded"] = True
    except Exception:
        report["errorCode"] = "shadow_contract_error"
        report["excluded"] = True
    return report


def strategy_shadow_event(
    report: Mapping[str, object],
    *,
    task_id: str,
    revision: int,
    thread_id: str = "",
) -> dict[str, object]:
    """Build the existing redacted node-event shape from a report."""
    strategy = report.get("strategy")
    route = strategy if type(strategy) is str else "shadow_error"
    error = report.get("errorCode")
    return {
        "nodeName": "strategy-shadow",
        "phase": "end",
        "codeSource": "agent/app/graph/strategy_shadow.py",
        "enteredBecause": "durable_entry",
        "routeDecision": route,
        "revisionBefore": revision,
        "revisionAfter": revision,
        "durationMs": None,
        "toolName": None,
        "modelName": None,
        "redactedStateHash": None,
        "errorCode": error if type(error) is str else None,
        "threadId": thread_id,
        "taskId": task_id,
        "checkpointHash": None,
    }


__all__ = [
    "StrategyShadowContractError",
    "build_strategy_shadow_report",
    "derive_server_observation",
    "strategy_shadow_event",
]
