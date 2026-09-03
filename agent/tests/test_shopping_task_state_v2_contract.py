from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domains.ecommerce.shopping_task_state_v2 import (
    CandidateScopeV2,
    CurrentAction,
    InformationSufficiency,
    RequirementStateDelta,
    ShoppingRequirementV2,
    ShoppingTaskStateV2,
    replay_requirement,
)


def _requirement(
    requirement_id: str,
    *,
    key: str = "price",
    status: str = "active",
    supersedes: str | None = None,
    value: int = 2000,
):
    return ShoppingRequirementV2(
        requirementId=requirement_id, key=key, operator="lte", value=value,
        priority="hard", polarity="include", source="user", confidence=1.0,
        status=status, memoryScope="task_only", introducedTurn=1, supersedes=supersedes,
    )


def _state(*, requirements, deltas=(), baseline=()):
    if not deltas and not baseline:
        baseline = requirements
    return ShoppingTaskStateV2(
        goal="find a phone", useCase="travel", stage="searching", requirements=requirements,
        deltas=deltas, historyBaseline=baseline, unknowns=[],
        informationSufficiency=InformationSufficiency(status="sufficient"),
        candidateScope=CandidateScopeV2(scopeId="scope-1", category="phone", candidateIds=[1, 2], sourceTurn=1),
        currentAction=CurrentAction(kind="search", reason="constraints are known"),
    )


def test_contract_expresses_full_requirement_and_lifecycle():
    first = _requirement("budget")
    replacement = _requirement("budget-2", supersedes="budget", value=1800)
    state = _state(
        requirements=[replacement],
        deltas=[
            RequirementStateDelta(op="add", requirement=first),
            RequirementStateDelta(op="override", requirement=replacement),
        ],
    )
    dumped = state.model_dump(by_alias=True)
    assert dumped["requirements"][0]["memoryScope"] == "task_only"
    assert dumped["requirements"][0]["supersedes"] == "budget"
    assert state.requirements == (replacement,)
    assert isinstance(state.requirements, tuple)


def test_retain_replay_requires_active_id_and_full_content():
    first = _requirement("budget")
    state = _state(
        requirements=[first],
        deltas=[
            RequirementStateDelta(op="add", requirement=first),
            RequirementStateDelta(op="retain", requirement=first),
        ],
    )
    assert state.requirements == (first,)
    changed = first.model_copy(update={"value": 1800})
    with pytest.raises(ValidationError, match="identical full content"):
        _state(
            requirements=[first],
            deltas=[
                RequirementStateDelta(op="add", requirement=first),
                RequirementStateDelta(op="retain", requirement=changed),
            ],
        )
    with pytest.raises(ValueError, match="historical baseline"):
        replay_requirement(first, first, historical_baseline=None)
    assert replay_requirement(first, first, historical_baseline=(first,)) == first


@pytest.mark.parametrize("op,status", [("revoke", "revoked"), ("suppress", "suppressed")])
def test_terminal_operations_bind_active_predecessor_except_status(op, status):
    first = _requirement("budget")
    terminal = _requirement("budget", status=status)
    state = _state(
        requirements=[],
        deltas=[
            RequirementStateDelta(op="add", requirement=first),
            RequirementStateDelta(op=op, requirement=terminal),
        ],
    )
    assert state.requirements == ()
    tampered = terminal.model_copy(update={"value": 1999})
    with pytest.raises(ValidationError, match="bind the active predecessor"):
        _state(
            requirements=[],
            deltas=[
                RequirementStateDelta(op="add", requirement=first),
                RequirementStateDelta(op=op, requirement=tampered),
            ],
        )


def test_add_rejects_fake_supersedes_duplicate_active_and_non_active_current():
    with pytest.raises(ValidationError, match="add cannot"):
        RequirementStateDelta(op="add", requirement=_requirement("new", supersedes="old"))
    first = _requirement("one")
    duplicate_key = _requirement("two")
    with pytest.raises(ValidationError, match="duplicate active requirement"):
        _state(requirements=[first, duplicate_key])
    with pytest.raises(ValidationError, match="baseline must contain only active"):
        _state(requirements=[_requirement("gone", status="revoked")])


def test_collections_are_deep_immutable_and_candidate_ids_are_checked():
    state = _state(requirements=[_requirement("budget")])
    with pytest.raises((TypeError, ValidationError)):
        state.requirements += (_requirement("other", key="brand"),)
    with pytest.raises(ValidationError, match="positive integer"):
        CandidateScopeV2(scopeId="scope", category="phone", candidateIds=[0], sourceTurn=1)
    with pytest.raises(ValidationError, match="sufficient information"):
        InformationSufficiency(status="sufficient", missingKeys=["budget"])


def test_source_provenance_is_optional_but_consistent_with_source_class():
    legacy = _requirement("legacy")
    assert legacy.source_provenance is None

    inferred = ShoppingRequirementV2(
        requirementId="battery", key="battery_health", operator="gte", value=80,
        priority="soft", polarity="include", source="inferred",
        sourceProvenance="inferred: 续航好映射为较高电池健康度偏好",
        confidence=0.8, status="active", memoryScope="task_only", introducedTurn=1,
    )
    assert inferred.model_dump(by_alias=True)["sourceProvenance"].startswith("inferred:")

    with pytest.raises(ValidationError, match="requires inferred source class"):
        inferred.model_copy(update={"source": "server"}).model_validate(
            inferred.model_copy(update={"source": "server"}).model_dump(by_alias=True)
        )
