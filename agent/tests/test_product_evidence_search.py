import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.domains.ecommerce.models import (
    ShoppingRequirement,
    compare_product_details,
    rule_rerank_candidates,
)
from app.domains.ecommerce.ranking_contract import normalize_search_products_detail
from app.domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_RULESET_VERSION,
    materialize_used_phone_product_attributes,
)
from app.domains.ecommerce.tools import search_products_tool
from app.executor import StepExecutionResult
from app.harness import (
    _guide_product_from_presentation,
    _project_validator_tool_evidence,
)
from app.planning import PlanArgumentSource, PlanStep, TaskPlan
from app.schemas import ToolTrace
from app.validator import (
    ValidatorContext,
    ValidatorStepContext,
    _validate_guide_decision,
    validate_task_result,
)


def _product(product_id: int, memory: float | None, *, title: str | None = None):
    attributes = []
    if memory is not None:
        attributes.append({
            "key": "memory_gb",
            "normalizedNumber": memory,
            "normalizedBoolean": None,
            "normalizedText": None,
            "rawValue": f"{memory}GB",
            "evidenceField": "title",
            "extractionMethod": "deterministic_regex",
            "confidence": 1.0,
        })
    return {
        "id": product_id,
        "source": "kuaisearch",
        "title": title or f"测试手机 {product_id}",
        "brand": "测试品牌",
        "categoryL1": "手机/数码/电脑办公",
        "categoryL2": "手机通讯",
        "categoryL3": "智能手机",
        "snapshotPriceMinor": 299900,
        "currency": "CNY",
        "priceStatus": "verified",
        "lifecycleStatus": "ACTIVE",
        "entityVersion": 1,
        "availableQuantity": 1,
        "inventoryVersion": 1,
        "attributeText": "测试商品描述",
        "provenanceUrl": "https://example.test/catalog",
        "attributes": attributes,
    }


def _requirement(priority="hard"):
    return ShoppingRequirement(
        key="memory_gb",
        operator="gte",
        value=12,
        unit="GB",
        priority=priority,
        source="用户原话：至少 12GB 内存" if priority == "hard" else "inferred:大内存",
    )


def _with_enum_attribute(product, key, normalized, raw):
    copy = {**product, "attributes": list(product["attributes"])}
    copy["attributes"].append({
        "key": key,
        "normalizedNumber": None,
        "normalizedBoolean": None,
        "normalizedText": normalized,
        "rawValue": raw,
        "evidenceField": "relevance.attr_value",
        "extractionMethod": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
        "confidence": 1.0,
    })
    return copy


def test_rule_reranker_eliminates_hard_failures_and_puts_unknown_last():
    result = rule_rerank_candidates(
        "phone",
        [_product(3, None), _product(2, 16), _product(1, 8)],
        [_requirement()],
        {1: 1.0, 2: 0.4, 3: 2.0},
    )

    assert [row["product"]["id"] for row in result["products"]] == [2, 3]
    assert result["eliminated"][0]["productId"] == 1
    assert result["products"][1]["checks"][0]["displayValue"] == "未知"
    assert result["rankingTrace"]["confirmedBeforeUnknown"] is True


def test_rule_reranker_uses_fixed_formula_and_product_id_tie_break():
    result = rule_rerank_candidates(
        "phone",
        [_product(9, 16), _product(4, 16)],
        [_requirement(priority="soft")],
        {9: 1.0, 4: 1.0},
    )

    assert [row["product"]["id"] for row in result["products"]] == [4, 9]
    score = result["products"][0]["scoreBreakdown"]
    assert score["final"] == round(
        0.55 * score["normalizedRecall"]
        + 0.30 * score["softRequirementMatch"]
        + 0.15 * score["evidenceCompleteness"],
        8,
    )


def test_hard_max_budget_orders_eligible_products_from_ceiling_downward():
    products = [
        _product(1, 16),
        _product(2, 16),
        _product(3, 16),
        _product(4, 16),
    ]
    for product, price in zip(products, (90000, 295000, 250000, 310000)):
        product["snapshotPriceMinor"] = price
    budget = ShoppingRequirement(
        key="price_minor", operator="lte", value=300000,
        unit="CNY_MINOR", priority="hard", source="user",
    )

    result = rule_rerank_candidates(
        "phone", products, [budget], {1: 1.0, 2: 0.1, 3: 0.5, 4: 2.0}
    )

    assert [row["product"]["id"] for row in result["products"]] == [2, 3, 1]
    assert result["eliminated"][0]["productId"] == 4
    assert result["rankingTrace"]["budgetOrdering"] == "price_desc_within_hard_lte"


def test_ordered_battery_preference_precedes_budget_then_price_descends_in_tier():
    products = [
        _with_enum_attribute(_product(1, None), "battery_health", "80_90", "80%-90%"),
        _with_enum_attribute(_product(2, None), "battery_health", "90_plus", "90%+"),
        _with_enum_attribute(_product(3, None), "battery_health", "90_plus", "90%+"),
    ]
    for product, price in zip(products, (299000, 250000, 290000)):
        product["snapshotPriceMinor"] = price
    battery = ShoppingRequirement(
        key="battery_health", operator="in", value=["90_plus", "80_90"],
        unit="enum", priority="soft", source="user",
    )
    budget = ShoppingRequirement(
        key="price_minor", operator="lte", value=300000,
        unit="CNY_MINOR", priority="hard", source="user",
    )

    result = rule_rerank_candidates(
        "phone", products, [battery, budget], {1: 1.0, 2: 0.1, 3: 0.2}
    )

    assert [row["product"]["id"] for row in result["products"]] == [3, 2, 1]
    assert [row["softPreferenceScore"] for row in result["products"]] == [1.0, 1.0, 0.5]


