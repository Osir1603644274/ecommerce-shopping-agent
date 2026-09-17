"""Brand-negation ranking behaviour (handoff 5.1/5.2).

These tests pin the deterministic rerank contract for explicit brand
avoidances: a hard ``brand not_in`` eliminates a candidate outright, a soft one
down-weights without eliminating, an Android-hard + Huawei-soft + Apple-hard
turn ranks Huawei first while never restoring Apple or a non-Android device,
and multiple hard/soft avoidances keep independent operator/value/priority
checks instead of being collapsed under one ``brand`` key.

The last tests exercise the two-stage contract's duplicate-key identity path:
``normalize_search_products_detail`` must accept a ranked detail whose resolved
requirements carry several ``brand`` rows and fail closed when any identity
(operator/value/priority) is substituted.
"""

import pytest

from app.domains.ecommerce.models import (
    ShoppingRequirement,
    rule_rerank_candidates,
    structured_requirement_rank,
)
from app.domains.ecommerce.ranking_contract import (
    RANKING_FORMULA,
    RANKING_TIE_BREAK,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
    TwoStageRankingContractError,
    normalize_search_products_detail,
)
from app.domains.ecommerce.used_phone_attributes import (
    materialize_used_phone_product_attributes,
)


def _phone(product_id: int, brand: str, *, os_token: str | None = None):
    """A phone-shaped candidate whose attributes come from the frozen observer."""
    return {
        "id": product_id,
        "source": "kuaisearch",
        "title": f"二手手机 {product_id}",
        "brand": brand,
        "categoryL1": "手机/数码/电脑办公",
        "categoryL2": "手机通讯",
        "categoryL3": "智能手机",
        "snapshotPriceMinor": 199900,
        "currency": "CNY",
        "priceStatus": "verified",
        "attributeText": os_token or "测试商品描述",
        "provenanceUrl": "https://example.test/catalog",
        "attributes": list(materialize_used_phone_product_attributes(os_token)),
    }


def _brand_req(operator, value, priority):
    return ShoppingRequirement(
        key="brand",
        operator=operator,
        value=value,
        unit="text",
        priority=priority,
        source="user",
    )


def _os_req(operator, value, priority):
    return ShoppingRequirement(
        key="os",
        operator=operator,
        value=value,
        unit="enum",
        priority=priority,
        source="user",
    )


def _check_map(checks):
    return {
        (item["key"], item["operator"], repr(item["expected"])): item
        for item in checks
    }


def test_hard_brand_avoidance_eliminates_apple_outright():
    products = [
        _phone(1, "苹果/Apple"),
        _phone(2, "华为/HUAWEI"),
        _phone(3, "OPPO"),
    ]

    result = rule_rerank_candidates(
        "phone",
        products,
        [_brand_req("not_in", ["apple"], "hard")],
        {1: 1.0, 2: 0.9, 3: 0.8},
    )

    assert [row["product"]["id"] for row in result["products"]] == [2, 3]
    eliminated = result["eliminated"]
    assert eliminated[0]["productId"] == 1
    assert eliminated[0]["reason"] == "explicit_hard_constraint_violation"


def test_soft_brand_avoidance_downweights_without_eliminating_apple():
    products = [
        _phone(1, "苹果/Apple"),
        _phone(2, "华为/HUAWEI"),
        _phone(3, "OPPO"),
    ]

    result = rule_rerank_candidates(
        "phone",
        products,
        [_brand_req("not_in", ["apple"], "soft")],
        {1: 1.0, 2: 0.9, 3: 0.8},
    )

    assert result["eliminated"] == []
    assert [row["product"]["id"] for row in result["products"]] == [2, 3, 1]
    apple_row = result["products"][-1]
    assert apple_row["product"]["id"] == 1
    assert apple_row["softScore"] == 0
    check = next(item for item in apple_row["checks"] if item["key"] == "brand")
    assert check["status"] == "fail"
    assert check["priority"] == "soft"


def test_android_hard_apple_hard_huawei_soft_prioritizes_huawei():
    products = [
        _phone(1, "苹果/Apple", os_token="iOS"),
        _phone(2, "华为/HUAWEI", os_token="安卓"),
        _phone(3, "OPPO", os_token="安卓"),
        _phone(4, "三星/Samsung", os_token="iOS"),
    ]
    requirements = [
        _os_req("eq", "android", "hard"),
        _brand_req("eq", "huawei", "soft"),
        _brand_req("not_in", ["apple"], "hard"),
    ]

    result = rule_rerank_candidates(
        "phone",
        products,
        requirements,
        {1: 1.0, 2: 0.95, 3: 0.9, 4: 0.85},
    )

    # Turn-3 acceptance: Apple and every non-Android device are gone, Huawei
    # ranks first among the qualifying Androids, and Apple never returns.
    assert sorted(row["product"]["id"] for row in result["products"]) == [2, 3]
    assert result["products"][0]["product"]["id"] == 2
    assert sorted(item["productId"] for item in result["eliminated"]) == [1, 4]
    assert all(
        item["reason"] == "explicit_hard_constraint_violation"
        for item in result["eliminated"]
    )
    huawei_checks = _check_map(result["products"][0]["checks"])
    assert huawei_checks[("os", "eq", "'android'")]["status"] == "pass"
    assert huawei_checks[("brand", "eq", "'huawei'")]["status"] == "pass"
    assert huawei_checks[("brand", "not_in", "['apple']")]["status"] == "pass"


