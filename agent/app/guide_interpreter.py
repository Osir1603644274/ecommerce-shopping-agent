"""One model interpretation and one TaskState CAS for a commerce user turn."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .catalog_conversation import expand_intent, intent_from_execution, plan_turn
from .guide_state import (
    GuideRequirement, GuideTurn, ShoppingState, catalog_projection,
    bind_guide_write, ensure_shopping_state, transition,
)
from .task_state import (
    TaskState, TaskStatePatchRequest, TaskStateRevisionConflictError,
    get_task_state, update_task_state,
)


class GuideRevisionConflictError(RuntimeError):
    """A different user turn won the TaskState compare-and-swap."""


class GuideInterpretationError(ValueError):
    """Two bounded model attempts did not yield a valid semantic transition."""


def _requirement(row: dict[str, Any], current: ShoppingState, message: str, *, trusted_anchor: str = "") -> GuideRequirement:
    mode = row["mode"]
    operator = row.get("operator") or ("not_in" if mode in {"exclude", "avoid"} else "eq")
    priority = "hard" if mode in {"require", "exclude"} else "soft"
    value = row["value"]
    if operator in {"lte", "gte"}:
        if not re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
            raise ValueError("numeric guide requirement must contain a number")
        normalized_value = float(value) if "." in str(value) else int(value)
    elif operator in {"in", "not_in"}:
        normalized_value = value if isinstance(value, list) else [value]
    else:
        normalized_value = value
    # Preserve provenance of unchanged requirements across a refinement.
    prior = next((r for r in current.requirements if r.facet == row["facet"]
                  and r.priority == priority and r.operator == operator
                  and r.value == normalized_value), None)
    if prior is not None:
        return prior
    explicit = str(value).casefold() in message.casefold() or any(
        str(term).casefold() in message.casefold() for term in row.get("terms", [])
    )
    anchored = bool(trusted_anchor and str(value).casefold() in trusted_anchor.casefold())
    if priority == "hard" and not (explicit or anchored):
        raise ValueError("new hard guide requirement lacks a user-language span")
    return GuideRequirement(
        facet=row["facet"], operator=operator,
        value=normalized_value, unit=row.get("unit", ""),
        priority=priority, source="user" if explicit else "server:product_anchor" if anchored else "inferred:catalog_plan",
        terms=row.get("terms", []),
    )


def _turn(plan: dict[str, Any], current: ShoppingState, message: str) -> GuideTurn:
    anchor = plan.get("productContext") or {}
    return GuideTurn(
        route=plan["route"], action=plan["action"],
        query=plan.get("query", ""), retrievalQuery=plan.get("retrievalQuery", ""),
        requirements=[_requirement(r, current, message, trusted_anchor=anchor.get("title", "")) for r in plan.get("requirements", [])],
        numbers=plan.get("numbers", []), question=plan.get("question", ""),
        followup=plan.get("followup", "none"), accessory=plan.get("accessory", ""),
        referenceModel=plan.get("referenceModel", ""),
    )


def _catalog_workspace_view(task: TaskState, workspace: dict[str, Any]) -> dict[str, Any]:
    """Present the canonical requirements to the existing model prompt."""
    from .catalog_service import fingerprint, verify_scope

    guide = ShoppingState.model_validate(task.domain_state["shopping"])
    current = dict(workspace.get("catalogSearch") or {})
    scope = workspace.get("catalogScope") or current.get("scope")
    try:
        verify_scope(scope)
    except ValueError:
        scope = None
    ref = task.domain_state.get("guideEvidenceRef") or {}
    if scope is not None and (scope.get("query") != guide.query
                              or ref.get("taskId") != task.task_id
                              or ref.get("scopeId") != scope.get("scopeId")
                              or ref.get("scopeSha256") != fingerprint(scope)):
        scope = None
    return {**workspace, "catalogSearch": catalog_projection(
        guide, scope=scope, revision=task.revision,
    )}


async def interpret_and_commit(
    message: str,
    task: TaskState,
    *,
    workspace: dict[str, Any] | None = None,
    turn_id: str,
) -> tuple[TaskState, dict[str, Any], dict[str, Any]]:
    """Model chooses the route; server validates and owns all state writes."""
    workspace = workspace or {}
    task = await ensure_shopping_state(task, catalog_search=workspace.get("catalogSearch"))
    message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    previous = task.domain_state.get("guideTurnDecision")
    if isinstance(previous, dict) and previous.get("turnId") == turn_id:
        if previous.get("messageSha256") != message_hash:
            raise ValueError("guide turn ID was reused with different input")
        return task, previous["plan"], previous["modelCall"]
    current = ShoppingState.model_validate(task.domain_state["shopping"])
    view = _catalog_workspace_view(task, workspace)
    repair_reason = None
    prior_calls = []
    for attempt in range(2):
        receipt = None
        try:
            plan, receipt = await plan_turn(message, view, repair_reason=repair_reason)
            plan = expand_intent(plan)
            turn = _turn(plan, current, message)
            break
        except ValueError as exc:
            failed_receipt = getattr(exc, "receipt", None) or receipt
            if isinstance(failed_receipt, dict):
                prior_calls.append({"durationMs": failed_receipt.get("durationMs"),
                                    "usage": failed_receipt.get("usage"),
                                    "error": type(exc).__name__})
            if attempt:
                raise GuideInterpretationError("guide_interpretation_repair_exhausted") from exc
            repair_reason = type(exc).__name__ + ": " + str(exc)
    receipt = {**receipt, "parseAttempts": attempt + 1, "priorAttempts": prior_calls}
    if workspace:
        from .product_followup import bind_plan
        try:
            plan = await bind_plan(plan, view, task, message)
            plan = {**plan, "intent": intent_from_execution(plan)}
            turn = _turn(plan, current, message)
        except ValueError as exc:
            raise GuideInterpretationError("guide_reference_binding_invalid") from exc
    migration = task.domain_state.get("shoppingMigration") or {}
    if migration.get("needsClarification") and turn.action not in {"new", "cancel", "clarify"}:
        plan = {**plan, "intent": "clarify", "route": "catalog", "action": "clarify", "query": "",
                "retrievalQuery": "", "requirements": [], "numbers": [],
                "question": "旧会话中有无法无歧义转换的商品条件。请明确是重新提出完整需求，还是先澄清旧条件。",
                "followup": "none"}
        turn = _turn(plan, current, message)
    next_guide = transition(current, turn)
    decision = {
        "turnId": turn_id,
        "messageSha256": message_hash,
        "intent": plan["intent"],
        "baseRevision": task.revision,
        "semanticChanged": next_guide != current,
        "plan": plan,
        "modelCall": receipt,
    }
    patch = TaskStatePatchRequest(
        expectedRevision=task.revision,
        actor="agent",
        status="collecting_information" if turn.action == "clarify" else "ready",
        goal=(next_guide.query or message)[:500],
        addUnknowns=["guide_clarification"] if turn.action == "clarify" else [],
        resolveUnknowns=[] if turn.action == "clarify" else ["guide_clarification"],
        pendingQuestions=[turn.question or "请补充商品需求。"] if turn.action == "clarify" else None,
        domainStatePatch={
            "shopping": next_guide.model_dump(by_alias=True, mode="json"),
            "guideTurnDecision": decision,
            "guideEvidenceRef": None if next_guide != current else task.domain_state.get("guideEvidenceRef"),
            "shoppingMigration": None if turn.action in {"new", "cancel"} else migration or None,
        },
    )
    try:
        with bind_guide_write():
            updated = await update_task_state(task.task_id, patch)
    except TaskStateRevisionConflictError:
        latest = await get_task_state(task.task_id)
        previous = latest.domain_state.get("guideTurnDecision") if latest is not None else None
        if (isinstance(previous, dict) and previous.get("turnId") == turn_id
                and previous.get("messageSha256") == message_hash):
            return latest, previous["plan"], previous["modelCall"]
        raise GuideRevisionConflictError("guide_state_changed_during_interpretation")
    return updated, plan, receipt