def test_public_guide_product_id_survives_browser_json_precision_boundary():
    product_id = 2237583271291033870

    projected = _guide_product_from_presentation({
        "productId": product_id,
        "title": "大整数商品",
        "brand": "test",
        "attributes": [],
    })
    decoded = json.loads(json.dumps(projected, ensure_ascii=False))

    assert decoded["product"]["id"] == "2237583271291033870"
    assert isinstance(decoded["product"]["id"], str)


def test_brand_constraint_compares_canonical_identity_not_display_label():
    apple = _product(1, None)
    apple["brand"] = "苹果/Apple"
    other = _product(2, None)
    other["brand"] = "华为/HUAWEI"
    requirement = ShoppingRequirement(
        key="brand",
        operator="eq",
        value="apple",
        unit="text",
        priority="hard",
        source="user",
    )

    result = rule_rerank_candidates(
        "phone",
        [apple, other],
        [requirement],
        {1: 1.0, 2: 0.5},
    )

    assert [row["product"]["id"] for row in result["products"]] == [1]
    assert result["products"][0]["checks"][0]["actual"] == "苹果/Apple"
    assert result["products"][0]["checks"][0]["status"] == "pass"
    assert result["eliminated"][0]["productId"] == 2


def test_validator_uses_same_canonical_brand_identity_as_reranker():
    product = _product(1, None)
    product["brand"] = "华为/HUAWEI"
    requirement = ShoppingRequirement(
        key="brand",
        operator="eq",
        value="huawei",
        unit="text",
        priority="hard",
        source="user",
    )
    context, _ = _validator_context_for_product(product, requirement)

    assert _validate_guide_decision(context)[0] == "satisfied"


def test_used_phone_enum_requirement_uses_controlled_source_evidence():
    weak = _with_enum_attribute(
        _product(1, None), "battery_health", "80_90", "80%-90%"
    )
    matched = _with_enum_attribute(
        _product(2, None), "battery_health", "90_plus", "90%+"
    )
    unknown = _product(3, None)
    requirement = ShoppingRequirement(
        key="battery_health",
        operator="eq",
        value="90_plus",
        unit="enum",
        priority="hard",
        source="用户原话：电池健康 90% 以上",
    )

    result = rule_rerank_candidates(
        "phone", [weak, matched, unknown], [requirement], {1: 1.0, 2: 0.5, 3: 2.0}
    )

    assert [row["product"]["id"] for row in result["products"]] == [2, 3]
    check = result["products"][0]["checks"][0]
    assert check["status"] == "pass"
    assert check["actual"] == "90_plus"
    evidence = {item["ref"]: item for item in result["evidence"]}
    citation = evidence[check["evidenceRef"]]
    assert citation["rawValue"] == "90%+"
    assert citation["field"] == "relevance.attr_value"
    assert citation["method"] == USED_PHONE_ATTRIBUTE_RULESET_VERSION
    assert result["eliminated"][0]["productId"] == 1
    assert result["products"][1]["hardUnknowns"] == 1


@pytest.mark.parametrize(
    ("key", "value", "raw"),
    [
        ("motherboard_repair", "not_repaired", "主板未维修"),
        ("battery_originality", "original", "原装电池"),
        ("scratch_level", "light", "轻微划痕"),
        ("shell_condition", "normal", "外壳正常"),
    ],
)
def test_new_controlled_fields_filter_and_emit_bound_evidence(key, value, raw):
    product = _with_enum_attribute(_product(2, None), key, value, raw)
    requirement = ShoppingRequirement(
        key=key,
        operator="eq",
        value=value,
        unit="enum",
        priority="hard",
        source="user",
    )
    result = rule_rerank_candidates("phone", [product], [requirement], {2: 1.0})
    check = result["products"][0]["checks"][0]
    citation = next(
        item for item in result["evidence"] if item["ref"] == check["evidenceRef"]
    )
    assert check["status"] == "pass"
    assert check["actual"] == value
    assert citation["rawValue"] == raw
    assert citation["field"] == "relevance.attr_value"
    assert citation["method"] == USED_PHONE_ATTRIBUTE_RULESET_VERSION


def test_frozen_not_in_projection_eliminates_repaired_motherboard():
    repaired = _with_enum_attribute(
        _product(1, None), "motherboard_repair", "repaired", "主板有过维修"
    )
    clean = _with_enum_attribute(
        _product(2, None), "motherboard_repair", "not_repaired", "主板未维修"
    )
    requirement = ShoppingRequirement(
        key="motherboard_repair",
        operator="not_in",
        value=["repaired"],
        unit="enum",
        priority="hard",
        source="user",
    )
    result = rule_rerank_candidates(
        "phone", [repaired, clean], [requirement], {1: 1.0, 2: 0.5}
    )
    assert [row["product"]["id"] for row in result["products"]] == [2]
    assert result["eliminated"][0]["productId"] == 1


def test_conflict_snapshot_is_retained_but_never_used_as_a_fact():
    product = _product(7, None)
    product["attributes"] = list(
        materialize_used_phone_product_attributes("无划痕,明显划痕")
    )
    requirement = ShoppingRequirement(
        key="scratch_level",
        operator="eq",
        value="none",
        unit="enum",
        priority="hard",
        source="user",
    )
    result = rule_rerank_candidates("phone", [product], [requirement], {7: 1.0})
    row = result["products"][0]
    assert row["checks"][0]["status"] == "unknown"
    assert row["checks"][0]["actual"] is None
    assert row["checks"][0]["evidenceRef"] is None
    assert row["hardUnknowns"] == 1
    snapshot = row["product"]["attributes"][0]
    assert snapshot["rawValue"] == "无划痕,明显划痕"
    assert snapshot["normalizedText"] is None


