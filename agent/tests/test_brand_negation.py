import pytest
from datetime import datetime, timezone

from app.domains.ecommerce import (
    DOMESTIC_PHONE_BRANDS,
    ShoppingGuideState,
    ShoppingRequirement,
    compiled_shopping_requirements,
    parse_brand_negations,
)
from app.llm import (
    TaskStatePayloadValidationError,
    _build_validated_task_state_payload,
    _canonicalize_explicit_used_phone_negation,
    _deterministic_used_phone_task_state_decision,
    _explicit_used_phone_requirement_removals,
    _explicit_used_phone_requirements,
    _server_owned_use_case_guide_patch,
    _used_phone_presentation_only_request,
)
from app.task_state import TaskState


@pytest.mark.parametrize(
    ("message", "strength", "brands"),
    [
        ("不要苹果", "hard", ["apple"]),
        ("我都说了不要苹果", "hard", ["apple"]),
        ("不要苹果和三星", "hard", ["apple", "samsung"]),
        ("苹果和华为都不要", "hard", ["apple", "huawei"]),
        ("不喜欢苹果", "soft", ["apple"]),
        ("对苹果和华为不感兴趣", "soft", ["apple", "huawei"]),
        ("我不想要苹果", "hard", ["apple"]),
        ("预算不超过 2000 元，不考虑苹果", "hard", ["apple"]),
        ("想买一台非苹果的二手手机", "hard", ["apple"]),
    ],
)
def test_brand_negation_binds_strength_and_targets(message, strength, brands):
    result = parse_brand_negations(message)

    assert result.unresolved_cues == []
    assert len(result.targets) == 1
    assert result.targets[0].strength == strength
    assert result.targets[0].values == brands


@pytest.mark.parametrize(
    "message",
    [
        "不是不要苹果",
        "并非不喜欢苹果",
        "苹果也可以",
        "不介意苹果",
        "可以接受苹果",
    ],
)
def test_brand_negation_does_not_invent_negation_for_acceptance_or_cancellation(message):
    result = parse_brand_negations(message)

    assert result.targets == []
    assert result.unresolved_cues == []


@pytest.mark.parametrize(
    "message",
    [
        "不是不要苹果",
        "并非不喜欢苹果",
        "苹果也可以",
        "不介意苹果",
        "可以接受苹果",
    ],
)
def test_cancellation_or_acceptance_brand_is_guarded_from_positive_extraction(message):
    result = parse_brand_negations(message)

    assert result.guarded_brands == ["apple"]
    assert result.released_brands == ["apple"]
    assert "brand" not in _explicit_used_phone_requirements(message)


def test_one_clause_can_bind_separate_negative_cues():
    result = parse_brand_negations("不要苹果也不喜欢华为")

    assert [target.model_dump(by_alias=True) for target in result.targets] == [
        {
            "key": "brand",
            "values": ["apple"],
            "strength": "hard",
            "cue": "不要",
            "sourceText": "不要苹果也不喜欢华为",
        },
        {
            "key": "brand",
            "values": ["huawei"],
            "strength": "soft",
            "cue": "不喜欢",
            "sourceText": "不要苹果也不喜欢华为",
        },
    ]


def test_unbound_negative_cue_is_explicitly_unresolved():
    result = parse_brand_negations("那个我不要")

    assert result.targets == []
    assert result.unresolved_cues == ["不要"]


def test_not_required_capability_is_not_an_unbound_brand_rejection():
    result = parse_brand_negations("不怎么打游戏，也不要求拍照")

    assert result.targets == []
    assert result.unresolved_cues == []


@pytest.mark.parametrize(
    "message",
    [
        "不喜欢苹果的",
        "我都说了不要苹果",
        "苹果都不要",
        "我不想要苹果；预算 3000 元以内",
        "预算 2500 元以内，想买一台非苹果的二手手机",
    ],
)
def test_negated_brand_cannot_become_a_positive_brand_requirement(message):
    requirements = _explicit_used_phone_requirements(message)

    assert "brand" not in requirements


