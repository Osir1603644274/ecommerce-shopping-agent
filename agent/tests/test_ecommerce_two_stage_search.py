import asyncio
from unittest.mock import AsyncMock, patch

from app.domains.ecommerce.ranking_contract import (
    RANKING_FORMULA,
    RANKING_TIE_BREAK,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
)
from app.domains.ecommerce.models import (
    ShoppingRequirement,
    structured_requirement_rank,
)
from app.domains.ecommerce.used_phone_attributes import (
    used_phone_attribute_ruleset_payload,
)
from app.domains.ecommerce.tools import search_products_tool
from app.schemas import ToolTrace


class _Response:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": self._data}


class _Client:
    def __init__(self, catalog=None):
        self.catalog = list(catalog or [])

    async def get(self, url, params=None):
        if url.endswith("/api/products/retrieval"):
            return _Response({
                "channel": "elasticsearch",
                "products": (
                    [{"id": item["id"]} for item in self.catalog]
                    if self.catalog else [{"id": 1}, {"id": 2}, {"id": 3}]
                ),
            })
        return _Response(self.catalog)


def _reranked_only_product_three(category, products, requirements, scores, *, limit):
    assert [item["id"] for item in products] == [1, 3]
    evidence = [{"ref": "product:3:title", "field": "title", "rawValue": "three"}]
    return {
        "products": [{
            "product": products[1],
            "facts": {"title": "three"},
            "checks": [],
            "selectionType": "full_match",
            "scoreBreakdown": {"final": 1.0},
            "evidenceRefs": ["product:3:title"],
            "evidence": evidence,
        }],
        "eliminated": [{"productId": 1, "reason": "explicit_hard_constraint_violation"}],
        "rankingTrace": {
            "inputCandidateCount": 2,
            "eligibleCandidateCount": 1,
            "eliminatedHardViolationCount": 1,
            "confirmedBeforeUnknown": True,
            "tieBreak": RANKING_TIE_BREAK,
            "formula": RANKING_FORMULA,
        },
        "evidence": evidence,
    }


def test_search_exposes_resolved_pool_and_independent_ranked_subset():
    resolved = ToolTrace(
        tool="get_product_details",
        ok=True,
        detail={
            "products": [{"id": 3, "title": "three"}, {"id": 1, "title": "one"}],
            "productIds": [3, 1],
        },
    )
    with (
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "bm25"),
        patch("app.domains.ecommerce.tools._product_http_client", return_value=_Client()),
        patch("app.domains.ecommerce.tools._product_query_embedding", side_effect=RuntimeError("offline")),
        patch(
            "app.domains.ecommerce.tools.reciprocal_rank_fusion",
            return_value=[(1, 3.0), (2, 2.0), (3, 1.0)],
        ),
        patch(
            "app.domains.ecommerce.tools.get_product_details_tool",
            new=AsyncMock(return_value=resolved),
        ),
        patch(
            "app.domains.ecommerce.tools.rule_rerank_candidates",
            side_effect=_reranked_only_product_three,
        ),
    ):
        result = asyncio.run(
            search_products_tool("phone", "phone", requirements=[])
        )

    assert result.ok is True
    detail = result.detail
    assert detail["contractVersion"] == TWO_STAGE_RANKING_CONTRACT_VERSION
    assert detail["retrievalTrace"]["fusedTop50"] == [1, 2, 3]
    assert detail["candidatePoolIds"] == [1, 3]
    assert detail["rankedItemIds"] == [3]
    assert detail["candidateIds"] == [3]
    assert [item["id"] for item in detail["candidates"]] == [3]
    assert detail["eliminated"] == [{
        "productId": 1,
        "reason": "explicit_hard_constraint_violation",
    }]
    assert detail["retrievalTrace"]["candidatePoolCount"] == 2
    assert detail["rankingTrace"]["rankedItemCount"] == 1
    assert detail["citationTrace"]["rankedItemIds"] == [3]


def test_search_budget_contract_controls_candidate_pool_before_rerank():
    def product(product_id, price_minor):
        return {
            "id": product_id,
            "title": f"phone {product_id}",
            "brand": "test",
            "categoryL1": "二手",
            "categoryL2": "二手手机通讯",
            "categoryL3": "二手手机",
            "snapshotPriceMinor": price_minor,
            "currency": "CNY",
            "priceStatus": "verified",
            "dataNature": "historical_dataset_snapshot",
            "attributeText": "",
            "source": "test",
            "provenanceUrl": "https://example.test/catalog",
            "attributes": [],
        }

    catalog = [product(1, 90000), product(2, 300000), product(3, 299500)]

    async def details(product_ids):
        by_id = {item["id"]: item for item in catalog}
        products = [by_id[item_id] for item_id in product_ids]
        return ToolTrace(
            tool="get_product_details", ok=True,
            detail={"products": products, "productIds": list(product_ids)},
        )

    budget = ShoppingRequirement(
        key="price_minor", operator="lte", value=300000,
        unit="CNY_MINOR", priority="hard", source="user",
    )
    with (
        patch("app.domains.ecommerce.tools.settings.ecommerce_guide_enabled", True),
        patch("app.domains.ecommerce.tools.settings.product_retrieval_mode", "bm25"),
        patch("app.domains.ecommerce.tools.settings.used_phone_synthetic_price_policy", "disabled"),
        patch("app.domains.ecommerce.tools._product_http_client", return_value=_Client(catalog)),
        patch("app.domains.ecommerce.tools.get_product_details_tool", new=details),
    ):
        result = asyncio.run(search_products_tool(
            "三千以下的手机", "手机", requirements=[budget.model_dump()]
        ))

    assert result.ok is True
    assert result.detail["retrievalTrace"]["fusion"] == "structured_budget_primary"
    assert result.detail["candidatePoolIds"] == [2, 3, 1]
    assert result.detail["rankedItemIds"] == [2, 3, 1]


