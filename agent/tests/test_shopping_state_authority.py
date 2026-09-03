import asyncio
from copy import deepcopy

import pytest

from app.context_pack import build_context_pack
from app.domains.ecommerce.models import ShoppingGuideState
from app.domains.ecommerce.shopping_state_authority import (
    AUTHORITY_BINDING_KEY,
    ShoppingStateAuthorityError,
    bind_authoritative_write,
    select_shopping_state_authority,
)
from app.domains.ecommerce.shopping_state_update import build_shopping_state_transition_patch
from app.planner import build_planner_context
from tests.test_shopping_state_update import _state


def _bound_domain():
    state = _state()
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])
    domain = dict(state.domain_state)
    domain.update(build_shopping_state_transition_patch(
        state, guide, {"status": "ready"}, constraints_changed=False
    ))
    domain = bind_authoritative_write(
        domain,
        task_id=state.task_id,
        task_revision=state.revision + 1,
        goal=state.goal,
        unknowns=state.unknowns,
        pending_questions=state.pending_questions,
    )
    assert domain["shoppingTaskStateV2"]["schemaVersion"] == "shopping-task-state-v2.1"
    assert domain[AUTHORITY_BINDING_KEY]["schemaVersion"] == "shopping-state-authority-binding-v2"
    return state, domain


def _select(state, domain, *, revision=None, mode="v2"):
    return select_shopping_state_authority(
        domain_state=domain,
        task_id=state.task_id,
        task_revision=revision or state.revision + 1,
        goal=state.goal,
        unknowns=state.unknowns,
        pending_questions=state.pending_questions,
        mode=mode,
    )


def _tamper(domain, field):
    changed = deepcopy(domain)
    if field == "mode":
        changed["shoppingGuide"]["mode"] = "compare"
    elif field == "category":
        changed["shoppingGuide"]["category"] = "laptop"
    elif field == "candidateIds":
        changed["shoppingGuide"]["candidateIds"] = [13]
    elif field == "comparedIds":
        changed["shoppingGuide"]["comparedIds"] = [12, 13]
    elif field == "evidenceStatus":
        changed["shoppingGuide"]["evidenceStatus"] = "missing"
    elif field == "candidatePoolIds":
        changed["candidateScope"]["candidatePoolIds"] = [11, 12, 13, 14]
    elif field == "rankedItemIds":
        changed["candidateScope"]["rankedItemIds"] = [12, 11]
        changed["candidateScope"]["visibleProductIds"] = [12]
    elif field == "evidenceRefs":
        changed["candidateScope"]["evidenceRefs"] = ["evidence:tampered"]
    else:  # pragma: no cover
        raise AssertionError(field)
    return changed


def test_v2_1_is_default_and_publishes_only_embedded_complete_projection():
    state, domain = _bound_domain()
    selected = _select(state, domain)
    snapshot = domain["shoppingTaskStateV2"]

    assert selected.source == "v2"
    assert selected.degraded_reason is None
    assert selected.domain_state["shoppingGuide"] == snapshot["shoppingGuide"]
    assert selected.domain_state["candidateScope"] == snapshot["candidateScope"]
    assert selected.goal == snapshot["goal"]


@pytest.mark.parametrize(
    "field",
    [
        "mode",
        "category",
        "candidateIds",
        "comparedIds",
        "evidenceStatus",
        "candidatePoolIds",
        "rankedItemIds",
        "evidenceRefs",
    ],
)
def test_valid_v2_round_is_unaffected_by_legacy_only_tampering(field):
    state, domain = _bound_domain()
    expected = _select(state, domain)
    selected = _select(state, _tamper(domain, field))

    assert selected.source == "v2"
    assert selected.semantic_hash == expected.semantic_hash
    assert selected.domain_state == expected.domain_state