def test_positive_brand_outside_negative_scope_remains_available():
    requirements = _explicit_used_phone_requirements("不要苹果，想要华为")

    assert requirements["brand"].value == "huawei"


def test_domestic_phone_request_compiles_to_explicit_catalog_brand_group():
    requirements = _explicit_used_phone_requirements("国产手机有没有？")

    assert requirements["brand"].operator == "in"
    assert requirements["brand"].value == list(DOMESTIC_PHONE_BRANDS)
    assert "apple" not in requirements["brand"].value
    assert "samsung" not in requirements["brand"].value


def test_result_distribution_question_does_not_become_positive_apple_requirement():
    message = "只有苹果的吗？有没有安卓的"
    requirements = _explicit_used_phone_requirements(message)

    assert requirements["os"].value == "android"
    assert "brand" not in requirements
    assert _explicit_used_phone_requirement_removals(message) == {"brand"}


@pytest.mark.parametrize(
    "message",
    [
        "八百块以下的，苹果或者安卓都可以，我是学生平时在学校 偶尔打打游戏就好",
        "800 元以内，安卓和苹果都行，偶尔打游戏",
        "八百以下，Apple / Android 均可",
    ],
)
def test_apple_android_acceptance_does_not_become_conjunctive_requirements(message):
    requirements = _explicit_used_phone_requirements(message)

    assert set(requirements) == {"price_minor"}
    assert requirements["price_minor"].value == 80000
    assert _explicit_used_phone_requirement_removals(message) == {"brand", "os"}


def test_platform_acceptance_bootstraps_first_phone_turn_without_model_inference():
    message = "八百块以下的，苹果或者安卓都可以，我是学生平时在学校 偶尔打打游戏就好"
    state = _phone_state(
        ShoppingGuideState(mode="recommend", category="phone")
    ).model_copy(update={"domain_state": {"origin": "chat", "turnCount": 0}})

    arguments, observation = _deterministic_used_phone_task_state_decision(
        state,
        message,
    )

    assert arguments is not None
    assert observation is not None
    assert observation["route"] == "deterministic_text_claim_discovery"
    requirements = arguments["domainStatePatch"]["shoppingGuide"][
        "upsertRequirements"
    ]
    assert [item["key"] for item in requirements] == ["price_minor"]
    assert requirements[0]["value"] == 80000
    server_patch = _server_owned_use_case_guide_patch(
        state,
        "gaming_title_claim",
        message,
    )
    server_guide = ShoppingGuideState.model_validate(server_patch["shoppingGuide"])
    assert [item.key for item in server_guide.requirements] == ["price_minor"]
    assert server_guide.use_cases == ["gaming_title_claim"]


def test_ios_android_acceptance_releases_only_os():
    message = "预算八百以内，iOS 或 Android 都可以"

    requirements = _explicit_used_phone_requirements(message)

    assert set(requirements) == {"price_minor"}
    assert _explicit_used_phone_requirement_removals(message) == {"os"}


@pytest.mark.parametrize(
    ("message", "expected_minor"),
    [
        ("预算1200左右，希望华为手机", 120000),
        ("推荐一部 2000 元左右的二手手机", 200000),
        ("想买一部两千块上下的手机", 200000),
        ("先看2000元左右的手机，不过实际预算1500元以内", 150000),
        ("华为 vivo 之类的，不要苹果。预算2000吧。", 200000),
    ],
)
def test_natural_approximate_budget_compiles_as_first_pass_ceiling(
    message,
    expected_minor,
):
    requirements = _explicit_used_phone_requirements(message)

    assert requirements["price_minor"].value == expected_minor
    assert requirements["price_minor"].operator == "lte"


