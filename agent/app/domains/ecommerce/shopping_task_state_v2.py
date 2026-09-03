"""Storage-free, fail-closed Shopping Task State V2 contracts.

All collections are tuples so a validated snapshot cannot be mutated through
an alias.  The lifecycle is explicit: a current requirement is always active,
while revoke/suppress records are tombstones in the delta history.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    CandidateScope,
    ScopeRerankRequest,
    ShoppingGuideState,
    compiled_shopping_requirements,
)


RequirementOperator = Literal["eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in"]
RequirementPriority = Literal["hard", "soft"]
RequirementPolarity = Literal["include", "exclude"]
RequirementStatus = Literal["active", "revoked", "suppressed", "superseded", "expired"]
MemoryScope = Literal["task_only", "profile"]
DeltaOperation = Literal["add", "retain", "override", "revoke", "suppress"]
_Scalar = str | int | float | bool


class _ContractModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)


def canonical_requirement_key(key: object) -> str:
    """Canonical logical-key contract shared by state and memory overlays."""

    if type(key) is not str:
        raise ValueError("requirement key must be an exact string")
    canonical = key.casefold().strip()
    if not canonical:
        raise ValueError("requirement key cannot be blank")
    return canonical


def _semantic_key(requirement: "ShoppingRequirementV2") -> tuple[str, str]:
    """Identify one concurrently active requirement lane.

    Include and exclude constraints for the same field are independent lanes:
    ``brand in domestic`` and ``brand not_in apple`` must coexist.
    """

    return canonical_requirement_key(requirement.key), requirement.polarity


class ShoppingRequirementV2(_ContractModel):
    """An auditable requirement with its state and provenance explicit."""

    requirement_id: str = Field(alias="requirementId", min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=128)
    operator: RequirementOperator
    value: _Scalar | tuple[_Scalar, ...]
    priority: RequirementPriority
    polarity: RequirementPolarity
    source: Literal["user", "server", "inferred"]
    source_provenance: str | None = Field(
        default=None,
        alias="sourceProvenance",
        min_length=1,
        max_length=500,
    )
    confidence: float = Field(ge=0.0, le=1.0)
    status: RequirementStatus = "active"
    memory_scope: MemoryScope = Field(alias="memoryScope")
    introduced_turn: int = Field(alias="introducedTurn", ge=1)
    supersedes: str | None = Field(default=None, max_length=128)

    @field_validator("value", mode="before")
    @classmethod
    def _freeze_value(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_requirement(self) -> "ShoppingRequirementV2":
        if self.source == "inferred" and self.priority == "hard":
            raise ValueError("inferred requirements cannot be hard")
        if (
            self.source_provenance is not None
            and self.source_provenance.startswith("inferred:")
            and self.source != "inferred"
        ):
            raise ValueError("inferred source provenance requires inferred source class")
        if (
            self.source == "inferred"
            and self.source_provenance is not None
            and not self.source_provenance.startswith("inferred:")
        ):
            raise ValueError("inferred source provenance must retain the inferred: prefix")
        if self.status == "superseded" and not self.supersedes:
            raise ValueError("superseded requirements must name their replacement")
        if self.supersedes == self.requirement_id:
            raise ValueError("a requirement cannot supersede itself")
        canonical_requirement_key(self.key)
        return self


class RequirementStateDelta(_ContractModel):
    """One explicit lifecycle transition; implicit replacement is forbidden."""

    op: DeltaOperation
    requirement: ShoppingRequirementV2

    @model_validator(mode="after")
    def _validate_operation_status(self) -> "RequirementStateDelta":
        expected = {
            "add": "active",
            "retain": "active",
            "override": "active",
            "revoke": "revoked",
            "suppress": "suppressed",
        }[self.op]
        if self.requirement.status != expected:
            raise ValueError(f"{self.op} delta requires requirement status {expected}")
        if self.op == "add" and self.requirement.supersedes:
            raise ValueError("add cannot claim a supersedes predecessor")
        if self.op == "override":
            predecessor = self.requirement.supersedes
            if not predecessor:
                raise ValueError("override delta requires supersedes")
            if predecessor == self.requirement.requirement_id:
                raise ValueError("override cannot self-supersede")
        return self


class UnknownItem(_ContractModel):
    key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)
    blocking: bool = True


class InformationSufficiency(_ContractModel):
    status: Literal["insufficient", "partial", "sufficient"]
    missing_keys: tuple[str, ...] = Field(default=(), alias="missingKeys")

    @field_validator("missing_keys", mode="before")
    @classmethod
    def _freeze_missing(cls, value: object) -> object:
        return tuple(value or ()) if isinstance(value, (list, tuple)) else value

    @model_validator(mode="after")
    def _validate_sufficiency(self) -> "InformationSufficiency":
        if self.status == "sufficient" and self.missing_keys:
            raise ValueError("sufficient information cannot have missing keys")
        if self.status == "insufficient" and not self.missing_keys:
            raise ValueError("insufficient information must name missing keys")
        if len(self.missing_keys) != len(set(self.missing_keys)):
            raise ValueError("missingKeys must be unique")
        return self


class CandidateScopeV2(_ContractModel):
    scope_id: str = Field(alias="scopeId", min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    candidate_ids: tuple[int, ...] = Field(default=(), alias="candidateIds")
    source_turn: int = Field(alias="sourceTurn", ge=1)

    @field_validator("candidate_ids", mode="before")
    @classmethod
    def _freeze_ids(cls, value: object) -> object:
        return tuple(value or ()) if isinstance(value, (list, tuple)) else value

    @model_validator(mode="after")
    def _validate_candidate_ids(self) -> "CandidateScopeV2":
        if any(type(candidate) is not int or candidate <= 0 for candidate in self.candidate_ids):
            raise ValueError("candidateIds must contain positive integer IDs")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidateIds must be unique")
        return self


class ScopeRerankRequestV2(_ContractModel):
    scope_id: str = Field(alias="scopeId", min_length=1, max_length=128)
    ranking_intent: Literal["camera_title_claim", "gaming_title_claim"] = Field(
        alias="rankingIntent"
    )


class CurrentAction(_ContractModel):
    kind: Literal["clarify", "search", "compare", "recommend", "answer", "wait"]
    reason: str = Field(min_length=1, max_length=512)


def _validate_history_replay(
    baseline: tuple[ShoppingRequirementV2, ...],
    replay: ShoppingRequirementV2,
) -> None:
    if not baseline:
        raise ValueError("historical baseline is required for replay validation")
    matches = [item for item in baseline if item.requirement_id == replay.requirement_id]
    if not matches:
        raise ValueError("replay requirementId is absent from historical baseline")
    if replay.status != "active" or any(item.status != "active" for item in matches):
        raise ValueError("replay current requirement must be active")
    if matches[0] != replay:
        raise ValueError("replay requires identical full requirement content")


class ShoppingTaskStateV2(_ContractModel):
    """Historical V2 experiment/shadow contract.

    This schema intentionally remains parseable for frozen experiment assets.
    It is not complete enough to drive production ContextPack/Planner reads;
    production authority requires ``ShoppingTaskStateV2Authoritative`` below.
    """

    schema_version: Literal["shopping-task-state-v2"] = Field(
        default="shopping-task-state-v2", alias="schemaVersion"
    )
    goal: str = Field(min_length=1, max_length=1024)
    use_case: str = Field(alias="useCase", min_length=1, max_length=256)
    stage: Literal["intake", "clarifying", "searching", "recommending", "completed"]
    requirements: tuple[ShoppingRequirementV2, ...] = Field(default=())
    deltas: tuple[RequirementStateDelta, ...] = Field(default=())
    unknowns: tuple[UnknownItem, ...] = Field(default=())
    history_baseline: tuple[ShoppingRequirementV2, ...] = Field(
        default=(), alias="historyBaseline"
    )
    information_sufficiency: InformationSufficiency = Field(alias="informationSufficiency")
    candidate_scope: CandidateScopeV2 | None = Field(default=None, alias="candidateScope")
    scope_rerank_request: ScopeRerankRequestV2 | None = Field(
        default=None, alias="scopeRerankRequest"
    )
    current_action: CurrentAction = Field(alias="currentAction")

    @field_validator("requirements", "deltas", "unknowns", "history_baseline", mode="before")
    @classmethod
    def _freeze_collections(cls, value: object) -> object:
        return tuple(value or ()) if isinstance(value, (list, tuple)) else value

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> "ShoppingTaskStateV2":
        baseline_by_id: dict[str, ShoppingRequirementV2] = {}
        baseline_keys: set[tuple[str, str]] = set()
        for item in self.history_baseline:
            if item.status != "active":
                raise ValueError("historical baseline must contain only active requirements")
            if item.requirement_id in baseline_by_id or _semantic_key(item) in baseline_keys:
                raise ValueError("historical baseline has duplicate active requirement")
            baseline_by_id[item.requirement_id] = item
            baseline_keys.add(_semantic_key(item))

        active = dict(baseline_by_id)
        known: set[str] = set(active)
        seen_delta_ids: set[str] = set()
        for delta in self.deltas:
            requirement = delta.requirement
            requirement_id = requirement.requirement_id
            if delta.op in {"add", "override"} and requirement_id in seen_delta_ids:
                raise ValueError("input contains duplicate requirementId delta")
            if delta.op in {"add", "override"}:
                seen_delta_ids.add(requirement_id)
            if delta.op == "add":
                if requirement_id in known or _semantic_key(requirement) in {
                    _semantic_key(item) for item in active.values()
                }:
                    raise ValueError("add cannot reuse an ID or fork an active requirement lane")
                known.add(requirement_id)
                active[requirement_id] = requirement
            elif delta.op == "retain":
                current = active.get(requirement_id)
                if current is None:
                    raise ValueError("retain requires an active requirement")
                if requirement != current:
                    raise ValueError("retain/replay requires identical full content")
            elif delta.op == "override":
                predecessor = requirement.supersedes
                current = active.get(predecessor or "")
                if current is None or requirement_id in known:
                    raise ValueError("override requires one active predecessor and a new ID")
                if _semantic_key(requirement) != _semantic_key(current):
                    raise ValueError("override must preserve the logical requirement lane")
                active.pop(predecessor)  # type: ignore[arg-type]
                known.add(requirement_id)
                active[requirement_id] = requirement
            else:
                current = active.get(requirement_id)
                if current is None:
                    raise ValueError(f"{delta.op} requires an active requirement")
                if requirement.model_copy(update={"status": "active"}) != current:
                    raise ValueError(f"{delta.op} must bind the active predecessor exactly")
                active.pop(requirement_id)

        current_by_id = {item.requirement_id: item for item in self.requirements}
        if len(current_by_id) != len(self.requirements):
            raise ValueError("requirements must have unique requirementIds")
        current_keys = [_semantic_key(item) for item in self.requirements]
        if len(current_keys) != len(set(current_keys)):
            raise ValueError("requirements must have unique active requirement lanes")
        if any(item.status != "active" for item in self.requirements):
            raise ValueError("current requirements must be active")
        # Even a snapshot without a new delta is a replay boundary: terminal
        # requirements must be the exact fully-typed baseline/replay result.
        if set(current_by_id) != set(active) or any(current_by_id[key] != value for key, value in active.items()):
            raise ValueError("requirements must equal terminal active lifecycle state")
        if self.stage == "completed" and self.information_sufficiency.status == "insufficient":
            raise ValueError("a completed task cannot have insufficient information")
        return self

    def validate_replay(
        self,
        replay: ShoppingRequirementV2,
        *,
        historical_baseline: tuple[ShoppingRequirementV2, ...] | None = None,
    ) -> None:
        baseline = historical_baseline if historical_baseline is not None else self.history_baseline
        _validate_history_replay(tuple(baseline), replay)


def _requirement_projection(requirement: object) -> tuple[object, ...]:
    operator = getattr(requirement, "operator")
    value = getattr(requirement, "value")
    if isinstance(value, list):
        value = tuple(value)
    return (
        getattr(requirement, "key").casefold().strip(),
        operator,
        value,
        getattr(requirement, "priority"),
        "exclude" if operator in {"neq", "not_in"} else "include",
        getattr(requirement, "source_provenance", None) or getattr(requirement, "source"),
    )


class ShoppingTaskStateV2Authoritative(ShoppingTaskStateV2):
    """Complete production read contract introduced after the V2 experiments.

    ``shopping-task-state-v2.1`` is a deliberate version boundary.  It embeds
    every ecommerce object consumed by ContextPack/Planner so normal V2 reads
    never consult compatibility ``shoppingGuide``/``candidateScope`` fields.
    Historical ``shopping-task-state-v2`` snapshots must be upgraded during a
    server-owned atomic write; they are never silently treated as complete.
    """

    schema_version: Literal["shopping-task-state-v2.1"] = Field(
        default="shopping-task-state-v2.1", alias="schemaVersion"
    )
    shopping_guide: ShoppingGuideState = Field(alias="shoppingGuide")
    pending_questions: tuple[str, ...] = Field(
        default=(), alias="pendingQuestions", max_length=20
    )
    candidate_scope: CandidateScope | None = Field(default=None, alias="candidateScope")
    scope_rerank_request: ScopeRerankRequest | None = Field(
        default=None, alias="scopeRerankRequest"
    )

    @field_validator("pending_questions", mode="before")
    @classmethod
    def _freeze_pending_questions(cls, value: object) -> object:
        return tuple(value or ()) if isinstance(value, (list, tuple)) else value

    @field_validator("pending_questions")
    @classmethod
    def _validate_pending_questions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("pendingQuestions must contain non-blank strings")
        if len(value) != len(set(value)):
            raise ValueError("pendingQuestions must be unique")
        return value

    @model_validator(mode="after")
    def _validate_complete_projection(self) -> "ShoppingTaskStateV2Authoritative":
        guide = self.shopping_guide
        if guide.category is None:
            if (
                self.stage != "clarifying"
                or self.current_action.kind != "clarify"
                or guide.mode != "recommend"
                or guide.requirements
                or guide.brand_avoidances
                or guide.candidate_ids
                or guide.compared_ids
                or guide.evidence_status != "missing"
                or self.requirements
                or self.candidate_scope is not None
                or self.scope_rerank_request is not None
            ):
                raise ValueError(
                    "category=None is allowed only for an empty blocked clarification"
                )
            return self
        expected_use_case = "; ".join(guide.use_cases) or "shopping"
        if self.use_case != expected_use_case[:256]:
            raise ValueError("useCase must equal the authoritative shoppingGuide use cases")
        guide_requirements = sorted(
            (_requirement_projection(item) for item in compiled_shopping_requirements(guide)),
            key=lambda item: (item[0], item[4]),
        )
        snapshot_requirements = sorted(
            (_requirement_projection(item) for item in self.requirements),
            key=lambda item: (item[0], item[4]),
        )
        if guide_requirements != snapshot_requirements:
            raise ValueError("requirements must equal the authoritative shoppingGuide requirements")

        scope = self.candidate_scope
        if scope is None:
            if guide.candidate_ids:
                raise ValueError("candidateIds require an authoritative CandidateScope")
            if self.scope_rerank_request is not None:
                raise ValueError("scopeRerankRequest requires an authoritative CandidateScope")
            return self

        if scope.category != guide.category:
            raise ValueError("authoritative CandidateScope identity is invalid")
        if scope.status == "invalidated":
            if guide.candidate_ids or self.scope_rerank_request is not None:
                raise ValueError("invalidated CandidateScope cannot publish candidates or rerank")
            return self
        if list(scope.requirements_snapshot) != list(compiled_shopping_requirements(guide)):
            raise ValueError("CandidateScope requirements do not match shoppingGuide")
        if list(scope.brand_avoidances_snapshot) != list(guide.brand_avoidances):
            raise ValueError("CandidateScope brand avoidances do not match shoppingGuide")
        expected_candidates = list(scope.visible_product_ids or scope.ranked_item_ids)
        if guide.candidate_ids != expected_candidates:
            raise ValueError("shoppingGuide.candidateIds must equal CandidateScope visible IDs")
        if guide.mode == "compare" and not set(guide.compared_ids).issubset(scope.ranked_item_ids):
            raise ValueError("shoppingGuide.comparedIds are outside CandidateScope")
        if (
            self.scope_rerank_request is not None
            and self.scope_rerank_request.scope_id != scope.scope_id
        ):
            raise ValueError("scopeRerankRequest must bind the authoritative CandidateScope")
        return self


def parse_shopping_task_state_v2_snapshot(
    raw: object,
) -> ShoppingTaskStateV2 | ShoppingTaskStateV2Authoritative:
    """Parse historical V2 or the complete V2.1 contract without upgrading."""

    if isinstance(raw, dict) and raw.get("schemaVersion") == "shopping-task-state-v2.1":
        return ShoppingTaskStateV2Authoritative.model_validate(raw)
    return ShoppingTaskStateV2.model_validate(raw)


def validate_requirement_replay(
    replay: ShoppingRequirementV2,
    *,
    historical_baseline: tuple[ShoppingRequirementV2, ...] | None,
) -> None:
    _validate_history_replay(tuple(historical_baseline or ()), replay)


def replay_requirement(
    current: ShoppingRequirementV2,
    replay: ShoppingRequirementV2,
    *,
    historical_baseline: tuple[ShoppingRequirementV2, ...] | None,
) -> ShoppingRequirementV2:
    validate_requirement_replay(replay, historical_baseline=historical_baseline)
    if current.requirement_id != replay.requirement_id or current != replay:
        raise ValueError("replay requires identical ID and full content")
    if current.status != "active":
        raise ValueError("replay current requirement must be active")
    return current


def apply_requirement_delta(
    requirements: tuple[ShoppingRequirementV2, ...] | list[ShoppingRequirementV2],
    delta: RequirementStateDelta,
) -> tuple[ShoppingRequirementV2, ...]:
    """Apply one delta in memory; no persistence or TaskState I/O occurs."""

    current = tuple(requirements)
    active = {item.requirement_id: item for item in current}
    if len(active) != len(current) or any(item.status != "active" for item in current):
        raise ValueError("input requirements must be unique active values")
    if len({_semantic_key(item) for item in current}) != len(current):
        raise ValueError("input requirements have a forked active requirement lane")
    item = delta.requirement
    if delta.op == "add":
        if item.requirement_id in active or _semantic_key(item) in {_semantic_key(x) for x in current}:
            raise ValueError("add cannot reuse ID or fork requirement lane")
        active[item.requirement_id] = item
    elif delta.op == "retain":
        if active.get(item.requirement_id) != item:
            raise ValueError("retain/replay requires identical full content")
    elif delta.op == "override":
        predecessor = item.supersedes
        if predecessor not in active or item.requirement_id in active:
            raise ValueError("override requires one active predecessor and a new ID")
        if _semantic_key(item) != _semantic_key(active[predecessor]):
            raise ValueError("override must preserve the logical requirement lane")
        active.pop(predecessor)
        active[item.requirement_id] = item
    else:
        predecessor = active.get(item.requirement_id)
        if predecessor is None or item.model_copy(update={"status": "active"}) != predecessor:
            raise ValueError(f"{delta.op} must bind the active predecessor exactly")
        active.pop(item.requirement_id)
    return tuple(active.values())


__all__ = [
    "CandidateScopeV2",
    "CurrentAction",
    "InformationSufficiency",
    "RequirementStateDelta",
    "ScopeRerankRequestV2",
    "ShoppingRequirementV2",
    "ShoppingTaskStateV2",
    "ShoppingTaskStateV2Authoritative",
    "UnknownItem",
    "apply_requirement_delta",
    "parse_shopping_task_state_v2_snapshot",
    "replay_requirement",
    "validate_requirement_replay",
]
