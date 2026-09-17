"""Bounded EvidenceResearchAgent V1 contracts and deterministic gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domains.ecommerce.models import CandidateScope
from .schemas import ToolTrace
from .settings import settings


SELECTION_POLICY_VERSION = "investigation-selection-policy-v1"
MAX_INVESTIGATION_CANDIDATES = 8
Verdict = Literal["SATISFIED", "VIOLATED", "UNKNOWN", "CONFLICT"]
_RESEARCH_MERGE_KEY_PREFIX = "agent-research-merge-v1"
_RESEARCH_MERGE_CLAIM_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return {1, ARGV[1]}
end
if current == ARGV[1] then
  return {0, current}
end
return {-1, current}
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def candidate_scope_hash_v1(scope: CandidateScope) -> str:
    return _sha256(scope.model_dump(by_alias=True, mode="json"))


def canonical_gap_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.casefold().strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized or len(normalized) > 160:
        raise ValueError("evidence gap key is invalid")
    return normalized


class InvestigationSetV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["investigation-set-v1"] = Field(
        default="investigation-set-v1", alias="schemaVersion"
    )
    investigation_set_id: str = Field(alias="investigationSetId")
    task_id: str = Field(alias="taskId")
    task_revision: int = Field(alias="taskRevision", ge=1)
    candidate_scope_id: str = Field(alias="candidateScopeId")
    candidate_scope_source_revision: int = Field(
        alias="candidateScopeSourceRevision", ge=1
    )
    candidate_scope_hash: str = Field(
        alias="candidateScopeHash", pattern=r"^[0-9a-f]{64}$"
    )
    candidate_ids: tuple[int, ...] = Field(
        alias="candidateIds", min_length=1, max_length=8
    )
    evidence_gap_keys: tuple[str, ...] = Field(
        alias="evidenceGapKeys", min_length=1
    )
    selection_policy_version: Literal["investigation-selection-policy-v1"] = Field(
        default=SELECTION_POLICY_VERSION, alias="selectionPolicyVersion"
    )
    expires_at: datetime = Field(alias="expiresAt")
    binding_hash: str = Field(alias="bindingHash", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_uniqueness_and_order(self) -> "InvestigationSetV1":
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidateIds must be unique")
        if tuple(sorted(self.evidence_gap_keys)) != self.evidence_gap_keys:
            raise ValueError("evidenceGapKeys must be normalized and sorted")
        if len(self.evidence_gap_keys) != len(set(self.evidence_gap_keys)):
            raise ValueError("evidenceGapKeys must be unique")
        return self


@dataclass(frozen=True)
class InvestigationSelectionResult:
    investigation_set: InvestigationSetV1
    reasons_by_candidate: dict[int, tuple[str, ...]]
    excluded_known_hard_violations: tuple[int, ...]


class InvestigationSetError(ValueError):
    pass


class NoInvestigationNeeded(InvestigationSetError):
    pass


def _normalized_gap_map(
    values: Mapping[int, list[str] | tuple[str, ...] | set[str]] | None,
    *,
    ranked: set[int],
    label: str,
) -> dict[int, tuple[str, ...]]:
    result: dict[int, tuple[str, ...]] = {}
    for raw_id, raw_keys in (values or {}).items():
        if type(raw_id) is not int or raw_id not in ranked:
            raise InvestigationSetError(f"{label} contains an out-of-scope candidate")
        keys = tuple(sorted({canonical_gap_key(key) for key in raw_keys}))
        if keys:
            result[raw_id] = keys
    return result


def _binding_payload(value: InvestigationSetV1 | dict[str, Any]) -> dict[str, Any]:
    raw = (
        value.model_dump(by_alias=True, mode="json")
        if isinstance(value, InvestigationSetV1)
        else dict(value)
    )
    expires = raw["expiresAt"]
    if isinstance(expires, datetime):
        expires = expires.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "investigationSetId": raw["investigationSetId"],
        "taskId": raw["taskId"],
        "taskRevision": raw["taskRevision"],
        "candidateScopeId": raw["candidateScopeId"],
        "candidateScopeSourceRevision": raw["candidateScopeSourceRevision"],
        "candidateScopeHash": raw["candidateScopeHash"],
        "candidateIds": list(raw["candidateIds"]),
        "evidenceGapKeys": list(raw["evidenceGapKeys"]),
        "selectionPolicyVersion": raw["selectionPolicyVersion"],
        "expiresAt": expires,
    }


def investigation_binding_hash(value: InvestigationSetV1 | dict[str, Any]) -> str:
    return _sha256(_binding_payload(value))


def build_investigation_set_v1(
    *,
    candidate_scope: CandidateScope,
    task_id: str,
    task_revision: int,
    hard_unknowns_by_product: Mapping[
        int, list[str] | tuple[str, ...] | set[str]
    ] | None,
    conflicts_by_product: Mapping[
        int, list[str] | tuple[str, ...] | set[str]
    ] | None = None,
    unknowns_by_product: Mapping[
        int, list[str] | tuple[str, ...] | set[str]
    ] | None = None,
    known_hard_violations: set[int] | frozenset[int] | None = None,
    expires_at: datetime,
) -> InvestigationSelectionResult:
    """Select a ranked subset while prioritizing unresolved hard evidence."""

    if candidate_scope.status != "active":
        raise InvestigationSetError("candidate scope is not active")
    if candidate_scope.task_id != task_id:
        raise InvestigationSetError("candidate scope task identity mismatch")
    if task_revision < candidate_scope.source_revision:
        raise InvestigationSetError("task revision predates candidate scope")
    if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
        raise InvestigationSetError("investigation expiry is invalid")

    ranked_ids = list(candidate_scope.ranked_item_ids)
    ranked_set = set(ranked_ids)
    hard_unknowns = _normalized_gap_map(
        hard_unknowns_by_product,
        ranked=ranked_set,
        label="hardUnknownsByProduct",
    )
    conflicts = _normalized_gap_map(
        conflicts_by_product,
        ranked=ranked_set,
        label="conflictsByProduct",
    )
    unknowns = _normalized_gap_map(
        unknowns_by_product,
        ranked=ranked_set,
        label="unknownsByProduct",
    )
    violations = set(known_hard_violations or ())
    if any(type(item) is not int for item in violations):
        raise InvestigationSetError("known hard violations contain an invalid id")

    priority: list[int] = []
    reasons: dict[int, list[str]] = {}

    def add_group(group: Mapping[int, tuple[str, ...]], reason: str) -> None:
        for candidate_id in ranked_ids:
            if candidate_id not in group or candidate_id in violations:
                continue
            reasons.setdefault(candidate_id, []).append(reason)
            if candidate_id not in priority:
                priority.append(candidate_id)

    add_group(hard_unknowns, "hard_unknown")
    add_group(conflicts, "conflict")
    add_group(unknowns, "unknown")
    if not priority:
        raise NoInvestigationNeeded("candidate scope contains no evidence gap")

    selected = priority[:MAX_INVESTIGATION_CANDIDATES]
    if len(selected) < MAX_INVESTIGATION_CANDIDATES:
        for candidate_id in ranked_ids:
            if candidate_id in violations or candidate_id in selected:
                continue
            selected.append(candidate_id)
            reasons.setdefault(candidate_id, []).append("rank_fill")
            if len(selected) == MAX_INVESTIGATION_CANDIDATES:
                break
    selected_set = set(selected)
    selected = [candidate_id for candidate_id in ranked_ids if candidate_id in selected_set]

    gap_keys = sorted(
        {
            key
            for candidate_id in selected
            for mapping in (hard_unknowns, conflicts, unknowns)
            for key in mapping.get(candidate_id, ())
        }
    )
    if not gap_keys:
        raise NoInvestigationNeeded("selected candidates contain no evidence gap")

    scope_hash = candidate_scope_hash_v1(candidate_scope)
    expires_text = expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    id_payload = {
        "taskId": task_id,
        "taskRevision": task_revision,
        "candidateScopeId": candidate_scope.scope_id,
        "candidateScopeSourceRevision": candidate_scope.source_revision,
        "candidateScopeHash": scope_hash,
        "candidateIds": selected,
        "evidenceGapKeys": gap_keys,
        "selectionPolicyVersion": SELECTION_POLICY_VERSION,
        "expiresAt": expires_text,
    }
    investigation_set_id = "iset-" + _sha256(id_payload)[:24]
    raw = {
        "schemaVersion": "investigation-set-v1",
        "investigationSetId": investigation_set_id,
        **id_payload,
    }
    raw["bindingHash"] = investigation_binding_hash(raw)
    investigation_set = InvestigationSetV1.model_validate(raw)
    return InvestigationSelectionResult(
        investigation_set=investigation_set,
        reasons_by_candidate={
            candidate_id: tuple(reasons.get(candidate_id, ()))
            for candidate_id in selected
        },
        excluded_known_hard_violations=tuple(
            candidate_id for candidate_id in ranked_ids if candidate_id in violations
        ),
    )


def validate_investigation_set_v1(
    value: InvestigationSetV1,
    *,
    candidate_scope: CandidateScope,
    task_id: str,
    task_revision: int,
    now: datetime | None = None,
) -> None:
    if candidate_scope.status != "active":
        raise InvestigationSetError("candidate scope is not active")
    if value.task_id != task_id or candidate_scope.task_id != task_id:
        raise InvestigationSetError("task identity mismatch")
    if value.task_revision != task_revision:
        raise InvestigationSetError("task revision mismatch")
    if value.candidate_scope_id != candidate_scope.scope_id:
        raise InvestigationSetError("candidate scope id mismatch")
    if value.candidate_scope_source_revision != candidate_scope.source_revision:
        raise InvestigationSetError("candidate scope revision mismatch")
    if value.candidate_scope_hash != candidate_scope_hash_v1(candidate_scope):
        raise InvestigationSetError("candidate scope hash mismatch")
    ranks = {candidate_id: index for index, candidate_id in enumerate(candidate_scope.ranked_item_ids)}
    if any(candidate_id not in ranks for candidate_id in value.candidate_ids):
        raise InvestigationSetError("investigation candidate is outside scope")
    if list(value.candidate_ids) != sorted(value.candidate_ids, key=ranks.__getitem__):
        raise InvestigationSetError("investigation candidates are not in scope rank order")
    if value.binding_hash != investigation_binding_hash(value):
        raise InvestigationSetError("investigation binding hash mismatch")
    current = now or datetime.now(timezone.utc)
    if value.expires_at <= current:
        raise InvestigationSetError("investigation set is expired")


class ResearchRequestV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["research-request-v1"] = Field(
        default="research-request-v1", alias="schemaVersion"
    )
    request_id: str = Field(alias="requestId")
    handoff_id: str = Field(alias="handoffId")
    parent_run_id: str = Field(alias="parentRunId")
    child_run_id: str = Field(alias="childRunId")
    task_id: str = Field(alias="taskId")
    task_revision: int = Field(alias="taskRevision", ge=1)
    candidate_scope_id: str = Field(alias="candidateScopeId")
    candidate_scope_source_revision: int = Field(
        alias="candidateScopeSourceRevision", ge=1
    )
    candidate_scope_hash: str = Field(
        alias="candidateScopeHash", pattern=r"^[0-9a-f]{64}$"
    )
    investigation_set_id: str = Field(alias="investigationSetId")
    investigation_set_binding_hash: str = Field(
        alias="investigationSetBindingHash", pattern=r"^[0-9a-f]{64}$"
    )
    parent_context_binding_hash: str = Field(
        alias="parentContextBindingHash", pattern=r"^[0-9a-f]{64}$"
    )
    child_context_binding_hash: str = Field(
        alias="childContextBindingHash", pattern=r"^[0-9a-f]{64}$"
    )
    capability_grant_hash: str = Field(
        alias="capabilityGrantHash", pattern=r"^[0-9a-f]{64}$"
    )
    research_goal: str = Field(alias="researchGoal", min_length=1, max_length=2000)
    candidate_ids: tuple[int, ...] = Field(
        alias="candidateIds", min_length=1, max_length=8
    )
    evidence_gap_keys: tuple[str, ...] = Field(
        alias="evidenceGapKeys", min_length=1
    )
    allowed_tools: tuple[Literal["get_product_details"], ...] = Field(
        alias="allowedTools", min_length=1, max_length=1
    )
    max_tool_calls: int = Field(alias="maxToolCalls", ge=0, le=64)
    max_model_decisions: int = Field(alias="maxModelDecisions", ge=0, le=32)
    deadline_at: datetime = Field(alias="deadlineAt")
    payload_hash: str = Field(alias="payloadHash", pattern=r"^[0-9a-f]{64}$")
    binding_hash: str = Field(alias="bindingHash", pattern=r"^[0-9a-f]{64}$")
    sensitivity: Literal["SERVER_ONLY"] = "SERVER_ONLY"

    @model_validator(mode="after")
    def validate_bounded_values(self) -> "ResearchRequestV1":
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidateIds must be unique")
        if self.evidence_gap_keys != tuple(sorted(set(self.evidence_gap_keys))):
            raise ValueError("evidenceGapKeys must be normalized, sorted and unique")
        if self.allowed_tools != ("get_product_details",):
            raise ValueError("V1 research tool allowlist is fixed")
        return self


class ResearchFindingV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    candidate_id: int = Field(alias="candidateId", ge=1)
    evidence_gap_key: str = Field(alias="evidenceGapKey", min_length=1, max_length=160)
    verdict: Verdict
    evidence_refs: tuple[str, ...] = Field(default=(), alias="evidenceRefs")
    summary: str = Field(min_length=1, max_length=2000)
    source_authority: Literal[
        "TOOL_FACT", "VERIFIED_CATALOG", "INSUFFICIENT", "CONFLICTING"
    ] = Field(alias="sourceAuthority")

    @model_validator(mode="after")
    def validate_evidence_discipline(self) -> "ResearchFindingV1":
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidenceRefs must be unique")
        if self.verdict in {"SATISFIED", "VIOLATED"} and not self.evidence_refs:
            raise ValueError("a resolved verdict requires evidence")
        if self.verdict == "CONFLICT" and len(self.evidence_refs) < 2:
            raise ValueError("a conflict requires at least two evidence refs")
        if self.verdict == "UNKNOWN" and self.source_authority != "INSUFFICIENT":
            raise ValueError("UNKNOWN must retain insufficient authority")
        return self


class RejectedResearchFindingV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)
    candidate_id: int = Field(alias="candidateId", ge=1)
    evidence_gap_key: str = Field(alias="evidenceGapKey")
    reason: Literal[
        "OUT_OF_SCOPE",
        "STALE_REVISION",
        "BINDING_MISMATCH",
        "UNAUTHORIZED_TOOL",
        "INVALID_EVIDENCE",
        "DUPLICATE",
    ]


class UnresolvedResearchGapV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)
    candidate_id: int = Field(alias="candidateId", ge=1)
    evidence_gap_key: str = Field(alias="evidenceGapKey")
    reason: str = Field(min_length=1, max_length=500)


class ResearchReportV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["research-report-v1"] = Field(
        default="research-report-v1", alias="schemaVersion"
    )
    report_id: str = Field(alias="reportId")
    request_id: str = Field(alias="requestId")
    handoff_id: str = Field(alias="handoffId")
    parent_run_id: str = Field(alias="parentRunId")
    child_run_id: str = Field(alias="childRunId")
    task_id: str = Field(alias="taskId")
    task_revision: int = Field(alias="taskRevision", ge=1)
    candidate_scope_id: str = Field(alias="candidateScopeId")
    candidate_scope_source_revision: int = Field(
        alias="candidateScopeSourceRevision", ge=1
    )
    candidate_scope_hash: str = Field(
        alias="candidateScopeHash", pattern=r"^[0-9a-f]{64}$"
    )
    investigation_set_id: str = Field(alias="investigationSetId")
    investigation_set_binding_hash: str = Field(
        alias="investigationSetBindingHash", pattern=r"^[0-9a-f]{64}$"
    )
    findings: tuple[ResearchFindingV1, ...] = ()
    rejected_findings: tuple[RejectedResearchFindingV1, ...] = Field(
        default=(), alias="rejectedFindings"
    )
    unresolved: tuple[UnresolvedResearchGapV1, ...] = ()
    stop_reason: Literal[
        "COMPLETE",
        "BUDGET_EXHAUSTED",
        "DEADLINE",
        "CANCELLED",
        "TOOL_FAILURE",
        "CONFLICT",
    ] = Field(alias="stopReason")
    generated_at: datetime = Field(alias="generatedAt")
    report_hash: str = Field(alias="reportHash", pattern=r"^[0-9a-f]{64}$")
    binding_hash: str = Field(alias="bindingHash", pattern=r"^[0-9a-f]{64}$")


class ResearchMergeReceiptV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["research-merge-receipt-v1"] = Field(
        default="research-merge-receipt-v1", alias="schemaVersion"
    )
    merge_receipt_id: str = Field(alias="mergeReceiptId")
    parent_run_id: str = Field(alias="parentRunId")
    handoff_id: str = Field(alias="handoffId")
    request_id: str = Field(alias="requestId")
    report_id: str = Field(alias="reportId")
    task_id: str = Field(alias="taskId")
    task_revision: int = Field(alias="taskRevision", ge=1)
    candidate_scope_id: str = Field(alias="candidateScopeId")
    candidate_scope_hash: str = Field(
        alias="candidateScopeHash", pattern=r"^[0-9a-f]{64}$"
    )
    report_hash: str = Field(alias="reportHash", pattern=r"^[0-9a-f]{64}$")
    report_binding_hash: str = Field(
        alias="reportBindingHash", pattern=r"^[0-9a-f]{64}$"
    )
    outcome: Literal["ACCEPTED", "REPLAY_NOOP"]
    accepted_finding_count: int = Field(alias="acceptedFindingCount", ge=0)
    unresolved_count: int = Field(alias="unresolvedCount", ge=0)
    persistence_status: Literal["PRIMARY"] = Field(
        default="PRIMARY", alias="persistenceStatus"
    )
    created_at: datetime = Field(alias="createdAt")
    receipt_hash: str = Field(alias="receiptHash", pattern=r"^[0-9a-f]{64}$")


class ResearchDecisionOutputV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)
    findings: tuple[ResearchFindingV1, ...] = ()
    unresolved: tuple[UnresolvedResearchGapV1, ...] = ()
    stop_reason: Literal[
        "COMPLETE",
        "BUDGET_EXHAUSTED",
        "DEADLINE",
        "CANCELLED",
        "TOOL_FAILURE",
        "CONFLICT",
    ] = Field(
        default="COMPLETE", alias="stopReason"
    )


class ResearchContractError(ValueError):
    pass


class ResearchReportRejected(ResearchContractError):
    pass


class ResearchMergePersistenceError(ResearchContractError):
    pass


class ResearchMergeConflict(ResearchContractError):
    pass


class RouteSignalsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    requires_clarification: bool = False
    transaction_intent: bool = False
    direct_answer_available: bool = False
    unresolved_evidence_gap_count: int = Field(default=0, ge=0)
    direct_resolution_within_budget: bool = False


class RouteDecisionV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)
    schema_version: Literal["route-decision-v1"] = Field(
        default="route-decision-v1", alias="schemaVersion"
    )
    decision_id: str = Field(alias="decisionId")
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    task_revision: int = Field(alias="taskRevision", ge=1)
    candidate_scope_id: str | None = Field(default=None, alias="candidateScopeId")
    candidate_scope_source_revision: int | None = Field(
        default=None, alias="candidateScopeSourceRevision", ge=1
    )
    route_class: Literal[
        "MUST_CLARIFY",
        "MUST_TRANSACTION",
        "MUST_DIRECT",
        "RESEARCH_ELIGIBLE",
        "RESEARCH_REQUIRED",
    ] = Field(alias="routeClass")
    reasons: tuple[str, ...] = Field(min_length=1)
    route_policy_version: Literal["context-multiagent-route-v1"] = Field(
        default="context-multiagent-route-v1", alias="routePolicyVersion"
    )
    decision_source: Literal["DETERMINISTIC_GATE"] = Field(
        default="DETERMINISTIC_GATE", alias="decisionSource"
    )
    research_allowed: bool = Field(alias="researchAllowed")
    transaction_allowed: bool = Field(alias="transactionAllowed")
    binding_hash: str = Field(alias="bindingHash", pattern=r"^[0-9a-f]{64}$")


def route_context_multiagent_v1(
    *,
    run_id: str,
    task_id: str,
    task_revision: int,
    candidate_scope: CandidateScope | None,
    signals: RouteSignalsV1,
) -> RouteDecisionV1:
    """Five-class deterministic gate; the tested agent never labels itself."""

    if signals.requires_clarification:
        route_class = "MUST_CLARIFY"
        reasons = ("required_information_missing",)
    elif signals.transaction_intent:
        route_class = "MUST_TRANSACTION"
        reasons = ("transaction_intent_requires_deterministic_backend",)
    elif signals.direct_answer_available or signals.unresolved_evidence_gap_count == 0:
        route_class = "MUST_DIRECT"
        reasons = ("current_verified_context_is_sufficient",)
    elif signals.direct_resolution_within_budget:
        route_class = "RESEARCH_ELIGIBLE"
        reasons = ("bounded_evidence_gap_can_use_either_arm",)
    else:
        route_class = "RESEARCH_REQUIRED"
        reasons = ("evidence_gap_exceeds_frozen_direct_budget",)
    research_allowed = route_class in {"RESEARCH_ELIGIBLE", "RESEARCH_REQUIRED"}
    transaction_allowed = route_class == "MUST_TRANSACTION"
    base = {
        "runId": run_id,
        "taskId": task_id,
        "taskRevision": task_revision,
        "candidateScopeId": candidate_scope.scope_id if candidate_scope else None,
        "candidateScopeSourceRevision": (
            candidate_scope.source_revision if candidate_scope else None
        ),
        "routeClass": route_class,
        "reasons": list(reasons),
        "routePolicyVersion": "context-multiagent-route-v1",
        "decisionSource": "DETERMINISTIC_GATE",
        "researchAllowed": research_allowed,
        "transactionAllowed": transaction_allowed,
    }
    decision_id = "route-" + _sha256({**base, "signals": signals.model_dump()})[:24]
    raw = {"schemaVersion": "route-decision-v1", "decisionId": decision_id, **base}
    raw["bindingHash"] = _sha256(raw)
    return RouteDecisionV1.model_validate(raw)


def validate_route_decision_v1(value: RouteDecisionV1) -> None:
    raw = value.model_dump(by_alias=True, mode="json")
    binding = raw.pop("bindingHash")
    if binding != _sha256(raw):
        raise ResearchContractError("route decision binding hash mismatch")
    expected_research = value.route_class in {
        "RESEARCH_ELIGIBLE",
        "RESEARCH_REQUIRED",
    }
    expected_transaction = value.route_class == "MUST_TRANSACTION"
    if (
        value.research_allowed != expected_research
        or value.transaction_allowed != expected_transaction
    ):
        raise ResearchContractError("route decision capability mismatch")


def _research_request_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "researchGoal": raw["researchGoal"],
        "candidateIds": list(raw["candidateIds"]),
        "evidenceGapKeys": list(raw["evidenceGapKeys"]),
        "allowedTools": list(raw["allowedTools"]),
        "maxToolCalls": raw["maxToolCalls"],
        "maxModelDecisions": raw["maxModelDecisions"],
        "deadlineAt": raw["deadlineAt"],
    }


def _research_request_binding(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: raw[key]
        for key in (
            "requestId",
            "handoffId",
            "parentRunId",
            "childRunId",
            "taskId",
            "taskRevision",
            "candidateScopeId",
            "candidateScopeSourceRevision",
            "candidateScopeHash",
            "investigationSetId",
            "investigationSetBindingHash",
            "parentContextBindingHash",
            "childContextBindingHash",
            "capabilityGrantHash",
            "payloadHash",
            "sensitivity",
        )
    }


def build_research_request_v1(
    *,
    investigation_set: InvestigationSetV1,
    candidate_scope: CandidateScope,
    task_id: str,
    task_revision: int,
    parent_run_id: str,
    handoff_id: str,
    child_run_id: str,
    parent_context_binding_hash: str,
    child_context_binding_hash: str,
    capability_grant_hash: str,
    research_goal: str,
    deadline_at: datetime,
    max_tool_calls: int,
    max_model_decisions: int,
) -> ResearchRequestV1:
    validate_investigation_set_v1(
        investigation_set,
        candidate_scope=candidate_scope,
        task_id=task_id,
        task_revision=task_revision,
    )
    if deadline_at > investigation_set.expires_at:
        raise ResearchContractError("research deadline exceeds InvestigationSet expiry")
    if deadline_at <= datetime.now(timezone.utc):
        raise ResearchContractError("research deadline is expired")
    if max_tool_calls < 1:
        raise ResearchContractError("V1 research requires one read-only detail call")
    base = {
        "handoffId": handoff_id,
        "parentRunId": parent_run_id,
        "childRunId": child_run_id,
        "taskId": task_id,
        "taskRevision": task_revision,
        "candidateScopeId": candidate_scope.scope_id,
        "candidateScopeSourceRevision": candidate_scope.source_revision,
        "candidateScopeHash": candidate_scope_hash_v1(candidate_scope),
        "investigationSetId": investigation_set.investigation_set_id,
        "investigationSetBindingHash": investigation_set.binding_hash,
        "parentContextBindingHash": parent_context_binding_hash,
        "childContextBindingHash": child_context_binding_hash,
        "capabilityGrantHash": capability_grant_hash,
        "researchGoal": research_goal.strip(),
        "candidateIds": list(investigation_set.candidate_ids),
        "evidenceGapKeys": list(investigation_set.evidence_gap_keys),
        "allowedTools": ["get_product_details"],
        "maxToolCalls": max_tool_calls,
        "maxModelDecisions": max_model_decisions,
        "deadlineAt": deadline_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    for name in (
        "parentContextBindingHash",
        "childContextBindingHash",
        "capabilityGrantHash",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(base[name])):
            raise ResearchContractError(f"invalid handoff binding: {name}")
    base["sensitivity"] = "SERVER_ONLY"
    payload_hash = _sha256(_research_request_payload(base))
    request_id = "rreq-" + _sha256({**base, "payloadHash": payload_hash})[:24]
    raw = {"schemaVersion": "research-request-v1", "requestId": request_id, **base}
    raw["payloadHash"] = payload_hash
    raw["bindingHash"] = _sha256(_research_request_binding(raw))
    return ResearchRequestV1.model_validate(raw)


def validate_research_request_v1(
    request: ResearchRequestV1,
    *,
    investigation_set: InvestigationSetV1,
    candidate_scope: CandidateScope,
    task_id: str,
    task_revision: int,
    parent_context_binding_hash: str,
    child_context_binding_hash: str,
    capability_grant_hash: str,
    now: datetime | None = None,
) -> None:
    validate_investigation_set_v1(
        investigation_set,
        candidate_scope=candidate_scope,
        task_id=task_id,
        task_revision=task_revision,
        now=now,
    )
    raw = request.model_dump(by_alias=True, mode="json")
    if request.task_id != task_id or request.task_revision != task_revision:
        raise ResearchContractError("research task identity mismatch")
    if request.candidate_scope_id != candidate_scope.scope_id:
        raise ResearchContractError("research CandidateScope id mismatch")
    if request.candidate_scope_hash != candidate_scope_hash_v1(candidate_scope):
        raise ResearchContractError("research CandidateScope hash mismatch")
    if request.investigation_set_id != investigation_set.investigation_set_id:
        raise ResearchContractError("research InvestigationSet id mismatch")
    expected_handoff_bindings = {
        "parentContextBindingHash": (
            request.parent_context_binding_hash,
            parent_context_binding_hash,
        ),
        "childContextBindingHash": (
            request.child_context_binding_hash,
            child_context_binding_hash,
        ),
        "capabilityGrantHash": (
            request.capability_grant_hash,
            capability_grant_hash,
        ),
    }
    for name, (actual, expected) in expected_handoff_bindings.items():
        if actual != expected:
            raise ResearchContractError(f"research handoff binding mismatch: {name}")
    if request.sensitivity != "SERVER_ONLY":
        raise ResearchContractError("research request must remain server-only")
    if request.allowed_tools != ("get_product_details",):
        raise ResearchContractError("research tool is not allowed")
    if request.max_tool_calls < 1:
        raise ResearchContractError("research tool budget is exhausted")
    if request.investigation_set_binding_hash != investigation_set.binding_hash:
        raise ResearchContractError("research InvestigationSet binding mismatch")
    if request.candidate_ids != investigation_set.candidate_ids:
        raise ResearchContractError("research candidate set changed")
    if request.evidence_gap_keys != investigation_set.evidence_gap_keys:
        raise ResearchContractError("research evidence gaps changed")
    if request.payload_hash != _sha256(_research_request_payload(raw)):
        raise ResearchContractError("research payload hash mismatch")
    if request.binding_hash != _sha256(_research_request_binding(raw)):
        raise ResearchContractError("research binding hash mismatch")
    if request.deadline_at <= (now or datetime.now(timezone.utc)):
        raise ResearchContractError("research request is expired")


def _collect_evidence_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidenceRef" and isinstance(item, str) and item:
                refs.add(item)
            elif key == "evidenceRefs" and isinstance(item, list):
                refs.update(ref for ref in item if isinstance(ref, str) and ref)
            else:
                refs.update(_collect_evidence_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_collect_evidence_refs(item))
    return refs


def _report_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "findings": raw["findings"],
        "rejectedFindings": raw["rejectedFindings"],
        "unresolved": raw["unresolved"],
        "stopReason": raw["stopReason"],
        "generatedAt": raw["generatedAt"],
    }


def _report_binding(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: raw[key]
        for key in (
            "reportId",
            "requestId",
            "handoffId",
            "parentRunId",
            "childRunId",
            "taskId",
            "taskRevision",
            "candidateScopeId",
            "candidateScopeSourceRevision",
            "candidateScopeHash",
            "investigationSetId",
            "investigationSetBindingHash",
            "reportHash",
        )
    }


def build_research_report_v1(
    *,
    request: ResearchRequestV1,
    decision: ResearchDecisionOutputV1,
    tool_trace: ToolTrace | None,
    generated_at: datetime | None = None,
) -> ResearchReportV1:
    allowed_candidates = set(request.candidate_ids)
    allowed_gaps = set(request.evidence_gap_keys)
    tool_refs = _collect_evidence_refs(tool_trace.detail) if tool_trace is not None else set()
    seen: set[tuple[int, str]] = set()
    for finding in decision.findings:
        identity = (finding.candidate_id, finding.evidence_gap_key)
        if finding.candidate_id not in allowed_candidates:
            raise ResearchReportRejected("finding candidate is outside InvestigationSet")
        if finding.evidence_gap_key not in allowed_gaps:
            raise ResearchReportRejected("finding gap is outside ResearchRequest")
        if identity in seen:
            raise ResearchReportRejected("duplicate research finding")
        seen.add(identity)
        if not set(finding.evidence_refs).issubset(tool_refs):
            raise ResearchReportRejected("finding cites evidence absent from tool receipt")
    for item in decision.unresolved:
        if item.candidate_id not in allowed_candidates or item.evidence_gap_key not in allowed_gaps:
            raise ResearchReportRejected("unresolved gap is outside ResearchRequest")
        identity = (item.candidate_id, item.evidence_gap_key)
        if identity in seen:
            raise ResearchReportRejected("duplicate or conflicting research outcome")
        seen.add(identity)
    expected = {
        (candidate_id, gap)
        for candidate_id in request.candidate_ids
        for gap in request.evidence_gap_keys
    }
    if seen != expected:
        raise ResearchReportRejected(
            "research report must resolve or explicitly retain every requested gap"
        )
    generated = generated_at or datetime.now(timezone.utc)
    generated_text = generated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    base = {
        "requestId": request.request_id,
        "handoffId": request.handoff_id,
        "parentRunId": request.parent_run_id,
        "childRunId": request.child_run_id,
        "taskId": request.task_id,
        "taskRevision": request.task_revision,
        "candidateScopeId": request.candidate_scope_id,
        "candidateScopeSourceRevision": request.candidate_scope_source_revision,
        "candidateScopeHash": request.candidate_scope_hash,
        "investigationSetId": request.investigation_set_id,
        "investigationSetBindingHash": request.investigation_set_binding_hash,
        "findings": [item.model_dump(by_alias=True, mode="json") for item in decision.findings],
        "rejectedFindings": [],
        "unresolved": [item.model_dump(by_alias=True, mode="json") for item in decision.unresolved],
        "stopReason": decision.stop_reason,
        "generatedAt": generated_text,
    }
    report_hash = _sha256(_report_content(base))
    report_id = "rpt-" + _sha256({**base, "reportHash": report_hash})[:24]
    raw = {"schemaVersion": "research-report-v1", "reportId": report_id, **base}
    raw["reportHash"] = report_hash
    raw["bindingHash"] = _sha256(_report_binding(raw))
    return ResearchReportV1.model_validate(raw)


def validate_research_report_v1(
    report: ResearchReportV1,
    *,
    request: ResearchRequestV1,
    investigation_set: InvestigationSetV1,
    candidate_scope: CandidateScope,
    task_id: str,
    task_revision: int,
    parent_context_binding_hash: str,
    child_context_binding_hash: str,
    capability_grant_hash: str,
    tool_trace: ToolTrace | None,
    now: datetime | None = None,
) -> tuple[ResearchFindingV1, ...]:
    current = now or datetime.now(timezone.utc)
    if current > request.deadline_at or report.generated_at > request.deadline_at:
        raise ResearchReportRejected("research report is late")
    validate_research_request_v1(
        request,
        investigation_set=investigation_set,
        candidate_scope=candidate_scope,
        task_id=task_id,
        task_revision=task_revision,
        parent_context_binding_hash=parent_context_binding_hash,
        child_context_binding_hash=child_context_binding_hash,
        capability_grant_hash=capability_grant_hash,
        now=now,
    )
    raw = report.model_dump(by_alias=True, mode="json")
    for field in (
        "request_id",
        "handoff_id",
        "parent_run_id",
        "child_run_id",
        "task_id",
        "task_revision",
        "candidate_scope_id",
        "candidate_scope_source_revision",
        "candidate_scope_hash",
        "investigation_set_id",
        "investigation_set_binding_hash",
    ):
        if getattr(report, field) != getattr(request, field):
            raise ResearchReportRejected(f"research report binding mismatch: {field}")
    if report.report_hash != _sha256(_report_content(raw)):
        raise ResearchReportRejected("research report hash mismatch")
    if report.binding_hash != _sha256(_report_binding(raw)):
        raise ResearchReportRejected("research report binding hash mismatch")
    # Reuse the exact child-side evidence checks; parent validation is not a
    # trust shortcut and never accepts the report merely because it parses.
    rebuilt = build_research_report_v1(
        request=request,
        decision=ResearchDecisionOutputV1(
            findings=report.findings,
            unresolved=report.unresolved,
            stopReason=report.stop_reason,
        ),
        tool_trace=tool_trace,
        generated_at=report.generated_at,
    )
    if rebuilt.report_hash != report.report_hash:
        raise ResearchReportRejected("research report content is not canonical")
    return report.findings


def _research_merge_claim_payload(report: ResearchReportV1) -> dict[str, Any]:
    return {
        "parentRunId": report.parent_run_id,
        "handoffId": report.handoff_id,
        "requestId": report.request_id,
        "reportId": report.report_id,
        "taskId": report.task_id,
        "taskRevision": report.task_revision,
        "candidateScopeId": report.candidate_scope_id,
        "candidateScopeHash": report.candidate_scope_hash,
        "reportHash": report.report_hash,
        "reportBindingHash": report.binding_hash,
    }


def _build_merge_receipt(
    report: ResearchReportV1,
    *,
    outcome: Literal["ACCEPTED", "REPLAY_NOOP"],
    created_at: datetime | None = None,
) -> ResearchMergeReceiptV1:
    raw = {
        "schemaVersion": "research-merge-receipt-v1",
        "mergeReceiptId": "rmrg-"
        + _sha256(
            {
                "requestId": report.request_id,
                "reportBindingHash": report.binding_hash,
                "outcome": outcome,
            }
        )[:24],
        **_research_merge_claim_payload(report),
        "outcome": outcome,
        "acceptedFindingCount": len(report.findings) if outcome == "ACCEPTED" else 0,
        "unresolvedCount": len(report.unresolved),
        "persistenceStatus": "PRIMARY",
        "createdAt": (created_at or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    raw["receiptHash"] = _sha256(raw)
    return ResearchMergeReceiptV1.model_validate(raw)


class ResearchMergeGuardV1:
    """Atomically claim one parent merge per ResearchRequest.

    A same-report replay becomes an explicit no-op. A different report for the
    same request is a conflict. Storage failure fails closed because a local or
    append-only fallback cannot preserve cross-process exactly-once behavior.
    """

    def __init__(
        self,
        *,
        client: redis.Redis | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        self._ttl_seconds = max(
            int(ttl_seconds or settings.research_merge_claim_ttl_seconds), 60
        )

    def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(settings.redis_url, decode_responses=True)
        return self._client

    async def claim(
        self, report: ResearchReportV1
    ) -> Literal["ACCEPTED", "REPLAY_NOOP"]:
        key = ":".join(
            (
                _RESEARCH_MERGE_KEY_PREFIX,
                report.parent_run_id,
                report.handoff_id,
                report.request_id,
            )
        )
        claim = _canonical_json(_research_merge_claim_payload(report))
        try:
            result = await self._get_client().eval(
                _RESEARCH_MERGE_CLAIM_LUA,
                1,
                key,
                claim,
                str(self._ttl_seconds),
            )
        except Exception as exc:
            raise ResearchMergePersistenceError(
                "research merge claim is not durable"
            ) from exc
        if not isinstance(result, (list, tuple)) or not result:
            raise ResearchMergePersistenceError("research merge claim returned invalid data")
        code = int(result[0])
        if code == 1:
            return "ACCEPTED"
        if code == 0:
            return "REPLAY_NOOP"
        if code == -1:
            raise ResearchMergeConflict(
                "a different report already claimed this ResearchRequest"
            )
        raise ResearchMergePersistenceError("research merge claim returned unknown code")


async def merge_research_report_v1(
    report: ResearchReportV1,
    *,
    request: ResearchRequestV1,
    investigation_set: InvestigationSetV1,
    candidate_scope: CandidateScope,
    task_id: str,
    task_revision: int,
    parent_context_binding_hash: str,
    child_context_binding_hash: str,
    capability_grant_hash: str,
    tool_trace: ToolTrace,
    guard: ResearchMergeGuardV1,
    now: datetime | None = None,
) -> tuple[ResearchMergeReceiptV1, tuple[ResearchFindingV1, ...]]:
    findings = validate_research_report_v1(
        report,
        request=request,
        investigation_set=investigation_set,
        candidate_scope=candidate_scope,
        task_id=task_id,
        task_revision=task_revision,
        parent_context_binding_hash=parent_context_binding_hash,
        child_context_binding_hash=child_context_binding_hash,
        capability_grant_hash=capability_grant_hash,
        tool_trace=tool_trace,
        now=now,
    )
    outcome = await guard.claim(report)
    receipt = _build_merge_receipt(report, outcome=outcome, created_at=now)
    return receipt, findings if outcome == "ACCEPTED" else ()


ResearchDecision = Callable[
    [dict[str, Any]], Awaitable[ResearchDecisionOutputV1 | dict[str, Any]]
]
ResearchToolCaller = Callable[[str, dict[str, Any]], Awaitable[ToolTrace]]
ModelCallObserver = Callable[..., None]


async def run_evidence_research_agent_v1(
    request: ResearchRequestV1,
    *,
    investigation_set: InvestigationSetV1,
    candidate_scope: CandidateScope,
    task_id: str,
    task_revision: int,
    parent_context_binding_hash: str,
    child_context_binding_hash: str,
    capability_grant_hash: str,
    tool_caller: ResearchToolCaller,
    decide: ResearchDecision,
    on_model_call: ModelCallObserver | None = None,
) -> tuple[ResearchReportV1, ToolTrace]:
    """Run one read-only child context with one bounded evidence fetch."""

    validate_research_request_v1(
        request,
        investigation_set=investigation_set,
        candidate_scope=candidate_scope,
        task_id=task_id,
        task_revision=task_revision,
        parent_context_binding_hash=parent_context_binding_hash,
        child_context_binding_hash=child_context_binding_hash,
        capability_grant_hash=capability_grant_hash,
    )

    remaining = (request.deadline_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise ResearchContractError("research request is expired")
    if request.max_tool_calls < 1:
        raise ResearchContractError("research tool budget is exhausted")
    try:
        tool_trace = await asyncio.wait_for(
            tool_caller(
                "get_product_details",
                {"productIds": list(request.candidate_ids)},
            ),
            timeout=max(remaining, 0.01),
        )
    except asyncio.TimeoutError as exc:
        raise ResearchContractError("research detail tool timed out") from exc

    if not tool_trace.ok:
        decision = ResearchDecisionOutputV1(
            findings=(),
            unresolved=tuple(
                UnresolvedResearchGapV1(
                    candidateId=candidate_id,
                    evidenceGapKey=gap,
                    reason="read-only evidence tool failed",
                )
                for candidate_id in request.candidate_ids
                for gap in request.evidence_gap_keys
            ),
            stopReason="TOOL_FAILURE",
        )
        return (
            build_research_report_v1(
                request=request,
                decision=decision,
                tool_trace=tool_trace,
            ),
            tool_trace,
        )

    if request.max_model_decisions < 1:
        decision_output = ResearchDecisionOutputV1(
            findings=(),
            unresolved=tuple(
                UnresolvedResearchGapV1(
                    candidateId=candidate_id,
                    evidenceGapKey=gap,
                    reason="research model budget is zero",
                )
                for candidate_id in request.candidate_ids
                for gap in request.evidence_gap_keys
            ),
            stopReason="BUDGET_EXHAUSTED",
        )
    else:
        child_view = {
            "schemaVersion": "research-decision-view-v1",
            "requestId": request.request_id,
            "candidateIds": list(request.candidate_ids),
            "evidenceGapKeys": list(request.evidence_gap_keys),
            "researchGoal": request.research_goal,
            "verifiedToolObservation": tool_trace.detail,
        }
        started = time.perf_counter()
        failed = True
        try:
            raw_decision = await asyncio.wait_for(
                decide(child_view),
                timeout=max(
                    (request.deadline_at - datetime.now(timezone.utc)).total_seconds(),
                    0.01,
                ),
            )
            decision_output = (
                raw_decision
                if isinstance(raw_decision, ResearchDecisionOutputV1)
                else ResearchDecisionOutputV1.model_validate(raw_decision)
            )
            if decision_output.stop_reason not in {
                "COMPLETE",
                "BUDGET_EXHAUSTED",
                "CONFLICT",
            }:
                raise ResearchContractError(
                    "research model cannot assert runtime-owned stop reason"
                )
            failed = False
        finally:
            if on_model_call is not None:
                on_model_call(
                    "research_decision",
                    (time.perf_counter() - started) * 1000.0,
                    failed=failed,
                    parent_run_id=request.parent_run_id,
                    handoff_id=request.handoff_id,
                )
    return (
        build_research_report_v1(
            request=request,
            decision=decision_output,
            tool_trace=tool_trace,
        ),
        tool_trace,
    )


__all__ = [
    "InvestigationSelectionResult",
    "InvestigationSetError",
    "InvestigationSetV1",
    "MAX_INVESTIGATION_CANDIDATES",
    "NoInvestigationNeeded",
    "SELECTION_POLICY_VERSION",
    "Verdict",
    "ResearchContractError",
    "ResearchDecisionOutputV1",
    "ResearchFindingV1",
    "ResearchReportRejected",
    "ResearchMergeConflict",
    "ResearchMergeGuardV1",
    "ResearchMergePersistenceError",
    "ResearchMergeReceiptV1",
    "ResearchReportV1",
    "ResearchRequestV1",
    "RouteDecisionV1",
    "RouteSignalsV1",
    "UnresolvedResearchGapV1",
    "build_investigation_set_v1",
    "build_research_report_v1",
    "build_research_request_v1",
    "candidate_scope_hash_v1",
    "canonical_gap_key",
    "investigation_binding_hash",
    "merge_research_report_v1",
    "route_context_multiagent_v1",
    "run_evidence_research_agent_v1",
    "validate_investigation_set_v1",
    "validate_research_report_v1",
    "validate_research_request_v1",
    "validate_route_decision_v1",
]