def _validator_context_for_product(product, requirement):
    requirements = requirement if isinstance(requirement, list) else [requirement]
    detail = compare_product_details("phone", [product], requirements)
    step = PlanStep(
        stepId="compare",
        description="compare candidates",
        toolName="compare_products",
        arguments={},
        argumentSources={},
        expectedOutput={"requiresGuideDecision": True},
        status="executed",
    )
    now = datetime.now(timezone.utc)
    execution = StepExecutionResult(
        taskId="task",
        planId="plan",
        stepId="compare",
        toolName="compare_products",
        resolvedArguments={
            "productIds": [product["id"]],
            "category": "phone",
            "requirements": [item.model_dump() for item in requirements],
        },
        outcome="tool_succeeded",
        toolTrace=ToolTrace(
            tool="compare_products", ok=True,
            detail=_project_validator_tool_evidence("compare_products", detail),
        ),
        startedAt=now,
        finishedAt=now,
        durationMs=0,
    )
    return ValidatorStepContext(step=step, executionResult=execution), detail


def _mixed_controlled_validator_context():
    raw = (
        "iOS,90%+,原装屏,主板未维修,原装电池,无划痕,外壳正常"
    )
    product = _product(12, None)
    product["attributes"] = list(materialize_used_phone_product_attributes(raw))
    requirements = [
        ShoppingRequirement(
            key="motherboard_repair",
            operator="eq",
            value="not_repaired",
            unit="enum",
            priority="hard",
            source="user",
        ),
        ShoppingRequirement(
            key="battery_originality",
            operator="eq",
            value="original",
            unit="enum",
            priority="hard",
            source="user",
        ),
        ShoppingRequirement(
            key="scratch_level",
            operator="not_in",
            value=["obvious"],
            unit="enum",
            priority="soft",
            source="user",
        ),
    ]
    return _validator_context_for_product(product, requirements)


@pytest.mark.parametrize(
    "attributes",
    [
        [],
        list(materialize_used_phone_product_attributes("无划痕,明显划痕")),
    ],
)
def test_validator_accepts_unknown_and_conflict_only_as_unknown(attributes):
    product = _product(7, None)
    product["attributes"] = deepcopy(attributes)
    requirement = ShoppingRequirement(
        key="scratch_level",
        operator="eq",
        value="none",
        unit="enum",
        priority="hard",
        source="user",
    )
    context, detail = _validator_context_for_product(product, requirement)
    check = detail["products"][0]["checks"][0]
    assert check["status"] == "unknown"
    assert check["actual"] is None
    assert check["evidenceRef"] is None
    assert _validate_guide_decision(context)[0] == "satisfied"


def test_opaque_token_cannot_be_upgraded_by_a_forged_normalized_snapshot():
    product = _with_enum_attribute(
        _product(8, None), "battery_originality", "original", "原装"
    )
    requirement = ShoppingRequirement(
        key="battery_originality",
        operator="eq",
        value="original",
        unit="enum",
        priority="hard",
        source="user",
    )
    context, detail = _validator_context_for_product(product, requirement)
    check = detail["products"][0]["checks"][0]
    assert check["status"] == "unknown"
    assert check["evidenceRef"] is None
    assert _validate_guide_decision(context)[0:2] == (
        "invalid_evidence",
        "controlled_evidence_mismatch",
    )


def test_opaque_raw_with_null_normalized_value_remains_truthfully_unknown():
    product = _with_enum_attribute(
        _product(9, None), "battery_originality", None, "原装"
    )
    requirement = ShoppingRequirement(
        key="battery_originality",
        operator="eq",
        value="original",
        unit="enum",
        priority="hard",
        source="user",
    )
    context, detail = _validator_context_for_product(product, requirement)
    check = detail["products"][0]["checks"][0]
    assert check["status"] == "unknown"
    assert check["actual"] is None
    assert _validate_guide_decision(context)[0] == "satisfied"


def test_known_raw_cannot_be_downgraded_to_unknown_with_synchronized_counts():
    context, detail = _controlled_validator_context()
    detail = deepcopy(detail)
    row = detail["products"][0]
    check = row["checks"][0]
    snapshot = row["product"]["attributes"][0]
    snapshot["normalizedText"] = None
    check.update({"actual": None, "displayValue": "未知", "status": "unknown", "evidenceRef": None})
    row.update(
        {
            "hardFailures": 0,
            "hardUnknowns": 1,
            "fullyMatched": False,
            "selectionType": "closest_alternative",
        }
    )
    detail["comparisonMatrix"][0]["checks"] = deepcopy(row["checks"])
    detail["hasCompleteMatch"] = False

    result = _validate_guide_decision(_context_with_detail(context, detail))
    assert result[0:2] == ("invalid_evidence", "controlled_evidence_mismatch")


