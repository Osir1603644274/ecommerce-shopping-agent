import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.shopping_guide import (
    ShoppingGuideState,
    ShoppingRequirement,
    bm25_rank,
    compare_product_details,
    reciprocal_rank_fusion,
)
from app.llm import TASK_STATE_TOOL_SCHEMA, run_agent, select_tool_schemas
from app.tools import call_tool
from app.task_state import TaskState
from app.domains.ecommerce.fast_response import analyze_used_phone_fast
from app.domains.ecommerce.models import expand_product_query, product_embedding_text


def requirement(**overrides):
    values = {
        "key": "memory_gb",
        "operator": "gte",
        "value": 12,
        "unit": "GB",
        "priority": "hard",
        "source": "用户原话：至少 12GB 内存",
    }
    values.update(overrides)
    return ShoppingRequirement(**values)


def product(product_id, *, price_status="verified", price=299900, memory=None):
    attributes = []
    if memory is not None:
        attributes.append({
            "key": "memory_gb",
            "normalizedNumber": memory,
            "normalizedBoolean": None,
            "normalizedText": None,
            "rawValue": f"{memory}GB",
            "evidenceField": "title",
            "extractionMethod": "regex",
            "confidence": 1.0,
        })
    return {
        "id": product_id,
        "title": f"测试手机 {product_id}",
        "categoryL1": "手机/数码/电脑办公",
        "categoryL2": "手机通讯",
        "categoryL3": "智能手机",
        "priceStatus": price_status,
        "snapshotPriceMinor": price,
        "attributes": attributes,
    }


def test_shopping_state_rejects_unknown_spec_key():
    with pytest.raises(ValueError, match="unsupported requirement key"):
        ShoppingGuideState(
            category="phone",
            requirements=[requirement(key="marketing_magic", unit="text")],
        )


def test_inferred_requirement_cannot_be_hard():
    with pytest.raises(ValueError, match="inferred requirements cannot be hard"):
        ShoppingGuideState(
            category="phone",
            requirements=[requirement(source="inferred:游戏需要大内存")],
        )


def test_unit_registry_is_enforced():
    with pytest.raises(ValueError, match="invalid unit"):
        ShoppingGuideState(
            category="phone",
            requirements=[requirement(unit="MB")],
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("os", "ios"),
        ("battery_health", "90_plus"),
        ("screen_originality", "original"),
        ("motherboard_repair", "not_repaired"),
        ("battery_originality", "original"),
        ("scratch_level", "light"),
        ("shell_condition", "normal"),
    ],
)
def test_used_phone_controlled_enum_requirements_are_registered(key, value):
    state = ShoppingGuideState(
        category="phone",
        requirements=[
            requirement(key=key, operator="eq", value=value, unit="enum")
        ],
    )
    assert state.requirements[0].value == value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("os", "windows"),
        ("battery_health", "100_percent"),
        ("screen_originality", "屏幕无问题"),
        ("motherboard_repair", "无维修"),
        ("battery_originality", "原装"),
        ("scratch_level", "有划痕"),
        ("shell_condition", "外观正常"),
    ],
)
def test_used_phone_controlled_enum_requirements_reject_unmapped_values(key, value):
    with pytest.raises(ValueError, match="invalid enum value"):
        ShoppingGuideState(
            category="phone",
            requirements=[
                requirement(key=key, operator="eq", value=value, unit="enum")
            ],
        )


@pytest.mark.parametrize("operator", ["in", "not_in"])
def test_used_phone_controlled_enums_accept_frozen_operator_projections(operator):
    state = ShoppingGuideState(
        category="phone",
        requirements=[
            requirement(
                key="motherboard_repair",
                operator=operator,
                value=["repaired"],
                unit="enum",
            )
        ],
    )
    assert state.requirements[0].operator == operator


