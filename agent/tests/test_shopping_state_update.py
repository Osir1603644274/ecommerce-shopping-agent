from datetime import datetime, timezone

from app.domains.ecommerce.models import ShoppingGuideState
from app.domains.ecommerce.shopping_state_update import (
    build_shopping_state_transition_patch,
    clear_stale_guide_references,
    refresh_shopping_state_v2_after_validation,
)
from app.domains.ecommerce.shopping_state_authority import (
    synchronize_v2_compatibility_projection,
)
from app.domains.ecommerce.shopping_task_state_v2 import (
    parse_shopping_task_state_v2_snapshot,
)
from app.llm import _build_validated_task_state_payload
from app.task_state import TaskState


def _state() -> TaskState:
    now = datetime.now(timezone.utc)
    requirements = [
        {
            "key": "os", "operator": "eq", "value": "ios", "unit": "enum",
            "priority": "hard", "source": "user",
        },
        {
            "key": "price_minor", "operator": "lte", "value": 220_000,
            "unit": "CNY_MINOR", "priority": "hard", "source": "user",
        },
    ]
    return TaskState(
        taskId="task-shopping-state-update",
        taskType="ecommerce_guide",
        status="ready",
        revision=7,
        goal="预算2200以内，只看iOS",
        unknowns=[],
        pendingQuestions=[],
        domainState={
            "shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": requirements,
                "candidateIds": [11, 12], "comparedIds": [11, 12],
                "evidenceStatus": "complete",
            },
            "candidateScope": {
                "scopeId": "scope-old", "taskId": "task-shopping-state-update",
                "sourceRevision": 6, "sourcePlanId": "plan-old",
                "sourceStepId": "step-old", "category": "phone",
                "candidatePoolIds": [11, 12, 13],
                "rankedItemIds": [11, 12, 13],
                "visibleProductIds": [11, 12],
                "requirementsSnapshot": requirements,
                "brandAvoidancesSnapshot": [],
                "evidenceRefs": ["evidence:old"],
                "createdAt": now.isoformat(), "status": "active",
                "invalidationReason": None,
            },
        },
        createdAt=now,
        updatedAt=now,
    )


def test_budget_override_atomically_invalidates_candidate_scope() -> None:
    state = _state()
    next_guide = ShoppingGuideState.model_validate({
        **state.domain_state["shoppingGuide"],
        "requirements": [
            state.domain_state["shoppingGuide"]["requirements"][0],
            {
                "key": "price_minor", "operator": "lte", "value": 160_000,
                "unit": "CNY_MINOR", "priority": "hard", "source": "user",
            },
        ],
    })

    normalized, changed = clear_stale_guide_references(state, next_guide)
    patch = build_shopping_state_transition_patch(
        state,
        normalized,
        {"status": "ready", "goal": "预算改成1600，系统要求不变"},
        constraints_changed=changed,
    )
    shadow = parse_shopping_task_state_v2_snapshot(patch["shoppingTaskStateV2"])

    assert changed is True
    assert normalized.candidate_ids == []
    assert normalized.compared_ids == []
    assert normalized.evidence_status == "missing"
    assert patch["candidateScope"]["status"] == "invalidated"
    assert patch["candidateScopeInvalidation"] == {
        "scopeId": "scope-old",
        "status": "invalidated",
        "invalidationReason": "shopping_constraints_changed",
        "replacedByScopeId": None,
        "invalidatedAtRevision": 8,
    }
    assert shadow.candidate_scope is None
    assert [(delta.op, delta.requirement.key) for delta in shadow.deltas] == [
        ("override", "price_minor")
    ]
    assert {item.key: item.value for item in shadow.requirements} == {
        "os": "ios",
        "price_minor": 160_000,
    }


def test_unchanged_constraints_keep_bound_scope_in_v2_shadow() -> None:
    state = _state()
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])

    normalized, changed = clear_stale_guide_references(state, guide)
    patch = build_shopping_state_transition_patch(
        state,
        normalized,
        {"status": "ready"},
        constraints_changed=changed,
    )
    shadow = parse_shopping_task_state_v2_snapshot(patch["shoppingTaskStateV2"])

    assert changed is False
    assert "candidateScope" not in patch
    assert shadow.candidate_scope is not None
    assert shadow.candidate_scope.scope_id == "scope-old"
    assert shadow.candidate_scope.candidate_ids == (11, 12)
    assert shadow.deltas == ()


def test_v2_shadow_preserves_detailed_requirement_source_provenance() -> None:
    state = _state()
    inferred_source = "inferred: 续航好映射为较高电池健康度偏好"
    requirements = [
        *state.domain_state["shoppingGuide"]["requirements"],
        {
            "key": "battery_health", "operator": "eq", "value": "90_plus",
            "unit": "enum", "priority": "soft", "source": inferred_source,
        },
    ]
    state.domain_state["shoppingGuide"]["requirements"] = requirements
    state.domain_state["candidateScope"]["requirementsSnapshot"] = requirements
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])

    patch = build_shopping_state_transition_patch(
        state,
        guide,
        {"status": "ready"},
        constraints_changed=False,
    )
    shadow = parse_shopping_task_state_v2_snapshot(patch["shoppingTaskStateV2"])
    battery = next(item for item in shadow.requirements if item.key == "battery_health")

    assert battery.source == "inferred"
    assert battery.source_provenance == inferred_source
    assert shadow.candidate_scope is not None


