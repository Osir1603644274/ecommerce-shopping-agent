import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.rag import search_reviews, search_reviews_grouped_by_shop


CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "shop_aggregation_validation_cases.json"
)
DEFAULT_THRESHOLDS: tuple[float | None, ...] = (None, 0.35, 0.40, 0.45, 0.50)

GlobalRetriever = Callable[..., list[dict[str, Any]]]
GroupedRetriever = Callable[..., list[dict[str, Any]]]


def load_shop_aggregation_validation_cases() -> list[dict[str, Any]]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return list(payload["cases"])


def _aggregate_reviews(
    reviews: list[dict[str, Any]],
    candidate_shop_ids: list[int],
    *,
    evidence_per_shop: int,
    score_threshold: float | None,
) -> tuple[list[dict[str, Any]], list[int]]:
    candidate_positions = {
        shop_id: position
        for position, shop_id in enumerate(candidate_shop_ids)
    }
    grouped: dict[int, list[dict[str, Any]]] = {
        shop_id: [] for shop_id in candidate_shop_ids
    }
    violations: set[int] = set()
    for review in reviews:
        shop_id = int(review["shopId"])
        if shop_id not in grouped:
            violations.add(shop_id)
            continue
        if score_threshold is not None and float(review["score"]) < score_threshold:
            continue
        grouped[shop_id].append(review)

    shops: list[dict[str, Any]] = []
    for shop_id in candidate_shop_ids:
        evidence = sorted(
            grouped[shop_id],
            key=lambda review: float(review["score"]),
            reverse=True,
        )[:evidence_per_shop]
        scores = [float(review["score"]) for review in evidence]
        shops.append(
            {
                "shopId": shop_id,
                "aggregateScore": sum(scores) / len(scores) if scores else None,
                "maxScore": max(scores) if scores else None,
                "evidenceCount": len(evidence),
                "reviewIds": [str(review["reviewId"]) for review in evidence],
            }
        )
    shops.sort(
        key=lambda shop: (
            shop["aggregateScore"] is None,
            -shop["aggregateScore"] if shop["aggregateScore"] is not None else 0,
            candidate_positions[shop["shopId"]],
        )
    )
    return shops, sorted(violations)


def _threshold_label(value: float | None) -> str:
    return "none" if value is None else f"{value:.2f}"


def _summarize(details: list[dict[str, Any]]) -> dict[str, Any]:
    case_count = len(details)
    relevant_hits = sum(detail["relevantReviewHit"] for detail in details)
    hits_at_1 = sum(detail["targetShopRank"] == 1 for detail in details)
    reciprocal_rank = sum(
        1 / detail["targetShopRank"]
        if detail["targetShopRank"] is not None
        else 0
        for detail in details
    )
    return {
        "caseCount": case_count,
        "relevantReviewHits": relevant_hits,
        "relevantReviewHitRate": relevant_hits / case_count if case_count else 0,
        "targetShopHitsAt1": hits_at_1,
        "targetShopHitAt1Rate": hits_at_1 / case_count if case_count else 0,
        "targetShopMrr": reciprocal_rank / case_count if case_count else 0,
        "averageShopCoverage": (
            sum(detail["shopCoverage"] for detail in details) / case_count
            if case_count
            else 0
        ),
        "averageEvidenceCount": (
            sum(detail["evidenceCount"] for detail in details) / case_count
            if case_count
            else 0
        ),
        "noEvidenceCandidateCount": sum(
            detail["noEvidenceCandidateCount"] for detail in details
        ),
        "filterViolationCount": sum(
            len(detail["filterViolations"]) for detail in details
        ),
    }


def build_shop_aggregation_validation_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    global_retrieve: GlobalRetriever = search_reviews,
    grouped_retrieve: GroupedRetriever = search_reviews_grouped_by_shop,
    thresholds: Sequence[float | None] = DEFAULT_THRESHOLDS,
    evidence_budget: int = 6,
    evidence_per_shop: int = 2,
) -> dict[str, Any]:
    selected_cases = cases if cases is not None else load_shop_aggregation_validation_cases()
    raw_results: dict[str, dict[str, list[dict[str, Any]]]] = {}
    timing: dict[str, list[float]] = {"global": [], "grouped": []}
    for case in selected_cases:
        case_id = str(case["id"])
        candidate_shop_ids = [int(value) for value in case["candidateShopIds"]]

        start = time.perf_counter()
        global_reviews = global_retrieve(
            str(case["question"]),
            evidence_budget,
            source="yelp",
            shop_ids=candidate_shop_ids,
        )
        timing["global"].append((time.perf_counter() - start) * 1000)

        start = time.perf_counter()
        grouped_shops = grouped_retrieve(
            str(case["question"]),
            candidate_shop_ids,
            reviews_per_shop=evidence_per_shop,
            source="yelp",
        )
        timing["grouped"].append((time.perf_counter() - start) * 1000)
        grouped_reviews = [
            review
            for shop in grouped_shops
            for review in shop["reviews"]
        ]
        raw_results[case_id] = {
            "global": global_reviews,
            "grouped": grouped_reviews,
        }

    configurations: list[dict[str, Any]] = []
    for mode in ("global", "grouped"):
        for threshold in thresholds:
            details: list[dict[str, Any]] = []
            for case in selected_cases:
                case_id = str(case["id"])
                candidate_shop_ids = [int(value) for value in case["candidateShopIds"]]
                shops, violations = _aggregate_reviews(
                    raw_results[case_id][mode],
                    candidate_shop_ids,
                    evidence_per_shop=evidence_per_shop,
                    score_threshold=threshold,
                )
                expected_shop_id = int(case["expectedTopShopId"])
                target_shop_rank = next(
                    (
                        rank
                        for rank, shop in enumerate(shops, start=1)
                        if shop["shopId"] == expected_shop_id
                        and shop["evidenceCount"] > 0
                    ),
                    None,
                )
                retained_review_ids = {
                    review_id
                    for shop in shops
                    for review_id in shop["reviewIds"]
                }
                relevant_review_ids = {
                    str(value) for value in case["relevantReviewIds"]
                }
                shops_with_evidence = sum(
                    shop["evidenceCount"] > 0 for shop in shops
                )
                details.append(
                    {
                        "caseId": case_id,
                        "expectedTopShopId": expected_shop_id,
                        "targetShopRank": target_shop_rank,
                        "relevantReviewHit": bool(
                            retained_review_ids & relevant_review_ids
                        ),
                        "shopCoverage": shops_with_evidence / len(candidate_shop_ids),
                        "evidenceCount": sum(
                            shop["evidenceCount"] for shop in shops
                        ),
                        "noEvidenceCandidateCount": (
                            len(candidate_shop_ids) - shops_with_evidence
                        ),
                        "filterViolations": violations,
                        "shops": shops,
                    }
                )
            configurations.append(
                {
                    "mode": mode,
                    "scoreThreshold": threshold,
                    "thresholdLabel": _threshold_label(threshold),
                    "metrics": _summarize(details),
                    "details": details,
                }
            )

    return {
        "methodology": {
            "split": "validation only",
            "testRead": False,
            "candidateShopCount": 3,
            "maxEvidenceBudget": evidence_budget,
            "evidencePerShop": evidence_per_shop,
            "thresholds": list(thresholds),
            "aggregation": "mean of retained review vector scores",
            "labelCaveat": "qrels contain one known relevant review per case and are not exhaustive",
        },
        "caseCount": len(selected_cases),
        "timing": {
            mode: {
                "averageMs": sum(values) / len(values) if values else 0,
                "measurementsMs": values,
            }
            for mode, values in timing.items()
        },
        "configurations": configurations,
    }