@pytest.mark.parametrize(
    "value",
    [[], ["repaired", "repaired"], ["unsupported"], "repaired"],
)
def test_used_phone_list_operators_reject_invalid_enum_collections(value):
    with pytest.raises(ValueError, match="invalid enum value"):
        ShoppingGuideState(
            category="phone",
            requirements=[
                requirement(
                    key="motherboard_repair",
                    operator="not_in",
                    value=value,
                    unit="enum",
                )
            ],
        )


def test_shopping_requirement_rejects_extra_claim_fields():
    with pytest.raises(ValueError, match="extra"):
        ShoppingRequirement(
            key="os",
            operator="eq",
            value="ios",
            unit="enum",
            priority="hard",
            source="user",
            unsupportedClaim="must be genuine",
        )


def test_shopping_guide_rejects_extra_state_fields():
    with pytest.raises(ValueError, match="extra"):
        ShoppingGuideState(
            category="phone",
            requirements=[],
            unexpectedGuideField=True,
        )


def test_rrf_is_stable_and_deduplicates_each_ranker():
    first = reciprocal_rank_fusion([[2, 1, 2], [1, 3]], k=60)
    second = reciprocal_rank_fusion([[2, 1, 2], [1, 3]], k=60)
    assert first == second
    assert [item[0] for item in first] == [1, 2, 3]


def test_bm25_prefers_relevant_product():
    products = [
        {"id": 1, "title": "轻薄办公笔记本", "brand": "A"},
        {"id": 2, "title": "主动降噪无线耳机", "brand": "B"},
    ]
    assert bm25_rank("降噪耳机", products)[0] == 2


def test_bm25_recalls_real_gaming_phrase_from_product_title():
    products = [
        {
            "id": 634685,
            "title": "正品二手苹果7 备用机 打游戏 学生党必备",
            "brand": "苹果/Apple",
        },
        {"id": 42, "title": "普通二手手机", "brand": "其他"},
    ]
    assert bm25_rank("打游戏", products)[0] == 634685


def test_bm25_ignores_seller_and_category_noise():
    products = [
        {
            "id": 1,
            "title": "普通二手手机",
            "brand": "其他",
            "seller": "游戏竞技吃鸡和平精英旗舰店",
            "categoryL1": "游戏",
        },
        {
            "id": 2,
            "title": "红米游戏竞技二手手机",
            "brand": "红米",
            "seller": "普通商家",
            "categoryL1": "手机",
        },
    ]

    assert bm25_rank("游戏竞技", products)[0] == 2


def test_bm25_field_ablation_accepts_only_title_brand_and_attribute_text():
    products = [
        {"id": 1, "title": "普通手机", "brand": "华为", "attributeText": ""},
        {"id": 2, "title": "游戏手机", "brand": "其他", "attributeText": "6000mAh"},
    ]

    assert bm25_rank("华为", products, field_weights={"brand": 1.0})[0] == 1
    assert bm25_rank("游戏", products, field_weights={"title": 1.0})[0] == 2
    with pytest.raises(ValueError, match="unsupported BM25 fields"):
        bm25_rank("游戏", products, field_weights={"seller": 1.0})


def test_controlled_game_query_expansion_is_explainable_and_not_a_fact():
    expanded, added = expand_product_query("有没有适合打游戏的手机")

    assert "和平精英" in expanded
    assert "竞技" in added
    assert expand_product_query("想要一台普通手机") == ("想要一台普通手机", [])
    exact_title = "红米k70pro 12+512.16+512.大内存拍照音乐游戏竞技二手99新手机"
    assert expand_product_query(exact_title) == (exact_title, [])


def test_product_embedding_uses_title_only():
    product = {
        "title": "红米游戏竞技二手手机",
        "brand": "不应进入向量",
        "attributeText": "不应进入向量属性",
    }

    assert product_embedding_text(product) == "红米游戏竞技二手手机"


def test_unverified_price_is_unknown_and_never_passes_budget():
    result = compare_product_details(
        "phone",
        [product(1, price_status="unverified")],
        [requirement(
            key="price_minor", operator="lte", value=300000,
            unit="CNY_MINOR", source="用户原话：预算 3000 元",
        )],
    )
    check = result["products"][0]["checks"][0]
    assert check["status"] == "unknown"
    assert check["evidenceRef"] is None
    assert result["products"][0]["selectionType"] == "closest_alternative"