@pytest.mark.parametrize(
    "message",
    [
        "续航好手机500",
        "撤销我之前所有关于手机的需求，我新的需求是：续航好手机推荐 500左右",
        "续航好二手手机五百上下",
    ],
)
def test_trailing_bare_phone_budget_is_compiled_as_cny_ceiling(message):
    requirements = _explicit_used_phone_requirements(message)

    assert requirements["price_minor"].value == 50000
    assert requirements["price_minor"].operator == "lte"
    assert requirements["battery_health"].priority == "soft"


def test_phone_model_number_is_not_mistaken_for_bare_budget():
    requirements = _explicit_used_phone_requirements("想看看苹果手机15")

    assert "price_minor" not in requirements


@pytest.mark.parametrize("message", ["手机内存256GB左右", "电池5000mAh左右的手机"])
def test_non_currency_approximate_specs_are_not_budgets(message):
    assert "price_minor" not in _explicit_used_phone_requirements(message)


@pytest.mark.parametrize("message", ["只要苹果", "苹果优先", "有没有苹果手机"])
def test_genuine_positive_apple_requests_still_bind(message):
    requirements = _explicit_used_phone_requirements(message)

    assert requirements["brand"].value == "apple"


def test_model_fallback_platform_acceptance_is_canonicalized_before_persistence():
    message = "八百块以下的，苹果或者安卓都可以，我是学生平时在学校 偶尔打打游戏就好"
    state = _phone_state(
        ShoppingGuideState(mode="recommend", category="phone")
    ).model_copy(update={"domain_state": {}})
    model_requirements = [
        {
            "key": "price_minor", "operator": "lte", "value": 80000,
            "unit": "CNY_MINOR", "priority": "hard", "source": "user",
        },
        {
            "key": "os", "operator": "eq", "value": "android",
            "unit": "enum", "priority": "hard", "source": "user",
        },
        {
            "key": "brand", "operator": "eq", "value": "apple",
            "unit": "text", "priority": "hard", "source": "user",
        },
    ]

    payload, _ = _build_validated_task_state_payload(
        state,
        {
            "status": "ready",
            "goal": message,
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "requirements": model_requirements,
            }},
        },
        message=message,
        require_status=True,
    )
    guide = ShoppingGuideState.model_validate(
        payload["domainStatePatch"]["shoppingGuide"]
    )

    assert [item.key for item in guide.requirements] == ["price_minor"]


def test_untrusted_model_cannot_persist_incompatible_apple_android_hard_pair():
    message = "八百块以下的手机"
    state = _phone_state(
        ShoppingGuideState(mode="recommend", category="phone")
    ).model_copy(update={"domain_state": {}})

    with pytest.raises(TaskStatePayloadValidationError) as exc:
        _build_validated_task_state_payload(
            state,
            {
                "status": "ready",
                "goal": message,
                "pendingQuestions": [],
                "domainStatePatch": {"shoppingGuide": {
                    "mode": "recommend",
                    "category": "phone",
                    "requirements": [
                        {
                            "key": "price_minor", "operator": "lte",
                            "value": 80000, "unit": "CNY_MINOR",
                            "priority": "hard", "source": "user",
                        },
                        {
                            "key": "os", "operator": "eq",
                            "value": "android", "unit": "enum",
                            "priority": "hard", "source": "user",
                        },
                        {
                            "key": "brand", "operator": "eq",
                            "value": "apple", "unit": "text",
                            "priority": "hard", "source": "user",
                        },
                    ],
                }},
            },
            message=message,
            require_status=True,
        )

    assert exc.value.code == "incompatible_phone_brand_os_requirements"


def test_model_positive_brand_requirement_is_canonicalized_into_avoidance():
    guide = ShoppingGuideState(
        mode="recommend",
        category="phone",
        requirements=[ShoppingRequirement(
            key="brand",
            operator="eq",
            value="apple",
            unit="text",
            priority="hard",
            source="user",
        )],
    )

    canonical = _canonicalize_explicit_used_phone_negation(
        "我都说了不要苹果",
        guide,
    )

    assert canonical is not None
    assert canonical.requirements == []
    assert canonical.brand_avoidances[0].model_dump() == {
        "values": ["apple"],
        "strength": "hard",
        "source": "user",
    }


