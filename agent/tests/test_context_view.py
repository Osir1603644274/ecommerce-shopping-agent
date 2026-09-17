"""Tests for ContextProjector and all 5 ContextViews — determinism, hashing, fields."""

import asyncio

import pytest

from app.context_pack import ContextPack, TaskFact, TaskConstraint, build_context_pack
from app.context_view import (
    ContextProjector,
    PlannerContextView,
    ExecutorContextView,
    ValidatorContextView,
    ReplannerContextView,
    FinalAnswerContextView,
    ContextViewBudgetExceeded,
    _view_hash,
    _derive_view_hash,
)
from app.task_state import TaskState


def _make_pack(**overrides):
    """Build a minimal ContextPack for projector tests."""
    values = {
        "run_id": "run-001",
        "task_id": "task-001",
        "base_context_revision": 3,
        "goal": "帮我选一款降噪耳机，预算3000以内",
        "task_type": "ecommerce_guide",
        "confirmed_facts": [
            TaskFact(key="category", value="headphones", certainty="confirmed", source="user"),
            TaskFact(key="budget", value=300000, certainty="confirmed", source="user"),
        ],
        "hard_constraints": [
            TaskConstraint(key="price_max", operator="lte", value=300000, source="user"),
        ],
        "soft_preferences": [
            {"key": "color", "value": "black", "source": "inferred:偏好黑色"},
        ],
        "unknowns": ["wireless", "anc"],
        "pending_questions": [],
        "allowed_tools": ["search_products", "get_product_details"],
        "evidence_refs": ["ref-1", "ref-2"],
        "history_summaries": [],
    }
    values.update(overrides)
    return ContextPack(**values)


class TestViewHashing:
    """Deterministic hashing guarantees: same input → same hash always."""

    def test_current_turn_execution_is_hash_bound_not_historical_proof(self):
        projector = ContextProjector(_make_pack())
        old = projector.final_answer_view(validated_results=[])
        view = projector.final_answer_view(validated_results=[],
            current_turn_tool_names=["search_products", "compare_products"])
        assert "currentTurnExecution" not in old.answer_format
        assert view.answer_format["currentTurnExecution"] == {
            "runId": "run-001", "taskId": "task-001",
            "successfulToolCalls": ["search_products", "compare_products"],
            "productFactsRequireValidatedResults": True,
        }
        assert old.context_hash != view.context_hash
        assert view.validated_results == []

    def test_same_pack_same_planner_hash(self):
        pack = _make_pack()
        p1 = ContextProjector(pack)
        p2 = ContextProjector(pack)
        v1 = p1.planner_view(tool_names=["t1", "t2"], task_status="ready")
        v2 = p2.planner_view(tool_names=["t1", "t2"], task_status="ready")
        assert v1.context_hash == v2.context_hash

    def test_different_pack_different_hash(self):
        p1 = ContextProjector(_make_pack(run_id="r1", goal="帮我选耳机"))
        p2 = ContextProjector(_make_pack(run_id="r2", goal="帮我选音箱"))
        v1 = p1.planner_view(tool_names=["t1"], task_status="ready")
        v2 = p2.planner_view(tool_names=["t1"], task_status="ready")
        assert v1.context_hash != v2.context_hash

    def test_different_view_type_different_hash(self):
        projector = ContextProjector(_make_pack())
        pv = projector.planner_view(tool_names=["t1"], task_status="ready")
        ev = projector.executor_view(
            plan_id="plan-1", step_id="step-1",
            step_description="搜索耳机", tool_name="search_products",
        )
        assert pv.context_hash != ev.context_hash

    def test_same_view_params_same_hash(self):
        pack = _make_pack()
        p1 = ContextProjector(pack)
        p2 = ContextProjector(pack)
        v1 = p1.validator_view(executed_steps=[
            {"stepId": "s1", "toolName": "search", "outcome": "tool_succeeded", "evidenceRefs": [], "detailKeys": []},
        ])
        v2 = p2.validator_view(executed_steps=[
            {"stepId": "s1", "toolName": "search", "outcome": "tool_succeeded", "evidenceRefs": [], "detailKeys": []},
        ])
        assert v1.context_hash == v2.context_hash

    def test_projector_detects_tampered_view_payload(self):
        projector = ContextProjector(_make_pack())
        original = projector.planner_view(
            tool_names=["search_products"], task_status="ready"
        )
        tampered = original.model_copy(update={"goal": "tampered goal"})

        assert projector.verifies("planner", original) is True
        assert projector.verifies("planner", tampered) is False


