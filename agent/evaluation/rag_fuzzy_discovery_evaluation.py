import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from recommendation.metrics import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

from app.rag_bm25 import search_yelp_reviews_bm25
from app.rag_fusion import fuse_by_normalized_score, fuse_by_rrf
from app.rag_quality import search_yelp_reviews


CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "fuzzy_shop_discovery_validation_cases.json"
)

Retriever = Callable[[str, int], list[dict[str, Any]]]
FUZZY_QREL_VERSION = "fuzzy-shop-zh-validation-v2"


def load_fuzzy_shop_discovery_validation_cases() -> list[dict[str, Any]]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if payload.get("methodology", {}).get("split") != "validation":
        raise ValueError("fuzzy discovery cases must remain validation-only")
    cases = list(payload.get("cases") or [])
    seen_case_ids: set[str] = set()
    for case in cases:
        case_id = str(case.get("id") or "")
        question = str(case.get("question") or "").strip()
        judgments = list(case.get("relevanceJudgments") or [])
        if not case_id or case_id in seen_case_ids:
            raise ValueError("case IDs must be non-empty and unique")
        if not question:
            raise ValueError(f"{case_id} must contain a question")
        if len(judgments) < 2:
            raise ValueError(f"{case_id} must contain multiple relevant shops")
        seen_case_ids.add(case_id)

        seen_shop_ids: set[int] = set()
        for judgment in judgments:
            shop_id = int(judgment["shopId"])
            relevance = int(judgment["relevance"])
            review_ids = list(judgment.get("supportingReviewIds") or [])
            if shop_id in seen_shop_ids:
                raise ValueError(f"{case_id} contains duplicate shopId {shop_id}")
            if relevance not in {1, 2, 3}:
                raise ValueError(f"{case_id} relevance must be 1, 2, or 3")
            if not review_ids:
                raise ValueError(f"{case_id} shopId {shop_id} needs evidence")
            seen_shop_ids.add(shop_id)
    return cases


def rank_shops_from_reviews(
    reviews: list[dict[str, Any]],
    *,
    evidence_per_shop: int = 3,
) -> list[dict[str, Any]]:
    """Deduplicate a review ranking by each shop's first matching review."""
    if evidence_per_shop <= 0:
        raise ValueError("evidence_per_shop must be positive")
    shops: list[dict[str, Any]] = []
    by_shop_id: dict[int, dict[str, Any]] = {}
    for review_rank, review in enumerate(reviews, start=1):
        shop_id = int(review["shopId"])
        group = by_shop_id.get(shop_id)
        if group is None:
            group = {
                "shopId": shop_id,
                "shopName": review.get("shopName"),
                "firstReviewRank": review_rank,
                "reviewIds": [],
                "evidence": [],
            }
            by_shop_id[shop_id] = group
            shops.append(group)
        if len(group["evidence"]) < evidence_per_shop:
            group["reviewIds"].append(str(review["reviewId"]))
            group["evidence"].append(review)
    return shops


def score_fuzzy_discovery_case(
    case: dict[str, Any],
    ranked_reviews: list[dict[str, Any]],
    *,
    top_k: int,
    evidence_per_shop: int,
) -> dict[str, Any]:
    ranked_shops = rank_shops_from_reviews(
        ranked_reviews,
        evidence_per_shop=evidence_per_shop,
    )
    top_shops = ranked_shops[:top_k]
    ranked_shop_ids = [int(shop["shopId"]) for shop in ranked_shops]
    judgments = {
        int(item["shopId"]): item
        for item in case["relevanceJudgments"]
    }
    relevance_by_shop_id = {
        shop_id: float(item["relevance"])
        for shop_id, item in judgments.items()
    }
    retrieved_review_ids = {
        str(review["reviewId"])
        for review in ranked_reviews
    }
    evidence_shop_hits = sum(
        bool(
            retrieved_review_ids
            & {str(review_id) for review_id in item["supportingReviewIds"]}
        )
        for item in judgments.values()
    )
    first_relevant_rank = next(
        (
            rank
            for rank, shop_id in enumerate(ranked_shop_ids, start=1)
            if shop_id in judgments
        ),
        None,
    )
    return {
        "caseId": str(case["id"]),
        "question": str(case["question"]),
        "relevantShopCount": len(judgments),
        "precisionAtK": precision_at_k(ranked_shop_ids, judgments, top_k),
        "recallAtK": recall_at_k(ranked_shop_ids, judgments, top_k),
        "ndcgAtK": ndcg_at_k(ranked_shop_ids, relevance_by_shop_id, top_k),
        "reciprocalRank": reciprocal_rank(ranked_shop_ids, judgments),
        "firstRelevantShopRank": first_relevant_rank,
        "evidenceShopHits": evidence_shop_hits,
        "evidenceShopRecall": evidence_shop_hits / len(judgments),
        "topShops": [
            {
                **shop,
                "judgedRelevance": relevance_by_shop_id.get(int(shop["shopId"]), 0.0),
            }
            for shop in top_shops
        ],
    }


