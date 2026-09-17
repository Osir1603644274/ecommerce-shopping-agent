"""DAY3-CONTEXT-POLICY-SKILL-001 contract and isolation tests."""

import asyncio
import json

import pytest

from app.context_pack import ContextPack, ContextPackBudgetExceeded, build_context_pack
from app.context_policy import (
    CONTEXT_CONTRACT_VERSION,
    CONTEXT_PHASES,
    ECOMMERCE_CONTEXT_POLICY_ID,
    ContextPolicy,
    ContextPolicyError,
    ContextPolicyRegistry,
    get_context_policy,
)
from app.context_view import ContextProjector, _view_hash
from app.domains import registered_domains
from app.domains.context_skill import (
    ECOMMERCE_CONTEXT_SKILL_ID,
    GENERIC_CONTEXT_SKILL_ID,
    ContextSkill,
    ContextSkillError,
    ContextSkillRegistry,
    get_context_skill,
)


def _ecommerce_context():
    from app.domains.ecommerce.models import ShoppingGuideState
    from app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
    from app.domains.ecommerce.shopping_state_update import build_shopping_state_transition_patch
    from tests.test_candidate_scope import _state

    state = _state()
    raw_guide = dict(state.domain_state["shoppingGuide"])
    raw_guide["candidateIds"] = list(
        state.domain_state["candidateScope"]["visibleProductIds"]
    )
    raw_guide["evidenceStatus"] = "complete"
    guide = ShoppingGuideState.model_validate(raw_guide)
    domain = dict(state.domain_state)
    domain["shoppingGuide"] = guide.model_dump(by_alias=True, mode="json")
    domain.update(build_shopping_state_transition_patch(
        state,
        guide,
        {"status": state.status},
        constraints_changed=False,
    ))
    state.domain_state = bind_authoritative_write(
        domain,
        task_id=state.task_id,
        task_revision=state.revision,
        goal=state.goal,
        unknowns=state.unknowns,
        pending_questions=state.pending_questions,
    )
    return state, state.domain_state


def test_policy_is_immutable_versioned_and_queryable_for_all_phases():
    policy = get_context_policy(
        ECOMMERCE_CONTEXT_POLICY_ID,
        CONTEXT_CONTRACT_VERSION,
    )

    assert policy.key == (
        ECOMMERCE_CONTEXT_POLICY_ID,
        CONTEXT_CONTRACT_VERSION,
    )
    assert set(policy.phase_fields) == set((phase, tuple(sorted(policy.fields_for(phase)))) for phase in CONTEXT_PHASES)
    with pytest.raises((AttributeError, TypeError)):
        policy.policy_id = "tampered"


def test_unknown_phase_and_unknown_policy_fields_fail_closed():
    policy = get_context_policy(
        ECOMMERCE_CONTEXT_POLICY_ID,
        CONTEXT_CONTRACT_VERSION,
    )
    with pytest.raises(ContextPolicyError, match="unknown context phase"):
        policy.fields_for("unknown")

    declaration = {
        "policyId": "test-policy",
        "version": "1.0",
        "unexpected": True,
        "phases": {phase: {"fields": []} for phase in CONTEXT_PHASES},
    }
    with pytest.raises(ContextPolicyError, match="unknown policy declaration"):
        ContextPolicy.from_declaration(declaration)

    declaration = {
        "policyId": "test-policy",
        "version": "1.0",
        "phases": {
            **{phase: {"fields": []} for phase in CONTEXT_PHASES},
            "planner": {"fields": ["rawDomainState"]},
        },
    }
    with pytest.raises(ContextPolicyError, match="unknown context field"):
        ContextPolicy.from_declaration(declaration)


def test_duplicate_policy_and_skill_registration_is_rejected():
    policy_registry = ContextPolicyRegistry()
    declaration = {
        "policyId": "duplicate-test-policy",
        "version": "1.0",
        "phases": {phase: {"fields": []} for phase in CONTEXT_PHASES},
    }
    policy_registry.register(declaration)
    with pytest.raises(ContextPolicyError, match="duplicate context policy"):
        policy_registry.register(declaration)

    skill_registry = ContextSkillRegistry()
    skill = ContextSkill(
        skill_id="duplicate-test-skill",
        version="1.0",
        domain_id="test",
        policy_id="generic-context-policy",
        policy_version="1.0",
        publisher=lambda _state: {},
    )
    skill_registry.register(skill)
    with pytest.raises(ContextSkillError, match="duplicate context skill"):
        skill_registry.register(skill)