def _requirement(value, *, operator="eq", priority="hard"):
    return ShoppingRequirement(
        key="os",
        operator=operator,
        value=value,
        unit="enum",
        priority=priority,
        source="user",
    )


def _os_token(value):
    return used_phone_attribute_ruleset_payload()["fields"]["os"]["aliases"][value][0]


def test_structured_recall_uses_exact_summary_facts_and_retains_unknown_last():
    products = [
        {"id": 30, "attributeText": _os_token("ios")},
        {"id": 20, "attributeText": f'{_os_token("ios")},{_os_token("android")}'},
        {"id": 10, "attributeText": _os_token("android")},
        {"id": 40, "attributeText": ""},
    ]

    ranked = structured_requirement_rank(
        "phone", products, [_requirement("ios")], [40, 30, 20, 10]
    )

    assert ranked == [30, 40, 20]
    assert 10 not in ranked


def test_structured_recall_does_not_trust_unvalidated_normalized_attribute():
    products = [{
        "id": 1,
        "attributeText": _os_token("android"),
        "attributes": [{"key": "os", "normalizedText": "ios"}],
    }]

    assert structured_requirement_rank(
        "phone", products, [_requirement("ios")]
    ) == []


def test_structured_recall_preserves_negation_and_prior_empty_behavior():
    products = [
        {"id": 1, "attributeText": _os_token("ios")},
        {"id": 2, "attributeText": _os_token("android")},
        {"id": 3, "attributeText": None},
    ]

    assert structured_requirement_rank(
        "phone", products, [_requirement(["ios"], operator="not_in")]
    ) == [2, 3]
    assert structured_requirement_rank("phone", products, []) == []


def test_structured_recall_soft_requirement_orders_without_eliminating():
    products = [
        {"id": 1, "attributeText": _os_token("ios")},
        {"id": 2, "attributeText": _os_token("android")},
        {"id": 3, "attributeText": None},
    ]

    assert structured_requirement_rank(
        "phone",
        products,
        [_requirement("ios", priority="soft")],
        [2, 3, 1],
    ) == [1, 3, 2]


def test_structured_recall_orders_hard_max_budget_from_ceiling_downward():
    products = [
        {"id": 1, "priceStatus": "verified", "snapshotPriceMinor": 90000},
        {"id": 2, "priceStatus": "verified", "snapshotPriceMinor": 295000},
        {"id": 3, "priceStatus": "verified", "snapshotPriceMinor": 250000},
        {"id": 4, "priceStatus": "verified", "snapshotPriceMinor": 310000},
        {"id": 5, "priceStatus": "unverified", "snapshotPriceMinor": None},
    ]
    budget = ShoppingRequirement(
        key="price_minor", operator="lte", value=300000,
        unit="CNY_MINOR", priority="hard", source="user",
    )

    assert structured_requirement_rank(
        "phone", products, [budget], [1, 5, 3, 2, 4]
    ) == [2, 3, 1, 5]


def test_structured_recall_honors_ordered_battery_preference_before_budget():
    rules = used_phone_attribute_ruleset_payload()["fields"]["battery_health"]["aliases"]
    products = [
        {
            "id": 1, "attributeText": rules["80_90"][0],
            "priceStatus": "verified", "snapshotPriceMinor": 299000,
        },
        {
            "id": 2, "attributeText": rules["90_plus"][0],
            "priceStatus": "verified", "snapshotPriceMinor": 250000,
        },
        {
            "id": 3, "attributeText": rules["90_plus"][0],
            "priceStatus": "verified", "snapshotPriceMinor": 290000,
        },
    ]
    battery = ShoppingRequirement(
        key="battery_health", operator="in", value=["90_plus", "80_90"],
        unit="enum", priority="soft", source="user",
    )
    budget = ShoppingRequirement(
        key="price_minor", operator="lte", value=300000,
        unit="CNY_MINOR", priority="hard", source="user",
    )

    assert structured_requirement_rank(
        "phone", products, [battery, budget], [1, 2, 3]
    ) == [3, 2, 1]


def test_structured_recall_is_inactive_for_detail_only_fields():
    requirement = ShoppingRequirement(
        key="memory_gb",
        operator="gte",
        value=8,
        unit="GB",
        priority="hard",
        source="user",
    )

    assert structured_requirement_rank(
        "phone", [{"id": 1, "attributeText": _os_token("ios")}], [requirement]
    ) == []