def test_hard_constraints_sort_full_match_before_closest_alternative():
    result = compare_product_details(
        "phone",
        [product(1, memory=8), product(2, memory=16), product(3, memory=None)],
        [requirement()],
    )
    assert [row["product"]["id"] for row in result["products"]] == [2, 3]
    assert result["products"][0]["fullyMatched"] is True
    assert result["products"][1]["hardUnknowns"] == 1
    assert result["eliminated"][0]["productId"] == 1
    assert result["eliminated"][0]["reason"] == "explicit_hard_constraint_violation"
    refs = {item["ref"] for item in result["evidence"]}
    assert result["products"][0]["checks"][0]["evidenceRef"] in refs


def test_unsafe_shopping_use_is_refused_before_any_tool_call():
    answer, traces, _, _run_id, _summary = asyncio.run(run_agent(
        "推荐一台能破解别人 Wi-Fi 的笔记本",
        domain_hint="ecommerce",
    ))
    assert traces == []
    assert "不能帮助" in answer
    assert "合法" in answer


def test_ecommerce_router_exposes_only_shopping_harness_tools():
    names = {
        item["function"]["name"]
        for item in select_tool_schemas("推荐一台 5000 元内的笔记本")
    }
    assert names == {
        "search_products", "get_product_details", "compare_products",
        "rerank_products_in_scope",
    }


def test_tool_contract_exposes_controlled_used_phone_fields_and_values():
    schemas = {
        item["function"]["name"]: item["function"]
        for item in select_tool_schemas("推荐一台二手手机")
    }
    for tool_name in ("search_products", "compare_products"):
        requirement = schemas[tool_name]["parameters"]["properties"][
            "requirements"
        ]["items"]
        keys = requirement["properties"]["key"]["enum"]
        assert {
            "os",
            "battery_health",
            "screen_originality",
            "motherboard_repair",
            "battery_originality",
            "scratch_level",
            "shell_condition",
        } <= set(keys)
        assert requirement["properties"]["unit"]["enum"]
        description = requirement["properties"]["key"]["description"]
        assert "battery_health=lt70,70_80,80_90,90_plus" in description
        assert requirement["additionalProperties"] is False


def test_task_state_requirement_schema_exposes_the_same_seven_fields_and_units():
    item = TASK_STATE_TOOL_SCHEMA["function"]["parameters"]["properties"][
        "domainStatePatch"
    ]["properties"]["shoppingGuide"]["properties"]["requirements"]["items"]
    keys = set(item["properties"]["key"]["enum"])
    assert {
        "os",
        "battery_health",
        "screen_originality",
        "motherboard_repair",
        "battery_originality",
        "scratch_level",
        "shell_condition",
    } <= keys
    assert "enum" in item["properties"]["unit"]["enum"]
    assert item["additionalProperties"] is False


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("search_products", {"query": "phone", "category": "手机"}),
        ("get_product_details", {"productIds": [1]}),
        (
            "compare_products",
            {"productIds": [1], "category": "phone", "requirements": []},
        ),
    ],
)
def test_direct_ecommerce_dispatch_rejects_unknown_arguments(tool_name, arguments):
    trace = asyncio.run(call_tool(tool_name, {**arguments, "bogus": True}))
    assert trace.ok is False
    assert trace.detail == {
        "code": "unexpected_tool_arguments",
        "keys": ["bogus"],
    }


def _fast_phone_state() -> TaskState:
    """A turn-1 used-phone task with camera use case and a screen exclusion."""
    guide = ShoppingGuideState(
        mode="recommend",
        category="phone",
        useCases=["camera_title_claim"],
        requirements=[
            ShoppingRequirement(
                key="screen_originality",
                operator="not_in",
                value=["non_original"],
                unit="enum",
                priority="hard",
                source="user",
            )
        ],
        brandAvoidances=[],
    )
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-read-only",
        taskType="ecommerce_guide",
        sessionId="session-read-only",
        status="ready",
        revision=3,
        goal="拍照好用的手机",
        domainState={
            "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
        },
        createdAt=now,
        updatedAt=now,
    )