@pytest.mark.parametrize("tamper", ["field", "method", "raw_type", "duplicate"])
def test_unknown_snapshot_still_requires_unique_ruleset_bound_raw_evidence(tamper):
    product = _with_enum_attribute(
        _product(10, None), "battery_originality", None, "原装"
    )
    requirement = ShoppingRequirement(
        key="battery_originality",
        operator="eq",
        value="original",
        unit="enum",
        priority="hard",
        source="user",
    )
    context, detail = _validator_context_for_product(product, requirement)
    detail = deepcopy(detail)
    snapshot = detail["products"][0]["product"]["attributes"][0]
    if tamper == "field":
        snapshot["evidenceField"] = "title"
    elif tamper == "method":
        snapshot["extractionMethod"] = "llm_guess"
    elif tamper == "raw_type":
        snapshot["rawValue"] = 1
    else:
        detail["products"][0]["product"]["attributes"].append(deepcopy(snapshot))
    result = _validate_guide_decision(_context_with_detail(context, detail))
    assert result[0:2] == ("invalid_evidence", "controlled_evidence_mismatch")


def _controlled_validator_context(
    *, key="battery_health", value="90_plus", raw="90%+", operator="eq"
):
    requirement = ShoppingRequirement(
        key=key,
        operator=operator,
        value=value,
        unit="enum",
        priority="hard",
        source="user",
    )
    product = _with_enum_attribute(
        _product(2, None), key, value if isinstance(value, str) else value[0], raw
    )
    detail = compare_product_details("phone", [product], [requirement])
    step = PlanStep(
        stepId="compare",
        description="compare candidates",
        toolName="compare_products",
        arguments={},
        argumentSources={},
        expectedOutput={"requiresGuideDecision": True},
        status="executed",
    )
    now = datetime.now(timezone.utc)
    execution = StepExecutionResult(
        taskId="task",
        planId="plan",
        stepId="compare",
        toolName="compare_products",
        resolvedArguments={
            "productIds": [2],
            "category": "phone",
            "requirements": [requirement.model_dump()],
        },
        outcome="tool_succeeded",
        toolTrace=ToolTrace(
            tool="compare_products", ok=True,
            detail=_project_validator_tool_evidence("compare_products", detail),
        ),
        startedAt=now,
        finishedAt=now,
        durationMs=0,
    )
    return ValidatorStepContext(step=step, executionResult=execution), detail


def _context_with_detail(context, detail):
    execution = context.execution_result.model_copy(
        update={
            "tool_trace": context.execution_result.tool_trace.model_copy(
                update={"detail": detail}
            )
        }
    )
    return context.model_copy(update={"execution_result": execution})


def _context_with_resolved(context, resolved_arguments):
    execution = context.execution_result.model_copy(
        update={"resolved_arguments": resolved_arguments}
    )
    return context.model_copy(update={"execution_result": execution})


def test_compact_controlled_evidence_groups_expand_before_validation():
    context, detail = _controlled_validator_context()
    compact = deepcopy(detail)
    evidence = compact["evidence"]
    controlled = [
        item for item in evidence
        if ":attribute:" in item.get("ref", "")
    ]
    compact["evidence"] = [{
        "refs": [item["ref"] for item in controlled],
        **{
            key: controlled[0][key]
            for key in ("field", "method", "rawValue", "confidence")
            if key in controlled[0]
        },
    }, *[
        item for item in evidence
        if ":attribute:" not in item.get("ref", "")
    ]]

    result = _validate_guide_decision(_context_with_detail(context, compact))

    assert result[0] == "satisfied"


def test_compact_controlled_evidence_group_still_rejects_cross_identity_ref():
    context, detail = _controlled_validator_context()
    compact = deepcopy(detail)
    evidence = compact["evidence"]
    compact["evidence"] = [{
        "refs": ["product:999:attribute:battery_health"],
        **{
            key: evidence[0][key]
            for key in ("field", "method", "rawValue", "confidence")
            if key in evidence[0]
        },
    }]

    result = _validate_guide_decision(_context_with_detail(context, compact))

    assert result[0:2] == ("invalid_evidence", "evidence_reference_out_of_bounds")


@pytest.mark.parametrize(
    ("source", "product_ids", "expected_outcome"),
    [
        (
            PlanArgumentSource(kind="shopping_guide", reference="comparedIds"),
            (2, 3),
            "passed",
        ),
        (
            PlanArgumentSource(kind="shopping_guide", reference="comparedIds"),
            (2, 3, 4),
            "passed",
        ),
        (
            PlanArgumentSource(kind="shopping_guide", reference="comparedIds"),
            (2, 3, 4, 5),
            "validation_failed",
        ),
        (
            PlanArgumentSource(kind="task_state", reference="facts.visibleIds"),
            (2, 3),
            "validation_failed",
        ),
    ],
)
def test_direct_comparison_provenance_accepts_only_server_published_selection(
    source, product_ids, expected_outcome
):
    requirement = ShoppingRequirement(
        key="battery_health", operator="eq", value="90_plus", unit="enum",
        priority="hard", source="user",
    )
    products = [
        _with_enum_attribute(_product(product_id, None), "battery_health", "90_plus", "90%+")
        for product_id in product_ids
    ]
    detail = compare_product_details("phone", products, [requirement])
    step = PlanStep(
        stepId="compare", description="compare visible pair",
        toolName="compare_products",
        arguments={"productIds": list(product_ids)},
        argumentSources={"productIds": source},
        expectedOutput={"requiresGuideDecision": True}, status="executed",
    )
    now = datetime.now(timezone.utc)
    execution = StepExecutionResult(
        taskId="task", planId="plan", stepId="compare",
        toolName="compare_products",
        resolvedArguments={
            "productIds": list(product_ids), "category": "phone",
            "requirements": [requirement.model_dump()],
        },
        outcome="tool_succeeded",
        toolTrace=ToolTrace(tool="compare_products", ok=True, detail=detail),
        startedAt=now, finishedAt=now, durationMs=0,
    )
    plan = TaskPlan(
        planId="plan", basedOnRevision=1, status="active", steps=[step]
    )
    context = ValidatorContext(
        taskId="task", taskRevision=2, plan=plan,
        steps=[ValidatorStepContext(step=step, executionResult=execution)],
    )

    result = validate_task_result(context)

    assert result.outcome == expected_outcome
    if expected_outcome == "validation_failed":
        assert result.error_code == "product_provenance_mismatch"


