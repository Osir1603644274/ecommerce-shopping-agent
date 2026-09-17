"""Tests for the used-phone fast-preview analyzer (先快后完整).

Covers the handoff contract sections 4.2 (single fast analyzer), 4.4
(preview + bounded cache) and 4.6 (safety gates fail closed): the five
controlled screen expressions, fast-path eligibility for camera/gaming
queries, every safety gate, multi-turn requirement projection, cache-key
identity, bounded deep-copy cache semantics and the provisional projection.
"""

import pytest
from datetime import datetime, timezone

from pydantic import BaseModel

from app.domains.ecommerce import (
    BrandAvoidance,
    ShoppingGuideState,
    ShoppingRequirement,
)
from app.domains.ecommerce.fast_response import (
    PreviewCandidateCache,
    _fast_safety_verdict,
    analyze_used_phone_fast,
    build_preview_cache_key,
    build_provisional_guide_result,
    catalog_revision,
)
from app.task_state import TaskState


def _freeze(value):
    """Make a requirement value hashable (lists become tuples) for set/tuple
    membership assertions."""
    return tuple(value) if isinstance(value, list) else value


def _phone_state(
    requirements: list[ShoppingRequirement] | None = None,
    avoidances: list[BrandAvoidance] | None = None,
    use_cases: list[str] | None = None,
    *,
    goal: str = "拍照好用的手机",
    task_id: str = "task-fast-preview",
    session_id: str = "session-fast-preview",
    revision: int = 1,
) -> TaskState:
    now = datetime.now(timezone.utc)
    guide = ShoppingGuideState(
        mode="recommend",
        category="phone",
        useCases=use_cases or ["camera_title_claim"],
        requirements=requirements or [],
        brandAvoidances=avoidances or [],
    )
    return TaskState(
        taskId=task_id,
        taskType="ecommerce_guide",
        sessionId=session_id,
        status="ready",
        revision=revision,
        goal=goal,
        domainState={
            "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
        },
        createdAt=now,
        updatedAt=now,
    )


def _screen_requirement(*, operator: str = "not_in", value=None) -> ShoppingRequirement:
    return ShoppingRequirement(
        key="screen_originality",
        operator=operator,
        value=value if value is not None else ["non_original"],
        unit="enum",
        priority="hard",
        source="user",
    )


class TestScreenExpressions:
    """Handoff §4.2: the five controlled screen expressions must bind."""

    @pytest.mark.parametrize(
        "message",
        [
            "屏幕不要非原装的",
            "不要非原装的屏幕",
            "非原装屏不要",
            "屏幕不能是非原装",
        ],
    )
    def test_non_original_forms_bind_not_in_hard(self, message):
        fast = analyze_used_phone_fast(message, _phone_state())

        assert fast.status in {"full", "safe_partial"}
        assert fast.allow_product_preview is True
        assert "screen_originality" in fast.mentioned_keys
        assert any(
            item.key == "screen_originality"
            and item.operator == "not_in"
            and item.value == ["non_original"]
            and item.priority == "hard"
            for item in fast.preview_requirements
        )
        # The negated attribute phrase is the rejected object, never a spurious
        # positive (``非原装屏不要`` must not also read as "want non-original").
        assert not any(
            item.key == "screen_originality"
            and item.operator == "eq"
            and item.value == "non_original"
            for item in fast.preview_requirements
        )

    def test_accept_original_screen_binds_eq_hard(self):
        fast = analyze_used_phone_fast("只接受原装屏", _phone_state())

        assert "screen_originality" in fast.mentioned_keys
        assert any(
            item.key == "screen_originality"
            and item.operator == "eq"
            and item.value == "original"
            and item.priority == "hard"
            for item in fast.preview_requirements
        )

    @pytest.mark.parametrize(
        "message",
        ["屏幕不要贴膜", "锁屏不要"],
    )
    def test_uncontrolled_screen_negation_fails_closed(self, message):
        fast = analyze_used_phone_fast(message, _phone_state())

        # "贴膜"/"锁屏" are not controlled attributes; an unbound negation must
        # never be turned into a screen guess.
        assert fast.status == "unsafe"
        assert fast.allow_product_preview is False
        assert "unbound_negation_target" in fast.risk_codes