def test_hard_and_soft_avoidances_compile_into_independent_requirements():
    guide = ShoppingGuideState(mode="recommend", category="phone")
    guide = _canonicalize_explicit_used_phone_negation(
        "不要苹果，但对三星不感兴趣",
        guide,
    )

    assert guide is not None
    assert [item.model_dump() for item in guide.brand_avoidances] == [
        {"values": ["apple"], "strength": "hard", "source": "user"},
        {"values": ["samsung"], "strength": "soft", "source": "user"},
    ]
    assert [item.model_dump() for item in compiled_shopping_requirements(guide)] == [
        {
            "key": "brand", "operator": "not_in", "value": ["apple"],
            "unit": "text", "priority": "hard", "source": "user",
        },
        {
            "key": "brand", "operator": "not_in", "value": ["samsung"],
            "unit": "text", "priority": "soft", "source": "user",
        },
    ]


def test_acceptance_removes_prior_brand_avoidance():
    guide = ShoppingGuideState.model_validate({
        "mode": "recommend",
        "category": "phone",
        "brandAvoidances": [{
            "values": ["apple"], "strength": "hard", "source": "user",
        }],
    })

    canonical = _canonicalize_explicit_used_phone_negation("苹果也可以", guide)

    assert canonical is not None
    assert canonical.brand_avoidances == []


def _phone_state(guide: ShoppingGuideState, *, revision: int = 1) -> TaskState:
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-negation-sequence",
        taskType="ecommerce_guide",
        sessionId="session-negation-sequence",
        status="ready",
        revision=revision,
        goal="拍照好用的手机",
        domainState={
            "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
        },
        createdAt=now,
        updatedAt=now,
    )


def _apply_deterministic_turn(state: TaskState, message: str) -> TaskState:
    arguments, observation = _deterministic_used_phone_task_state_decision(
        state,
        message,
    )
    assert arguments is not None
    assert observation is not None
    payload, _ = _build_validated_task_state_payload(
        state,
        arguments,
        message=message,
        require_status=True,
        allow_auto_ready=False,
    )
    return state.model_copy(update={
        "revision": state.revision + 1,
        "goal": message,
        # Production normalization omits an unchanged ``status`` from the
        # payload when the proposed status equals the current state.  Keep the
        # helper faithful to that contract instead of forcing the field back.
        "status": payload.get("status", state.status),
        "domain_state": payload["domainStatePatch"],
    })


@pytest.mark.parametrize(
    "message",
    [
        "高中生适合的手机有吗",
        "就是性价比高的，不怎么打游戏，也不要求拍照，其他方面质量好的",
        "续航好，其他主要方面都可以的手机，你先推荐我看看",
    ],
)
def test_broad_real_queries_search_without_forced_clarification(message):
    state = _phone_state(ShoppingGuideState(mode="recommend", category="phone"))

    arguments, observation = _deterministic_used_phone_task_state_decision(
        state,
        message,
    )

    assert arguments is not None
    assert arguments["status"] == "ready"
    assert arguments["pendingQuestions"] == []
    assert observation["route"] == (
        "deterministic_complete"
        if "续航好" in message
        else "deterministic_exploratory_discovery"
    )
    if "续航好" in message:
        requirement = next(
            item
            for item in arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"]
            if item["key"] == "battery_health"
        )
        assert requirement["value"] == ["90_plus", "80_90"]
        assert requirement["priority"] == "soft"
    if "不要求拍照" in message:
        assert "打游戏" not in arguments["goal"]
        assert "拍照" not in arguments["goal"]