def test_validator_accepts_product_field_raw_and_method_bound_controlled_evidence():
    context, _ = _controlled_validator_context()
    assert _validate_guide_decision(context)[0] == "satisfied"


def test_phone_comparison_without_preferences_projects_all_validated_fields():
    products = [
        _with_enum_attribute(
            _product(product_id, None), "battery_health", "90_plus", "90%+"
        )
        for product_id in (21, 22)
    ]
    detail = compare_product_details("phone", products, [])
    step = PlanStep(
        stepId="compare", description="compare fields", toolName="compare_products",
        arguments={"productIds": [21, 22]},
        argumentSources={
            "productIds": PlanArgumentSource(kind="shopping_guide", reference="comparedIds")
        },
        expectedOutput={"requiresGuideDecision": True}, status="executed",
    )
    now = datetime.now(timezone.utc)
    execution = StepExecutionResult(
        taskId="task", planId="plan", stepId="compare", toolName="compare_products",
        resolvedArguments={"productIds": [21, 22], "category": "phone", "requirements": []},
        outcome="tool_succeeded",
        toolTrace=ToolTrace(
            tool="compare_products", ok=True,
            detail=_project_validator_tool_evidence("compare_products", detail),
        ),
        startedAt=now, finishedAt=now, durationMs=0,
    )
    outcome, error, _, summary = _validate_guide_decision(
        ValidatorStepContext(step=step, executionResult=execution)
    )
    assert (outcome, error) == ("satisfied", None)
    finalists = summary["rankedFinalists"]
    assert [row["productId"] for row in finalists] == [21, 22]
    assert [row["title"] for row in finalists] == ["测试手机 21", "测试手机 22"]
    assert all(row["titleEvidenceRef"] == f"product:{row['productId']}:title" for row in finalists)
    assert all(len(row["fieldEvidence"]) == 7 for row in finalists)
    assert all(
        next(item for item in row["fieldEvidence"] if item["key"] == "battery_health")
        == {
            "key": "battery_health", "status": "known", "actual": "90_plus",
            "evidenceRef": f"product:{row['productId']}:attribute:battery_health",
        }
        for row in finalists
    )


def test_phone_comparison_field_projection_rejects_forged_normalized_value():
    context, detail = _controlled_validator_context()
    forged = deepcopy(detail)
    forged["products"][0]["product"]["attributes"][0]["normalizedText"] = "80_90"
    result = _validate_guide_decision(_context_with_detail(context, forged))
    assert result[0:2] == ("invalid_evidence", "controlled_evidence_mismatch")


def test_validator_binds_every_mixed_resolved_requirement_to_every_finalist_check():
    context, detail = _mixed_controlled_validator_context()
    assert len(detail["products"][0]["checks"]) == 3
    assert _validate_guide_decision(context)[0] == "satisfied"


@pytest.mark.parametrize("attack", ["missing", "extra", "duplicate"])
def test_validator_rejects_missing_extra_or_duplicate_finalist_checks(attack):
    context, detail = _mixed_controlled_validator_context()
    detail = deepcopy(detail)
    checks = detail["products"][0]["checks"]
    if attack == "missing":
        del checks[0]
    elif attack == "duplicate":
        checks.append(deepcopy(checks[0]))
    else:
        extra = deepcopy(checks[0])
        extra.update(
            {
                "key": "shell_condition",
                "operator": "eq",
                "expected": "normal",
                "unit": "enum",
                "priority": "soft",
                "source": "user",
                "actual": "normal",
                "status": "pass",
                "evidenceRef": "product:12:attribute:shell_condition",
            }
        )
        checks.append(extra)
    result = _validate_guide_decision(_context_with_detail(context, detail))
    assert result[0:2] == ("invalid_evidence", "requirement_check_mismatch")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("key", "shell_condition"),
        ("operator", "not_in"),
        ("expected", "repaired"),
        ("unit", "text"),
        ("priority", "soft"),
        ("source", "forged"),
    ],
)
def test_validator_rejects_substituted_check_contract_fields(field, replacement):
    context, detail = _mixed_controlled_validator_context()
    detail = deepcopy(detail)
    detail["products"][0]["checks"][0][field] = replacement
    result = _validate_guide_decision(_context_with_detail(context, detail))
    assert result[0:2] == ("invalid_evidence", "requirement_check_mismatch")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("hardFailures", 1),
        ("hardUnknowns", 1),
        ("softScore", 0),
        ("fullyMatched", False),
    ],
)
def test_validator_rejects_check_count_tampering(field, replacement):
    context, detail = _mixed_controlled_validator_context()
    detail = deepcopy(detail)
    detail["products"][0][field] = replacement
    result = _validate_guide_decision(_context_with_detail(context, detail))
    assert result[0:2] == ("invalid_evidence", "requirement_count_mismatch")