class TestPlannerView:
    def test_all_fields_present(self):
        projector = ContextProjector(_make_pack())
        view = projector.planner_view(tool_names=["search_products"], task_status="ready")
        assert view.schema_version == "1.0"
        assert view.run_id == "run-001"
        assert view.task_id == "task-001"
        assert view.base_context_revision == 3
        assert view.context_hash != ""
        assert view.goal.startswith("帮我选")
        assert view.task_status == "ready"
        assert len(view.confirmed_facts) == 2
        assert len(view.hard_constraints) == 1
        assert len(view.soft_preferences) == 1
        assert view.allowed_tool_names == ["search_products"]
        assert view.unknowns == ["wireless", "anc"]

    def test_tool_names_empty(self):
        projector = ContextProjector(_make_pack())
        view = projector.planner_view(tool_names=[], task_status="executing")
        assert view.allowed_tool_names == []


class TestExecutorView:
    def test_all_fields_present(self):
        projector = ContextProjector(_make_pack())
        tool_schema = {
            "function": {
                "name": "search_products",
                "description": "Search product catalog",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
        view = projector.executor_view(
            plan_id="plan-1",
            step_id="step-1",
            step_description="搜索降噪耳机",
            tool_name="search_products",
            tool_schema=tool_schema,
            resolved_arguments={"query": "降噪耳机"},
            required_fact_keys=["category"],
            required_constraint_keys=["price_max"],
            prior_step_outputs={"step-0": {"output": "ok"}},
        )
        assert view.plan_id == "plan-1"
        assert view.step_id == "step-1"
        assert view.tool_name == "search_products"
        assert view.tool_schema is not None
        assert view.tool_schema.name == "search_products"
        assert view.tool_schema.parameters == {"type": "object", "properties": {"query": {"type": "string"}}}
        assert view.resolved_arguments == {"query": "降噪耳机"}
        assert len(view.relevant_facts) == 1
        assert view.relevant_facts[0]["key"] == "category"
        assert len(view.relevant_constraints) == 1

    def test_no_filter_shows_all(self):
        projector = ContextProjector(_make_pack())
        view = projector.executor_view(
            plan_id="p1", step_id="s1",
            step_description="test", tool_name="t1",
        )
        assert len(view.relevant_facts) == 2  # all facts
        assert len(view.relevant_constraints) == 1  # all constraints

    def test_explicit_empty_filters_show_no_unreferenced_state(self):
        projector = ContextProjector(_make_pack())
        view = projector.executor_view(
            plan_id="p1",
            step_id="s1",
            step_description="search",
            tool_name="search_products",
            required_fact_keys=[],
            required_constraint_keys=[],
        )

        assert view.relevant_facts == []
        assert view.relevant_constraints == []

    def test_null_tool_schema(self):
        projector = ContextProjector(_make_pack())
        view = projector.executor_view(
            plan_id="p1", step_id="s1", step_description="test", tool_name="t1",
        )
        assert view.tool_schema is None


class TestValidatorView:
    def test_all_fields_present(self):
        projector = ContextProjector(_make_pack())
        view = projector.validator_view(
            executed_steps=[
                {"stepId": "s1", "toolName": "search_products", "outcome": "tool_succeeded",
                 "evidenceRefs": ["r1"], "detailKeys": ["products"]},
                {"stepId": "s2", "toolName": "get_product_details", "outcome": "tool_succeeded",
                 "evidenceRefs": ["r2"], "detailKeys": ["specs"]},
            ],
            unmet_constraints=["price_max"],
        )
        assert view.goal.startswith("帮我选")
        assert len(view.executed_steps) == 2
        assert view.executed_steps[0].step_id == "s1"
        assert view.executed_steps[0].tool_name == "search_products"
        assert view.executed_steps[0].outcome == "tool_succeeded"
        assert view.executed_steps[0].evidence_refs == ["r1"]
        assert view.evidence_refs == ["r1", "r2"]
        assert view.unmet_constraints == ["price_max"]

    def test_empty_steps(self):
        projector = ContextProjector(_make_pack())
        view = projector.validator_view(executed_steps=[])
        assert view.executed_steps == []


class TestReplannerView:
    def test_all_fields_present(self):
        projector = ContextProjector(_make_pack())
        view = projector.replanner_view(
            failed_plan_summary={"planId": "plan-1", "status": "failed", "steps": []},
            failure_reason="Validator rejected: insufficient evidence",
            unsatisfied_constraints=["price_max"],
            remaining_tool_names=["search_products"],
            replan_attempt=2,
        )
        assert view.failed_plan_summary["planId"] == "plan-1"
        assert "insufficient evidence" in view.failure_reason
        assert view.unsatisfied_constraints == ["price_max"]
        assert view.remaining_tool_names == ["search_products"]
        assert view.replan_attempt == 2
        assert len(view.confirmed_facts) == 2
        assert view.unknowns == ["wireless", "anc"]


class TestFinalAnswerView:
    def test_all_fields_present(self):
        projector = ContextProjector(_make_pack())
        view = projector.final_answer_view(
            validated_results=[
                {"product": "Sony WH-1000", "score": 0.95},
                {"product": "Bose QC", "score": 0.88},
            ],
            evidence_refs=["validated-ref-1", "validated-ref-2"],
        )
        assert view.goal.startswith("帮我选")
        assert len(view.validated_results) == 2
        assert view.evidence_refs == ["validated-ref-1", "validated-ref-2"]
        assert view.answer_format["maxProducts"] == 3
        assert view.answer_format["requireEvidenceRefs"] is True
        assert view.answer_format["requireUnknownsDisclosure"] is True

    def test_no_unknowns_disclosure(self):
        projector = ContextProjector(_make_pack(unknowns=[]))
        view = projector.final_answer_view(validated_results=[])
        assert view.answer_format["requireUnknownsDisclosure"] is False

    def test_allowed_facts(self):
        projector = ContextProjector(_make_pack())
        view = projector.final_answer_view(validated_results=[])
        assert len(view.allowed_facts) == 2

    def test_comparison_answer_format_preserves_source_display_ordinals(self):
        projector = ContextProjector(_make_pack(shopping_guide_state={
            "mode": "compare",
            "category": "phone",
            "requirements": [],
            "candidateIds": [11, 22, 33],
            "comparedIds": [11, 33],
            "evidenceStatus": "complete",
        }))

        view = projector.final_answer_view(validated_results=[])

        assert view.answer_format["comparisonSelection"] == {
            "selectedProductIds": [11, 33],
            "sourceDisplayOrdinals": [1, 3],
        }


class TestContextViewDeterminism:
    """Pure projector: no LLM, no I/O, same pack = same views always."""

    def test_all_views_deterministic(self):
        pack = _make_pack()
        p1 = ContextProjector(pack)
        p2 = ContextProjector(pack)

        v1 = p1.planner_view(["t1"], "ready")
        v2 = p2.planner_view(["t1"], "ready")
        assert v1.context_hash == v2.context_hash

        e1 = p1.executor_view(plan_id="p1", step_id="s1", step_description="d", tool_name="t1")
        e2 = p2.executor_view(plan_id="p1", step_id="s1", step_description="d", tool_name="t1")
        assert e1.context_hash == e2.context_hash

        c1 = p1.validator_view(executed_steps=[])
        c2 = p2.validator_view(executed_steps=[])
        assert c1.context_hash == c2.context_hash

        r1 = p1.replanner_view(failed_plan_summary={}, failure_reason="test")
        r2 = p2.replanner_view(failed_plan_summary={}, failure_reason="test")
        assert r1.context_hash == r2.context_hash

        f1 = p1.final_answer_view(validated_results=[])
        f2 = p2.final_answer_view(validated_results=[])
        assert f1.context_hash == f2.context_hash


class TestContextViewPhaseBudgets:
    def test_planner_rejects_projection_over_its_phase_budget(self):
        projector = ContextProjector(_make_pack())
        oversized_schema = {
            "type": "function",
            "function": {
                "name": "oversized_tool",
                "description": "说明" * 20_000,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        with pytest.raises(ContextViewBudgetExceeded, match="planner ContextView"):
            projector.planner_view(
                tool_names=["oversized_tool"],
                task_status="ready",
                candidate_tool_schemas=[oversized_schema],
            )

    def test_normal_views_fit_their_independent_phase_budgets(self):
        projector = ContextProjector(_make_pack())
        projector.planner_view(["search_products"], "ready")
        projector.executor_view(
            plan_id="p1", step_id="s1", step_description="search",
            tool_name="search_products",
        )
        projector.validator_view(executed_steps=[])
        projector.replanner_view(failed_plan_summary={}, failure_reason="retry")
        projector.final_answer_view(validated_results=[])


_ECOMMERCE_GUIDE = {
    "mode": "recommend",
    "category": "phone",
    "useCases": [],
    "requirements": [
        {
            "key": "os", "operator": "eq", "value": "ios",
            "unit": "enum", "priority": "hard", "source": "user",
        }
    ],
    "candidateIds": [],
    "comparedIds": [],
    "evidenceStatus": "missing",
}


def _ecommerce_pack():
    return _make_pack(shopping_guide_state=dict(_ECOMMERCE_GUIDE))


class TestShoppingGuideSourcesInViews:
    """E2E-STAGE5-PLANNER-SOURCE-CONTRACT-001: views carry the same server source."""

    def test_planner_view_carries_mapped_sources_and_verifies(self):
        projector = ContextProjector(_ecommerce_pack())
        view = projector.planner_view(
            tool_names=["search_products"], task_status="ready"
        )

        assert view.shopping_guide_sources["category"] == "手机"
        assert view.shopping_guide_sources["requirements"][0]["value"] == "ios"
        assert projector.verifies("planner", view)

    def test_executor_view_carries_sources_and_verifies(self):
        projector = ContextProjector(_ecommerce_pack())
        view = projector.executor_view(
            plan_id="plan-1", step_id="step-1",
            step_description="检索商品", tool_name="search_products",
        )

        assert view.shopping_guide_sources["category"] == "手机"
        assert view.shopping_guide_sources["requirements"][0]["value"] == "ios"
        assert projector.verifies("executor", view)

    def test_replanner_view_carries_sources_and_verifies(self):
        projector = ContextProjector(_ecommerce_pack())
        view = projector.replanner_view(
            failed_plan_summary={}, failure_reason="insufficient_evidence"
        )

        assert view.shopping_guide_sources["category"] == "手机"
        assert view.shopping_guide_sources["requirements"][0]["value"] == "ios"
        assert projector.verifies("replanner", view)

    def test_non_ecommerce_pack_never_exposes_sources(self):
        projector = ContextProjector(_make_pack())  # no shopping_guide_state
        planner = projector.planner_view(tool_names=["t1"], task_status="ready")
        executor = projector.executor_view(
            plan_id="plan-1", step_id="step-1",
            step_description="d", tool_name="t1",
        )
        replanner = projector.replanner_view(
            failed_plan_summary={}, failure_reason="retry"
        )

        assert planner.shopping_guide_sources is None
        assert executor.shopping_guide_sources is None
        assert replanner.shopping_guide_sources is None


def _cross_domain_state_with_guide(task_type: str = "local_life") -> TaskState:
    """A non-ecommerce TaskState carrying a format-valid shopping guide."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return TaskState(
        taskId=f"task-cross-domain-{task_type}",
        taskType=task_type,
        status="ready",
        revision=3,
        goal="想找 iOS 二手机。",
        facts=[],
        constraints=[],
        unknowns=[],
        pendingQuestions=[],
        domainState={
            "shoppingGuide": dict(_ECOMMERCE_GUIDE),
        },
        createdAt=now,
        updatedAt=now,
    )


class TestCrossDomainViewIsolation:
    """E2E-STAGE5-PLANNER-SOURCE-CONTRACT-002: no view exposes a cross-domain guide."""

    def test_local_life_with_valid_guide_never_reaches_any_view(self):
        # Direct reproduction of the Codex blocking finding: taskType=local_life
        # with a complete valid ShoppingGuideState must yield None everywhere.
        pack = asyncio.run(build_context_pack(
            _cross_domain_state_with_guide("local_life"),
            allowed_tools=["t1"],
        ))
        projector = ContextProjector(pack)
        planner = projector.planner_view(tool_names=["t1"], task_status="ready")
        executor = projector.executor_view(
            plan_id="plan-1", step_id="step-1",
            step_description="d", tool_name="t1",
        )
        replanner = projector.replanner_view(
            failed_plan_summary={}, failure_reason="retry"
        )

        assert pack.shopping_guide_state is None
        assert planner.shopping_guide_sources is None
        assert executor.shopping_guide_sources is None
        assert replanner.shopping_guide_sources is None

    def test_unknown_task_type_with_valid_guide_never_reaches_views(self):
        pack = asyncio.run(build_context_pack(
            _cross_domain_state_with_guide("custom_domain"),
            allowed_tools=["t1"],
        ))
        projector = ContextProjector(pack)
        planner = projector.planner_view(tool_names=["t1"], task_status="ready")

        assert pack.shopping_guide_state is None
        assert planner.shopping_guide_sources is None