def test_unknown_skill_and_skill_published_field_fail_closed():
    with pytest.raises(ContextSkillError, match="unknown context skill"):
        get_context_skill("missing-context-skill", "1.0")

    with pytest.raises(ValueError, match="unknown context skill"):
        ContextPack(
            runId="r-unknown-skill",
            taskId="t-unknown-skill",
            baseContextRevision=1,
            goal="test",
            contextPolicyId="generic-context-policy",
            contextPolicyVersion="1.0",
            contextSkillId="missing-context-skill",
            contextSkillVersion="1.0",
        )

    skill = ContextSkill(
        skill_id="bad-publisher-skill",
        version="1.0",
        domain_id="test",
        policy_id="generic-context-policy",
        policy_version="1.0",
        publisher=lambda _state: {"rawDomainState": {"secret": "x"}},
    )
    with pytest.raises(ContextSkillError, match="unknown field"):
        skill.publish_context({})


def test_domain_specs_explicitly_bind_registered_skills():
    domains = {item.domain_id: item for item in registered_domains()}
    assert set(domains) == {"local_life", "ecommerce"}
    for domain in domains.values():
        skill = get_context_skill(
            domain.context_skill_id,
            domain.context_skill_version,
        )
        assert skill.domain_id == domain.domain_id


def test_ecommerce_skill_rejects_invalid_structures_and_never_publishes_raw_state():
    skill = get_context_skill(ECOMMERCE_CONTEXT_SKILL_ID, CONTEXT_CONTRACT_VERSION)
    with pytest.raises(ContextSkillError, match="invalid ecommerce context field"):
        skill.publish_context({"shoppingGuide": {"category": "not-a-category"}})

    state, raw_domain = _ecommerce_context()
    raw_domain["privateOracle"] = {"answer": "must not escape"}
    pack = asyncio.run(build_context_pack(state))
    dumped = json.dumps(pack.model_dump(by_alias=True, mode="json"), ensure_ascii=False)
    assert "privateOracle" not in dumped
    assert "domainState" not in dumped


def test_cross_domain_residual_is_not_published():
    state, raw_domain = _ecommerce_context()
    state.task_type = "local_life"
    raw_domain["shoppingGuide"] = raw_domain["shoppingGuide"]
    pack = asyncio.run(build_context_pack(state))
    projector = ContextProjector(pack)

    planner = projector.planner_view(["search_shops"], "ready")
    executor = projector.executor_view(
        plan_id="p1", step_id="s1", step_description="本地查询", tool_name="search_shops"
    )
    final_answer = projector.final_answer_view(validated_results=[])
    assert pack.shopping_guide_state is None
    assert planner.shopping_guide_sources is None
    assert executor.shopping_guide_sources is None
    assert final_answer.answer_category is None
    planner_dump = json.dumps(planner.model_dump(by_alias=True), ensure_ascii=False)
    assert '"shoppingGuideState"' not in planner_dump
    assert '"scopeId"' not in planner_dump


def test_task_domain_skill_policy_identity_matrix_freezes_constructor_and_bypasses():
    state, _raw_domain = _ecommerce_context()
    pack = asyncio.run(build_context_pack(state))
    pack_data = pack.model_dump(mode="python")

    constructor_cases = (
        {"task_type": "local_life"},
        {
            "context_policy_id": "generic-context-policy",
            "context_skill_id": GENERIC_CONTEXT_SKILL_ID,
        },
        {"task_type": "unknown_task"},
        {"context_policy_id": "generic-context-policy"},
        {"context_skill_version": "9.9"},
    )
    for update in constructor_cases:
        explicit = dict(pack_data)
        explicit.update(update)
        with pytest.raises(ValueError, match="context"):
            ContextPack(**explicit)

    projector_cases = {
        "local_life_with_ecommerce": {"task_type": "local_life"},
        "ecommerce_with_generic": {
            "context_policy_id": "generic-context-policy",
            "context_skill_id": GENERIC_CONTEXT_SKILL_ID,
        },
        "unknown_task_with_ecommerce": {"task_type": "unknown_task"},
        "wrong_policy": {"context_policy_id": "generic-context-policy"},
        "wrong_skill_version": {"context_skill_version": "9.9"},
    }
    for name, update in projector_cases.items():
        forged = pack.model_copy(update=update)
        with pytest.raises(ContextSkillError, match="context"):
            ContextProjector(forged)

    for update in (
        {"task_type": "local_life"},
        {
            "context_policy_id": "generic-context-policy",
            "context_skill_id": GENERIC_CONTEXT_SKILL_ID,
        },
    ):
        constructed = ContextPack.model_construct(**{**pack_data, **update})
        with pytest.raises(ContextSkillError, match="context"):
            ContextProjector(constructed)

    invalid_payload = ContextPack.model_construct(
        **{**pack_data, "shopping_guide_state": {"category": "not-a-category"}},
    )
    with pytest.raises(ContextSkillError, match="invalid ecommerce context field"):
        ContextProjector(invalid_payload)