def test_validator_recomputes_ordered_soft_preference_score():
    product = _with_enum_attribute(
        _product(31, None), "battery_health", "80_90", "80%-90%"
    )
    requirement = ShoppingRequirement(
        key="battery_health", operator="in", value=["90_plus", "80_90"],
        unit="enum", priority="soft", source="user",
    )
    context, detail = _validator_context_for_product(product, requirement)

    assert detail["products"][0]["softPreferenceScore"] == 0.5
    assert _validate_guide_decision(context)[0] == "satisfied"

    forged = deepcopy(_project_validator_tool_evidence("compare_products", detail))
    forged["products"][0]["softPreferenceScore"] = 1.0
    result = _validate_guide_decision(_context_with_detail(context, forged))
    assert result[0:2] == ("invalid_evidence", "requirement_count_mismatch")


def test_validator_rejects_duplicate_or_changed_resolved_requirements():
    context, _ = _mixed_controlled_validator_context()
    resolved = deepcopy(context.execution_result.resolved_arguments)
    resolved["requirements"].append(deepcopy(resolved["requirements"][0]))
    assert _validate_guide_decision(_context_with_resolved(context, resolved))[0:2] == (
        "invalid_evidence",
        "requirement_check_mismatch",
    )

    resolved = deepcopy(context.execution_result.resolved_arguments)
    resolved["requirements"][0]["value"] = "repaired"
    assert _validate_guide_decision(_context_with_resolved(context, resolved))[0:2] == (
        "invalid_evidence",
        "requirement_check_mismatch",
    )


def test_validator_rejects_comparison_matrix_or_complete_match_tampering():
    context, detail = _mixed_controlled_validator_context()
    detail = deepcopy(detail)
    detail["comparisonMatrix"][0]["checks"] = detail["comparisonMatrix"][0][
        "checks"
    ][:-1]
    assert _validate_guide_decision(_context_with_detail(context, detail))[0:2] == (
        "invalid_evidence",
        "requirement_check_mismatch",
    )

    context, detail = _mixed_controlled_validator_context()
    detail = deepcopy(detail)
    detail["hasCompleteMatch"] = False
    assert _validate_guide_decision(_context_with_detail(context, detail))[0:2] == (
        "invalid_evidence",
        "requirement_count_mismatch",
    )


@pytest.mark.parametrize(
    ("key", "value", "raw"),
    [
        ("motherboard_repair", "not_repaired", "主板未维修"),
        ("battery_originality", "original", "原装电池"),
        ("scratch_level", "light", "轻微划痕"),
        ("shell_condition", "normal", "外壳正常"),
    ],
)
def test_validator_accepts_all_new_controlled_fields(key, value, raw):
    context, _ = _controlled_validator_context(key=key, value=value, raw=raw)
    assert _validate_guide_decision(context)[0] == "satisfied"


@pytest.mark.parametrize(
    ("key", "value", "raw", "forged"),
    [
        ("motherboard_repair", "not_repaired", "主板未维修", "主板有过维修"),
        ("battery_originality", "original", "原装电池", "非原装电池"),
        ("scratch_level", "light", "轻微划痕", "明显划痕"),
        ("shell_condition", "normal", "外壳正常", "外壳有磕碰"),
    ],
)
def test_validator_rejects_new_field_raw_value_tampering(key, value, raw, forged):
    context, detail = _controlled_validator_context(key=key, value=value, raw=raw)
    detail = deepcopy(detail)
    check = detail["products"][0]["checks"][0]
    citation = next(
        item for item in detail["evidence"] if item["ref"] == check["evidenceRef"]
    )
    citation["rawValue"] = forged
    result = _validate_guide_decision(_context_with_detail(context, detail))
    assert result[0:2] == ("invalid_evidence", "controlled_evidence_mismatch")


@pytest.mark.parametrize(
    "tamper",
    [
        "ref", "raw", "field", "method", "actual",
        "snapshot_missing", "snapshot_changed",
    ],
)
def test_validator_rejects_unbound_controlled_evidence(tamper):
    context, detail = _controlled_validator_context()
    detail = deepcopy(detail)
    check = detail["products"][0]["checks"][0]
    citation = next(
        item
        for item in detail["evidence"]
        if item["ref"] == check["evidenceRef"]
    )
    if tamper == "ref":
        forged = "product:999:attribute:battery_health"
        check["evidenceRef"] = forged
        citation["ref"] = forged
    elif tamper == "raw":
        citation["rawValue"] = "80%-90%"
    elif tamper == "field":
        citation["field"] = "title"
    elif tamper == "method":
        citation["method"] = "llm_guess"
    elif tamper == "actual":
        check["actual"] = "80_90"
    elif tamper == "snapshot_missing":
        detail["products"][0]["product"]["attributes"] = []
    else:
        attribute = detail["products"][0]["product"]["attributes"][0]
        attribute["normalizedText"] = "80_90"
        attribute["rawValue"] = "80%-90%"
    result = _validate_guide_decision(_context_with_detail(context, detail))
    assert result[0:2] == ("invalid_evidence", "controlled_evidence_mismatch")


class _Response:
    def __init__(self, data, *, failure=False):
        self.data = data
        self.failure = failure

    def raise_for_status(self):
        if self.failure:
            raise RuntimeError("injected backend recall failure")

    def json(self):
        return {"data": self.data}


class _FakeClient:
    fail_backend_recall = False
    products = [_product(1, 8), _product(2, 16), _product(3, None)]

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, params=None):
        if url.endswith("/retrieval"):
            return _Response(
                {
                    "products": self.products,
                    "channel": "elasticsearch",
                    "degradedChannels": [],
                },
                failure=self.fail_backend_recall,
            )
        return _Response(self.products)

    async def post(self, url, json=None):
        requested = set(json["productIds"])
        return _Response([item for item in self.products if item["id"] in requested])


