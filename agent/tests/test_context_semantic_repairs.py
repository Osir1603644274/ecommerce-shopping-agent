"""Semantic boundaries exposed by frozen Context v2 responses; no network."""
from copy import deepcopy

import pytest

from app import llm
from app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
from tests.test_shopping_state_update import _state


def build(args, message="继续", state=None):
    return llm._build_validated_task_state_payload(
        state or _state(), args, message=message, require_status=True,
    )[0]


@pytest.mark.parametrize("goal", ["null", " NULL ", "None", "undefined", "", "   ", None, 7])
def test_reject_placeholder_goal(goal):
    with pytest.raises(llm.TaskStatePayloadValidationError) as caught:
        build({"status": "ready", "goal": goal})
    assert caught.value.code == "invalid_goal_placeholder"
    assert caught.value.field_path == "goal"


def test_goal_check_does_not_rewrite_fact_values_or_legitimate_text():
    args = {"status": "ready", "goal": "解释 JSON null 与字符串的区别",
            "upsertFacts": [{"key": "literal", "value": "null", "source": "user"}]}
    assert llm._validated_model_task_patch(args) == args


@pytest.mark.parametrize("guide_patch", [{}, {"upsertRequirements": []}, {"mode": "recommend"}])
def test_sparse_guide_inherits_valid_recommend_mode(guide_patch):
    payload = build({"status": "ready", "domainStatePatch": {"shoppingGuide": guide_patch},
                     "optionalShoppingQuestions": [{"kind": "use_case"}]})
    assert payload["domainStatePatch"]["optionalShoppingQuestions"][0]["kind"] == "use_case"


@pytest.mark.parametrize("mode,message", [("compare", "继续"), ("recommend", "比较这两款")])
def test_optional_questions_cannot_mask_comparison(mode, message):
    with pytest.raises(llm.TaskStatePayloadValidationError):
        build({"status": "ready", "domainStatePatch": {"shoppingGuide": {"mode": mode}},
               "optionalShoppingQuestions": [{"kind": "use_case"}]}, message)


@pytest.mark.parametrize("mutation", ["missing_category", "empty_requirements"])
def test_optional_questions_still_require_executable_state(mutation):
    state = _state().model_copy(deep=True)
    state.domain_state.pop("shoppingTaskStateV2", None)
    guide = state.domain_state["shoppingGuide"]
    if mutation == "missing_category": guide["category"] = None
    else: guide["requirements"] = []
    with pytest.raises(llm.TaskStatePayloadValidationError):
        build({"status": "ready", "optionalShoppingQuestions": [{"kind": "use_case"}]}, state=state)


def battery_args(key="battery_hours", value=8, unit="hour"):
    return {"status": "ready", "domainStatePatch": {"shoppingGuide": {"upsertRequirements": [{
        "key": key, "operator": "gte", "value": value, "unit": unit,
        "priority": "soft", "source": "inferred:user",
    }]}}}


def test_wrong_category_has_actionable_registry_error():
    with pytest.raises(llm.TaskStatePayloadValidationError) as caught:
        build(battery_args(), "考虑续航体验")
    assert caught.value.code == "unsupported_category_requirement"
    assert "category=phone" in str(caught.value)
    assert "battery_mah" in str(caught.value)


def test_correct_category_does_not_make_invented_threshold_legal():
    state = _state().model_copy(deep=True)
    state.domain_state = {"shoppingGuide": {"mode": "recommend", "category": "headphones", "requirements": []}}
    with pytest.raises(llm.TaskStatePayloadValidationError) as caught:
        build(battery_args(), "考虑续航体验", state)
    assert caught.value.code == "unsupported_inferred_battery_threshold"
    payload = build(battery_args(), "耳机续航至少8小时", state)
    assert payload["domainStatePatch"]["shoppingGuide"]["requirements"][0]["value"] == 8


def test_qualitative_phone_preference_cannot_become_battery_capacity():
    with pytest.raises(llm.TaskStatePayloadValidationError) as caught:
        build(battery_args("battery_mah", 5000, "mAh"), "把续航体验也考虑进去")
    assert caught.value.code == "unsupported_inferred_battery_threshold"


@pytest.mark.parametrize("aspect,message", [
    ("endurance", "把续航体验也考虑进去"), ("stability", "兼顾日常稳定性"),
    ("elderly", "给长辈用是否省心"), ("wechat_video", "考虑微信和视频使用"),
    ("long_term", "兼顾长期使用体验"), ("fluency", "考虑日常流畅性"),
])
def test_server_preserves_qualitative_literal_without_numeric_invention(aspect, message):
    payload = build({"status": "ready"}, message)
    guide = payload["domainStatePatch"]["shoppingGuide"]
    assert f"user_preference:{aspect}:{message}" in guide["useCases"]
    assert {r["key"] for r in guide["requirements"]} == {"os", "price_minor"}
    state = _state()
    merged = {**state.domain_state, **payload["domainStatePatch"]}
    bound = bind_authoritative_write(
        {k: v for k, v in merged.items() if v is not None},
        task_id=state.task_id, task_revision=state.revision + 1,
        goal=state.goal, unknowns=[], pending_questions=[],
    )
    assert bound["shoppingTaskStateV2"]["shoppingGuide"] == guide


def test_preference_retraction_and_unrelated_ledger_preservation():
    state = _state().model_copy(deep=True)
    state.domain_state["shoppingGuide"]["useCases"] = ["gaming_title_claim", "user_preference:endurance:续航优先"]
    payload = build({"status": "ready"}, "不用考虑续航", state)
    assert payload["domainStatePatch"]["shoppingGuide"]["useCases"] == ["gaming_title_claim"]


def test_model_still_cannot_write_server_use_cases():
    with pytest.raises(llm.TaskStatePayloadValidationError):
        build({"status": "ready", "domainStatePatch": {"shoppingGuide": {"useCases": ["pretend"]}}})