class TestFastPathEligibility:
    def test_battery_health_must_form_is_fully_controlled(self):
        fast = analyze_used_phone_fast(
            "再加一个硬条件：电池健康必须90%以上，其他条件不变",
            _phone_state(),
        )

        assert fast.status == "full"
        assert fast.allow_task_state_bypass is True
        assert "battery_health" in fast.covered_keys
        assert any(
            item.key == "battery_health"
            and item.operator == "eq"
            and item.value == "90_plus"
            and item.priority == "hard"
            for item in fast.preview_requirements
        )

    def test_camera_query_enters_fast_path(self):
        fast = analyze_used_phone_fast("拍照好用的手机", _phone_state())

        assert fast.status == "full"
        assert fast.allow_task_manager_bypass is True
        assert fast.allow_task_state_bypass is True
        assert fast.allow_product_preview is True

    def test_gaming_query_enters_fast_path(self):
        fast = analyze_used_phone_fast("适合打游戏的手机", _phone_state())

        assert fast.status == "full"
        assert fast.allow_product_preview is True

    def test_no_state_is_not_applicable(self):
        fast = analyze_used_phone_fast("屏幕不要非原装的", None)

        assert fast.status == "not_applicable"
        assert fast.reason == "not_ecommerce_task"
        assert fast.allow_product_preview is False

    def test_non_ecommerce_task_is_not_applicable(self):
        state = _phone_state().model_copy(
            update={"task_type": "local_life_guide"}
        )
        fast = analyze_used_phone_fast("屏幕不要非原装的", state)

        assert fast.status == "not_applicable"

    def test_non_phone_category_is_not_applicable(self):
        state = _phone_state().model_copy(update={})
        guide = ShoppingGuideState.model_validate(
            state.domain_state["shoppingGuide"]
        )
        state.domain_state["shoppingGuide"] = guide.model_copy(
            update={"category": "laptop"}
        ).model_dump(by_alias=True, mode="json")
        fast = analyze_used_phone_fast("屏幕不要非原装的", state)

        assert fast.status == "not_applicable"
        assert fast.reason == "non_phone_category"

    def test_cancel_expression_is_not_applicable(self):
        fast = analyze_used_phone_fast("算了不买了", _phone_state())

        assert fast.status == "not_applicable"
        assert fast.allow_product_preview is False

    def test_explicit_new_task_is_not_applicable(self):
        fast = analyze_used_phone_fast("新任务", _phone_state())

        assert fast.status == "not_applicable"
        assert fast.reason == "explicit_new_task"