class _Embedding:
    def embed(self, values):
        return [type("Vector", (), {"tolist": lambda self: [0.1, 0.2]})()]


class _Qdrant:
    def query_points(self, **kwargs):
        points = [type("Point", (), {"id": item})() for item in (3, 2, 1)]
        return type("Result", (), {"points": points})()


def _search(*, qdrant_failure=False, backend_failure=False):
    _FakeClient.fail_backend_recall = backend_failure
    vector_side_effect = RuntimeError("injected qdrant failure") if qdrant_failure else None
    with (
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "rrf"),
        patch("app.rag.get_embedding_model", return_value=_Embedding()),
        patch(
            "app.rag.get_qdrant_client",
            side_effect=vector_side_effect,
            return_value=None if qdrant_failure else _Qdrant(),
        ),
    ):
        return asyncio.run(search_products_tool(
            "至少 12GB 内存的手机",
            "手机",
            requirements=[_requirement().model_dump()],
        ))


def test_search_runs_recall_channels_and_authoritative_rerank():
    trace = _search()

    assert trace.ok is True
    assert trace.detail["candidateIds"] == [2, 3]
    assert trace.detail["retrievalTrace"]["fusion"] == "rrf"
    assert set(trace.detail["retrievalTrace"]["channels"]) == {
        "elasticsearch", "bm25", "qdrant", "structuredRequirements"
    }
    assert trace.detail["eliminated"][0]["productId"] == 1
    assert trace.detail["candidates"][0]["evidenceRefs"]
    assert trace.detail["candidates"][0]["scoreBreakdown"]["weights"] == {
        "recall": 0.55, "soft": 0.30, "evidence": 0.15
    }
    presentation = normalize_search_products_detail(
        trace.detail,
        requirements=[_requirement().model_dump()],
        category="phone",
    ).candidate_support["productPresentations"][0]
    assert presentation["productId"] == 2
    assert presentation["title"] == "测试手机 2"
    assert presentation["brand"] == "测试品牌"
    assert presentation["priceStatus"] == "verified"
    assert presentation["priceMinor"] == 299900
    assert presentation["priceEvidenceRef"] == "product:2:snapshotPriceMinor"


def test_search_tool_filters_new_field_and_keeps_conflict_after_known():
    repaired = _with_enum_attribute(
        _product(1, None), "motherboard_repair", "repaired", "主板有过维修"
    )
    clean = _with_enum_attribute(
        _product(2, None), "motherboard_repair", "not_repaired", "主板未维修"
    )
    conflict = _product(3, None)
    conflict["attributes"] = list(
        materialize_used_phone_product_attributes("主板未维修,主板有过维修")
    )
    requirement = ShoppingRequirement(
        key="motherboard_repair",
        operator="not_in",
        value=["repaired"],
        unit="enum",
        priority="hard",
        source="user",
    )
    with (
        patch.object(_FakeClient, "products", [repaired, clean, conflict]),
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "rrf"),
        patch("app.rag.get_embedding_model", return_value=_Embedding()),
        patch("app.rag.get_qdrant_client", return_value=_Qdrant()),
    ):
        trace = asyncio.run(
            search_products_tool(
                "主板不要维修过的二手手机",
                "手机",
                requirements=[requirement.model_dump()],
            )
        )

    assert trace.ok is True
    assert trace.detail["candidateIds"] == [2, 3]
    assert trace.detail["eliminated"][0]["productId"] == 1
    checks = {
        row["id"]: row["checks"][0]
        for row in trace.detail["candidates"]
    }
    assert checks[2]["status"] == "pass"
    assert checks[2]["evidenceRef"] == "product:2:attribute:motherboard_repair"
    assert checks[3]["status"] == "unknown"
    assert checks[3]["evidenceRef"] is None


def test_search_continues_when_qdrant_fails_and_records_degradation():
    trace = _search(qdrant_failure=True)

    assert trace.ok is True
    assert trace.detail["candidateIds"] == [2, 3]
    assert trace.detail["retrievalTrace"]["channels"]["qdrant"]["status"] == "failed"
    assert {item["channel"] for item in trace.detail["retrievalTrace"]["degraded"]} == {"qdrant"}


def test_rrf_vector_timeout_degrades_without_blocking_other_channels():
    _FakeClient.fail_backend_recall = False

    async def never_finishes(*_args, **_kwargs):
        await asyncio.Future()

    with (
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "rrf"),
        patch("app.domains.ecommerce.tools.settings.product_vector_timeout_seconds", 0.01),
        patch("app.domains.ecommerce.tools.asyncio.to_thread", new=never_finishes),
    ):
        trace = asyncio.run(search_products_tool(
            "至少 12GB 内存的手机",
            "手机",
            requirements=[_requirement().model_dump()],
        ))

    assert trace.ok is True
    assert trace.detail["retrievalTrace"]["channels"]["qdrant"]["status"] == "failed"
    assert trace.detail["candidateIds"] == [2, 3]


def test_bm25_mode_never_initializes_embedding_or_qdrant():
    _FakeClient.fail_backend_recall = False
    with (
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "bm25"),
        patch(
            "app.domains.ecommerce.tools._product_query_embedding",
            side_effect=AssertionError("BM25 mode must not initialize embeddings"),
        ),
        patch(
            "app.rag.get_qdrant_client",
            side_effect=AssertionError("BM25 mode must not connect to Qdrant"),
        ),
    ):
        trace = asyncio.run(search_products_tool(
            "至少 12GB 内存的手机",
            "手机",
            requirements=[_requirement().model_dump()],
        ))

    assert trace.ok is True
    assert trace.detail["retrievalTrace"]["channels"]["qdrant"] == {
        "status": "disabled",
        "count": 0,
        "reason": "retrieval_mode_bm25",
    }


