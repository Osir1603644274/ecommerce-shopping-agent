from collections.abc import Awaitable, Callable
from typing import Any

from app.knowledge_retrieval_quality import load_retrieval_quality_cases
from app.rag import DEFAULT_TOP_K
from app.rag_bm25_benchmark import score_ranked_results
from app.schemas import ToolTrace
from app.shop_entity import resolve_unique_shop
from app.tools import search_reviews_tool, search_shops


ShopSearch = Callable[[int | None, str | None], Awaitable[ToolTrace]]
ReviewSearch = Callable[[str, int | None], Awaitable[ToolTrace]]


def _entity_validation_cases(
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            **case,
            "relevantReviewIds": [
                chunk_id.removeprefix("review:")
                for chunk_id in case["relevantChunkIds"]
                if chunk_id.startswith("review:")
            ],
        }
        for case in cases
        if case["split"] == "validation"
        and isinstance(case.get("shopEntity"), dict)
        and isinstance(case.get("reviewFilter"), dict)
    ]


async def build_shop_review_entity_validation_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    find_shops: ShopSearch = search_shops,
    find_reviews: ReviewSearch = search_reviews_tool,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    validation_cases = _entity_validation_cases(
        cases if cases is not None else load_retrieval_quality_cases()
    )
    if not validation_cases:
        raise ValueError("no shop entity validation cases found")

    details: list[dict[str, Any]] = []
    ranked_by_case_id: dict[str, list[dict[str, Any]]] = {}
    for case in validation_cases:
        requested_name = str(case["shopEntity"]["name"])
        expected_shop_id = int(case["reviewFilter"]["shopId"])
        shop_trace = await find_shops(None, requested_name)
        shops = (
            shop_trace.detail.get("shops", [])
            if shop_trace.ok and isinstance(shop_trace.detail, dict)
            else []
        )
        resolved_shop, resolution_error = resolve_unique_shop(
            shops,
            requested_name,
        )
        resolved_shop_id = (
            int(resolved_shop["id"])
            if resolved_shop is not None
            else None
        )
        resolution_correct = resolved_shop_id == expected_shop_id
        reviews: list[dict[str, Any]] = []
        review_trace: ToolTrace | None = None
        if resolved_shop_id is not None:
            review_trace = await find_reviews(case["question"], resolved_shop_id)
            if review_trace.ok and isinstance(review_trace.detail, dict):
                reviews = list(review_trace.detail.get("reviews", []))
        ranked_by_case_id[case["id"]] = reviews
        details.append(
            {
                "caseId": case["id"],
                "shopName": requested_name,
                "expectedShopId": expected_shop_id,
                "shopSearchOk": shop_trace.ok,
                "shopCandidateCount": len(shops),
                "resolvedShopId": resolved_shop_id,
                "resolutionError": resolution_error,
                "resolutionCorrect": resolution_correct,
                "reviewSearchOk": review_trace.ok if review_trace else False,
                "retrievedReviewIds": [
                    str(review["reviewId"])
                    for review in reviews
                ],
            }
        )

    retrieval = score_ranked_results(
        validation_cases,
        ranked_by_case_id,
        top_k=top_k,
    )
    resolved = sum(item["resolvedShopId"] is not None for item in details)
    correct = sum(item["resolutionCorrect"] for item in details)
    return {
        "methodology": {
            "split": "validation only",
            "testRead": False,
            "topK": top_k,
            "chain": "search_shops(name) -> unique shopId -> search_reviews(shopId)",
            "ambiguityPolicy": "prefer one exact case-insensitive name match; never pick the first ambiguous partial match",
        },
        "caseCount": len(validation_cases),
        "entityResolution": {
            "resolved": resolved,
            "resolvedRate": resolved / len(validation_cases),
            "correct": correct,
            "accuracy": correct / len(validation_cases),
        },
        "reviewRetrieval": retrieval,
        "details": details,
        "failures": [
            item
            for item in details
            if not item["resolutionCorrect"] or not item["reviewSearchOk"]
        ],
    }