class TestSafetyGates:
    """Handoff §4.6: every gate fails closed (no bypass, no preview)."""

    @pytest.mark.parametrize(
        ("message", "risk_code"),
        [
            ("预算3000", "unbound_numeric_unit"),
            ("内存16", "unbound_numeric_unit"),
            ("屏幕不要贴膜", "unbound_negation_target"),
            ("能解锁的不要", "unbound_negation_target"),
            ("我要换一台笔记本", "category_switch"),
            ("这两个我都要", "unbound_ordinal_reference"),
            ("前两个都要", "unbound_ordinal_reference"),
            ("屏幕要原装屏，屏幕不要原装屏", "same_turn_accept_reject"),
            ("原装屏要，原装屏不要", "same_turn_accept_reject"),
            ("不要苹果，苹果也可以", "same_turn_accept_reject"),
        ],
    )
    def test_gate_fails_closed(self, message, risk_code):
        fast = analyze_used_phone_fast(message, _phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_task_manager_bypass is False
        assert fast.allow_task_state_bypass is False
        assert fast.allow_product_preview is False
        assert risk_code in fast.risk_codes


class TestMixedUnresolvedCuesFailClosed:
    """Repair 1: ANY unbound negation target, bare ordinal, demonstrative
    reference or "换一个" remaining this turn — even next to a fully-resolved
    brand/os/screen condition — must force every bypass/preview flag off and
    status=unsafe.  Not a per-string blacklist: each case is decided by the
    unified verdict over the same shared cue tables."""

    @pytest.mark.parametrize(
        "message",
        [
            "不要苹果，第一个呢",
            "不要苹果，第二个",
            "不要苹果，换一个",
            "原装屏，这个怎么样",
            "不要苹果，这个也不要",
            "不要苹果，贴膜也不要",
            "不要非原装屏，另外那个也不要",
            "我要原装屏但也不要原装屏",
        ],
    )
    def test_mixed_turn_fails_closed(self, message):
        fast = analyze_used_phone_fast(message, _phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_task_manager_bypass is False
        assert fast.allow_task_state_bypass is False
        assert fast.allow_product_preview is False
        assert fast.risk_codes, f"expected a risk code for {message!r}"

    def test_same_turn_conflict_not_silently_last_value_overridden(self):
        # The negation parser swallows the positive half (「要原装屏但也不要原装屏」);
        # the message-level positive-vs-negative scan must still flag it, not
        # let the turn run as a clean screen exclusion.
        fast = analyze_used_phone_fast("我要原装屏但也不要原装屏", _phone_state())

        assert fast.status == "unsafe"
        assert "same_turn_accept_reject" in fast.risk_codes
        assert fast.allow_product_preview is False

    @pytest.mark.parametrize(
        "message",
        [
            "不要苹果，这个也不要",
            "不要非原装屏，另外那个也不要",
        ],
    )
    def test_unbound_negation_next_to_bound_brand_is_flagged(self, message):
        # The brand clause is fully resolved (hard apple exclusion); the sibling
        # clause's unbound negation must still fail the whole turn.
        fast = analyze_used_phone_fast(message, _phone_state())

        assert fast.status == "unsafe"
        assert "unbound_negation_target" in fast.risk_codes

    def test_bare_ordinal_next_to_bound_brand_is_flagged(self):
        fast = analyze_used_phone_fast("不要苹果，第一个呢", _phone_state())

        assert fast.status == "unsafe"
        assert "unbound_ordinal_reference" in fast.risk_codes
        assert "第一个" in fast.unresolved_cues

    def test_unified_verdict_shared_table_no_duplicate_keywords(self):
        # The verdict owns the cue tables once; a second call site must not
        # re-parse with a drifted vocabulary.  Re-running the scan is stable.
        from app.llm import (
            _explicit_used_phone_exclusions,
            _explicit_used_phone_requirements,
            parse_brand_negations,
        )

        message = "不要苹果，这个也不要"
        explicit = _explicit_used_phone_requirements(message)
        brand_negations = parse_brand_negations(message)
        exclusions = _explicit_used_phone_exclusions(message)
        first = _fast_safety_verdict(
            message,
            _phone_state(),
            explicit=explicit,
            brand_negations=brand_negations,
            exclusions=exclusions,
        )
        second = _fast_safety_verdict(
            message,
            _phone_state(),
            explicit=explicit,
            brand_negations=brand_negations,
            exclusions=exclusions,
        )
        assert first == second
        assert "unbound_negation_target" in first[0]
        assert "unbound_ordinal_reference" in first[0]


class TestPreservedValidTurns:
    """Repair 2: valid fast turns must remain full + previewable."""

    @pytest.mark.parametrize(
        "message",
        [
            "拍照好用的手机",
            "适合打游戏的手机",
            "屏幕不要非原装的",
            "不要非原装的屏幕",
            "非原装屏不要",
            "屏幕不能是非原装",
            "只接受原装屏",
        ],
    )
    def test_valid_turn_stays_full_and_previewable(self, message):
        fast = analyze_used_phone_fast(message, _phone_state())

        assert fast.status == "full"
        assert fast.allow_task_manager_bypass is True
        assert fast.allow_task_state_bypass is True
        assert fast.allow_product_preview is True
        assert fast.risk_codes == []

    def test_brand_negation_turn_with_persisted_screen_exclusion(self):
        # Attempt 001's controlled turn 3: brand negation + android preference
        # on top of a persisted screen exclusion must stay full/previewable.
        state = _phone_state(
            requirements=[_screen_requirement()],
            avoidances=[
                BrandAvoidance(values=["apple"], strength="soft", source="user"),
            ],
        )

        fast = analyze_used_phone_fast("不要苹果，安卓优先", state)

        assert fast.status == "full"
        assert fast.allow_product_preview is True
        assert fast.risk_codes == []


class TestConsumedSpanNegationBinding:
    """Codex defect #1: every negation cue needs target binding/consumed-span
    accounting.  A valid brand/scoped-exclusion target in the same clause must
    never mask a remaining unconsumed negation cue (不要苹果而且贴膜也不要).  The
    cases are metamorphic across coordination boundaries, word order and
    punctuation — each turns on the same per-cue walk, not a sentence list."""

    @pytest.mark.parametrize(
        "message",
        [
            # Same-clause coordination boundaries the postfix span must not cross.
            "不要苹果而且贴膜也不要",
            "不要苹果并且贴膜也不要",
            "不要苹果但贴膜也不要",
            "不要苹果但是贴膜也不要",
            "不要苹果和贴膜也不要",
            "不要苹果与贴膜也不要",
            "不要苹果然后贴膜也不要",
            "不要苹果接着贴膜也不要",
            "不要苹果同时贴膜也不要",
            "不要苹果还要贴膜也不要",
            "不要苹果或者贴膜也不要",
            "不要苹果或是贴膜也不要",
            "不要苹果以及贴膜也不要",
            # Sibling-clause variants (word order / punctuation must not change
            # the fail-closed property).
            "不要苹果，贴膜也不要",
            "贴膜也不要，苹果不要",
            "苹果不要，贴膜也不要",
            "不要苹果。贴膜也不要",
            # A valid scoped exclusion in one sibling clause does NOT mask an
            # unbound cue in another.
            "不要贴膜，屏幕不要非原装的",
            "屏幕不要非原装的，贴膜也不要",
            # Unbound targets in general.
            "贴膜也不要",
            "手机壳也不要",
            "碎屏也不要",
        ],
    )
    def test_unconsumed_negation_cue_fails_closed(self, message):
        fast = analyze_used_phone_fast(message, _phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_task_manager_bypass is False
        assert fast.allow_task_state_bypass is False
        assert fast.allow_product_preview is False
        assert "unbound_negation_target" in fast.risk_codes, fast.risk_codes

    @pytest.mark.parametrize(
        "message",
        [
            "不要苹果也不要华为",
            "不要苹果，华为也不要",
            "苹果也不要，华为也不要",
            "不要苹果而且不要华为",
        ],
    )
    def test_every_cue_bound_stays_full(self, message):
        # Reverse metamorphic control: when EVERY negation cue binds a real
        # brand, the turn stays full/previewable (the walk is not just flagging
        # any turn with a negation).
        fast = analyze_used_phone_fast(message, _phone_state())

        assert fast.status in {"full", "safe_partial"}
        assert fast.risk_codes == []
        assert fast.allow_product_preview is True


class TestUnifiedOrdinalGrammar:
    """Codex defect #2: one ordinal/quantity grammar over Arabic AND Chinese
    numerals (第N个/前N个/头N个) drives the fail-closed scan.  Only the exact
    surfaces ``_ordinal_comparison_binding`` can server-bind to Validator
    display ids are exempt under ``bound``; every other surface fails closed."""

    @pytest.mark.parametrize(
        "message",
        [
            "前3个哪个好",
            "前5款哪个好",
            "头两个哪个好",
            "第3个哪个好",
            "前三台哪个好",
            "前十个哪个好",
            "第20款怎么样",
            "这两个哪个好",
            "前两个哪个好",
        ],
    )
    def test_unbound_ordinal_fails_closed(self, message):
        # Fresh task: no Validator publication boundary, every ordinal is
        # unbound regardless of numeral system.
        fast = analyze_used_phone_fast(message, _phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_task_manager_bypass is False
        assert fast.allow_task_state_bypass is False
        assert fast.allow_product_preview is False
        assert "unbound_ordinal_reference" in fast.risk_codes

    def test_bound_compare_surfaces_stay_exempt(self):
        from tests.test_shopping_guide import _validator_bound_phone_state

        state = _validator_bound_phone_state()
        for message in ["第一个和第二个哪个好", "前两个哪个好"]:
            fast = analyze_used_phone_fast(message, state)
            assert fast.status == "full", message
            assert fast.allow_product_preview is False
            assert fast.risk_codes == [], message

    def test_two_item_display_binds_these_two_only(self):
        from tests.test_shopping_guide import _validator_bound_phone_state

        state = _validator_bound_phone_state(trusted_ids=[5989522, 1092185])

        bound = analyze_used_phone_fast("这两个哪个好", state)
        assert bound.status == "full"
        assert bound.risk_codes == []
        assert bound.allow_product_preview is False

        # 这两台 is not a surface ``_ordinal_comparison_binding`` binds.
        unbound = analyze_used_phone_fast("这两台哪个好", state)
        assert unbound.status == "unsafe"
        assert "unbound_ordinal_reference" in unbound.risk_codes

    @pytest.mark.parametrize(
        "message",
        [
            # Never-bindable surfaces stay unbound even under a bound compare.
            "前3个哪个好",
            "第3个哪个好",
            "头两个哪个好",
            "第5个哪个好",
            # A bound pair plus an unbound ordinal in one turn still fails closed.
            "第一个和第二个哪个好，第3个呢",
            "前两个哪个好，前3个呢",
        ],
    )
    def test_non_bindable_ordinal_fails_closed_under_bound_compare(self, message):
        from tests.test_shopping_guide import _validator_bound_phone_state

        fast = analyze_used_phone_fast(message, _validator_bound_phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_product_preview is False
        assert "unbound_ordinal_reference" in fast.risk_codes

    def test_unified_verdict_deterministic_across_forms(self):
        # The same unbound ordinal must produce the same unresolved cue whether
        # expressed in Arabic or Chinese digits.
        from tests.test_shopping_guide import _validator_bound_phone_state

        state = _validator_bound_phone_state()
        arabic = analyze_used_phone_fast("前3个哪个好", state)
        chinese = analyze_used_phone_fast("前三台哪个好", state)

        assert "unbound_ordinal_reference" in arabic.risk_codes
        assert "unbound_ordinal_reference" in chinese.risk_codes
        assert arabic.status == chinese.status == "unsafe"


class TestRequirementProjection:
    """Multi-turn projection: persisted baseline + this-turn ops, preview-only."""

    def test_second_turn_negation_merges_with_persisted_exclusion(self):
        state = _phone_state(
            requirements=[_screen_requirement()],
            avoidances=[
                BrandAvoidance(values=["apple"], strength="soft", source="user"),
            ],
        )

        fast = analyze_used_phone_fast("不要苹果，安卓优先", state)

        assert fast.status == "full"
        dumped = [item.model_dump() for item in fast.preview_requirements]
        # Persisted screen exclusion survives.
        assert {
            "key": "screen_originality", "operator": "not_in",
            "value": ["non_original"], "unit": "enum",
            "priority": "hard", "source": "user",
        } in dumped
        # This turn's soft os preference lands.
        assert {
            "key": "os", "operator": "eq", "value": "android",
            "unit": "enum", "priority": "soft", "source": "user",
        } in dumped
        # The hard brand negation overwrites the persisted soft avoidance.
        assert {
            "key": "brand", "operator": "not_in", "value": ["apple"],
            "unit": "text", "priority": "hard", "source": "user",
        } in dumped
        assert not any(
            item["key"] == "brand" and item["operator"] == "eq"
            for item in dumped
        )

    def test_explicit_positive_overwrites_persisted_exclusion(self):
        state = _phone_state(requirements=[_screen_requirement()])

        fast = analyze_used_phone_fast("屏幕要原装的", state)

        assert fast.status == "full"
        dumped = [item.model_dump() for item in fast.preview_requirements]
        assert dumped == [{
            "key": "screen_originality", "operator": "eq",
            "value": "original", "unit": "enum",
            "priority": "hard", "source": "user",
        }]

    def test_removal_makes_turn_partial_but_preview_uses_persisted_state(self):
        state = _phone_state(requirements=[_screen_requirement()])

        fast = analyze_used_phone_fast("屏幕要求取消", state)

        # Removal semantics are not fully deterministic: the TaskState model may
        # still run (no task-state bypass) while the preview shows the baseline.
        assert fast.status == "safe_partial"
        assert fast.allow_task_state_bypass is False
        assert fast.allow_product_preview is True
        assert any(
            item.key == "screen_originality" and item.operator == "not_in"
            for item in fast.preview_requirements
        )

    def test_bounded_number_is_not_a_safety_risk(self):
        fast = analyze_used_phone_fast(
            "屏幕不要非原装的，预算3000元以内", _phone_state()
        )

        assert fast.status == "full"
        assert any(
            item.key == "price_minor" and item.operator == "lte"
            and item.value == 300000.0
            for item in fast.preview_requirements
        )
        assert "unbound_numeric_unit" not in fast.risk_codes


class TestPreviewCacheKey:
    def test_requirement_identity_changes_key(self):
        base = build_preview_cache_key(
            catalog_revision="r1", query="屏幕不要非原装的",
        )
        with_req = build_preview_cache_key(
            catalog_revision="r1", query="屏幕不要非原装的",
            requirements=[_screen_requirement()],
        )
        hard_soft = build_preview_cache_key(
            catalog_revision="r1", query="屏幕不要非原装的",
            requirements=[
                _screen_requirement().model_copy(update={"priority": "soft"})
            ],
        )

        assert base != with_req
        assert with_req != hard_soft

    def test_catalog_revision_and_query_normalization(self):
        key_a = build_preview_cache_key(catalog_revision="r1", query="拍照好用 的手机")
        key_b = build_preview_cache_key(catalog_revision="r2", query="拍照好用的手机")
        key_c = build_preview_cache_key(catalog_revision="r1", query="拍照好用的手机")

        assert key_a == key_c  # whitespace normalization
        assert key_a != key_b  # catalog revision sensitivity

    def test_use_cases_change_key(self):
        key_a = build_preview_cache_key(
            catalog_revision="r1", query="适合打游戏的手机",
        )
        key_b = build_preview_cache_key(
            catalog_revision="r1", query="适合打游戏的手机",
            use_cases=["camera_title_claim", "gaming_title_claim"],
        )

        assert key_a != key_b

    def test_catalog_revision_is_deterministic_token(self):
        revision = catalog_revision()

        assert isinstance(revision, str)
        assert len(revision) == 16
        assert int(revision, 16) >= 0
        assert revision == catalog_revision()


class TestPreviewCandidateCache:
    def test_get_misses_then_set_get_hits(self):
        cache = PreviewCandidateCache(max_entries=8)

        assert cache.get("k") is None
        cache.set("k", [{"id": 1}])
        assert cache.get("k") == [{"id": 1}]

    def test_get_returns_deep_copy(self):
        cache = PreviewCandidateCache(max_entries=8)
        source = [{"id": 1, "facts": {"specifications": {"os": "ios"}}}]
        cache.set("k", source)

        returned = cache.get("k")
        returned[0]["id"] = 999
        returned[0]["facts"]["specifications"]["os"] = "android"

        # Mutations never leak back into the stored value or the source.
        assert source[0]["id"] == 1
        assert cache.get("k") == [{"id": 1, "facts": {"specifications": {"os": "ios"}}}]

    def test_bounded_fifo_eviction(self):
        cache = PreviewCandidateCache(max_entries=2)
        cache.set("a", [1])
        cache.set("b", [2])
        cache.set("c", [3])

        stats = cache.stats()
        assert stats["size"] == 2
        assert cache.get("a") is None  # oldest evicted
        assert cache.get("b") == [2]
        assert cache.get("c") == [3]

    def test_clear(self):
        cache = PreviewCandidateCache(max_entries=8)
        cache.set("a", [1])

        cache.clear()

        assert cache.get("a") is None
        assert cache.stats()["size"] == 0

    def test_catalog_revision_change_auto_invalidates(self, monkeypatch):
        from app.domains.ecommerce import fast_response

        cache = PreviewCandidateCache(max_entries=8)
        cache.set("k", [{"id": 1}])
        assert cache.get("k") == [{"id": 1}]

        monkeypatch.setattr(
            fast_response.settings,
            "used_phone_fast_preview_catalog_revision",
            "used-phone-catalog-v2-test",
        )

        assert cache.get("k") is None

    def test_invalidate_catalog_purges_target(self):
        cache = PreviewCandidateCache(max_entries=8)
        cache.set("k", [1])

        removed = cache.invalidate_catalog("different-revision")

        assert removed == 1
        assert cache.get("k") is None

    def test_stats_are_observable(self):
        cache = PreviewCandidateCache(max_entries=8)
        cache.set("k", [1])
        cache.get("k")
        cache.get("missing")

        stats = cache.stats()

        assert stats["size"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] == 1


class TestProvisionalProjection:
    """Only rule-level full_match candidates may appear as provisional picks."""

    def _candidate(self, row_id: int, selection: str) -> dict:
        return {
            "id": row_id,
            "product": {
                "id": row_id,
                "title": f"Phone {row_id}",
                "brand": "xiaomi",
                "priceStatus": "verified",
                "snapshotPriceMinor": 199900,
                "currency": "CNY",
            },
            "facts": {"specifications": {"os": "android"}},
            "checks": [],
            "selectionType": selection,
            "evidenceRefs": [f"product:{row_id}:title"],
        }

    def test_only_full_match_and_top3(self):
        candidates = [
            self._candidate(1, "full_match"),
            self._candidate(2, "closest_alternative"),
            self._candidate(3, "full_match"),
            self._candidate(4, "full_match"),
            self._candidate(5, "full_match"),
        ]

        result = build_provisional_guide_result(candidates, limit=3)

        ids = [item["product"]["id"] for item in result["products"]]
        assert ids == ["1", "3", "4"]
        assert result["status"] == "provisional"
        assert result["hasCompleteMatch"] is True
        assert all(
            item["selectionType"] == "full_match"
            for item in result["products"]
        )

    def test_empty_pool_never_fabricates_candidates(self):
        result = build_provisional_guide_result([], limit=3)

        assert result["products"] == []
        assert result["hasCompleteMatch"] is False
        assert result["rankedItemCount"] == 0

    def test_hard_fail_is_excluded(self):
        candidates = [
            self._candidate(1, "full_match"),
            {
                **self._candidate(2, "full_match"),
                "selectionType": "hard_fail",
            },
        ]

        result = build_provisional_guide_result(candidates, limit=3)

        assert [item["product"]["id"] for item in result["products"]] == ["1"]

    def test_none_pool_returns_empty_not_crash(self):
        result = build_provisional_guide_result(None, limit=3)

        assert result["products"] == []


class _SiblingShoppingRequirement(BaseModel):
    """Wire-identical to ShoppingRequirement but a deliberately distinct class.

    Mirrors the demo's dual-tree ``app``/``agent.app`` import layout: the Agent
    is launched as ``uvicorn agent.app.main:app`` while modules use absolute
    ``app.*`` imports, so ``app.llm`` can hand the analyzer a *sibling*
    ShoppingRequirement class object that pydantic rejects with a model_type
    error unless the analyzer canonicalises the class identity first.
    """

    model_config = {"extra": "forbid"}

    key: str
    operator: str
    value: float | bool | str | list[str]
    unit: str
    priority: str
    source: str


def _sibling_requirement(**overrides):
    values = dict(
        key="os",
        operator="eq",
        value="android",
        unit="enum",
        priority="soft",
        source="用户原话：安卓优先",
    )
    values.update(overrides)
    return _SiblingShoppingRequirement(**values)


def test_coerce_rebuilds_sibling_tree_requirement_against_local_class():
    """Regression: a sibling-class ShoppingRequirement from ``app.llm`` must be
    rebuilt against the analyzer's own class before FastAnalysis construction."""
    from app.domains.ecommerce.fast_response import _coerce_shopping_requirements

    sibling = _sibling_requirement()
    assert type(sibling) is not ShoppingRequirement

    coerced = _coerce_shopping_requirements([sibling])

    assert len(coerced) == 1
    assert type(coerced[0]) is ShoppingRequirement
    assert coerced[0].key == "os"
    assert coerced[0].operator == "eq"
    assert coerced[0].value == "android"
    assert coerced[0].unit == "enum"
    assert coerced[0].priority == "soft"
    assert coerced[0].source == "用户原话：安卓优先"


def test_fast_analysis_accepts_coerced_sibling_requirements():
    """The exact crash the live demo hit: sibling instances in the FastAnalysis
    requirement lists.  Coercion makes the same values pass model validation."""
    from app.domains.ecommerce.fast_response import (
        FastAnalysis,
        _coerce_shopping_requirements,
    )

    coerced = _coerce_shopping_requirements([_sibling_requirement()])
    analysis = FastAnalysis(
        status="full",
        route="deterministic_complete",
        reason="complete_controlled_coverage",
        requirements=coerced,
        preview_requirements=coerced,
        allow_task_manager_bypass=True,
        allow_task_state_bypass=True,
        allow_product_preview=True,
    )

    assert analysis.requirements[0].value == "android"
    assert analysis.preview_requirements[0].priority == "soft"


def test_fast_analyzer_covers_brand_negation_and_android_preference():
    """The turn that crashed the live demo (brand negation + Android preference)
    is a full controlled turn whose preview requirements merge the persisted
    screen exclusion with this turn's os=android soft and brand not-in apple."""
    state = _phone_state(requirements=[
        ShoppingRequirement(
            key="screen_originality",
            operator="not_in",
            value=["non_original"],
            unit="enum",
            priority="hard",
            source="user",
        )
    ])

    fast = analyze_used_phone_fast("不要苹果，安卓优先", state)

    assert fast.status == "full"
    assert fast.allow_task_manager_bypass is True
    assert fast.allow_task_state_bypass is True
    assert fast.allow_product_preview is True
    requirement_tuples = {
        (r.key, r.operator, _freeze(r.value), r.unit, r.priority)
        for r in fast.requirements
    }
    assert ("os", "eq", "android", "enum", "soft") in requirement_tuples
    assert ("brand", "not_in", ("apple",), "text", "hard") in requirement_tuples
    # Multi-turn projection: the persisted screen exclusion survives alongside
    # this turn's os/brand operations, all canonicalised to the local class.
    assert [r.key for r in fast.preview_requirements] == [
        "screen_originality",
        "os",
        "brand",
    ]
    assert all(type(r) is ShoppingRequirement for r in fast.preview_requirements)