def test_comparison_preserves_prior_user_requirements_for_llm_judgment():
    requirements = [
        ShoppingRequirement(
            key="price_minor",
            operator="lte",
            value=120000,
            unit="CNY_MINOR",
            priority="hard",
            source="user",
        ),
        ShoppingRequirement(
            key="brand",
            operator="eq",
            value="huawei",
            unit="text",
            priority="hard",
            source="user",
        ),
    ]
    state = _phone_state(ShoppingGuideState(
        mode="recommend",
        category="phone",
        requirements=requirements,
        comparedIds=[11, 22],
    ))

    arguments, _ = _deterministic_used_phone_task_state_decision(
        state,
        "这两个哪个好？",
    )
    persisted = arguments["domainStatePatch"]["shoppingGuide"][
        "upsertRequirements"
    ]

    assert [(item["key"], item["value"]) for item in persisted] == [
        ("price_minor", 120000),
        ("brand", "huawei"),
    ]


def test_result_distribution_question_releases_prior_brand_and_selects_android():
    state = _phone_state(ShoppingGuideState(
        mode="recommend",
        category="phone",
        requirements=[ShoppingRequirement(
            key="brand",
            operator="eq",
            value="apple",
            unit="text",
            priority="hard",
            source="user",
        )],
    ))

    state = _apply_deterministic_turn(state, "只有苹果的吗？有没有安卓的")
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])

    assert [item.model_dump() for item in compiled_shopping_requirements(guide)] == [
        {
            "key": "os", "operator": "eq", "value": "android",
            "unit": "enum", "priority": "hard", "source": "user",
        },
    ]


@pytest.mark.parametrize(
    "message",
    [
        "预算 2500 元以内，想买一台非苹果的二手手机，优先 256GB、电池状况好，请推荐并说明依据。",
        "我不想要苹果；预算 3000 元以内，优先 256GB。",
    ],
)
def test_scenario_lab_language_turns_compile_non_apple_as_hard_exclusion(message):
    state = _phone_state(ShoppingGuideState(mode="recommend", category="phone"))
    state = _apply_deterministic_turn(state, message)
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])

    compiled = compiled_shopping_requirements(guide)
    assert any(
        item.key == "brand"
        and item.operator == "not_in"
        and item.value == ["apple"]
        and item.priority == "hard"
        for item in compiled
    )
    assert not any(
        item.key == "brand"
        and item.operator in {"eq", "in"}
        and "apple" in (
            item.value if isinstance(item.value, list) else [item.value]
        )
        for item in compiled
    )


def test_real_three_turn_correction_preserves_exclusion_and_scoped_priorities():
    state = _phone_state(ShoppingGuideState(
        mode="recommend",
        category="phone",
        useCases=["camera_title_claim"],
    ))

    state = _apply_deterministic_turn(state, "不喜欢苹果的")
    first_guide = ShoppingGuideState.model_validate(
        state.domain_state["shoppingGuide"]
    )
    assert first_guide.requirements == []
    assert first_guide.brand_avoidances[0].strength == "soft"

    state = _apply_deterministic_turn(
        state,
        "我都说了不要苹果的 我要安卓的 华为优先",
    )
    final_guide = ShoppingGuideState.model_validate(
        state.domain_state["shoppingGuide"]
    )
    compiled = [
        item.model_dump()
        for item in compiled_shopping_requirements(final_guide)
    ]

    assert compiled == [
        {
            "key": "os", "operator": "eq", "value": "android",
            "unit": "enum", "priority": "hard", "source": "user",
        },
        {
            "key": "brand", "operator": "eq", "value": "huawei",
            "unit": "text", "priority": "soft", "source": "user",
        },
        {
            "key": "brand", "operator": "not_in", "value": ["apple"],
            "unit": "text", "priority": "hard", "source": "user",
        },
    ]


