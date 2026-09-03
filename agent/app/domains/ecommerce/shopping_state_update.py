"""Server-owned shopping state transition and compatibility projection.

This is the single boundary that couples a validated ShoppingGuide change to
candidate-scope invalidation and the ShoppingTaskStateV2 shadow.  It performs
no model/tool calls and never persists on its own.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ...task_state import TaskState
from .models import (
    CandidateScope,
    ShoppingGuideState,
    ShoppingRequirement,
    compiled_shopping_requirements,
)
from .shopping_task_state_v2 import (
    CandidateScopeV2,
    CurrentAction,
    InformationSufficiency,
    RequirementStateDelta,
    ShoppingRequirementV2,
    ShoppingTaskStateV2,
    UnknownItem,
    parse_shopping_task_state_v2_snapshot,
)


def _requirement_payload(requirement: ShoppingRequirement) -> dict[str, Any]:
    return requirement.model_dump(mode="json")


def _requirement_id(task_id: str, requirement: ShoppingRequirement) -> str:
    payload = json.dumps(
        {"taskId": task_id, **_requirement_payload(requirement)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "req-" + hashlib.sha256(payload).hexdigest()[:24]


def _to_v2_requirement(
    task_id: str,
    requirement: ShoppingRequirement,
    *,
    introduced_turn: int,
    supersedes: str | None = None,
) -> ShoppingRequirementV2:
    source = (
        "user"
        if requirement.source == "user"
        else "server"
        if requirement.priority == "hard"
        else "inferred"
    )
    source_provenance = requirement.source if requirement.source != source else None
    return ShoppingRequirementV2(
        requirementId=_requirement_id(task_id, requirement),
        key=requirement.key,
        operator=requirement.operator,
        value=requirement.value,
        priority=requirement.priority,
        polarity=("exclude" if requirement.operator in {"neq", "not_in"} else "include"),
        source=source,
        sourceProvenance=source_provenance,
        confidence=(1.0 if source in {"user", "server"} else 0.8),
        status="active",
        memoryScope="task_only",
        introducedTurn=max(1, introduced_turn),
        supersedes=supersedes,
    )


def _same_requirement(left: ShoppingRequirement, right: ShoppingRequirement) -> bool:
    return _requirement_payload(left) == _requirement_payload(right)


def _requirement_lane(requirement: ShoppingRequirement) -> tuple[str, str]:
    polarity = "exclude" if requirement.operator in {"neq", "not_in"} else "include"
    return requirement.key.casefold().strip(), polarity


def _v2_requirement_lane(requirement: ShoppingRequirementV2) -> tuple[str, str]:
    return requirement.key.casefold().strip(), requirement.polarity


def _parse_guide(raw: object) -> ShoppingGuideState | None:
    if not isinstance(raw, dict):
        return None
    try:
        return ShoppingGuideState.model_validate(raw)
    except ValueError:
        return None


def guide_constraints_changed(state: TaskState, next_guide: ShoppingGuideState) -> bool:
    """Return whether the executable shopping condition set changed."""

    previous = _parse_guide(state.domain_state.get("shoppingGuide"))
    if previous is None:
        return bool(next_guide.requirements or next_guide.brand_avoidances)
    return (
        previous.category != next_guide.category
        or list(compiled_shopping_requirements(previous))
        != list(compiled_shopping_requirements(next_guide))
    )


def clear_stale_guide_references(
    state: TaskState,
    next_guide: ShoppingGuideState,
) -> tuple[ShoppingGuideState, bool]:
    """Clear candidate identities in the same transition as a condition change."""

    changed = guide_constraints_changed(state, next_guide)
    if not changed:
        return next_guide, False
    return next_guide.model_copy(update={
        "candidate_ids": [],
        "compared_ids": [],
        "evidence_status": "missing",
    }), True


def _effective_unknowns(state: TaskState, payload: Mapping[str, Any]) -> list[str]:
    resolved = set(payload.get("resolveUnknowns", []))
    result = [item for item in state.unknowns if item not in resolved]
    for item in payload.get("addUnknowns", []):
        if isinstance(item, str) and item not in result:
            result.append(item)
    return result


def _previous_v2_requirements(
    state: TaskState,
) -> dict[tuple[str, str], ShoppingRequirementV2]:
    raw = state.domain_state.get("shoppingTaskStateV2")
    try:
        snapshot = parse_shopping_task_state_v2_snapshot(raw)
    except (TypeError, ValueError):
        return {}
    return {_v2_requirement_lane(item): item for item in snapshot.requirements}


def _build_requirement_lifecycle(
    state: TaskState,
    next_guide: ShoppingGuideState,
) -> tuple[
    tuple[ShoppingRequirementV2, ...],
    tuple[RequirementStateDelta, ...],
    tuple[ShoppingRequirementV2, ...],
]:
    previous_guide = _parse_guide(state.domain_state.get("shoppingGuide"))
    previous_requirements = (
        list(compiled_shopping_requirements(previous_guide))
        if previous_guide is not None and previous_guide.category is not None
        else []
    )
    next_requirements = (
        list(compiled_shopping_requirements(next_guide))
        if next_guide.category is not None
        else []
    )
    persisted_v2 = _previous_v2_requirements(state)
    previous_by_key = {_requirement_lane(item): item for item in previous_requirements}
    next_by_key = {_requirement_lane(item): item for item in next_requirements}

    baseline_by_key: dict[tuple[str, str], ShoppingRequirementV2] = {}
    for key, requirement in previous_by_key.items():
        persisted = persisted_v2.get(key)
        if (
            persisted is not None
            and persisted.operator == requirement.operator
            and persisted.value == requirement.value
            and persisted.priority == requirement.priority
            and (persisted.source_provenance or persisted.source) == requirement.source
        ):
            baseline_by_key[key] = persisted
        else:
            baseline_by_key[key] = _to_v2_requirement(
                state.task_id,
                requirement,
                introduced_turn=state.revision,
            )

    terminal_by_key: dict[tuple[str, str], ShoppingRequirementV2] = {}
    deltas: list[RequirementStateDelta] = []
    for key, requirement in next_by_key.items():
        previous_requirement = previous_by_key.get(key)
        baseline = baseline_by_key.get(key)
        if (
            previous_requirement is not None
            and baseline is not None
            and _same_requirement(previous_requirement, requirement)
        ):
            terminal_by_key[key] = baseline
            continue
        if baseline is None:
            current = _to_v2_requirement(
                state.task_id,
                requirement,
                introduced_turn=state.revision + 1,
            )
            deltas.append(RequirementStateDelta(op="add", requirement=current))
        else:
            current = _to_v2_requirement(
                state.task_id,
                requirement,
                introduced_turn=state.revision + 1,
                supersedes=baseline.requirement_id,
            )
            deltas.append(RequirementStateDelta(op="override", requirement=current))
        terminal_by_key[key] = current

    for key, baseline in baseline_by_key.items():
        if key in next_by_key:
            continue
        deltas.append(RequirementStateDelta(
            op="revoke",
            requirement=baseline.model_copy(update={"status": "revoked"}),
        ))

    baseline = tuple(baseline_by_key[key] for key in previous_by_key)
    terminal = tuple(terminal_by_key[key] for key in next_by_key)
    return baseline, tuple(deltas), terminal


def _candidate_scope_v2(
    state: TaskState,
    guide: ShoppingGuideState,
    *,
    constraints_changed: bool,
) -> CandidateScopeV2 | None:
    if constraints_changed:
        return None
    raw = state.domain_state.get("candidateScope")
    try:
        scope = CandidateScope.model_validate(raw)
    except (TypeError, ValueError):
        return None
    if (
        scope.status != "active"
        or scope.task_id != state.task_id
        or scope.category != guide.category
        or list(scope.requirements_snapshot) != list(compiled_shopping_requirements(guide))
    ):
        return None
    candidate_ids = scope.visible_product_ids or scope.ranked_item_ids
    return CandidateScopeV2(
        scopeId=scope.scope_id,
        category=scope.category,
        candidateIds=candidate_ids,
        sourceTurn=max(1, scope.source_revision),
    )


def _stage_and_action(
    state: TaskState,
    guide: ShoppingGuideState,
    payload: Mapping[str, Any],
    unknowns: list[str],
) -> tuple[str, CurrentAction]:
    status = payload.get("status", state.status)
    questions = payload.get("pendingQuestions", state.pending_questions)
    if status == "completed":
        return "completed", CurrentAction(kind="answer", reason="task_completed")
    if unknowns or questions or status == "collecting_information":
        return "clarifying", CurrentAction(kind="clarify", reason="blocking_information_missing")
    if status == "executing":
        return "searching", CurrentAction(kind="wait", reason="tool_execution_in_progress")
    if guide.mode == "compare":
        return "recommending", CurrentAction(kind="compare", reason="validated_comparison_requested")
    return "searching", CurrentAction(kind="search", reason="validated_search_ready")


def build_shopping_state_transition_patch(
    state: TaskState,
    guide: ShoppingGuideState,
    payload: Mapping[str, Any],
    *,
    constraints_changed: bool,
) -> dict[str, Any]:
    """Build the atomic server patch for scope invalidation and V2 shadow."""

    unknowns = _effective_unknowns(state, payload)
    stage, action = _stage_and_action(state, guide, payload, unknowns)
    baseline, deltas, terminal = _build_requirement_lifecycle(state, guide)
    sufficiency = InformationSufficiency(
        status="insufficient" if unknowns else "sufficient",
        missingKeys=tuple(f"unknown-{index}" for index, _ in enumerate(unknowns, start=1)),
    )
    snapshot = ShoppingTaskStateV2(
        goal=str(payload.get("goal", state.goal)),
        useCase=("; ".join(guide.use_cases) or "shopping")[0:256],
        stage=stage,
        requirements=terminal,
        deltas=deltas,
        unknowns=tuple(
            UnknownItem(key=f"unknown-{index}", reason=text, blocking=True)
            for index, text in enumerate(unknowns, start=1)
        ),
        historyBaseline=baseline,
        informationSufficiency=sufficiency,
        candidateScope=_candidate_scope_v2(
            state,
            guide,
            constraints_changed=constraints_changed,
        ),
        currentAction=action,
    )
    result: dict[str, Any] = {
        "shoppingTaskStateV2": snapshot.model_dump(by_alias=True, mode="json"),
    }
    if constraints_changed:
        raw_scope = state.domain_state.get("candidateScope")
        try:
            scope = CandidateScope.model_validate(raw_scope)
        except (TypeError, ValueError):
            scope = None
        if scope is not None and scope.status == "active" and scope.task_id == state.task_id:
            reason = "shopping_constraints_changed"
            invalidated = scope.model_copy(update={
                "status": "invalidated",
                "invalidation_reason": reason,
            })
            result.update({
                "candidateScope": invalidated.model_dump(by_alias=True, mode="json"),
                "candidateScopeInvalidation": {
                    "scopeId": scope.scope_id,
                    "status": "invalidated",
                    "invalidationReason": reason,
                    "replacedByScopeId": None,
                    "invalidatedAtRevision": state.revision + 1,
                },
                "scopeRerankRequest": None,
            })

    return result


def refresh_shopping_state_v2_after_validation(
    state: TaskState,
    domain_patch: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Refresh the V2 scope/action from one server-validated execution patch."""

    future_domain = dict(state.domain_state)
    for key, value in domain_patch.items():
        if value is None:
            future_domain.pop(key, None)
        else:
            future_domain[key] = value
    guide = _parse_guide(future_domain.get("shoppingGuide"))
    if guide is None:
        return None
    future = state.model_copy(update={"domain_state": future_domain})
    candidate_scope = _candidate_scope_v2(
        future,
        guide,
        constraints_changed=False,
    )
    validation = future_domain.get("validationResult")
    validation_outcome = (
        validation.get("outcome") if isinstance(validation, dict) else None
    )
    try:
        full_scope = CandidateScope.model_validate(future_domain.get("candidateScope"))
    except (TypeError, ValueError):
        full_scope = None
    if full_scope is not None and full_scope.status == "active":
        guide = guide.model_copy(update={
            "candidate_ids": list(
                full_scope.visible_product_ids or full_scope.ranked_item_ids
            ),
            "evidence_status": (
                "complete" if validation_outcome == "passed" else "partial"
            ),
        })
        future_domain["shoppingGuide"] = guide.model_dump(by_alias=True, mode="json")
    action = CurrentAction(
        kind="answer" if validation_outcome == "passed" else "clarify",
        reason=(
            "validated_result_ready"
            if validation_outcome == "passed"
            else "validated_result_requires_review"
        ),
    )
    raw = state.domain_state.get("shoppingTaskStateV2")
    try:
        existing = parse_shopping_task_state_v2_snapshot(raw)
    except (TypeError, ValueError):
        existing = None
    if existing is not None:
        common = existing.model_dump(by_alias=True, mode="json")
        common["schemaVersion"] = "shopping-task-state-v2"
        common.pop("shoppingGuide", None)
        # V2.1 owns pendingQuestions, while the historical V2 lifecycle model
        # deliberately does not.  Strip both V2.1-only projection fields before
        # rebuilding the historical lifecycle; the atomic synchronizer below
        # restores them from the authoritative top-level TaskState.
        common.pop("pendingQuestions", None)
        common["candidateScope"] = (
            candidate_scope.model_dump(by_alias=True, mode="json")
            if candidate_scope is not None
            else None
        )
        common["scopeRerankRequest"] = None
        historical = ShoppingTaskStateV2.model_validate(common)
        refreshed = historical.model_copy(update={
            "stage": "recommending",
            "candidate_scope": candidate_scope,
            "current_action": action,
        })
        future_domain["shoppingTaskStateV2"] = refreshed.model_dump(
            by_alias=True,
            mode="json",
        )
        from .shopping_state_authority import synchronize_v2_compatibility_projection

        return synchronize_v2_compatibility_projection(
            future_domain,
            task_id=state.task_id,
            goal=state.goal,
            unknowns=state.unknowns,
            pending_questions=state.pending_questions,
        ).get("shoppingTaskStateV2")

    projected = build_shopping_state_transition_patch(
        future,
        guide,
        {"status": "ready"},
        constraints_changed=False,
    )
    snapshot = parse_shopping_task_state_v2_snapshot(projected["shoppingTaskStateV2"])
    refreshed = snapshot.model_copy(update={
        "stage": "recommending",
        "current_action": action,
    })
    return refreshed.model_dump(by_alias=True, mode="json")


__all__ = [
    "build_shopping_state_transition_patch",
    "clear_stale_guide_references",
    "guide_constraints_changed",
    "refresh_shopping_state_v2_after_validation",
]