def summarize_fuzzy_discovery_details(
    details: list[dict[str, Any]],
    *,
    top_k: int,
) -> dict[str, Any]:
    total = len(details)
    return {
        "caseCount": total,
        f"meanPrecisionAt{top_k}": (
            sum(detail["precisionAtK"] for detail in details) / total
            if total
            else 0.0
        ),
        f"meanRecallAt{top_k}": (
            sum(detail["recallAtK"] for detail in details) / total if total else 0.0
        ),
        f"meanNdcgAt{top_k}": (
            sum(detail["ndcgAtK"] for detail in details) / total if total else 0.0
        ),
        "mrr": (
            sum(detail["reciprocalRank"] for detail in details) / total
            if total
            else 0.0
        ),
        "meanEvidenceShopRecall": (
            sum(detail["evidenceShopRecall"] for detail in details) / total
            if total
            else 0.0
        ),
    }


def build_fuzzy_shop_discovery_validation_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    vector_retrieve: Retriever = search_yelp_reviews,
    bm25_retrieve: Retriever = search_yelp_reviews_bm25,
    candidate_limit: int = 30,
    top_k: int = 5,
    evidence_per_shop: int = 3,
    vector_weight: float = 0.20,
    bm25_weight: float = 0.80,
    rrf_k: int = 60,
) -> dict[str, Any]:
    if candidate_limit <= 0 or top_k <= 0:
        raise ValueError("candidate_limit and top_k must be positive")
    selected_cases = (
        cases
        if cases is not None
        else load_fuzzy_shop_discovery_validation_cases()
    )
    ranked_by_method: dict[str, dict[str, list[dict[str, Any]]]] = {
        "vector": {},
        "bm25": {},
        "hybridV02B08": {},
        "rrf": {},
    }
    timing: dict[str, list[float]] = {"vector": [], "bm25": []}
    for case in selected_cases:
        case_id = str(case["id"])
        question = str(case["question"])
        start = time.perf_counter()
        vector = vector_retrieve(question, candidate_limit)
        timing["vector"].append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        bm25 = bm25_retrieve(question, candidate_limit)
        timing["bm25"].append((time.perf_counter() - start) * 1000)
        ranked_by_method["vector"][case_id] = vector
        ranked_by_method["bm25"][case_id] = bm25
        ranked_by_method["hybridV02B08"][case_id] = fuse_by_normalized_score(
            vector,
            bm25,
            normalization="min_max",
            vector_weight=vector_weight,
            bm25_weight=bm25_weight,
        )
        ranked_by_method["rrf"][case_id] = fuse_by_rrf(
            vector,
            bm25,
            rrf_k=rrf_k,
        )

    methods: dict[str, Any] = {}
    for method_name, ranked_cases in ranked_by_method.items():
        details = [
            score_fuzzy_discovery_case(
                case,
                ranked_cases[str(case["id"])],
                top_k=top_k,
                evidence_per_shop=evidence_per_shop,
            )
            for case in selected_cases
        ]
        methods[method_name] = {
            "metrics": summarize_fuzzy_discovery_details(details, top_k=top_k),
            "details": details,
        }

    return {
        "methodology": {
            "split": "validation only",
            "testRead": False,
            "datasetRole": "temporary diagnostic set",
            "qrelVersion": FUZZY_QREL_VERSION,
            "candidateLimitPerRetriever": candidate_limit,
            "topKShops": top_k,
            "shopRanking": "deduplicate reviews by the first occurrence of each shop",
            "evidencePerShopInReport": evidence_per_shop,
            "hybridWeights": {"vector": vector_weight, "bm25": bm25_weight},
            "selectionUse": False,
            "labelCaveat": "pooled subjective qrels are not exhaustive and must not be presented as a production benchmark",
        },
        "caseCount": len(selected_cases),
        "timing": {
            name: {
                "averageMs": sum(values) / len(values) if values else 0.0,
                "measurementsMs": values,
            }
            for name, values in timing.items()
        },
        "methods": methods,
    }
