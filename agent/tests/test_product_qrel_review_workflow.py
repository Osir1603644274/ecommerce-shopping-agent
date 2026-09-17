import pytest

from scripts.apply_product_qrel_reviews import merge_reviews
from scripts.audit_product_qrel_readiness import build_report


def _review(requirements):
    return {
        "queryId": "headphones-001",
        "category": "headphones",
        "originalRequirements": requirements,
    }


def _product(*, product_id=1, price=None, status="unverified"):
    return {
        "id": product_id,
        "title": "主动降噪蓝牙耳机",
        "category_l1": "数码",
        "category_l2": "耳机",
        "category_l3": "蓝牙耳机",
        "snapshot_price_minor": price,
        "price_status": status,
    }


def test_readiness_blocks_unknown_hard_fact_instead_of_grading_it():
    report = build_report(
        [_review([
            {"key": "price_minor", "operator": "lte", "value": 50000, "priority": "hard"},
            {"key": "noise_cancelling", "operator": "eq", "value": True, "priority": "hard"},
        ])],
        [_product()],
        [{
            "product_id": 1,
            "attribute_key": "noise_cancelling",
            "value_type": "boolean",
            "normalized_text": None,
            "normalized_number": None,
            "normalized_boolean": 1,
        }],
    )

    query = report["queries"][0]
    assert query["status"] == "blocked"
    assert query["hardRequirementFactCoverage"] == {
        "price_minor": 0,
        "noise_cancelling": 1,
    }
    assert query["fullyJudgeableCandidateCount"] == 0


def test_readiness_excludes_accessories_and_phone_cards_from_primary_products():
    phone_review = {
        "queryId": "phone-001",
        "category": "phone",
        "originalRequirements": [
            {"key": "price_minor", "operator": "lte", "value": 50000, "priority": "hard"},
        ],
    }
    phone_card = {
        "id": 9,
        "title": "19元5G手机流量卡",
        "category_l1": "通信",
        "category_l2": "手机服务",
        "category_l3": "新入网手机号服务",
        "snapshot_price_minor": 1900,
        "price_status": "verified",
    }

    report = build_report([phone_review], [phone_card], [])

    query = report["queries"][0]
    assert query["candidateCatalogCount"] == 0
    assert query["status"] == "blocked"


def test_readiness_accepts_candidate_with_all_authoritative_facts():
    report = build_report(
        [_review([
            {"key": "price_minor", "operator": "lte", "value": 50000, "priority": "hard"},
            {"key": "noise_cancelling", "operator": "eq", "value": True, "priority": "hard"},
        ])],
        [_product(price=39900, status="verified")],
        [{
            "product_id": 1,
            "attribute_key": "noise_cancelling",
            "value_type": "boolean",
            "normalized_text": None,
            "normalized_number": None,
            "normalized_boolean": 1,
        }],
    )

    query = report["queries"][0]
    assert query["status"] == "ready_for_human_review"
    assert query["verifiedMatchCandidateCount"] == 1
    assert query["sampleVerifiedMatchProductIds"] == [1]


def test_merge_refuses_pending_or_unknown_reviews():
    qrels = [{"queryId": "headphones-001", "reviewStatus": "draft", "judgments": []}]
    pending = [{"queryId": "headphones-001", "reviewStatus": "pending_review"}]
    with pytest.raises(ValueError, match="not human-confirmed"):
        merge_reviews(qrels, pending)

    unknown = [{
        "queryId": "headphones-001",
        "reviewStatus": "reviewed",
        "humanConfirmed": True,
        "reviewerId": "human-1",
        "reviewedAt": "2026-08-08T00:00:00+00:00",
        "judgments": [{"productId": 1, "relevance": "unknown"}],
    }]
    with pytest.raises(ValueError, match="UNKNOWN is a data blocker"):
        merge_reviews(qrels, unknown)


def test_merge_converts_explicit_human_relevance_to_numeric_grades():
    qrels = [{"queryId": "headphones-001", "reviewStatus": "draft", "judgments": []}]
    reviews = [{
        "queryId": "headphones-001",
        "reviewStatus": "reviewed",
        "humanConfirmed": True,
        "reviewerId": "human-1",
        "reviewedAt": "2026-08-08T00:00:00+00:00",
        "judgments": [
            {"productId": 1, "relevance": "perfect_match"},
            {"productId": 2, "relevance": "acceptable_alternative"},
            {"productId": 3, "relevance": "partial_match"},
            {"productId": 4, "relevance": "not_relevant"},
        ],
    }]

    merged = merge_reviews(qrels, reviews)

    assert [item["grade"] for item in merged[0]["judgments"]] == [3, 2, 1, 0]
    assert merged[0]["reviewStatus"] == "human_confirmed"
    assert merged[0]["reviewedBy"] == "human-1"