def test_combined_exclusions_with_scoped_priority():
    state = _phone_state(ShoppingGuideState(mode="recommend", category="phone"))
    state = _apply_deterministic_turn(state, "不要苹果和三星，华为优先")
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])

    assert [item.model_dump() for item in guide.brand_avoidances] == [
        {"values": ["apple", "samsung"], "strength": "hard", "source": "user"},
    ]
    assert [item.model_dump() for item in compiled_shopping_requirements(guide)] == [
        {
            "key": "brand", "operator": "eq", "value": "huawei",
            "unit": "text", "priority": "soft", "source": "user",
        },
        {
            "key": "brand", "operator": "not_in", "value": ["apple", "samsung"],
            "unit": "text", "priority": "hard", "source": "user",
        },
    ]


def test_explicit_positive_releases_prior_avoidance():
    state = _phone_state(ShoppingGuideState.model_validate({
        "mode": "recommend",
        "category": "phone",
        "brandAvoidances": [{
            "values": ["apple"], "strength": "hard", "source": "user",
        }],
    }))
    state = _apply_deterministic_turn(state, "我要苹果")
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])

    assert guide.brand_avoidances == []
    assert [item.model_dump() for item in compiled_shopping_requirements(guide)] == [
        {
            "key": "brand", "operator": "eq", "value": "apple",
            "unit": "text", "priority": "hard", "source": "user",
        },
    ]


def test_same_turn_reject_and_accept_is_ambiguous_and_fails_closed():
    state = _phone_state(ShoppingGuideState(mode="recommend", category="phone"))

    with pytest.raises(TaskStatePayloadValidationError) as exc:
        _apply_deterministic_turn(state, "不要苹果，但苹果也可以")

    assert exc.value.code == "ambiguous_brand_negation_update"


def test_explicit_removal_of_prior_brand_avoidance_is_not_a_new_negation():
    parsed = parse_brand_negations("苹果也可以，撤掉刚才不要苹果的条件")
    assert parsed.negated_brands == frozenset()
    assert parsed.released_brands == ["apple"]

    state = _phone_state(ShoppingGuideState.model_validate({
        "mode": "recommend",
        "category": "phone",
        "brandAvoidances": [{
            "values": ["apple"], "strength": "hard", "source": "user",
        }],
    }))
    state = _apply_deterministic_turn(
        state,
        "苹果也可以，撤掉刚才不要苹果的条件",
    )
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])
    assert guide.brand_avoidances == []
    assert state.status == "ready"
    assert state.pending_questions == []


def test_retention_phrase_does_not_create_unbound_brand_negation():
    parsed = parse_brand_negations("如果没有就把预算放宽到800，其他条件不要动")
    assert parsed.targets == []
    assert parsed.unresolved_cues == []


def test_display_only_request_is_deterministic_control_language():
    assert _used_phone_presentation_only_request(
        "先不用把20个都详细写出来，只展示前三个"
    )
    assert not _used_phone_presentation_only_request("再推荐三款新的手机")


def test_other_brands_remain_after_explicit_exclusion():
    state = _phone_state(ShoppingGuideState(mode="recommend", category="phone"))
    state = _apply_deterministic_turn(state, "不要苹果，其他都行")
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])

    assert [item.model_dump() for item in compiled_shopping_requirements(guide)] == [
        {
            "key": "brand", "operator": "not_in", "value": ["apple"],
            "unit": "text", "priority": "hard", "source": "user",
        },
    ]


def test_brand_avoidance_never_inherits_across_tasks():
    first = _phone_state(ShoppingGuideState(mode="recommend", category="phone"))
    first = _apply_deterministic_turn(first, "不喜欢苹果的")
    assert ShoppingGuideState.model_validate(
        first.domain_state["shoppingGuide"]
    ).brand_avoidances

    fresh = _phone_state(ShoppingGuideState(mode="recommend", category="phone"))
    fresh_guide = ShoppingGuideState.model_validate(
        fresh.domain_state["shoppingGuide"]
    )
    assert fresh_guide.brand_avoidances == []
    assert compiled_shopping_requirements(fresh_guide) == []