def test_multiple_brand_avoidances_keep_independent_contract():
    products = [
        _phone(1, "苹果/Apple"),
        _phone(2, "三星/Samsung"),
        _phone(3, "华为/HUAWEI"),
        _phone(4, "OPPO"),
    ]
    requirements = [
        _brand_req("not_in", ["apple"], "hard"),
        _brand_req("not_in", ["samsung"], "hard"),
        _brand_req("not_in", ["oppo"], "soft"),
    ]

    result = rule_rerank_candidates(
        "phone",
        products,
        requirements,
        {1: 1.0, 2: 0.9, 3: 0.8, 4: 0.7},
    )

    assert sorted(item["productId"] for item in result["eliminated"]) == [1, 2]
    assert [row["product"]["id"] for row in result["products"]] == [3, 4]
    brand_checks = [
        item for item in result["products"][0]["checks"] if item["key"] == "brand"
    ]
    assert [(item["operator"], item["expected"], item["priority"])
            for item in brand_checks] == [
        ("not_in", ["apple"], "hard"),
        ("not_in", ["samsung"], "hard"),
        ("not_in", ["oppo"], "soft"),
    ]


def test_structured_recall_channel_excludes_hard_brand_eliminations():
    products = [
        _phone(1, "苹果/Apple"),
        _phone(2, "华为/HUAWEI"),
        _phone(3, "OPPO"),
    ]
    requirements = [
        _brand_req("not_in", ["apple"], "hard"),
        _brand_req("eq", "huawei", "soft"),
    ]

    ranked = structured_requirement_rank(
        "phone", products, requirements, lexical_rank=[1, 2, 3]
    )

    assert ranked == [2, 3]


def _two_stage_detail(products, requirements, retrieval_scores):
    """Build a valid two-stage detail from the deterministic rerank output."""
    reranked = rule_rerank_candidates(
        "phone", products, requirements, retrieval_scores
    )
    ranked_rows = reranked["products"]
    pool_ids = [int(product["id"]) for product in products]
    ranked_ids = [int(row["product"]["id"]) for row in ranked_rows]
    evidence = []
    candidates = []
    for row in ranked_rows:
        product = row["product"]
        product_id = int(product["id"])
        row_evidence = [
            item
            for item in row["evidence"]
            if str(item["ref"]).startswith(f"product:{product_id}:")
        ]
        evidence.extend(row_evidence)
        candidates.append({
            "id": product_id,
            "title": product["title"],
            "brand": product["brand"],
            "priceStatus": product["priceStatus"],
            "snapshotPriceMinor": product["snapshotPriceMinor"],
            "currency": product["currency"],
            "attributes": product["attributes"],
            "checks": row["checks"],
            "evidenceRefs": [item["ref"] for item in row_evidence],
        })
    return {
        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
        "candidatePoolIds": pool_ids,
        "rankedItemIds": ranked_ids,
        "candidateIds": ranked_ids,
        "candidates": candidates,
        "retrievalTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "candidatePoolCount": len(pool_ids),
            "authoritativeFactCount": len(pool_ids),
        },
        "rankingTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "inputCandidateCount": len(pool_ids),
            "rankedItemCount": len(ranked_ids),
            "tieBreak": RANKING_TIE_BREAK,
            "formula": RANKING_FORMULA,
        },
        "citationTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "sourceTool": "search_products",
            "rankedItemIds": ranked_ids,
            "evidenceRefCount": len(evidence),
            "binding": "current_successful_tool_call_ranked_items_only",
        },
        "evidenceRefs": [item["ref"] for item in evidence],
        "evidence": evidence,
        "eliminated": reranked["eliminated"],
    }


def test_contract_accepts_duplicate_key_brand_requirements_via_multiset_identity():
    products = [
        _phone(1, "苹果/Apple", os_token="iOS"),
        _phone(2, "华为/HUAWEI", os_token="安卓"),
        _phone(3, "OPPO", os_token="安卓"),
    ]
    requirements = [
        _brand_req("not_in", ["apple"], "hard"),
        _brand_req("eq", "huawei", "soft"),
        _os_req("eq", "android", "hard"),
    ]
    detail = _two_stage_detail(products, requirements, {1: 1.0, 2: 0.95, 3: 0.9})

    output = normalize_search_products_detail(
        detail,
        requirements=[item.model_dump() for item in requirements],
        category="phone",
    )

    assert output.ranked_item_ids == (2, 3)
    support = output.candidate_support
    assert support["fullySupportedProductIds"] == [2, 3]
    assert [item["productId"] for item in support["productPresentations"]] == [2, 3]
    assert [item["brand"] for item in support["productPresentations"]] == [
        "华为/HUAWEI", "OPPO",
    ]


def test_contract_rejects_substituted_brand_requirement_identity():
    products = [
        _phone(1, "苹果/Apple", os_token="iOS"),
        _phone(2, "华为/HUAWEI", os_token="安卓"),
        _phone(3, "OPPO", os_token="安卓"),
    ]
    requirements = [
        _brand_req("not_in", ["apple"], "hard"),
        _brand_req("eq", "huawei", "soft"),
        _os_req("eq", "android", "hard"),
    ]
    detail = _two_stage_detail(products, requirements, {1: 1.0, 2: 0.95, 3: 0.9})
    tampered = [item.model_dump() for item in requirements]
    tampered[0]["value"] = ["samsung"]

    with pytest.raises(TwoStageRankingContractError) as exc:
        normalize_search_products_detail(
            detail, requirements=tampered, category="phone"
        )

    assert exc.value.code == "candidate_support_mismatch"