def test_v2_shadow_preserves_brand_include_and_exclude_lanes() -> None:
    state = _state()
    guide = ShoppingGuideState.model_validate({
        **state.domain_state["shoppingGuide"],
        "requirements": [
            *state.domain_state["shoppingGuide"]["requirements"],
            {
                "key": "brand", "operator": "in",
                "value": ["huawei", "honor", "xiaomi"],
                "unit": "text", "priority": "hard", "source": "user",
            },
        ],
        "brandAvoidances": [
            {"values": ["apple"], "strength": "hard", "source": "user"},
        ],
    })

    patch = build_shopping_state_transition_patch(
        state,
        guide,
        {"status": "ready"},
        constraints_changed=True,
    )
    shadow = parse_shopping_task_state_v2_snapshot(patch["shoppingTaskStateV2"])
    brand_lanes = {
        (item.polarity, item.operator): tuple(item.value)
        for item in shadow.requirements
        if item.key == "brand"
    }

    assert brand_lanes == {
        ("include", "in"): ("huawei", "honor", "xiaomi"),
        ("exclude", "not_in"): ("apple",),
    }


def test_validated_task_patch_commits_budget_and_scope_invalidation_together() -> None:
    state = _state()

    payload, _model_patch = _build_validated_task_state_payload(
        state,
        {
            "status": "ready",
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "upsertRequirements": [{
                    "key": "price_minor", "operator": "lte", "value": 160_000,
                    "unit": "CNY_MINOR", "priority": "hard", "source": "user",
                }],
                "removeRequirementKeys": [],
            }},
        },
        message="预算改成1600，系统要求不变",
        require_status=True,
    )
    domain_patch = payload["domainStatePatch"]
    shadow = parse_shopping_task_state_v2_snapshot(domain_patch["shoppingTaskStateV2"])

    requirements = {
        item["key"]: item for item in domain_patch["shoppingGuide"]["requirements"]
    }
    assert requirements["price_minor"]["value"] == 160_000
    assert requirements["os"]["value"] == "ios"
    assert domain_patch["shoppingGuide"]["candidateIds"] == []
    assert domain_patch["candidateScope"]["status"] == "invalidated"
    assert payload["upsertConstraints"] == [
        {"key": "os", "operator": "eq", "value": "ios", "source": "user"},
        {
            "key": "price_minor", "operator": "lte", "value": 160_000,
            "source": "user",
        },
    ]
    assert [(item.op, item.requirement.key) for item in shadow.deltas] == [
        ("override", "price_minor")
    ]


def test_validator_refreshes_v2_scope_without_erasing_requirement_lifecycle() -> None:
    state = _state()
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])
    initial = build_shopping_state_transition_patch(
        state,
        guide,
        {"status": "ready"},
        constraints_changed=False,
    )["shoppingTaskStateV2"]
    state.domain_state["shoppingTaskStateV2"] = initial
    replacement_scope = {
        **state.domain_state["candidateScope"],
        "scopeId": "scope-new",
        "sourceRevision": 8,
        "sourcePlanId": "plan-new",
        "sourceStepId": "step-new",
        "candidatePoolIds": [21, 22],
        "rankedItemIds": [21, 22],
        "visibleProductIds": [21],
        "evidenceRefs": ["evidence:new"],
    }

    refreshed_raw = refresh_shopping_state_v2_after_validation(
        state,
        {
            "candidateScope": replacement_scope,
            "validationResult": {"outcome": "passed"},
        },
    )
    refreshed = parse_shopping_task_state_v2_snapshot(refreshed_raw)

    assert refreshed.requirements == parse_shopping_task_state_v2_snapshot(initial).requirements
    assert refreshed.deltas == parse_shopping_task_state_v2_snapshot(initial).deltas
    assert refreshed.candidate_scope is not None
    assert refreshed.candidate_scope.scope_id == "scope-new"
    assert refreshed.candidate_scope.visible_product_ids == [21]
    assert refreshed.current_action.kind == "answer"


def test_validator_refresh_accepts_authoritative_v2_1_snapshot() -> None:
    state = _state()
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])
    initial = build_shopping_state_transition_patch(
        state,
        guide,
        {"status": "ready"},
        constraints_changed=False,
    )["shoppingTaskStateV2"]
    state.domain_state["shoppingTaskStateV2"] = initial
    state.domain_state = synchronize_v2_compatibility_projection(
        state.domain_state,
        task_id=state.task_id,
        goal=state.goal,
        unknowns=state.unknowns,
        pending_questions=state.pending_questions,
    )
    assert state.domain_state["shoppingTaskStateV2"]["schemaVersion"] == (
        "shopping-task-state-v2.1"
    )
    replacement_scope = {
        **state.domain_state["candidateScope"],
        "scopeId": "scope-v2-1-refreshed",
        "sourceRevision": 8,
        "sourcePlanId": "plan-v2-1",
        "sourceStepId": "step-v2-1",
        "candidatePoolIds": [31, 32],
        "rankedItemIds": [31, 32],
        "visibleProductIds": [31],
        "evidenceRefs": ["evidence:v2-1"],
    }

    refreshed_raw = refresh_shopping_state_v2_after_validation(
        state,
        {
            "candidateScope": replacement_scope,
            "validationResult": {"outcome": "passed"},
        },
    )
    refreshed = parse_shopping_task_state_v2_snapshot(refreshed_raw)

    assert refreshed.schema_version == "shopping-task-state-v2.1"
    assert refreshed.pending_questions == ()
    assert refreshed.candidate_scope is not None
    assert refreshed.candidate_scope.scope_id == "scope-v2-1-refreshed"
    assert refreshed.candidate_scope.visible_product_ids == [31]
    assert refreshed.current_action.kind == "answer"
