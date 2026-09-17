"""Whole-round ecommerce authority selection with a complete V2.1 contract.

Compatibility fields remain dual-written for rollback. A normal V2 read is
projected only from ``shoppingTaskStateV2`` schema ``shopping-task-state-v2.1``;
legacy fields cannot influence that projection. Legacy degradation is allowed
only when a server-written binding matches the current task/revision and the
complete legacy semantics actually consumed by ContextPack/Planner.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Mapping

from .models import CandidateScope, ScopeRerankRequest, ShoppingGuideState
from .shopping_task_state_v2 import (
    CurrentAction,
    InformationSufficiency,
    ShoppingTaskStateV2,
    ShoppingTaskStateV2Authoritative,
    UnknownItem,
)


AUTHORITY_BINDING_KEY = "shoppingStateAuthorityBinding"
_PUBLISHED_KEYS = ("shoppingGuide", "candidateScope", "scopeRerankRequest")


class ShoppingStateAuthorityError(ValueError):
    """Fail-closed authority selection error."""


@dataclass(frozen=True)
class ShoppingStateReadSelection:
    domain_state: dict[str, Any]
    source: Literal["v2", "legacy_rollback", "legacy_degraded", "empty"]
    semantic_hash: str | None
    goal: str
    unknowns: tuple[str, ...]
    pending_questions: tuple[str, ...]
    degraded_reason: str | None = None


def _json_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _published_from_models(
    guide: ShoppingGuideState,
    scope: CandidateScope | None,
    rerank: ScopeRerankRequest | None,
) -> dict[str, Any]:
    published = {"shoppingGuide": guide.model_dump(by_alias=True, mode="json")}
    if scope is not None:
        published["candidateScope"] = scope.model_dump(by_alias=True, mode="json")
    if rerank is not None:
        published["scopeRerankRequest"] = rerank.model_dump(by_alias=True, mode="json")
    return published


def _validate_projection_identity(
    *,
    guide: ShoppingGuideState,
    scope: CandidateScope | None,
    rerank: ScopeRerankRequest | None,
    task_id: str,
) -> None:
    if guide.category is None:
        if scope is not None or rerank is not None:
            raise ShoppingStateAuthorityError("shopping_category_missing_with_scope")
        return
    if scope is not None and (scope.task_id != task_id or scope.category != guide.category):
        raise ShoppingStateAuthorityError("candidate_scope_identity_mismatch")
    if rerank is not None and (scope is None or rerank.scope_id != scope.scope_id):
        raise ShoppingStateAuthorityError("scope_rerank_identity_mismatch")


def _legacy_projection(
    domain_state: Mapping[str, Any],
    *,
    task_id: str,
    goal: str,
    unknowns: list[str],
    pending_questions: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_guide = domain_state.get("shoppingGuide")
    if raw_guide is None:
        return {}, {
            "goal": goal,
            "unknowns": list(unknowns),
            "pendingQuestions": list(pending_questions),
            "published": {},
        }
    try:
        guide = ShoppingGuideState.model_validate(raw_guide)
        scope = (
            CandidateScope.model_validate(domain_state.get("candidateScope"))
            if domain_state.get("candidateScope") is not None
            else None
        )
        rerank = (
            ScopeRerankRequest.model_validate(domain_state.get("scopeRerankRequest"))
            if domain_state.get("scopeRerankRequest") is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ShoppingStateAuthorityError("legacy_projection_invalid") from exc
    _validate_projection_identity(guide=guide, scope=scope, rerank=rerank, task_id=task_id)
    published = _published_from_models(guide, scope, rerank)
    return published, {
        "goal": goal,
        "unknowns": list(unknowns),
        "pendingQuestions": list(pending_questions),
        "published": published,
    }


def _authoritative_projection(
    snapshot: ShoppingTaskStateV2Authoritative,
    *,
    task_id: str,
) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    guide = snapshot.shopping_guide
    scope = snapshot.candidate_scope
    rerank = snapshot.scope_rerank_request
    _validate_projection_identity(guide=guide, scope=scope, rerank=rerank, task_id=task_id)
    published = _published_from_models(guide, scope, rerank)
    unknowns = tuple(item.reason for item in snapshot.unknowns)
    pending = tuple(snapshot.pending_questions)
    semantic = {
        "goal": snapshot.goal,
        "unknowns": list(unknowns),
        "pendingQuestions": list(pending),
        "published": published,
    }
    return published, semantic, unknowns, pending


def _parse_any_snapshot(raw: object) -> ShoppingTaskStateV2 | ShoppingTaskStateV2Authoritative:
    if isinstance(raw, Mapping) and raw.get("schemaVersion") == "shopping-task-state-v2.1":
        return ShoppingTaskStateV2Authoritative.model_validate(raw)
    return ShoppingTaskStateV2.model_validate(raw)


def _synchronize_authoritative_top_level(
    snapshot: ShoppingTaskStateV2Authoritative,
    *,
    goal: str,
    unknowns: list[str],
    pending_questions: list[str],
) -> ShoppingTaskStateV2Authoritative:
    unknown_items = tuple(
        UnknownItem(
            key=f"unknown-{index}",
            reason=reason,
            # TaskState stores user-facing questions separately from unknown
            # labels, so text equality cannot establish their relationship.
            # A pending clarification makes the unresolved unknown set blocking;
            # an unknown-only patch remains explicitly non-blocking.
            blocking=bool(pending_questions),
        )
        for index, reason in enumerate(unknowns, start=1)
    )
    sufficiency = InformationSufficiency(
        status="insufficient" if unknown_items else "sufficient",
        missingKeys=tuple(item.key for item in unknown_items),
    )
    updates: dict[str, Any] = {
        "goal": goal,
        "unknowns": unknown_items,
        "pending_questions": tuple(pending_questions),
        "information_sufficiency": sufficiency,
    }
    if snapshot.shopping_guide.category is None:
        updates.update({
            "stage": "clarifying",
            "current_action": CurrentAction(
                kind="clarify",
                reason="unsupported_category_blocked",
            ),
        })
    elif unknowns or pending_questions:
        updates.update({
            "stage": "clarifying",
            "current_action": CurrentAction(
                kind="clarify",
                reason="blocking_information_missing",
            ),
        })
    elif snapshot.stage == "clarifying":
        action = "compare" if snapshot.shopping_guide.mode == "compare" else "search"
        updates.update({
            "stage": "recommending" if action == "compare" else "searching",
            "current_action": CurrentAction(
                kind=action,
                reason="validated_comparison_requested" if action == "compare" else "validated_search_ready",
            ),
        })
    raw = snapshot.model_copy(update=updates).model_dump(by_alias=True, mode="json")
    return ShoppingTaskStateV2Authoritative.model_validate(raw)


def _upgrade_snapshot_from_compatibility(
    snapshot: ShoppingTaskStateV2 | ShoppingTaskStateV2Authoritative,
    *,
    domain_state: Mapping[str, Any],
    task_id: str,
    goal: str,
    unknowns: list[str],
    pending_questions: list[str],
) -> ShoppingTaskStateV2Authoritative:
    try:
        guide = ShoppingGuideState.model_validate(domain_state.get("shoppingGuide"))
        scope = (
            CandidateScope.model_validate(domain_state.get("candidateScope"))
            if domain_state.get("candidateScope") is not None
            else None
        )
        rerank = (
            ScopeRerankRequest.model_validate(domain_state.get("scopeRerankRequest"))
            if domain_state.get("scopeRerankRequest") is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ShoppingStateAuthorityError("v2_1_migration_compatibility_invalid") from exc
    _validate_projection_identity(guide=guide, scope=scope, rerank=rerank, task_id=task_id)
    raw = snapshot.model_dump(by_alias=True, mode="json")
    if guide.category is None:
        # Moving to the explicit unsupported/unknown-category clarification
        # clears executable constraints through typed lifecycle revocations;
        # it must not leave the previous category's requirements active in the
        # authoritative V2 snapshot.
        revoked = []
        for requirement in list(raw.get("requirements") or []):
            terminal = dict(requirement)
            terminal["status"] = "revoked"
            revoked.append({"op": "revoke", "requirement": terminal})
        raw["deltas"] = [*(raw.get("deltas") or []), *revoked]
        raw["requirements"] = []
    blocking = bool(pending_questions)
    unknown_items = [
        {
            "key": f"unknown-{index}",
            "reason": reason,
            "blocking": blocking,
        }
        for index, reason in enumerate(unknowns, start=1)
    ]
    if guide.category is None:
        stage = "clarifying"
        action = {"kind": "clarify", "reason": "unsupported_category_blocked"}
    elif unknowns or pending_questions:
        stage = "clarifying"
        action = {"kind": "clarify", "reason": "blocking_information_missing"}
    elif raw.get("stage") == "clarifying":
        action_kind = "compare" if guide.mode == "compare" else "search"
        stage = "recommending" if action_kind == "compare" else "searching"
        action = {
            "kind": action_kind,
            "reason": (
                "validated_comparison_requested"
                if action_kind == "compare"
                else "validated_search_ready"
            ),
        }
    else:
        stage = raw.get("stage")
        action = raw.get("currentAction")
    raw.update({
        "schemaVersion": "shopping-task-state-v2.1",
        # Migration validates the latest server-owned guide and top-level
        # TaskState as one snapshot. Never validate a stale historical useCase
        # or goal against the new guide first.
        "goal": goal,
        "useCase": ("; ".join(guide.use_cases) or "shopping")[:256],
        "stage": stage,
        "unknowns": unknown_items,
        "informationSufficiency": {
            "status": "insufficient" if unknown_items else "sufficient",
            "missingKeys": [item["key"] for item in unknown_items],
        },
        "currentAction": action,
        "pendingQuestions": list(pending_questions),
        "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
        "candidateScope": (
            scope.model_dump(by_alias=True, mode="json") if scope is not None else None
        ),
        "scopeRerankRequest": (
            rerank.model_dump(by_alias=True, mode="json") if rerank is not None else None
        ),
    })
    try:
        upgraded = ShoppingTaskStateV2Authoritative.model_validate(raw)
        return _synchronize_authoritative_top_level(
            upgraded,
            goal=goal,
            unknowns=unknowns,
            pending_questions=pending_questions,
        )
    except (TypeError, ValueError) as exc:
        raise ShoppingStateAuthorityError("v2_1_migration_semantic_mismatch") from exc


def synchronize_v2_compatibility_projection(
    domain_state: Mapping[str, Any],
    *,
    task_id: str,
    goal: str,
    unknowns: list[str],
    pending_questions: list[str],
) -> dict[str, Any]:
    """Explicitly upgrade/refresh V2.1 during the same server-owned write."""

    result = dict(domain_state)
    try:
        raw_snapshot = result.get("shoppingTaskStateV2")
        if raw_snapshot is None:
            # A server-owned write may introduce the first shoppingGuide after
            # task creation. Materialize the historical transition shape from
            # that latest guide, then perform the explicit V2.1 migration.
            from .shopping_state_update import build_shopping_state_transition_patch

            guide = ShoppingGuideState.model_validate(result.get("shoppingGuide"))
            synthetic_state = SimpleNamespace(
                task_id=task_id,
                revision=1,
                domain_state=result,
                unknowns=list(unknowns),
                pending_questions=list(pending_questions),
                status=(
                    "collecting_information"
                    if guide.category is None or unknowns or pending_questions
                    else "ready"
                ),
                goal=goal,
            )
            raw_snapshot = build_shopping_state_transition_patch(
                synthetic_state,
                guide,
                {"status": synthetic_state.status},
                constraints_changed=False,
            )["shoppingTaskStateV2"]
        snapshot = _parse_any_snapshot(raw_snapshot)
    except (TypeError, ValueError) as exc:
        raise ShoppingStateAuthorityError("v2_1_synchronization_invalid") from exc
    upgraded = _upgrade_snapshot_from_compatibility(
        snapshot,
        domain_state=result,
        task_id=task_id,
        goal=goal,
        unknowns=unknowns,
        pending_questions=pending_questions,
    )
    result["shoppingTaskStateV2"] = upgraded.model_dump(by_alias=True, mode="json")
    return result


def bind_authoritative_write(
    domain_state: Mapping[str, Any],
    *,
    task_id: str,
    task_revision: int,
    goal: str,
    unknowns: list[str],
    pending_questions: list[str],
    mode: str = "v2",
    compatibility_projection_changed: bool = False,
) -> dict[str, Any]:
    """Upgrade V2.1 and bind the complete dual-write semantics after a CAS write."""

    if mode not in {"v2", "legacy"}:
        raise ShoppingStateAuthorityError("unsupported_shopping_state_authority")
    result = dict(domain_state)
    result.pop(AUTHORITY_BINDING_KEY, None)
    has_projection = any(result.get(key) is not None for key in _PUBLISHED_KEYS)
    if result.get("shoppingTaskStateV2") is None and not has_projection:
        return result
    if mode == "v2":
        try:
            current = ShoppingTaskStateV2Authoritative.model_validate(
                result.get("shoppingTaskStateV2")
            )
            if compatibility_projection_changed:
                # The TaskState CAS boundary explicitly identified a newer
                # server-owned guide patch. Synchronize that guide before
                # validating V2.1 so stale useCase/goal/top-level fields cannot
                # win merely because the previous V2.1 still parses.
                raise ShoppingStateAuthorityError("server_projection_changed")
            current = _synchronize_authoritative_top_level(
                current,
                goal=goal,
                unknowns=unknowns,
                pending_questions=pending_questions,
            )
        except (TypeError, ValueError, ShoppingStateAuthorityError):
            result = synchronize_v2_compatibility_projection(
                result,
                task_id=task_id,
                goal=goal,
                unknowns=unknowns,
                pending_questions=pending_questions,
            )
        else:
            result["shoppingTaskStateV2"] = current.model_dump(
                by_alias=True,
                mode="json",
            )
            published, _semantic, _unknowns, _pending = _authoritative_projection(
                current,
                task_id=task_id,
            )
            for key in _PUBLISHED_KEYS:
                result.pop(key, None)
            result.update(published)
    else:
        result = synchronize_v2_compatibility_projection(
            result,
            task_id=task_id,
            goal=goal,
            unknowns=unknowns,
            pending_questions=pending_questions,
        )
    try:
        snapshot = ShoppingTaskStateV2Authoritative.model_validate(
            result.get("shoppingTaskStateV2")
        )
        _published, authoritative, v2_unknowns, v2_pending = _authoritative_projection(
            snapshot,
            task_id=task_id,
        )
        _legacy_published, legacy = _legacy_projection(
            result,
            task_id=task_id,
            goal=goal,
            unknowns=unknowns,
            pending_questions=pending_questions,
        )
    except (TypeError, ValueError, ShoppingStateAuthorityError) as exc:
        raise ShoppingStateAuthorityError("authoritative_write_incomplete") from exc
    legacy_hash = _json_hash(legacy)
    authoritative_hash = _json_hash(authoritative)
    if (
        legacy_hash != authoritative_hash
        or tuple(unknowns) != v2_unknowns
        or tuple(pending_questions) != v2_pending
    ):
        raise ShoppingStateAuthorityError("authoritative_write_semantic_mismatch")
    result[AUTHORITY_BINDING_KEY] = {
        "schemaVersion": "shopping-state-authority-binding-v2",
        "taskId": task_id,
        "taskRevision": task_revision,
        "semanticHash": legacy_hash,
    }
    return result


def select_shopping_state_authority(
    *,
    domain_state: Mapping[str, Any],
    task_id: str,
    task_revision: int,
    goal: str,
    unknowns: list[str],
    pending_questions: list[str],
    mode: str = "v2",
) -> ShoppingStateReadSelection:
    """Select one complete projection for the whole round."""

    if mode == "legacy":
        published, semantic = _legacy_projection(
            domain_state,
            task_id=task_id,
            goal=goal,
            unknowns=unknowns,
            pending_questions=pending_questions,
        )
        return ShoppingStateReadSelection(
            published,
            "legacy_rollback",
            _json_hash(semantic),
            goal,
            tuple(unknowns),
            tuple(pending_questions),
        )
    if mode != "v2":
        raise ShoppingStateAuthorityError("unsupported_shopping_state_authority")

    has_legacy = any(domain_state.get(key) is not None for key in _PUBLISHED_KEYS)
    raw_v2 = domain_state.get("shoppingTaskStateV2")
    if raw_v2 is None and not has_legacy:
        return ShoppingStateReadSelection(
            {}, "empty", None, goal, tuple(unknowns), tuple(pending_questions)
        )

    try:
        snapshot = ShoppingTaskStateV2Authoritative.model_validate(raw_v2)
        published, semantic, v2_unknowns, v2_pending = _authoritative_projection(
            snapshot,
            task_id=task_id,
        )
    except (TypeError, ValueError, ShoppingStateAuthorityError) as exc:
        reason = "v2_missing" if raw_v2 is None else "v2_invalid_or_incomplete"
        try:
            published, legacy = _legacy_projection(
                domain_state,
                task_id=task_id,
                goal=goal,
                unknowns=unknowns,
                pending_questions=pending_questions,
            )
        except ShoppingStateAuthorityError as legacy_exc:
            raise ShoppingStateAuthorityError(f"{reason}_legacy_invalid") from legacy_exc
        legacy_hash = _json_hash(legacy)
        binding = domain_state.get(AUTHORITY_BINDING_KEY)
        expected = {
            "schemaVersion": "shopping-state-authority-binding-v2",
            "taskId": task_id,
            "taskRevision": task_revision,
            "semanticHash": legacy_hash,
        }
        if binding != expected:
            raise ShoppingStateAuthorityError(f"{reason}_binding_mismatch") from exc
        return ShoppingStateReadSelection(
            published,
            "legacy_degraded",
            legacy_hash,
            goal,
            tuple(unknowns),
            tuple(pending_questions),
            f"{reason}_legacy_semantic_equivalent",
        )

    semantic_hash = _json_hash(semantic)
    if (
        snapshot.goal != goal
        or v2_unknowns != tuple(unknowns)
        or v2_pending != tuple(pending_questions)
    ):
        raise ShoppingStateAuthorityError("v2_top_level_semantic_mismatch")
    expected_binding = {
        "schemaVersion": "shopping-state-authority-binding-v2",
        "taskId": task_id,
        "taskRevision": task_revision,
        "semanticHash": semantic_hash,
    }
    if domain_state.get(AUTHORITY_BINDING_KEY) != expected_binding:
        raise ShoppingStateAuthorityError("v2_binding_mismatch")
    return ShoppingStateReadSelection(
        published,
        "v2",
        semantic_hash,
        snapshot.goal,
        v2_unknowns,
        v2_pending,
    )


__all__ = [
    "AUTHORITY_BINDING_KEY",
    "ShoppingStateAuthorityError",
    "ShoppingStateReadSelection",
    "bind_authoritative_write",
    "select_shopping_state_authority",
    "synchronize_v2_compatibility_projection",
]