def test_context_pack_and_planner_are_v2_derived_after_legacy_tampering():
    state, domain = _bound_domain()
    state = state.model_copy(update={"revision": state.revision + 1, "domain_state": domain})
    expected_pack = asyncio.run(build_context_pack(state, history=[], run_id="v2-original"))
    expected_planner = build_planner_context(state, "继续", [])

    tampered = _tamper(_tamper(_tamper(domain, "mode"), "candidateIds"), "evidenceRefs")
    state = state.model_copy(update={"domain_state": tampered})
    actual_pack = asyncio.run(build_context_pack(state, history=[], run_id="v2-tampered"))
    actual_planner = build_planner_context(state, "继续", [])

    assert actual_pack.shopping_guide_state == expected_pack.shopping_guide_state
    assert actual_pack.candidate_scope_state == expected_pack.candidate_scope_state
    assert actual_pack.scope_rerank_request == expected_pack.scope_rerank_request
    assert actual_planner.goal == expected_planner.goal
    assert actual_planner.shopping_guide_sources == expected_planner.shopping_guide_sources


def test_unrelated_v2_mode_write_repairs_legacy_from_v2_instead_of_importing_drift():
    state, domain = _bound_domain()
    expected = deepcopy(domain["shoppingTaskStateV2"]["shoppingGuide"])
    tampered = _tamper(domain, "mode")

    rebound = bind_authoritative_write(
        tampered,
        task_id=state.task_id,
        task_revision=state.revision + 2,
        goal=state.goal,
        unknowns=state.unknowns,
        pending_questions=state.pending_questions,
        mode="v2",
    )

    assert rebound["shoppingGuide"] == expected
    assert rebound["shoppingTaskStateV2"]["shoppingGuide"] == expected
    assert rebound[AUTHORITY_BINDING_KEY]["taskRevision"] == state.revision + 2

    # Only the TaskState CAS boundary may explicitly identify a newer
    # server-owned compatibility guide. In that case migration synchronizes
    # the latest guide/useCase and top-level goal before V2.1 validation.
    server_patch = deepcopy(domain)
    server_patch["shoppingGuide"]["useCases"] = ["gaming_title_claim"]
    migrated = bind_authoritative_write(
        server_patch,
        task_id=state.task_id,
        task_revision=state.revision + 3,
        goal="NEW GOAL",
        unknowns=[],
        pending_questions=[],
        mode="v2",
        compatibility_projection_changed=True,
    )
    assert migrated["shoppingTaskStateV2"]["goal"] == "NEW GOAL"
    assert migrated["shoppingTaskStateV2"]["useCase"] == "gaming_title_claim"
    assert migrated["shoppingTaskStateV2"]["shoppingGuide"]["useCases"] == [
        "gaming_title_claim"
    ]


@pytest.mark.parametrize(
    ("goal", "unknowns", "pending_questions"),
    [
        ("NEW GOAL", [], []),
        ("NEW GOAL", ["new unknown"], []),
        ("NEW GOAL", ["new unknown"], ["independent question"]),
        ("NEW GOAL", [], ["independent question"]),
    ],
)
def test_generic_top_level_semantics_are_synchronized_and_bound_in_v2_1(
    goal,
    unknowns,
    pending_questions,
):
    state, domain = _bound_domain()
    rebound = bind_authoritative_write(
        domain,
        task_id=state.task_id,
        task_revision=state.revision + 2,
        goal=goal,
        unknowns=unknowns,
        pending_questions=pending_questions,
        mode="v2",
    )
    selected = select_shopping_state_authority(
        domain_state=rebound,
        task_id=state.task_id,
        task_revision=state.revision + 2,
        goal=goal,
        unknowns=unknowns,
        pending_questions=pending_questions,
    )

    assert rebound[AUTHORITY_BINDING_KEY]["taskRevision"] == state.revision + 2
    assert rebound["shoppingTaskStateV2"]["goal"] == goal
    assert rebound["shoppingTaskStateV2"]["pendingQuestions"] == pending_questions
    assert [item["reason"] for item in rebound["shoppingTaskStateV2"]["unknowns"]] == unknowns
    assert selected.goal == goal
    assert selected.unknowns == tuple(unknowns)
    assert selected.pending_questions == tuple(pending_questions)