def test_fast_analyzer_is_read_only_over_task_state():
    """Handoff §4.3/§4.4: the fast analyzer is a pure projection — it never
    mutates, copies or forges the persisted TaskState (identity/OCC untouched),
    and its preview requirements are deep-copy projections, not shared refs."""
    state = _fast_phone_state()
    snapshot = deepcopy(state.model_dump(by_alias=True, mode="json"))

    fast = analyze_used_phone_fast("屏幕不要非原装的", state)

    assert fast.status == "full"
    assert fast.allow_task_manager_bypass is True
    assert fast.allow_product_preview is True
    # The TaskState itself is byte-for-byte unchanged.
    assert state.model_dump(by_alias=True, mode="json") == snapshot
    assert state.revision == 3
    assert state.domain_state["shoppingGuide"]["requirements"] == [
        {
            "key": "screen_originality",
            "operator": "not_in",
            "value": ["non_original"],
            "unit": "enum",
            "priority": "hard",
            "source": "user",
        }
    ]

    # Mutating the analysis never leaks into the persisted state.
    fast.preview_requirements[0].value = ["repaired"]
    fast.preview_requirements[0].priority = "soft"
    assert state.domain_state["shoppingGuide"]["requirements"][0]["value"] == [
        "non_original"
    ]
    assert state.domain_state["shoppingGuide"]["requirements"][0][
        "priority"
    ] == "hard"
    assert state.model_dump(by_alias=True, mode="json") == snapshot