def test_ecommerce_scope_rerank_is_the_only_published_source_for_each_relevant_phase():
    state, _raw_domain = _ecommerce_context()
    pack = asyncio.run(build_context_pack(state))
    projector = ContextProjector(pack)

    planner = projector.planner_view(["rerank_products_in_scope"], "ready")
    executor = projector.executor_view(
        plan_id="p1",
        step_id="s1",
        step_description="候选域排序",
        tool_name="rerank_products_in_scope",
    )
    replanner = projector.replanner_view(
        failed_plan_summary={}, failure_reason="retry"
    )
    for view in (planner, executor, replanner):
        assert view.shopping_guide_sources["scopeId"] == "scope-task-iphone-guide-3"
        assert view.shopping_guide_sources["scopeRankedItemIds"] == [101, 102, 103]
        assert view.shopping_guide_sources["rankingIntent"] == "camera_title_claim"
        assert "domainState" not in json.dumps(view.model_dump(by_alias=True), ensure_ascii=False)

    validator = projector.validator_view(executed_steps=[])
    assert validator.context_skill_id == ECOMMERCE_CONTEXT_SKILL_ID
    assert validator.context_policy_version == CONTEXT_CONTRACT_VERSION
    assert projector.final_answer_view(validated_results=[]).answer_category == "phone"


def test_phase_policy_disallows_ecommerce_fields_from_validator():
    state, raw_domain = _ecommerce_context()
    skill = get_context_skill(ECOMMERCE_CONTEXT_SKILL_ID, CONTEXT_CONTRACT_VERSION)
    assert skill.context_for_phase("validator", raw_domain) == {}

    raw_domain["scopeRerankRequest"]["unexpected"] = True
    pack = asyncio.run(build_context_pack(state))
    # The malformed compatibility residual cannot replace the bound V2 value.
    assert pack.scope_rerank_request["scopeId"] == "scope-task-iphone-guide-3"
    validator = ContextProjector(pack).validator_view(executed_steps=[])
    assert "scopeRerankRequest" not in validator.model_dump(
        by_alias=True, exclude_none=True,
    )


def test_all_views_carry_identity_and_identity_is_hashed_without_full_policy():
    state, _raw_domain = _ecommerce_context()
    projector = ContextProjector(asyncio.run(build_context_pack(state)))
    views = [
        projector.planner_view(["search_products"], "ready"),
        projector.executor_view(
            plan_id="p1", step_id="s1", step_description="search", tool_name="search_products"
        ),
        projector.validator_view(executed_steps=[]),
        projector.replanner_view(failed_plan_summary={}, failure_reason="retry"),
        projector.final_answer_view(validated_results=[]),
    ]
    for view in views:
        assert view.context_policy_id == ECOMMERCE_CONTEXT_POLICY_ID
        assert view.context_policy_version == CONTEXT_CONTRACT_VERSION
        assert view.context_skill_id == ECOMMERCE_CONTEXT_SKILL_ID
        assert view.context_skill_version == CONTEXT_CONTRACT_VERSION
        assert "phases" not in json.dumps(view.model_dump(by_alias=True), ensure_ascii=False)

    planner = views[0]
    changed = planner.model_copy(update={"context_policy_version": "1.0.1"})
    assert _view_hash(planner) != _view_hash(changed)


def test_context_budget_remains_fail_closed_with_skill_context():
    state, _raw_domain = _ecommerce_context()
    state.goal = "需求 " + "很长" * 400
    from app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
    state.domain_state = bind_authoritative_write(
        state.domain_state,
        task_id=state.task_id,
        task_revision=state.revision,
        goal=state.goal,
        unknowns=state.unknowns,
        pending_questions=state.pending_questions,
    )
    with pytest.raises(ContextPackBudgetExceeded):
        asyncio.run(build_context_pack(state, budget_tokens=200))
