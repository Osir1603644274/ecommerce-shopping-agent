"""Tests for ContextPack construction, truncation, and A/B compatibility."""

import asyncio

import pytest

from app.task_state import TaskState, TaskFact, TaskConstraint
from app.context_pack import (
    ContextPack,
    ContextPackBudgetExceeded,
    PHASE_TOKEN_BUDGETS,
    _build_history_summaries,
    _estimate_tokens,
    _extract_soft_preferences,
    _collect_evidence_refs,
    build_context_pack,
    context_pack_hash,
    context_pack_system_message,
    shopping_guide_argument_sources,
)
from app.settings import Settings, settings


@pytest.fixture(autouse=True)
def _legacy_authority_for_pre_migration_context_fixtures(monkeypatch):
    """These fixtures intentionally exercise the frozen compatibility ABI."""

    monkeypatch.setattr(settings, "shopping_state_authority", "legacy")


def _make_state(**overrides):
    import uuid
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    values = {
        "task_id": f"task-{uuid.uuid4().hex[:12]}",
        "task_type": "ecommerce_guide",
        "session_id": "test-session",
        "status": "ready",
        "revision": 5,
        "goal": "推荐一款预算3000元以内的降噪耳机",
        "facts": [
            TaskFact(key="category", value="headphones", certainty="confirmed", source="user"),
            TaskFact(key="budget", value=300000, certainty="confirmed", source="user"),
        ],
        "constraints": [
            TaskConstraint(key="price_minor", operator="lte", value=300000, source="user"),
        ],
        "unknowns": ["wireless"],
        "pending_questions": [],
        "domain_state": {
            "shoppingGuide": {
                "category": "headphones",
                "requirements": [
                    {
                        "key": "price_minor", "operator": "lte", "value": 300000,
                        "unit": "CNY_MINOR", "priority": "hard",
                        "source": "用户原话：预算3000以内",
                    },
                    {
                        "key": "noise_cancelling", "operator": "eq", "value": True,
                        "unit": "bool", "priority": "soft",
                        "source": "inferred:降噪需求",
                    },
                ],
            },
        },
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return TaskState(**values)


def _sample_history():
    return [
        {"role": "user", "content": "想买一个降噪耳机"},
        {"role": "assistant", "content": "好的，请问预算多少？"},
        {"role": "user", "content": "3000以内"},
        {"role": "assistant", "content": "明白了，我来搜索适合的耳机"},
    ]


class TestTokenEstimator:
    def test_cjk_estimate(self):
        assert _estimate_tokens("你好世界") >= 2

    def test_ascii_estimate(self):
        assert _estimate_tokens("hello world") >= 3

    def test_mixed_estimate(self):
        tokens = _estimate_tokens("你好 hello 世界 world")
        assert tokens > 0


class TestHistorySummaries:
    def test_empty(self):
        assert _build_history_summaries(None) == []

    def test_basic(self):
        summaries = _build_history_summaries(_sample_history())
        assert len(summaries) <= 6
        assert all(isinstance(s.summary, str) for s in summaries)

    def test_max_summaries(self):
        long_history = [
            {"role": "user", "content": f"message {i}"}
            for i in range(20)
        ]
        summaries = _build_history_summaries(long_history, max_summaries=4)
        assert len(summaries) <= 4

    def test_older_history_is_summarized_and_recent_history_stays_verbatim(self):
        history = [
            {"role": "user", "content": f"较早需求 {i}。还有重复背景。"}
            for i in range(8)
        ] + [
            {"role": "assistant", "content": "最近确认 A"},
            {"role": "user", "content": "最近确认 B"},
            {"role": "assistant", "content": "最近确认 C"},
            {"role": "user", "content": "最近确认 D"},
        ]

        summaries = _build_history_summaries(history)

        assert len(summaries) <= 6
        older = [item for item in summaries if item.kind == "older_summary"]
        recent = [item for item in summaries if item.kind == "recent_verbatim"]
        assert older
        assert [item.summary for item in recent] == [
            "最近确认 A", "最近确认 B", "最近确认 C", "最近确认 D"
        ]
        assert all(len(item.source_turns) <= 2 for item in older)

    def test_exact_duplicates_keep_only_latest_occurrence(self):
        history = [
            {"role": "user", "content": "预算 5000"},
            {"role": "assistant", "content": "已记录"},
            {"role": "user", "content": "预算 5000"},
        ]
        summaries = _build_history_summaries(history)
        assert [item.summary for item in summaries].count("预算 5000") == 1


class TestContextPackConstruction:
    def test_basic_build(self):
        state = _make_state()
        pack = asyncio.run(build_context_pack(
            state,
            allowed_tools=["search_products", "get_product_details"],
            history=_sample_history(),
        ))
        assert pack.schema_version == "1.0"
        assert pack.task_id == state.task_id
        assert pack.base_context_revision == state.revision
        assert pack.goal == state.goal
        assert len(pack.confirmed_facts) == 2
        assert len(pack.hard_constraints) >= 1
        assert pack.allowed_tools == ["search_products", "get_product_details"]
        assert pack.shopping_guide_state is not None
        assert pack.shopping_guide_state["category"] == "headphones"

    def test_truncation_on_tiny_budget(self):
        state = _make_state()
        state.facts = [
            TaskFact(key=f"fact_{i}", value=f"value_{i}", certainty="confirmed", source="user")
            for i in range(100)
        ]
        with pytest.raises(ContextPackBudgetExceeded):
            asyncio.run(build_context_pack(
                state,
                history=_sample_history(),
                budget_tokens=200,
            ))

    def test_hybrid_compression_never_drops_protected_facts_constraints_or_evidence(self):
        state = _make_state()
        state.domain_state["lastToolResult"] = {
            "detail": {
                "candidates": [
                    {"evidenceRefs": ["mysql:product:101:price", "mysql:product:101:brand"]}
                ]
            }
        }
        expected_facts = list(state.facts)
        expected_constraints = list(state.constraints)

        pack = asyncio.run(build_context_pack(
            state,
            history=[
                {"role": "user", "content": f"历史消息 {index} " + "很长" * 100}
                for index in range(20)
            ],
            budget_tokens=450,
        ))

        assert pack.confirmed_facts == expected_facts
        assert pack.hard_constraints == expected_constraints
        assert pack.evidence_refs == [
            "mysql:product:101:price", "mysql:product:101:brand"
        ]
        assert pack.truncation_trace is not None
        assert pack.truncation_trace.strategy == "hybrid"
        assert pack.truncation_trace.dropped_evidence_refs == 0
        assert pack.truncation_trace.preserved_evidence_refs == 2

    def test_hybrid_compression_is_deterministic(self):
        state = _make_state()
        history = [
            {"role": "user", "content": f"需求 {index}。补充说明。"}
            for index in range(12)
        ]
        first = asyncio.run(build_context_pack(
            state, history=history, run_id="run-a", budget_tokens=100_000
        ))
        second = asyncio.run(build_context_pack(
            state, history=history, run_id="run-b", budget_tokens=100_000
        ))
        assert first.history_summaries == second.history_summaries
        assert context_pack_hash(first) == context_pack_hash(second)
        assert first.truncation_trace is not None
        assert first.truncation_trace.compressed_history_groups > 0

    def test_phase_budgets_are_explicit_and_not_one_shared_number(self):
        assert PHASE_TOKEN_BUDGETS == {
            "planner": 6_000,
            "executor": 3_000,
            "validator": 10_000,
            "replanner": 4_000,
            "final_answer": 10_000,
        }

    def test_no_truncation_when_under_budget(self):
        state = _make_state()
        pack = asyncio.run(build_context_pack(
            state,
            budget_tokens=100_000,
        ))
        assert pack.truncation_trace is None

    def test_hash_stability(self):
        pack1 = ContextPack(
            run_id="r1",
            task_id="t1",
            base_context_revision=1,
            goal="test",
        )
        pack2 = ContextPack(
            run_id="r2",
            task_id="t1",
            base_context_revision=1,
            goal="test",
        )
        assert context_pack_hash(pack1) == context_pack_hash(pack2)

    def test_system_message(self):
        pack = ContextPack(
            run_id="r1",
            task_id="t1",
            base_context_revision=1,
            goal="test",
        )
        msg = context_pack_system_message(pack)
        assert msg["role"] == "system"
        assert "test" in msg["content"]


class TestSoftPreferences:
    def test_extract_from_guide_state(self):
        state = _make_state()
        guide = state.domain_state.get("shoppingGuide")
        prefs = _extract_soft_preferences(state, guide)
        noise_pref = [p for p in prefs if p["key"] == "noise_cancelling"]
        assert len(noise_pref) == 1

    def test_empty_when_no_inferred_facts(self):
        state = _make_state(domain_state={})
        prefs = _extract_soft_preferences(state, None)
        assert prefs == []


class TestEvidenceRefs:
    def test_collect_from_domain_state(self):
        refs = _collect_evidence_refs({
            "lastToolResult": {
                "detail": {
                    "candidates": [
                        {"evidenceRefs": ["ref-1", "ref-2"]},
                        {"evidenceRefs": ["ref-3"]},
                    ],
                },
            },
        })
        assert "ref-1" in refs
        assert "ref-2" in refs
        assert "ref-3" in refs

    def test_empty(self):
        assert _collect_evidence_refs({}) == []


class TestContextPackRuntimeMode:
    def test_context_pack_is_default_with_legacy_rollback_available(self):
        assert Settings.model_fields["agent_context_mode"].default == "context_pack"
        assert Settings(_env_file=None, agent_context_mode="legacy").agent_context_mode == "legacy"

    def test_context_mode_rejects_unknown_value(self):
        with pytest.raises(ValueError, match="legacy or context_pack"):
            Settings(_env_file=None, agent_context_mode="unknown")


class TestShoppingGuideArgumentSources:
    """E2E-STAGE5-PLANNER-SOURCE-CONTRACT-001: server-owned source derivation."""

    _GUIDE = {
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

    def test_maps_validated_category_to_tool_label_and_keeps_requirements(self):
        sources = shopping_guide_argument_sources(self._GUIDE)

        assert sources["category"] == "手机"  # phone → 手机, server-side mapping
        assert sources["requirements"] == [
            {
                "key": "os", "operator": "eq", "value": "ios",
                "unit": "enum", "priority": "hard", "source": "user",
            }
        ]

    def test_maps_each_internal_category_to_its_tool_label(self):
        # os=ios is only a valid used-phone requirement; use an empty
        # requirements list so the category mapping itself is the only thing
        # under test for laptop/headphones.
        labels = {"phone": "手机", "laptop": "笔记本", "headphones": "耳机"}
        for code, label in labels.items():
            guide = dict(self._GUIDE, category=code, requirements=[])
            sources = shopping_guide_argument_sources(guide)
            assert sources is not None, code
            assert sources["category"] == label, code

    def test_keeps_empty_requirements_as_reproducible_empty_list(self):
        guide = dict(self._GUIDE, requirements=[])
        sources = shopping_guide_argument_sources(guide)

        assert sources == {"category": "手机", "requirements": []}

    def test_returns_none_for_missing_guide(self):
        assert shopping_guide_argument_sources(None) is None
        assert shopping_guide_argument_sources({}) is None

    def test_fail_closed_on_invalid_guide(self):
        # Extra field violates ShoppingGuideState extra="forbid".
        bad = dict(self._GUIDE)
        bad["requirements"] = [
            dict(self._GUIDE["requirements"][0], extra=True)
        ]
        assert shopping_guide_argument_sources(bad) is None

    def test_fail_closed_when_requirements_violate_registry(self):
        bad = dict(self._GUIDE)
        bad["requirements"] = [
            {
                "key": "os", "operator": "eq", "value": "ios",
                "unit": "enum", "priority": "hard", "source": "inferred:编造",
            }
        ]
        # inferred requirements cannot be hard → validation fails → None.
        assert shopping_guide_argument_sources(bad) is None


class TestCrossDomainTaskTypeIsolation:
    """E2E-STAGE5-PLANNER-SOURCE-CONTRACT-002: guide is inert off ecommerce_guide."""

    def test_local_life_with_valid_guide_never_exposes_shopping_guide_state(self):
        # _make_state carries a complete valid headphones guide; only the task
        # type is flipped.  build_context_pack must still gate it to None.
        state = _make_state(task_type="local_life")
        pack = asyncio.run(build_context_pack(
            state,
            allowed_tools=["search_products"],
        ))
        assert pack.shopping_guide_state is None

    def test_unknown_task_type_with_valid_guide_never_exposes_shopping_guide_state(self):
        state = _make_state(task_type="custom_domain")
        pack = asyncio.run(build_context_pack(
            state,
            allowed_tools=["search_products"],
        ))
        assert pack.shopping_guide_state is None

    def test_ecommerce_guide_with_valid_guide_still_exposes_validated_guide(self):
        state = _make_state()  # task_type="ecommerce_guide"
        pack = asyncio.run(build_context_pack(
            state,
            allowed_tools=["search_products"],
        ))
        assert pack.shopping_guide_state is not None
        assert pack.shopping_guide_state["category"] == "headphones"