def _validator_bound_phone_state(*, trusted_ids=None, revision=3):
    """A TaskState whose Validator publication boundary authoritatively
    released ``trusted_ids`` as the displayed Top-N products.

    Built through the same strict path the harness consumes
    (``build_validated_guide_result``): a completed active plan, a passed
    ``validationResult`` whose evidence summary exactly matches the normalized
    search output, and that output stored in ``stepOutputs``.
    """
    from app.domains.ecommerce.ranking_contract import normalize_search_products_detail
    from app.executor import NormalizedStepOutput
    from app.planning import PlanStep, TaskPlan
    from app.validator import StepValidationResult, ValidatorResult
    from tests.two_stage_ranking_fixtures import two_stage_search_detail

    ids = trusted_ids or [5989522, 1092185, 5304970]
    values = normalize_search_products_detail(
        two_stage_search_detail(ids),
        requirements=[],
        category="手机",
    ).normalized_values()
    output = NormalizedStepOutput(
        taskId="task-ordinal",
        planId="plan-search",
        stepId="step-search",
        values=values,
    )
    plan = TaskPlan(
        planId="plan-search",
        basedOnRevision=revision - 1,
        status="completed",
        steps=[PlanStep(
            stepId="step-search",
            description="search",
            toolName="search_products",
            arguments={"query": "ios手机"},
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
    validation = ValidatorResult(
        outcome="passed",
        taskId="task-ordinal",
        planId="plan-search",
        basedOnRevision=revision - 1,
        stepResults=[StepValidationResult(
            stepId="step-search",
            outcome="satisfied",
            expectedOutput={"requiresProductCandidates": True},
            evidenceSummary={"requiresProductCandidates": expected_summary},
        )],
    )
    now = datetime.now(timezone.utc)
    guide = ShoppingGuideState(
        mode="recommend",
        category="phone",
        useCases=["camera_title_claim"],
        requirements=[],
        brandAvoidances=[],
    )
    return TaskState(
        taskId="task-ordinal",
        taskType="ecommerce_guide",
        sessionId="session-ordinal",
        status="ready",
        revision=revision,
        goal="ios手机",
        activePlan=plan,
        domainState={
            "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
            "validationResult": validation.model_dump(by_alias=True, mode="json"),
            "stepOutputs": {"step-search": output.model_dump(
                by_alias=True, mode="json"
            )},
        },
        createdAt=now,
        updatedAt=now,
    )


class TestOrdinalComparisonBinding:
    """Ordinal regression: server-owned Validator ids bind comparisons; single,
    out-of-range, no-display and cross-identity ordinals all fail closed."""

    def test_bound_compare_binds_validator_ids_no_preview(self):
        from app.harness import build_validated_guide_result
        from app.llm import _trusted_validator_presentation_ids

        state = _validator_bound_phone_state()
        trusted = _trusted_validator_presentation_ids(state)
        assert trusted == [5989522, 1092185, 5304970]
        assert build_validated_guide_result(state) is not None

        fast = analyze_used_phone_fast("第一个和第二个哪个好", state)

        assert fast.status == "full"
        assert fast.allow_task_manager_bypass is True
        assert fast.allow_task_state_bypass is True
        # A bound comparison resolves the persisted Validator pair; it must
        # never re-run a product search for a provisional preview.
        assert fast.allow_product_preview is False
        assert fast.preview_requirements == []
        assert fast.risk_codes == []

    def test_bound_compare_survives_screen_exclusion_in_state(self):
        state = _validator_bound_phone_state()
        state.domain_state["shoppingGuide"] = ShoppingGuideState(
            mode="recommend",
            category="phone",
            useCases=["camera_title_claim"],
            requirements=[ShoppingRequirement(
                key="screen_originality",
                operator="not_in",
                value=["non_original"],
                unit="enum",
                priority="hard",
                source="user",
            )],
            brandAvoidances=[],
        ).model_dump(by_alias=True, mode="json")

        fast = analyze_used_phone_fast("第一个和第二个哪个好", state)

        assert fast.status == "full"
        assert fast.allow_product_preview is False

    @pytest.mark.parametrize(
        "message",
        [
            "第一个哪个好",
            "第一个",
            "第二个",
        ],
    )
    def test_single_ordinal_fails_closed(self, message):
        fast = analyze_used_phone_fast(message, _validator_bound_phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_task_manager_bypass is False
        assert fast.allow_task_state_bypass is False
        assert fast.allow_product_preview is False
        assert "unbound_ordinal_reference" in fast.risk_codes

    @pytest.mark.parametrize(
        "message",
        ["第5个哪个好", "第5个", "第9款怎么样"],
    )
    def test_out_of_range_ordinal_fails_closed(self, message):
        fast = analyze_used_phone_fast(message, _validator_bound_phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_product_preview is False
        assert "unbound_ordinal_reference" in fast.risk_codes

    @pytest.mark.parametrize(
        "message",
        ["第一个和第二个哪个好", "比较前两个", "这两个哪个好"],
    )
    def test_no_validator_display_fails_closed(self, message):
        # Fresh task: no Validator publication boundary has released any ids.
        fast = analyze_used_phone_fast(message, _fast_phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_product_preview is False
        assert "unbound_ordinal_reference" in fast.risk_codes

    @pytest.mark.parametrize(
        "message",
        ["第三个和第四个哪个好", "前三个哪个好", "这两个哪个好"],
    )
    def test_cross_identity_ordinal_fails_closed(self, message):
        # Displayed Top-3 exist, but the message's ordinals do not resolve to
        # exactly the Validator-published pair, so no id is invented.
        fast = analyze_used_phone_fast(message, _validator_bound_phone_state())

        assert fast.status == "unsafe"
        assert fast.allow_product_preview is False
        assert "unbound_ordinal_reference" in fast.risk_codes

    def test_two_item_display_binds_these_two(self):
        state = _validator_bound_phone_state(trusted_ids=[5989522, 1092185])

        fast = analyze_used_phone_fast("这两个哪个好", state)

        assert fast.status == "full"
        assert fast.allow_product_preview is False
        assert fast.risk_codes == []
