import json
from pathlib import Path
from typing import Any

from app.rag import load_reviews
from app.rag_context_selection import select_reranked_review_context
from .rag_fuzzy_discovery_evaluation import (
    load_fuzzy_shop_discovery_validation_cases,
)


AGENT_ROOT = Path(__file__).resolve().parents[1]
CONTENT_RERANKER_REPORT_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "content_reranker_validation_report.json"
)


def load_content_reranker_report(
    path: Path = CONTENT_RERANKER_REPORT_PATH,
) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _audit_reviews(
    audit: dict[str, Any],
    reviews_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for item in audit["rankedCandidates"]:
        review_id = str(item["reviewId"])
        source = reviews_by_id.get(review_id)
        if source is None:
            raise ValueError(f"missing review text for {review_id}")
        ranked.append(
            {
                **source,
                "reviewId": review_id,
                "shopId": int(item["shopId"]),
                "shopName": item.get("shopName"),
                "originalRank": int(item["originalHybridRank"]),
                "contentRelation": str(item["relation"]),
                "contentScore": int(item["score"]),
                "contentReason": str(item.get("reason") or ""),
            }
        )
    return ranked


def _top_shop_ids(ranked_reviews: list[dict[str, Any]], limit: int) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for review in ranked_reviews:
        shop_id = int(review["shopId"])
        if shop_id not in seen:
            seen.add(shop_id)
            result.append(shop_id)
            if len(result) == limit:
                break
    return result


def build_context_selection_validation_report(
    *,
    cases: list[dict[str, Any]] | None = None,
    content_report: dict[str, Any] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    max_shops: int = 5,
    max_support_per_shop: int = 2,
    max_conflict_per_shop: int = 1,
    max_total_reviews: int = 12,
    max_total_chars: int = 6000,
    max_chars_per_review: int = 600,
) -> dict[str, Any]:
    selected_cases = (
        cases
        if cases is not None
        else load_fuzzy_shop_discovery_validation_cases()
    )
    report = content_report if content_report is not None else load_content_reranker_report()
    selected_reviews = reviews if reviews is not None else load_reviews()
    reviews_by_id = {str(item["reviewId"]): item for item in selected_reviews}
    cases_by_id = {str(item["id"]): item for item in selected_cases}

    details: list[dict[str, Any]] = []
    total_candidates = 0
    total_selected = 0
    total_characters = 0
    supported_top_shops = 0
    total_top_shops = 0
    exact_evidence_shop_hits = 0
    relevant_top_shops = 0
    selected_support_count = 0
    selected_support_qrel_hits = 0
    conflict_eligible_shops = 0
    conflict_selected_shops = 0
    naive_supported_shop_hits = 0
    selector_supported_shop_hits = 0
    support_eligible_shops = 0

    for audit in report["rerankerAudit"]:
        case_id = str(audit["caseId"])
        case = cases_by_id[case_id]
        ranked = _audit_reviews(audit, reviews_by_id)
        selection = select_reranked_review_context(
            ranked,
            max_shops=max_shops,
            max_support_per_shop=max_support_per_shop,
            max_conflict_per_shop=max_conflict_per_shop,
            max_total_reviews=max_total_reviews,
            max_total_chars=max_total_chars,
            max_chars_per_review=max_chars_per_review,
        )
        top_shop_ids = _top_shop_ids(ranked, max_shops)
        if top_shop_ids != [int(item["shopId"]) for item in selection["shops"]]:
            raise ValueError(f"context selection changed shop order for {case_id}")

        judgments = {
            int(item["shopId"]): item for item in case["relevanceJudgments"]
        }
        selected_by_shop: dict[int, list[dict[str, Any]]] = {
            shop_id: [] for shop_id in top_shop_ids
        }
        for item in selection["selectedReviews"]:
            selected_by_shop[int(item["shopId"])].append(item)

        naive = ranked[: selection["selectedReviewCount"]]
        available_support_by_shop = {
            shop_id: any(
                int(item["shopId"]) == shop_id
                and item["contentRelation"] == "support"
                for item in ranked
            )
            for shop_id in top_shop_ids
        }
        available_conflict_by_shop = {
            shop_id: any(
                int(item["shopId"]) == shop_id
                and item["contentRelation"] == "conflict"
                for item in ranked
            )
            for shop_id in top_shop_ids
        }

        case_exact_hits = 0
        case_relevant_top_shops = 0
        for shop_id in top_shop_ids:
            selected_for_shop = selected_by_shop[shop_id]
            selected_support = [
                item
                for item in selected_for_shop
                if item["selectedRole"] == "support"
            ]
            selected_support_count += len(selected_support)
            has_selected_support = bool(selected_support)
            supported_top_shops += int(has_selected_support)
            if available_support_by_shop[shop_id]:
                support_eligible_shops += 1
                selector_supported_shop_hits += int(has_selected_support)
                naive_supported_shop_hits += int(
                    any(
                        int(item["shopId"]) == shop_id
                        and item["contentRelation"] == "support"
                        for item in naive
                    )
                )
            if available_support_by_shop[shop_id] and available_conflict_by_shop[shop_id]:
                conflict_eligible_shops += 1
                conflict_selected_shops += int(
                    any(
                        item["selectedRole"] == "conflict"
                        for item in selected_for_shop
                    )
                )

            judgment = judgments.get(shop_id)
            if judgment is None:
                continue
            case_relevant_top_shops += 1
            supporting_ids = {
                str(review_id) for review_id in judgment["supportingReviewIds"]
            }
            if any(str(item["reviewId"]) in supporting_ids for item in selected_for_shop):
                case_exact_hits += 1
            selected_support_qrel_hits += sum(
                str(item["reviewId"]) in supporting_ids
                for item in selected_support
            )

        total_candidates += selection["candidateReviewCount"]
        total_selected += selection["selectedReviewCount"]
        total_characters += selection["selectedCharacterCount"]
        total_top_shops += selection["topShopCount"]
        exact_evidence_shop_hits += case_exact_hits
        relevant_top_shops += case_relevant_top_shops
        details.append(
            {
                "caseId": case_id,
                "question": case["question"],
                "candidateReviewCount": selection["candidateReviewCount"],
                "selectedReviewCount": selection["selectedReviewCount"],
                "selectedCharacterCount": selection["selectedCharacterCount"],
                "relevantTopShopCount": case_relevant_top_shops,
                "exactEvidenceShopHits": case_exact_hits,
                "shops": selection["shops"],
                "selectedReviews": [
                    {
                        "reviewId": str(item["reviewId"]),
                        "shopId": int(item["shopId"]),
                        "shopName": item.get("shopName"),
                        "selectedRole": item["selectedRole"],
                        "contentScore": int(item["contentScore"]),
                        "contextText": item["contextText"],
                    }
                    for item in selection["selectedReviews"]
                ],
            }
        )

    case_count = len(details)
    return {
        "methodology": {
            "split": "validation only",
            "testRead": False,
            "rankingInput": "frozen support-conflict-v1 content reranker audit",
            "shopOrderChanged": False,
            "selectionOrder": "one support per shop, second support per shop, then conflict",
            "limits": {
                "maxShops": max_shops,
                "maxSupportPerShop": max_support_per_shop,
                "maxConflictPerShop": max_conflict_per_shop,
                "maxTotalReviews": max_total_reviews,
                "maxTotalChars": max_total_chars,
                "maxCharsPerReview": max_chars_per_review,
            },
            "qrelCaveat": "supportingReviewIds are pooled and non-exhaustive",
        },
        "metrics": {
            "caseCount": case_count,
            "averageCandidateReviewCount": total_candidates / case_count if case_count else 0.0,
            "averageSelectedReviewCount": total_selected / case_count if case_count else 0.0,
            "reviewCountReduction": 1 - total_selected / total_candidates if total_candidates else 0.0,
            "averageSelectedCharacterCount": total_characters / case_count if case_count else 0.0,
            "supportedTopShopRate": supported_top_shops / total_top_shops if total_top_shops else 0.0,
            "selectorSupportEligibleShopCoverage": (
                selector_supported_shop_hits / support_eligible_shops
                if support_eligible_shops
                else 0.0
            ),
            "naiveTopNSupportEligibleShopCoverage": (
                naive_supported_shop_hits / support_eligible_shops
                if support_eligible_shops
                else 0.0
            ),
            "relevantTopShopExactEvidenceCoverage": (
                exact_evidence_shop_hits / relevant_top_shops
                if relevant_top_shops
                else 0.0
            ),
            "selectedSupportQrelPrecision": (
                selected_support_qrel_hits / selected_support_count
                if selected_support_count
                else 0.0
            ),
            "conflictSelectionCoverage": (
                conflict_selected_shops / conflict_eligible_shops
                if conflict_eligible_shops
                else 0.0
            ),
        },
        "details": details,
    }