def test_elasticsearch_production_mode_disables_local_recall_and_reranker():
    _FakeClient.fail_backend_recall = False
    with (
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "elasticsearch"),
        patch("app.domains.ecommerce.tools.settings.product_title_reranker_enabled", True),
        patch(
            "app.domains.ecommerce.tools.bm25_rank",
            side_effect=AssertionError("production path must not run local BM25"),
        ),
        patch(
            "app.domains.ecommerce.tools._product_query_embedding",
            side_effect=AssertionError("production path must not initialize dense recall"),
        ),
        patch(
            "app.domains.ecommerce.tools.rerank_titles",
            side_effect=AssertionError("production path must keep LLM reranking closed"),
        ),
    ):
        trace = asyncio.run(search_products_tool(
            "至少 12GB 内存的手机",
            "手机",
            requirements=[_requirement().model_dump()],
        ))

    channels = trace.detail["retrievalTrace"]["channels"]
    assert trace.ok is True
    assert trace.detail["retrievalTrace"]["fusion"] == "single_channel"
    assert channels["elasticsearch"]["analyzer"] == "standard"
    assert channels["bm25"]["status"] == "disabled"
    assert channels["qdrant"]["status"] == "disabled"
    assert channels["mysqlAuthority"]["status"] == "active"
    assert trace.detail["retrievalTrace"]["titleReranker"]["status"] == "disabled"


def test_hybrid_local_vector_fuses_without_qdrant_dependency():
    _FakeClient.fail_backend_recall = False
    with (
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "hybrid"),
        patch("app.domains.ecommerce.tools.settings.product_vector_backend", "local"),
        patch(
            "app.domains.ecommerce.tools._local_product_vector_rank",
            return_value=[3, 2, 1],
        ) as local_vector,
        patch(
            "app.rag.get_qdrant_client",
            side_effect=AssertionError("local vector mode must not connect to Qdrant"),
        ),
    ):
        trace = asyncio.run(search_products_tool(
            "至少 12GB 内存的手机",
            "手机",
            requirements=[_requirement().model_dump()],
        ))

    assert trace.ok is True
    assert trace.detail["retrievalTrace"]["channels"]["localVector"]["status"] == "active"
    assert trace.detail["retrievalTrace"]["channels"]["qdrant"]["status"] == "disabled"
    assert trace.detail["retrievalTrace"]["channels"]["localVector"]["embeddingFields"] == ["title"]
    local_vector.assert_called_once()


def test_game_query_expansion_is_sent_to_recall_and_exposed_in_trace():
    _FakeClient.fail_backend_recall = False
    with (
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "bm25"),
    ):
        trace = asyncio.run(search_products_tool("有没有适合打游戏的手机", "手机"))

    expansion = trace.detail["retrievalTrace"]["queryExpansion"]
    assert trace.ok is True
    assert expansion["applied"] is True
    assert "和平精英" in expansion["addedTerms"]
    assert expansion["factOrConstraint"] is False


def test_title_reranker_timeout_falls_back_to_deterministic_ranking():
    _FakeClient.fail_backend_recall = False
    with (
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "bm25"),
        patch("app.domains.ecommerce.tools.settings.product_title_reranker_enabled", True),
        patch("app.domains.ecommerce.tools.settings.deepseek_api_key", "configured"),
        patch(
            "app.domains.ecommerce.tools.rerank_titles",
            new=AsyncMock(side_effect=TimeoutError("injected timeout")),
        ),
    ):
        trace = asyncio.run(search_products_tool(
            "至少 12GB 内存的手机",
            "手机",
            requirements=[_requirement().model_dump()],
        ))

    assert trace.ok is True
    assert trace.detail["candidateIds"] == [2, 3]
    reranker = trace.detail["retrievalTrace"]["titleReranker"]
    assert reranker["status"] == "failed"
    assert reranker["reason"] == "TimeoutError"
    assert reranker["fallback"] == "bm25_vector_rule_ranking"


def test_search_continues_when_elasticsearch_fails_and_records_degradation():
    trace = _search(backend_failure=True)

    assert trace.ok is True
    assert trace.detail["candidateIds"] == [2, 3]
    assert trace.detail["retrievalTrace"]["channels"]["elasticsearch"]["status"] == "failed"
    assert "elasticsearch" in {
        item["channel"] for item in trace.detail["retrievalTrace"]["degraded"]
    }


def test_twenty_concurrent_mock_searches_complete_without_cross_request_failure():
    _FakeClient.fail_backend_recall = False
    started = time.perf_counter()

    async def run_all():
        return await asyncio.gather(*(
            search_products_tool(
                "至少 12GB 内存的手机",
                "手机",
                requirements=[_requirement().model_dump()],
            )
            for _ in range(20)
        ))

    with (
        patch("app.domains.ecommerce.tools.httpx.AsyncClient", _FakeClient),
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "rrf"),
        patch("app.rag.get_embedding_model", return_value=_Embedding()),
        patch("app.rag.get_qdrant_client", return_value=_Qdrant()),
    ):
        traces = asyncio.run(run_all())
    elapsed = time.perf_counter() - started

    assert len(traces) == 20
    assert all(trace.ok and trace.detail["candidateIds"] == [2, 3] for trace in traces)
    assert elapsed < 2.0  # mock control-flow check, not a real integration latency claim
