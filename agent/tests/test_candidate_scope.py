"""CandidateScope + in-scope deterministic rerank contract tests.

Covers USED-PHONE-CANDIDATE-SCOPE-001 §6.1-§6.12 plus the Attempt-002
authorization requirements: the three server-owned shopping-guide references
(``scopeId`` / ``scopeRankedItemIds`` / ``rankingIntent``) are accepted,
fabricated references stay rejected, and every fail-closed branch
(missing/expired/cross-task/cross-scope/order-tampered/snapshot-drift) is
exercised against the real TOOL_SCHEMAS / Planner / Executor / Validator /
Harness boundary — not the raw model alone.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.domains.ecommerce import (
    CandidateScope,
    ScopeRerankRequest,
    ShoppingGuideState,
    compiled_shopping_requirements,
)
from app.domains.ecommerce.ranking_contract import (
    SCOPE_RERANK_CONTRACT_VERSION,
    normalize_scope_rerank_detail,
    normalize_search_products_detail,
)
from app.domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
    USED_PHONE_ATTRIBUTE_RULESET_VERSION,
)
from app.planning import PlanArgumentSource
from app.planner import _deterministic_shopping_guide_plan, build_planner_context
from app.task_state import TaskState
from app.tools import TOOL_SCHEMAS
from tests.two_stage_ranking_fixtures import two_stage_search_detail

_OS_REQ = {
    "key": "os", "operator": "eq", "value": "ios",
    "unit": "enum", "priority": "hard", "source": "user",
}
_ANDROID_REQ = {
    "key": "os", "operator": "eq", "value": "android",
    "unit": "enum", "priority": "hard", "source": "user",
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guide(*, requirements=None, brand_avoidances=None, category="phone",
           compared_ids=None):
    return {
        "mode": "recommend",
        "category": category,
        "useCases": [],
        "requirements": list(requirements or []),
        "candidateIds": [],
        "comparedIds": list(compared_ids or []),
        "evidenceStatus": "missing",
        "brandAvoidances": list(brand_avoidances or []),
    }


def _scope(*, task_id="task-iphone-guide", scope_id="scope-task-iphone-guide-3",
           pool=None, ranked=None, visible=None, requirements_snapshot=None,
           status="active", source_revision=3, **overrides):
    pool = list(pool if pool is not None else [101, 102, 103, 104, 105])
    ranked = list(ranked if ranked is not None else [101, 102, 103])
    visible = list(visible if visible is not None else ranked)
    return CandidateScope(
        scopeId=scope_id,
        taskId=task_id,
        sourceRevision=source_revision,
        sourcePlanId="plan-search",
        sourceStepId="step-search",
        category="phone",
        candidatePoolIds=pool,
        rankedItemIds=ranked,
        visibleProductIds=visible,
        requirementsSnapshot=(
            requirements_snapshot if requirements_snapshot is not None else [_OS_REQ]
        ),
        brandAvoidancesSnapshot=[],
        evidenceRefs=[f"product:{item}:title" for item in ranked],
        createdAt=_utc_now(),
        status=status,
        **overrides,
    ).model_dump(by_alias=True, mode="json")


def _rerank_request(*, scope_id="scope-task-iphone-guide-3",
                    intent="camera_title_claim"):
    return ScopeRerankRequest(
        scopeId=scope_id, rankingIntent=intent, createdAt=_utc_now(),
    ).model_dump(by_alias=True, mode="json")


def _rerank_detail(scope_id, ranked, *, input_ids=None):
    ranked = list(ranked)
    input_ids = list(input_ids if input_ids is not None else ranked)
    evidence = []
    for item in ranked:
        evidence.append({
            "ref": f"product:{item}:title", "field": "title", "rawValue": str(item),
        })
        evidence.append({
            "ref": f"product:{item}:attribute:os",
            "field": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
            "method": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
            "rawValue": "iOS",
        })
    return {
        "contractVersion": SCOPE_RERANK_CONTRACT_VERSION,
        "scopeId": scope_id,
        "inputProductIds": input_ids,
        "rankedItemIds": ranked,
        "productIds": list(ranked),
        "rankingSignal": "title_order_claim",
        "degraded": [],
        "noFullSearch": True,
        "candidates": [
            {
                "id": item, "title": str(item), "priceStatus": "unverified",
                "snapshotPriceMinor": None, "currency": "CNY",
                "attributes": [{
                    "key": "os", "rawValue": "iOS", "normalizedText": "ios",
                    "evidenceField": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                    "extractionMethod": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
                }],
                "checks": [{
                    "key": "os", "operator": "eq", "expected": "ios",
                    "unit": "enum", "priority": "hard", "source": "user",
                    "status": "pass", "actual": "ios",
                    "evidenceRef": f"product:{item}:attribute:os",
                }],
                "evidenceRefs": [
                    f"product:{item}:title", f"product:{item}:attribute:os",
                ],
            }
            for item in ranked
        ],
        "citationTrace": {
            "contractVersion": SCOPE_RERANK_CONTRACT_VERSION,
            "sourceTool": "rerank_products_in_scope",
            "rankedItemIds": ranked,
            "evidenceRefCount": len(evidence),
            "binding": "current_successful_scope_rerank_only",
        },
        "evidenceRefs": [
            ref for item in ranked
            for ref in (f"product:{item}:title", f"product:{item}:attribute:os")
        ],
        "evidence": evidence,
    }


def _state(*, with_scope=True, with_request=True, task_id="task-iphone-guide",
           requirements=None, scope_extra=None, request_extra=None):
    now = datetime.now(timezone.utc)
    domain = {
        "shoppingGuide": _guide(
            requirements=requirements if requirements is not None else [_OS_REQ],
        ),
    }
    if with_scope:
        scope_kwargs = dict(scope_extra or {})
        scope_kwargs.setdefault("task_id", task_id)
        scope_kwargs.setdefault(
            "requirements_snapshot",
            requirements if requirements is not None else [_OS_REQ],
        )
        domain["candidateScope"] = _scope(**scope_kwargs)
    if with_request:
        domain["scopeRerankRequest"] = _rerank_request(**(request_extra or {}))
    return TaskState(
        taskId=task_id,
        taskType="ecommerce_guide",
        status="ready",
        revision=3,
        goal="iOS、主板未维修的手机",
        domainState=domain,
        createdAt=now,
        updatedAt=now,
    )


def _rerank_tool_schemas():
    return [
        t for t in TOOL_SCHEMAS
        if t["function"]["name"] in (
            "search_products", "compare_products", "rerank_products_in_scope",
        )
    ]


def _planned_step(state, message):
    # This file verifies the historical CandidateScope contract in isolation.
    # The production V2 authority path has its own dedicated contract suite.
    with patch("app.settings.settings.shopping_state_authority", "legacy"):
        ctx = build_planner_context(
            state, user_message=message,
            candidate_tool_schemas=_rerank_tool_schemas(),
        )
    result = _deterministic_shopping_guide_plan(
        ctx, plan_id_factory=lambda: "plan-rerank"
    )
    assert result is not None and result.outcome == "planned", (
        getattr(result, "reason", result)
    )
    assert len(result.plan.steps) == 1
    return result.plan.steps[0]


# ── §6.3 model boundary ──────────────────────────────────────────────────────


class TestCandidateScopeModel:
    def test_valid_scope_roundtrips(self):
        raw = _scope()
        model = CandidateScope.model_validate(raw)
        assert model.status == "active"
        assert model.ranked_item_ids == [101, 102, 103]
        assert model.visible_product_ids == [101, 102, 103]
        assert set(model.ranked_item_ids).issubset(model.candidate_pool_ids)

    @pytest.mark.parametrize("bad_id", ["101", True, -1, 0, 1.5])
    def test_rejects_invalid_ranked_id(self, bad_id):
        with pytest.raises(ValueError):
            _scope(ranked=[101, 102, bad_id])

    @pytest.mark.parametrize("bad_id", ["101", False, -1, 0, 1.5])
    def test_rejects_invalid_pool_id(self, bad_id):
        with pytest.raises(ValueError):
            _scope(pool=[101, 102, bad_id, 104, 105])

    def test_rejects_duplicate_ranked_ids(self):
        with pytest.raises(ValueError):
            _scope(ranked=[101, 101, 103])

    def test_rejects_ranked_not_subset_of_pool(self):
        with pytest.raises(ValueError):
            _scope(pool=[101, 102, 103, 104], ranked=[101, 102, 999])

    def test_rejects_visible_not_subset_of_ranked(self):
        with pytest.raises(ValueError):
            _scope(ranked=[101, 102, 103], visible=[101, 102, 104])

    def test_rejects_extra_unknown_field(self):
        raw = _scope()
        raw["hackedField"] = "injected"
        with pytest.raises(ValueError):
            CandidateScope.model_validate(raw)

    def test_rejects_blank_scope_id(self):
        with pytest.raises(ValueError):
            _scope(scope_id="   ")


class TestScopeRerankRequestModel:
    def test_valid_request_roundtrips(self):
        model = ScopeRerankRequest.model_validate(
            _rerank_request(intent="gaming_title_claim")
        )
        assert model.ranking_intent == "gaming_title_claim"

    def test_rejects_unknown_intent(self):
        with pytest.raises(ValueError):
            ScopeRerankRequest.model_validate(
                _rerank_request(intent="best_price_claim")
            )

    def test_rejects_extra_unknown_field(self):
        raw = _rerank_request()
        raw["hacked"] = True
        with pytest.raises(ValueError):
            ScopeRerankRequest.model_validate(raw)

    def test_rejects_blank_scope_id(self):
        with pytest.raises(ValueError):
            ScopeRerankRequest.model_validate(_rerank_request(scope_id=" "))


# ── §6.12 dual import-tree boundary ──────────────────────────────────────────


def _ensure_agent_tree_importable() -> None:
    """Expose the ``agent.app.*`` tree for the §6.12 boundary tests.

    The suite normally runs with the ``app.*`` tree on ``sys.path`` (cwd=agent);
    the demo server runs ``uvicorn agent.app.main:app``.  Both trees point at the
    same physical files but are distinct module objects, so class identity can
    diverge.  These tests prove the single source of truth and the runtime
    coercion boundary.
    """
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parent.parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


class TestCrossTreeImportIdentity:
    def test_both_trees_resolve_to_single_source_of_truth(self):
        from pathlib import Path
        _ensure_agent_tree_importable()
        import app.domains.ecommerce.models as a
        import agent.app.domains.ecommerce.models as b
        assert Path(a.__file__).resolve() == Path(b.__file__).resolve()

    def test_scope_models_round_trip_identically_across_trees(self):
        _ensure_agent_tree_importable()
        import agent.app.domains.ecommerce.models as b
        raw_scope = _scope()
        via_a = CandidateScope.model_validate(raw_scope).model_dump(
            by_alias=True, mode="json"
        )
        via_b = b.CandidateScope.model_validate(raw_scope).model_dump(
            by_alias=True, mode="json"
        )
        assert via_a == via_b
        raw_request = _rerank_request()
        via_a_request = ScopeRerankRequest.model_validate(
            raw_request
        ).model_dump(by_alias=True, mode="json")
        via_b_request = b.ScopeRerankRequest.model_validate(
            raw_request
        ).model_dump(by_alias=True, mode="json")
        assert via_a_request == via_b_request

    def test_forged_scope_rejected_identically_across_trees(self):
        _ensure_agent_tree_importable()
        import agent.app.domains.ecommerce.models as b
        forged = _scope()
        forged["hackedField"] = "injected"
        with pytest.raises(ValueError):
            CandidateScope.model_validate(forged)
        with pytest.raises(ValueError):
            b.CandidateScope.model_validate(forged)

    def test_coerce_shopping_requirements_canonicalises_sibling_class(self):
        # The demo runs ``uvicorn agent.app.main:app`` while modules use ``app.*``;
        # the two trees can hand a *sibling* ShoppingRequirement to fast-response
        # code.  ``_coerce_shopping_requirements`` must re-validate it against
        # this module's class so pydantic model_type errors never fire.
        from app.domains.ecommerce.fast_response import _coerce_shopping_requirements
        _ensure_agent_tree_importable()
        import agent.app.domains.ecommerce.models as b
        from app.domains.ecommerce.models import ShoppingRequirement
        sibling = b.ShoppingRequirement.model_validate(_OS_REQ)
        assert not isinstance(sibling, ShoppingRequirement)
        coerced = _coerce_shopping_requirements([sibling])
        assert isinstance(coerced[0], ShoppingRequirement)
        assert coerced[0].model_dump(mode="json") == ShoppingRequirement.model_validate(
            _OS_REQ
        ).model_dump(mode="json")


# ── Attempt-002 whitelist: three new references accepted, forged rejected ────


class TestPlanArgumentSourceWhitelist:
    @pytest.mark.parametrize("reference", [
        "scopeId", "scopeRankedItemIds", "rankingIntent",
    ])
    def test_scope_references_are_accepted(self, reference):
        source = PlanArgumentSource(kind="shopping_guide", reference=reference)
        assert source.reference == reference

    @pytest.mark.parametrize("forged", [
        "domainState.candidateScope.scopeId",
        "candidateScope.rankedItemIds",
        "scope.rankedItemIds",
        "scopeId.extra",
        "anythingAtAll",
    ])
    def test_fabricated_references_rejected(self, forged):
        with pytest.raises(ValueError, match="只允许固定引用"):
            PlanArgumentSource(kind="shopping_guide", reference=forged)


# ── shopping_guide_argument_sources gating ──────────────────────────────────


class TestShoppingGuideScopeReferences:
    def test_publishes_scope_references_when_valid(self):
        from app.context_pack import shopping_guide_argument_sources
        state = _state()
        sources = shopping_guide_argument_sources(
            state.domain_state["shoppingGuide"],
            scope=state.domain_state["candidateScope"],
            pending_rerank=state.domain_state["scopeRerankRequest"],
        )
        assert sources["scopeId"] == "scope-task-iphone-guide-3"
        assert sources["scopeRankedItemIds"] == [101, 102, 103]
        assert sources["rankingIntent"] == "camera_title_claim"
        assert sources["categoryCode"] == "phone"
        assert sources["category"] == "手机"

    def test_does_not_publish_when_request_missing(self):
        from app.context_pack import shopping_guide_argument_sources
        state = _state(with_request=False)
        sources = shopping_guide_argument_sources(
            state.domain_state["shoppingGuide"],
            scope=state.domain_state["candidateScope"],
            pending_rerank=None,
        )
        assert "scopeId" not in sources
        assert "scopeRankedItemIds" not in sources
        assert "rankingIntent" not in sources

    def test_does_not_publish_on_scope_id_mismatch(self):
        from app.context_pack import shopping_guide_argument_sources
        state = _state(request_extra={"scope_id": "scope-other-task-9"})
        sources = shopping_guide_argument_sources(
            state.domain_state["shoppingGuide"],
            scope=state.domain_state["candidateScope"],
            pending_rerank=state.domain_state["scopeRerankRequest"],
        )
        assert "scopeId" not in sources

    def test_does_not_publish_when_scope_inactive(self):
        from app.context_pack import shopping_guide_argument_sources
        state = _state(scope_extra={"status": "invalidated"})
        sources = shopping_guide_argument_sources(
            state.domain_state["shoppingGuide"],
            scope=state.domain_state["candidateScope"],
            pending_rerank=state.domain_state["scopeRerankRequest"],
        )
        assert "scopeId" not in sources

    def test_does_not_publish_on_category_mismatch(self):
        from app.context_pack import shopping_guide_argument_sources
        state = _state(requirements=[_OS_REQ])
        state.domain_state["shoppingGuide"] = _guide(
            requirements=[], category="laptop",
        )
        sources = shopping_guide_argument_sources(
            state.domain_state["shoppingGuide"],
            scope=state.domain_state["candidateScope"],
            pending_rerank=state.domain_state["scopeRerankRequest"],
        )
        assert "scopeId" not in sources

    def test_never_publishes_scope_refs_for_invalid_scope_dict(self):
        from app.context_pack import shopping_guide_argument_sources
        state = _state()
        tampered = dict(state.domain_state["candidateScope"])
        tampered["rankedItemIds"] = [999]
        sources = shopping_guide_argument_sources(
            state.domain_state["shoppingGuide"],
            scope=tampered,
            pending_rerank=state.domain_state["scopeRerankRequest"],
        )
        assert "scopeId" not in sources

    def test_publishes_compared_ids_for_bound_pair(self):
        from app.context_pack import shopping_guide_argument_sources
        sources = shopping_guide_argument_sources(
            _guide(compared_ids=[101, 102])
        )
        assert sources["comparedIds"] == [101, 102]
        assert sources["categoryCode"] == "phone"


# ── §6.6b explicit harness tool menu: rerank gate fires on retired-plan turn ──


class TestExplicitHarnessToolSchemasRerankGate:
    """The pending scopeRerankRequest must make rerank_products_in_scope visible
    to the deterministic Planner even when the completed search Plan has been
    retired (activePlan=None).  Regression for the real-demo round-2 failure
    ``tool_not_allowed`` caught in USED-PHONE-CANDIDATE-SCOPE-001 Attempt 002."""

    @staticmethod
    def _names(message, state):
        from app.llm import _explicit_harness_tool_schemas, _tool_schema_name
        return [
            _tool_schema_name(schema)
            for schema in _explicit_harness_tool_schemas(message, state)
        ]

    def test_rerank_visible_when_plan_retired_and_request_pending(self):
        state = _state()
        assert state.active_plan is None
        names = self._names("这其中哪一个拍照效果最好", state)
        assert "rerank_products_in_scope" in names

    def test_visible_rerank_without_scope_is_still_execution_blocked(self):
        from app.executor import _validate_scope_rerank_resolved

        state = _state(with_scope=False, with_request=False)
        with pytest.raises(Exception, match="CandidateScope"):
            _validate_scope_rerank_resolved(
                state,
                {
                    "scopeId": "scope-forged",
                    "productIds": [101, 102, 103],
                    "rankingIntent": "camera_title_claim",
                    "category": "phone",
                },
            )

    def test_rerank_menu_entry_is_visible_without_pending_request_but_not_authorized(self):
        state = _state(with_request=False)
        assert state.active_plan is None
        names = self._names("这其中哪一个拍照效果最好", state)
        assert "rerank_products_in_scope" in names

    def test_rerank_visible_when_resuming_rerank_plan(self):
        from app.planning import TaskPlan
        state = _state()
        steps = _planned_steps_as_plan(state, "这其中哪一个拍照效果最好")
        state.active_plan = TaskPlan(
            planId="plan-rerank",
            basedOnRevision=state.revision,
            status="active",
            steps=steps,
        )
        names = self._names("这其中哪一个拍照效果最好", state)
        assert "rerank_products_in_scope" in names

    def test_rerank_menu_entry_is_visible_for_arbitrary_message_but_not_authorized(self):
        state = _state(with_request=False)
        names = self._names("随便聊聊", state)
        assert "rerank_products_in_scope" in names


# ── §6.6 deterministic Planner: single in-scope rerank, model calls = 0 ──────


class TestDeterministicPlannerRerank:
    def test_single_rerank_step_server_owned_sources(self):
        state = _state()
        step = _planned_step(state, "这其中哪一个拍照效果最好")
        assert step.tool_name == "rerank_products_in_scope"
        assert step.arguments["scopeId"] == "scope-task-iphone-guide-3"
        assert step.arguments["productIds"] == [101, 102, 103]
        assert step.arguments["rankingIntent"] == "camera_title_claim"
        assert step.arguments["category"] == "phone"
        assert step.expected_output == {"requiresScopeRerank": True}
        for name in ("scopeId", "productIds", "rankingIntent", "category", "requirements"):
            assert step.argument_sources[name].kind == "shopping_guide"
        assert step.argument_sources["scopeId"].reference == "scopeId"
        assert step.argument_sources["productIds"].reference == "scopeRankedItemIds"
        assert step.argument_sources["rankingIntent"].reference == "rankingIntent"
        # One and only one step: no second search inside the same plan.
        assert len(_planned_steps_as_plan(state, "这其中哪一个拍照效果最好")) == 1

    def test_gaming_intent_rerank(self):
        state = _state(request_extra={"intent": "gaming_title_claim"})
        step = _planned_step(state, "这里面哪一个打游戏最好")
        assert step.tool_name == "rerank_products_in_scope"
        assert step.arguments["rankingIntent"] == "gaming_title_claim"

    def test_base_condition_change_emits_search_not_rerank(self):
        # Guide now demands android; no server-owned rerank request is
        # published, so the deterministic plan must re-search the full catalog
        # and never reuse the stale candidate scope.
        state = _state(requirements=[_ANDROID_REQ], with_request=False)
        step = _planned_step(state, "我改要安卓的，华为优先")
        assert step.tool_name == "search_products"
        assert step.arguments["category"] == "手机"
        assert step.expected_output == {"requiresProductCandidates": True}

    def test_no_scope_never_reranks(self):
        state = _state(with_scope=False, with_request=False)
        step = _planned_step(state, "这其中哪一个拍照效果最好")
        assert step.tool_name == "search_products"

    def test_bound_compare_stays_unique_compare_products(self):
        # A validator-bound comparison pair must never be absorbed by rerank:
        # the planner emits exactly one compare_products step.
        state = _state(with_scope=False, with_request=False)
        state.domain_state["shoppingGuide"] = _guide(
            requirements=[_OS_REQ], compared_ids=[101, 102],
        )
        step = _planned_step(state, "第一个和第二个哪个好")
        assert step.tool_name == "compare_products"
        assert step.arguments["productIds"] == [101, 102]
        assert step.expected_output == {"requiresGuideDecision": True}


def _planned_steps_as_plan(state, message):
    with patch("app.settings.settings.shopping_state_authority", "legacy"):
        ctx = build_planner_context(
            state, user_message=message,
            candidate_tool_schemas=_rerank_tool_schemas(),
        )
    result = _deterministic_shopping_guide_plan(
        ctx, plan_id_factory=lambda: "plan-rerank"
    )
    assert result is not None and result.outcome == "planned"
    return result.plan.steps


# ── §6.5 / §6.9 decision layer ───────────────────────────────────────────────


class TestDecisionScopeRerank:
    def _decide(self, state, message):
        from app.llm import _deterministic_used_phone_task_state_decision
        return _deterministic_used_phone_task_state_decision(state, message)

    def test_intent_only_update_preserves_requirements(self):
        state = _state()
        args, obs = self._decide(state, "这其中哪一个拍照效果最好")
        req = (obs or {}).get("_scopeRerankRequest")
        assert req is not None
        assert req["rankingIntent"] == "camera_title_claim"
        guide = args["domainStatePatch"]["shoppingGuide"]
        assert guide["upsertRequirements"] == [_OS_REQ]
        assert guide["removeRequirementKeys"] == []

    def test_no_scope_clarifies(self):
        state = _state(with_scope=False, with_request=False)
        args, obs = self._decide(state, "这其中哪一个拍照效果最好")
        assert args["status"] == "collecting_information"
        assert obs["route"] == "deterministic_scope_rerank_clarification"
        assert "请先完成一次商品检索" in args["pendingQuestions"][0]

    def test_base_condition_change_produces_no_rerank(self):
        state = _state()
        args, obs = self._decide(state, "我改要安卓的，华为优先")
        assert (obs or {}).get("_scopeRerankRequest") is None

    def test_snapshot_drift_produces_no_rerank(self):
        state = _state(scope_extra={"requirements_snapshot": [_ANDROID_REQ]})
        args, obs = self._decide(state, "这其中哪一个拍照效果最好")
        assert (obs or {}).get("_scopeRerankRequest") is None

    def test_cross_task_scope_fails_closed(self):
        state = _state(task_id="task-iphone-guide",
                       scope_extra={"task_id": "task-other-guide"})
        args, obs = self._decide(state, "这其中哪一个拍照效果最好")
        assert (obs or {}).get("_scopeRerankRequest") is None

    def test_expired_scope_fails_closed(self):
        state = _state(scope_extra={"status": "invalidated"})
        args, obs = self._decide(state, "这其中哪一个拍照效果最好")
        assert (obs or {}).get("_scopeRerankRequest") is None

    def test_measured_capability_question_falls_to_boundary(self):
        state = _state()
        args, obs = self._decide(state, "这里面哪个打游戏帧率最高")
        assert (obs or {}).get("_scopeRerankRequest") is None
        assert args["status"] == "collecting_information"

    def test_natural_deictic_question_without_validator_presentation_clarifies(self):
        state = _state()
        args, obs = self._decide(state, "这里面哪一个打游戏最好")
        assert (obs or {}).get("_scopeRerankRequest") is None
        assert obs["route"] == "deterministic_ordinal_clarification"
        assert args["status"] == "collecting_information"

    def test_natural_deictic_question_reuses_exact_comparison_receipt(self):
        state = _state()
        state.domain_state.update({
            "stepOutputs": {"step-compare": {
                "taskId": state.task_id,
                "planId": "plan-compare",
                "stepId": "step-compare",
                "values": {"productIds": [101, 102, 103]},
            }},
            "validationResult": {
                "outcome": "passed",
                "taskId": state.task_id,
                "planId": "plan-compare",
                "basedOnRevision": state.revision - 1,
                "stepResults": [{
                    "stepId": "step-compare",
                    "outcome": "satisfied",
                    "expectedOutput": {"requiresGuideDecision": True},
                    "evidenceSummary": {"requiresGuideDecision": {
                        "finalistIds": [101, 102, 103],
                    }},
                }],
            },
        })
        args, obs = self._decide(state, "这里面哪个适合我")
        assert obs["route"] == "deterministic_complete"
        assert obs["_boundComparedIds"] == [101, 102, 103]
        guide = args["domainStatePatch"]["shoppingGuide"]
        assert guide["mode"] == "compare"


# ── §6.7 Executor input boundary ─────────────────────────────────────────────


class TestExecutorScopeRerankResolved:
    def test_accepts_exact_scope_inputs(self):
        from app.executor import _validate_scope_rerank_resolved
        state = _state()
        resolved = {
            "scopeId": "scope-task-iphone-guide-3",
            "productIds": [101, 102, 103],
            "rankingIntent": "camera_title_claim",
            "category": "phone",
        }
        _validate_scope_rerank_resolved(state, resolved)  # must not raise

    def _reject(self, resolved, match):
        from app.executor import _validate_scope_rerank_resolved
        state = _state()
        with pytest.raises(Exception, match=match):
            _validate_scope_rerank_resolved(state, resolved)

    def test_rejects_scope_id_mismatch(self):
        self._reject({
            "scopeId": "scope-task-iphone-guide-OTHER",
            "productIds": [101, 102, 103],
            "rankingIntent": "camera_title_claim",
            "category": "phone",
        }, "scopeId")

    def test_rejects_reordered_ids(self):
        self._reject({
            "scopeId": "scope-task-iphone-guide-3",
            "productIds": [102, 101, 103],
            "rankingIntent": "camera_title_claim",
            "category": "phone",
        }, "完全一致")

    def test_rejects_extra_id(self):
        self._reject({
            "scopeId": "scope-task-iphone-guide-3",
            "productIds": [101, 102, 103, 104],
            "rankingIntent": "camera_title_claim",
            "category": "phone",
        }, "完全一致")

    def test_rejects_missing_scope(self):
        from app.executor import _validate_scope_rerank_resolved
        state = _state(with_scope=False, with_request=False)
        with pytest.raises(Exception, match="CandidateScope"):
            _validate_scope_rerank_resolved(state, {
                "scopeId": "scope-task-iphone-guide-3",
                "productIds": [101, 102, 103],
                "rankingIntent": "camera_title_claim",
                "category": "phone",
            })

    def test_rejects_inactive_scope(self):
        from app.executor import _validate_scope_rerank_resolved
        state = _state(scope_extra={"status": "invalidated"})
        with pytest.raises(Exception, match="已失效或跨任务"):
            _validate_scope_rerank_resolved(state, {
                "scopeId": "scope-task-iphone-guide-3",
                "productIds": [101, 102, 103],
                "rankingIntent": "camera_title_claim",
                "category": "phone",
            })

    def test_rejects_cross_task_scope(self):
        from app.executor import _validate_scope_rerank_resolved
        state = _state()
        state.domain_state["candidateScope"] = _scope(task_id="task-other-guide")
        with pytest.raises(Exception, match="已失效或跨任务"):
            _validate_scope_rerank_resolved(state, {
                "scopeId": "scope-task-iphone-guide-3",
                "productIds": [101, 102, 103],
                "rankingIntent": "camera_title_claim",
                "category": "phone",
            })

    def test_rejects_wrong_category(self):
        self._reject({
            "scopeId": "scope-task-iphone-guide-3",
            "productIds": [101, 102, 103],
            "rankingIntent": "camera_title_claim",
            "category": "laptop",
        }, "category")

    def test_rejects_uncontrolled_intent(self):
        self._reject({
            "scopeId": "scope-task-iphone-guide-3",
            "productIds": [101, 102, 103],
            "rankingIntent": "best_price_claim",
            "category": "phone",
        }, "意图")


# ── §6.1 / §6.2 / §6.8 Validator scope materialization ───────────────────────


class TestValidatorScopeMaterialization:
    def _search_state(self, *, ids=(101, 102, 103), pool=(101, 102, 103, 104, 105),
                      task_id="task-iphone-guide", revision=3, plan_id="plan-search",
                      step_id="step-search", tamper=None):
        """A TaskState whose active single-step search plan carries the passed
        Validator evidence summary (optionally tampered) plus the normalized
        search output — the exact input ``persist_validator_result`` consumes.
        """
        from app.executor import NormalizedStepOutput
        from app.planning import PlanStep, TaskPlan
        from app.validator import StepValidationResult, ValidatorResult
        values = normalize_search_products_detail(
            two_stage_search_detail(list(ids), candidate_pool_ids=list(pool)),
            requirements=[_OS_REQ], category="手机",
        ).normalized_values()
        output = NormalizedStepOutput(
            taskId=task_id, planId=plan_id, stepId=step_id, values=values,
        )
        plan = TaskPlan(
            planId=plan_id, basedOnRevision=revision, status="active",
            steps=[PlanStep(
                stepId=step_id, description="search", toolName="search_products",
                arguments={"query": "iOS手机"},
                argumentSources={"query": {"kind": "task_goal"}},
                expectedOutput={"requiresProductCandidates": True},
                status="executed",
            )],
        )
        expected_summary = {
            "candidatePoolCount": len(values["candidatePoolIds"]),
            "rankedItemCount": len(values["rankedItemIds"]),
            "candidatePoolIds": values["candidatePoolIds"],
            "rankedItemIds": values["rankedItemIds"],
            "productIds": values["productIds"],
            "evidenceRefs": values["evidenceRefs"],
            **values["candidateSupport"],
        }
        if tamper:
            expected_summary = tamper(expected_summary)
        validation = ValidatorResult(
            outcome="passed", taskId=task_id, planId=plan_id,
            basedOnRevision=revision,
            stepResults=[StepValidationResult(
                stepId=step_id, outcome="satisfied",
                expectedOutput={"requiresProductCandidates": True},
                evidenceSummary={"requiresProductCandidates": expected_summary},
            )],
        )
        return output, plan, validation

    def _persist_state(self, output, plan, validation, *, task_id="task-iphone-guide",
                       revision=3, extra_domain=None):
        now = datetime.now(timezone.utc)
        domain = {
            "shoppingGuide": _guide(requirements=[_OS_REQ]),
            "stepOutputs": {plan.steps[0].step_id: output.model_dump(
                by_alias=True, mode="json")},
        }
        if extra_domain:
            domain.update(extra_domain)
        return TaskState(
            taskId=task_id, taskType="ecommerce_guide", status="ready",
            revision=revision, goal="iOS手机", activePlan=plan,
            domainState=domain, createdAt=now, updatedAt=now,
        )

    def _run_persist(self, state, validation):
        from app.validator import persist_validator_result
        captured = {}

        async def persist(_task_id, patch):
            captured["patch"] = patch
            return state

        with patch("app.validator.update_task_state", new=persist):
            asyncio.run(persist_validator_result(state, validation))
        return captured["patch"]

    def test_passed_search_materializes_scope(self):
        output, plan, validation = self._search_state()
        state = self._persist_state(output, plan, validation)
        patch = self._run_persist(state, validation)
        scope_raw = patch.domain_state_patch["candidateScope"]
        scope = CandidateScope.model_validate(scope_raw)
        assert scope.scope_id == "scope-task-iphone-guide-3"
        assert scope.task_id == "task-iphone-guide"
        assert scope.candidate_pool_ids == [101, 102, 103, 104, 105]
        assert scope.ranked_item_ids == [101, 102, 103]
        assert scope.visible_product_ids == [101, 102, 103]
        assert [
            item.model_dump(mode="json") for item in scope.requirements_snapshot
        ] == [_OS_REQ]
        assert scope.status == "active"

    def test_identity_mismatch_rejected(self):
        from app.validator import ValidatorEvidenceError
        output, plan, validation = self._search_state(task_id="task-other-guide")
        state = self._persist_state(output, plan, validation)
        with pytest.raises(ValidatorEvidenceError, match="快照不匹配"):
            self._run_persist(state, validation)

    def test_forged_presentation_id_rejected(self):
        from app.validator import ValidatorEvidenceError

        def tamper(summary):
            mutated = dict(summary)
            mutated["productPresentations"] = [
                dict(card, productId=999) if idx == 0 else dict(card)
                for idx, card in enumerate(summary["productPresentations"])
            ]
            return mutated

        output, plan, validation = self._search_state(tamper=tamper)
        state = self._persist_state(output, plan, validation)
        with pytest.raises(ValidatorEvidenceError, match="不能物化 scope"):
            self._run_persist(state, validation)

    def test_new_search_invalidates_previous_scope(self):
        output, plan, validation = self._search_state(
            ids=(201, 202, 203), pool=(201, 202, 203, 204), revision=4,
        )
        old_scope = _scope(scope_id="scope-task-iphone-guide-3",
                           pool=[101, 102, 103, 104, 105])
        state = self._persist_state(output, plan, validation, revision=4,
                                    extra_domain={"candidateScope": old_scope})
        patch = self._run_persist(state, validation)
        invalidation = patch.domain_state_patch["candidateScopeInvalidation"]
        assert invalidation["scopeId"] == "scope-task-iphone-guide-3"
        assert invalidation["status"] == "invalidated"
        assert invalidation["invalidationReason"] == (
            "new_full_catalog_search_replaced_scope"
        )
        new_scope = CandidateScope.model_validate(
            patch.domain_state_patch["candidateScope"]
        )
        assert new_scope.scope_id == "scope-task-iphone-guide-4"


# ── §6.8 rerank Validator + §5 disclosure card ───────────────────────────────


class TestRerankValidatorAndHarness:
    def _rerank_state(self, *, scope_id="scope-task-iphone-guide-3",
                      reranked=(102, 101, 103), task_id="task-iphone-guide",
                      mode="harness"):
        """A passed single-step in-scope rerank turn.

        ``mode="harness"`` models the post-persistence revision (plan completed,
        revision = basedOnRevision + 1) exactly as the harness consumes it;
        ``mode="persist"`` models the pre-persistence revision the Validator
        inspects (plan still active, revision == basedOnRevision).
        """
        from app.executor import NormalizedStepOutput
        from app.planning import PlanStep, TaskPlan
        from app.validator import StepValidationResult, ValidatorResult
        assert mode in ("harness", "persist")
        based_on_revision = 4
        plan_status = "completed" if mode == "harness" else "active"
        revision = 5 if mode == "harness" else 4

        detail = _rerank_detail(scope_id, reranked)
        rerank_output = normalize_scope_rerank_detail(
            detail, requirements=[_OS_REQ], category="phone",
        )
        values = rerank_output.normalized_values()
        output = NormalizedStepOutput(
            taskId=task_id, planId="plan-rerank", stepId="step-rerank", values=values,
        )
        plan = TaskPlan(
            planId="plan-rerank", basedOnRevision=based_on_revision,
            status=plan_status,
            steps=[PlanStep(
                stepId="step-rerank", description="rerank",
                toolName="rerank_products_in_scope",
                arguments={"scopeId": scope_id, "productIds": [101, 102, 103],
                           "rankingIntent": "camera_title_claim",
                           "category": "phone",
                           "requirements": deepcopy([_OS_REQ])},
                argumentSources={
                    "scopeId": {"kind": "shopping_guide", "reference": "scopeId"},
                    "productIds": {"kind": "shopping_guide",
                                   "reference": "scopeRankedItemIds"},
                    "rankingIntent": {"kind": "shopping_guide",
                                      "reference": "rankingIntent"},
                    "category": {"kind": "shopping_guide", "reference": "categoryCode"},
                    "requirements": {"kind": "shopping_guide", "reference": "requirements"},
                },
                expectedOutput={"requiresScopeRerank": True},
                status="executed",
            )],
        )
        support = dict(values["candidateSupport"])
        expected_summary = {
            "scopeId": scope_id,
            "inputCount": len(values["inputProductIds"]),
            "outputCount": len(values["rankedItemIds"]),
            "rankedItemIds": values["rankedItemIds"],
            "productIds": values["rankedItemIds"],
            "evidenceRefs": values["evidenceRefs"],
            **support,
        }
        validation = ValidatorResult(
            outcome="passed", taskId=task_id, planId="plan-rerank",
            basedOnRevision=based_on_revision,
            stepResults=[StepValidationResult(
                stepId="step-rerank", outcome="satisfied",
                expectedOutput={"requiresScopeRerank": True},
                evidenceSummary={"requiresScopeRerank": expected_summary},
            )],
        )
        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId=task_id, taskType="ecommerce_guide", status="ready",
            revision=revision, goal="这里面哪一个拍照更好", activePlan=plan,
            domainState={
                "shoppingGuide": _guide(requirements=[_OS_REQ]),
                "candidateScope": _scope(scope_id=scope_id),
                "stepOutputs": {"step-rerank": output.model_dump(
                    by_alias=True, mode="json")},
                "validationResult": validation.model_dump(by_alias=True, mode="json"),
            },
            createdAt=now, updatedAt=now,
        )
        return state

    def test_harness_rerank_card_discloses_snapshot_boundary(self):
        from app.harness import build_validated_guide_result
        state = self._rerank_state()
        result = build_validated_guide_result(state)
        assert result is not None
        assert "上一轮候选的商品标题/公开文本" in result["snapshotNotice"]
        assert "不代表真实相机、性能等能力结论" in result["snapshotNotice"]
        assert result["rankedItemCount"] == 3
        assert result["products"]
        assert result["products"][0]["product"]["id"] == "102"

    def test_harness_build_validated_results_rerank_matches_normalized(self):
        from app.harness import _build_validated_results
        state = self._rerank_state()
        results = _build_validated_results(state, [])
        assert len(results) == 1
        assert results[0]["tool"] == "rerank_products_in_scope"
        summary = results[0]["validationSummary"]["requiresScopeRerank"]
        assert summary["scopeId"] == "scope-task-iphone-guide-3"
        assert summary["rankedItemIds"] == [102, 101, 103]
        assert results[0]["evidence"]["noFullSearch"] is True

    def test_rerank_validator_output_outside_scope_rejected(self):
        from app.validator import (
            ValidatorEvidenceError,
            ValidatorResult,
            persist_validator_result,
        )
        state = self._rerank_state(reranked=(101, 102, 999), mode="persist")
        validation = ValidatorResult.model_validate(
            state.domain_state["validationResult"]
        )
        captured = {}

        async def persist(_task_id, patch):
            captured["patch"] = patch
            return state

        with patch("app.validator.update_task_state", new=persist):
            with pytest.raises(ValidatorEvidenceError, match="范围重排输出必须属于"):
                asyncio.run(persist_validator_result(state, validation))
        # The scope boundary rejected the forged output before any persistence:
        # no TaskStatePatch was ever produced.
        assert "patch" not in captured


# ── §6.9b tool-level regression: rerank resolves a 20-ID scope in 10-ID batches ──
#
# Attempt-002 E2E caught a real bug: rerank_products_in_scope_tool handed all 20
# rankedItemIds to get_product_details_tool, which only resolves the first 10
# (the Java /api/products/resolve gateway is batched at 10).  The exact-set
# identity gate then failed with scope_product_identity_mismatch.  These tests
# pin the fix: the tool must fetch in ≤10-ID batches, merge, and only then run
# the identity gate.


def _resolve_fake(requested_ids: list[int]):
    """Return a resolve-shaped product payload for exactly the requested batch."""

    products = []
    for index, item in enumerate(requested_ids):
        camera_claim = index < 5  # first five carry a camera title claim
        products.append({
            "id": item,
            "title": f"手机 {item} 拍照相机摄影" if camera_claim else f"手机 {item}",
            "categoryL1": "手机",
            "categoryL2": "二手手机",
            "categoryL3": "智能手机",
            "source": "public",
            "attributes": [{
                "key": "os",
                "rawValue": "iOS",
                "normalizedText": "ios",
                "evidenceField": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                "extractionMethod": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
            }],
        })
    return SimpleNamespace(
        tool="get_product_details",
        ok=True,
        durationMs=1.0,
        detail={
            "products": products,
            "productIds": [item["id"] for item in products],
            "requestedProductIds": list(requested_ids),
        },
    )


class TestRerankProductsInScopeToolBatching:
    def test_20_id_scope_fetches_two_ten_id_batches_and_reranks(self):
        from app.domains.ecommerce.tools import rerank_products_in_scope_tool

        ids = list(range(1001, 1021))
        calls: list[list[int]] = []

        async def fake_resolve(product_ids):
            calls.append(list(product_ids))
            return _resolve_fake(product_ids)

        with patch(
            "app.domains.ecommerce.tools.get_product_details_tool",
            side_effect=fake_resolve,
        ):
            result = asyncio.run(rerank_products_in_scope_tool(
                scope_id="scope-task-iphone-guide-3",
                product_ids=ids,
                ranking_intent="camera_title_claim",
                category="phone",
                requirements=[_OS_REQ],
            ))

        # The resolve gateway is batched at 10: exactly two batches covering all 20.
        assert calls == [ids[:10], ids[10:]]
        assert result.ok is True
        detail = result.detail
        assert detail["contractVersion"] == SCOPE_RERANK_CONTRACT_VERSION
        assert detail["noFullSearch"] is True
        ranked = detail["rankedItemIds"]
        assert len(ranked) == 20
        assert set(ranked) == set(ids)
        # Camera-claim titles lead the reorder; ties resolve deterministically.
        assert ranked[:5] == ids[:5]

    def test_dropped_id_in_any_batch_fails_closed(self):
        from app.domains.ecommerce.tools import rerank_products_in_scope_tool

        ids = list(range(1001, 1021))
        calls: list[list[int]] = []

        async def fake_resolve_drops_second_batch(product_ids):
            calls.append(list(product_ids))
            if len(calls) == 2:  # second 10-ID batch silently loses one product
                requested_ids = list(product_ids)[:-1]
            else:
                requested_ids = list(product_ids)
            return _resolve_fake(requested_ids)

        with patch(
            "app.domains.ecommerce.tools.get_product_details_tool",
            side_effect=fake_resolve_drops_second_batch,
        ):
            result = asyncio.run(rerank_products_in_scope_tool(
                scope_id="scope-task-iphone-guide-3",
                product_ids=ids,
                ranking_intent="camera_title_claim",
                category="phone",
                requirements=[_OS_REQ],
            ))

        assert result.ok is False
        assert result.detail["code"] == "scope_product_identity_mismatch"
        assert len(calls) == 2


# ── §6.9c tool-level regression: rerank validator evidence stays in budget ─────
#
# Attempt-002 E2E caught a second real bug: after the rerank tool succeeded, the
# Validator projected the step's full candidate rows into ValidatorContextView
# as evidenceValues, blowing the protected 6000-token validator budget
# (executedSteps=75402).  The fix projects only compact scope-identity fields —
# the rerank proof lives in the normalized output, never in candidate bodies.


class TestRerankValidatorEvidenceInBudget:
    def _executed_rerank_state(self, *, ranked=tuple(range(101, 121)),
                               detail=None):
        """A completed single-step rerank turn with the full tool detail
        persisted in stepExecutionResults — exactly what the harness Validator
        consumed when it blew the budget."""
        from app.executor import NormalizedStepOutput, StepExecutionResult
        from app.planning import PlanStep, TaskPlan
        from app.schemas import ToolTrace

        scope_id = "scope-task-iphone-guide-3"
        ranked = list(ranked)
        if detail is None:
            detail = _rerank_detail(scope_id, ranked)
        rerank_output = normalize_scope_rerank_detail(
            detail, requirements=[_OS_REQ], category="phone",
        )
        values = rerank_output.normalized_values()
        output = NormalizedStepOutput(
            taskId="task-iphone-guide", planId="plan-rerank",
            stepId="step-rerank", values=values,
        )
        plan = TaskPlan(
            planId="plan-rerank", basedOnRevision=4, status="completed",
            steps=[PlanStep(
                stepId="step-rerank", description="rerank",
                toolName="rerank_products_in_scope",
                arguments={"scopeId": scope_id, "productIds": ranked,
                           "rankingIntent": "camera_title_claim",
                           "category": "phone",
                           "requirements": deepcopy([_OS_REQ])},
                argumentSources={
                    "scopeId": {"kind": "shopping_guide", "reference": "scopeId"},
                    "productIds": {"kind": "shopping_guide",
                                   "reference": "scopeRankedItemIds"},
                    "rankingIntent": {"kind": "shopping_guide",
                                      "reference": "rankingIntent"},
                    "category": {"kind": "shopping_guide", "reference": "categoryCode"},
                    "requirements": {"kind": "shopping_guide", "reference": "requirements"},
                },
                expectedOutput={"requiresScopeRerank": True},
                status="executed",
            )],
        )
        now = datetime.now(timezone.utc)
        execution = StepExecutionResult(
            taskId="task-iphone-guide", planId="plan-rerank",
            stepId="step-rerank", toolName="rerank_products_in_scope",
            resolvedArguments={
                "scopeId": scope_id, "productIds": ranked,
                "rankingIntent": "camera_title_claim", "category": "phone",
                "requirements": deepcopy([_OS_REQ]),
            },
            outcome="tool_succeeded",
            toolTrace=ToolTrace(
                tool="rerank_products_in_scope", ok=True, detail=detail,
            ),
            startedAt=now, finishedAt=now, durationMs=1.0,
        )
        state = TaskState(
            taskId="task-iphone-guide", taskType="ecommerce_guide", status="ready",
            revision=5, goal="这里面哪一个拍照更好", activePlan=plan,
            domainState={
                "shoppingGuide": _guide(requirements=[_OS_REQ]),
                "candidateScope": _scope(scope_id=scope_id),
                "stepExecutionResults": [
                    execution.model_dump(by_alias=True, mode="json")],
                "stepOutputs": {"step-rerank": output.model_dump(
                    by_alias=True, mode="json")},
            },
            createdAt=now, updatedAt=now,
        )
        return state

    def test_executed_steps_projection_excludes_candidate_rows(self):
        from app.harness import _build_executed_steps
        state = self._executed_rerank_state()
        steps = _build_executed_steps(state)
        assert len(steps) == 1
        assert steps[0]["toolName"] == "rerank_products_in_scope"
        values = steps[0]["evidenceValues"]
        # The full candidate rows + evidence must NOT enter ValidatorContextView.
        assert "candidates" not in values
        assert "evidence" not in values
        assert values["scopeId"] == "scope-task-iphone-guide-3"
        assert values["rankedItemIds"] == list(range(101, 121))
        assert values["noFullSearch"] is True
        # Evidence refs flow through the normalized output's own compact list
        # (the Validator's rerank proof), never through the projected step
        # evidence — the raw detail carries one ref per supporting fact (~256
        # for a 20-item scope) and three copies of it blew the phase budget.
        assert steps[0]["evidenceRefs"] == []
        assert "evidenceRefs" not in values
        assert len(steps[0]["normalizedOutput"]["evidenceRefs"]) == 20

    def test_validator_view_stays_under_budget_for_twenty_item_scope(self):
        from app.context_pack import build_context_pack
        from app.context_view import (
            ContextProjector,
            ContextViewBudgetExceeded,
        )
        from app.harness import _build_executed_steps
        state = self._executed_rerank_state()
        executed_steps = _build_executed_steps(state)
        # Mirror the real turn: the round-1 search step also sits in scope.
        executed_steps.append({
            "stepId": "step-search", "toolName": "search_products",
            "outcome": "tool_succeeded", "resolvedArguments": {},
            "evidenceRefs": [], "detailKeys": [], "evidenceValues": {},
            "normalizedOutput": {},
        })
        with patch("app.context_pack.settings.shopping_state_authority", "legacy"):
            pack = asyncio.run(build_context_pack(
                state,
                allowed_tools=["search_products", "rerank_products_in_scope"],
                history=[],
                run_id="run-test",
            ))
        projector = ContextProjector(pack)
        try:
            projector.validator_view(
                plan=state.active_plan,
                executed_steps=executed_steps,
                phase_task_revision=state.revision,
            )
        except ContextViewBudgetExceeded:
            pytest.fail(
                "validator view exceeded its token budget for a 20-item rerank scope"
            )

    def test_validator_view_stays_under_budget_for_real_shaped_evidence(self):
        """Attempt-002 E2E caught a residual overflow on top of candidate-row
        compaction: the raw rerank detail carries ~256 evidence refs (one per
        supporting fact across 20 ranked items).  Projecting them kept
        executedSteps=9029 and evidenceRefs=3133 even with compact rows, and
        12742 > 6000.  The rerank proof needs only the normalized output's own
        20-ref list, so the raw ref list must never enter ValidatorContextView."""
        from app.context_pack import build_context_pack
        from app.context_view import (
            ContextProjector,
            ContextViewBudgetExceeded,
        )
        from app.harness import _build_executed_steps

        rank = list(range(101, 121))
        refs: list[str] = []
        detail = _rerank_detail("scope-task-iphone-guide-3", rank)
        for item in rank:
            item_refs = [
                f"product:{item}:title", f"product:{item}:attribute:os",
            ] + [
                f"product:{item}:{f}" for f in (
                    "price", "condition", "battery", "camera", "storage",
                    "color", "ram", "warranty", "version", "repair",
                    "screen", "brand")
            ]
            refs.extend(item_refs)
            row = next(c for c in detail["candidates"] if c["id"] == item)
            row["evidenceRefs"] = item_refs
        # Contract: top-level evidenceRefs == evidence row refs == union of
        # ranked candidate evidenceRefs, all in order; citation trace must be
        # bound to the actual evidence count.  The title / attribute:os rows
        # keep their source-bound shape (the controlled-fact check at
        # _candidate_support reads method + rawValue); extra refs are appended.
        detail["evidenceRefs"] = refs
        original_by_ref = {e["ref"]: e for e in detail["evidence"]}
        detail["evidence"] = []
        for item in rank:
            for ref in (f"product:{item}:title", f"product:{item}:attribute:os"):
                detail["evidence"].append(original_by_ref[ref])
            for f in ("price", "condition", "battery", "camera", "storage",
                      "color", "ram", "warranty", "version", "repair",
                      "screen", "brand"):
                detail["evidence"].append({
                    "ref": f"product:{item}:{f}", "field": f, "rawValue": "real",
                })
        detail["citationTrace"]["evidenceRefCount"] = len(refs)
        assert len(refs) == 14 * 20  # real E2E carried 256 refs for 20 items

        state = self._executed_rerank_state(detail=detail)
        executed_steps = _build_executed_steps(state)
        # The raw 256-ref list is dropped from the projected step evidence; the
        # proof refs live in the normalized output (20 refs).
        assert executed_steps[0]["evidenceRefs"] == []
        assert "evidenceRefs" not in executed_steps[0]["evidenceValues"]
        assert len(executed_steps[0]["normalizedOutput"]["evidenceRefs"]) == 20

        with patch("app.context_pack.settings.shopping_state_authority", "legacy"):
            pack = asyncio.run(build_context_pack(
                state,
                allowed_tools=["search_products", "rerank_products_in_scope"],
                history=[],
                run_id="run-real-shaped",
            ))
        projector = ContextProjector(pack)
        try:
            view = projector.validator_view(
                plan=state.active_plan,
                executed_steps=executed_steps,
                phase_task_revision=state.revision,
            )
        except ContextViewBudgetExceeded:
            pytest.fail(
                "validator view exceeded its token budget with real-shaped "
                "256-ref rerank evidence"
            )
        assert view.evidence_refs == []

    def test_scope_rerank_validator_accepts_plan_argument_source_models(self):
        """Attempt-002 E2E caught a crash the budget tests masked: in-memory
        PlanStep.argument_sources values are PlanArgumentSource pydantic models,
        but _validate_scope_rerank called .get('reference') on the value.  The
        earlier E2E runs never reached this validator (the view budget blew up
        first); once the view fit, the real in-memory shape raised
        AttributeError.  The validator must read `reference` through the model
        attribute, matching the direct-compare validator at line ~1289."""
        from app.executor import NormalizedStepOutput, StepExecutionResult
        from app.validator import ValidatorStepContext, _validate_scope_rerank

        state = self._executed_rerank_state()
        plan = state.active_plan
        step = plan.steps[0]
        # The real in-memory shape: pydantic coerced the plain dicts into
        # PlanArgumentSource models.
        assert type(step.argument_sources["productIds"]).__name__ == (
            "PlanArgumentSource"
        )
        execution = StepExecutionResult.model_validate(
            state.domain_state["stepExecutionResults"][0]
        )
        normalized = NormalizedStepOutput.model_validate(
            state.domain_state["stepOutputs"][step.step_id]
        )
        ctx = ValidatorStepContext(
            step=step, execution_result=execution, normalized_output=normalized,
        )
        outcome, code, reason, summary = _validate_scope_rerank(ctx)
        assert outcome == "satisfied"
        assert summary["scopeId"] == "scope-task-iphone-guide-3"
        assert summary["rankedItemIds"] == list(range(101, 121))