def test_normal_v2_read_rejects_missing_or_stale_current_revision_binding():
    state, domain = _bound_domain()
    unbound = deepcopy(domain)
    unbound.pop(AUTHORITY_BINDING_KEY)
    with pytest.raises(ShoppingStateAuthorityError, match="v2_binding_mismatch"):
        _select(state, unbound)

    with pytest.raises(ShoppingStateAuthorityError, match="v2_binding_mismatch"):
        _select(state, domain, revision=state.revision + 2)


def test_normal_v2_read_rejects_top_level_semantic_drift_even_with_old_binding():
    state, domain = _bound_domain()
    with pytest.raises(ShoppingStateAuthorityError, match="top_level_semantic_mismatch"):
        select_shopping_state_authority(
            domain_state=domain,
            task_id=state.task_id,
            task_revision=state.revision + 1,
            goal="NEW GOAL",
            unknowns=["new unknown"],
            pending_questions=["new unknown"],
        )


@pytest.mark.parametrize(
    "replacement,reason",
    [
        (None, "v2_missing_legacy_semantic_equivalent"),
        ({"schemaVersion": "shopping-task-state-v2"}, "v2_invalid_or_incomplete_legacy_semantic_equivalent"),
    ],
)
def test_whole_round_legacy_degradation_requires_complete_revision_hash_binding(
    replacement,
    reason,
):
    state, domain = _bound_domain()
    if replacement is None:
        domain.pop("shoppingTaskStateV2")
    else:
        domain["shoppingTaskStateV2"] = replacement
    selected = _select(state, domain)

    assert selected.source == "legacy_degraded"
    assert selected.degraded_reason == reason


@pytest.mark.parametrize(
    "field",
    [
        "mode",
        "category",
        "candidateIds",
        "comparedIds",
        "evidenceStatus",
        "candidatePoolIds",
        "rankedItemIds",
        "evidenceRefs",
    ],
)
def test_fallback_rejects_any_legacy_consumed_semantic_tampering(field):
    state, domain = _bound_domain()
    domain.pop("shoppingTaskStateV2")
    domain = _tamper(domain, field)

    with pytest.raises(ShoppingStateAuthorityError, match="legacy_invalid|binding_mismatch"):
        _select(state, domain)


def test_degradation_rejects_stale_revision_and_task_identity():
    state, domain = _bound_domain()
    domain.pop("shoppingTaskStateV2")
    with pytest.raises(ShoppingStateAuthorityError, match="legacy_invalid|binding_mismatch"):
        _select(state, domain, revision=state.revision + 2)
    with pytest.raises(ShoppingStateAuthorityError, match="legacy_invalid|binding_mismatch"):
        select_shopping_state_authority(
            domain_state=domain,
            task_id="other-task",
            task_revision=state.revision + 1,
            goal=state.goal,
            unknowns=state.unknowns,
            pending_questions=state.pending_questions,
        )


def test_historical_v2_schema_is_not_silently_treated_as_complete():
    state, domain = _bound_domain()
    historical = deepcopy(domain["shoppingTaskStateV2"])
    historical["schemaVersion"] = "shopping-task-state-v2"
    historical.pop("shoppingGuide")
    historical["candidateScope"] = {
        "scopeId": "scope-old",
        "category": "phone",
        "candidateIds": [11, 12],
        "sourceTurn": 6,
    }
    domain["shoppingTaskStateV2"] = historical
    domain.pop(AUTHORITY_BINDING_KEY)

    with pytest.raises(ShoppingStateAuthorityError, match="incomplete_binding_mismatch"):
        _select(state, domain)


def test_global_rollback_uses_legacy_even_when_v2_is_damaged():
    state, domain = _bound_domain()
    domain["shoppingTaskStateV2"] = {"damaged": True}
    selected = _select(state, domain, mode="legacy")

    assert selected.source == "legacy_rollback"
    assert selected.domain_state["shoppingGuide"] == ShoppingGuideState.model_validate(
        domain["shoppingGuide"]
    ).model_dump(by_alias=True, mode="json")
