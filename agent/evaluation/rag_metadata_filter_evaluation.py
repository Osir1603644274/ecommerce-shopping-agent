from collections.abc import Callable
from typing import Any

from app.knowledge_retrieval_quality import load_retrieval_quality_cases
from app.rag import DEFAULT_TOP_K, search_reviews
from app.rag_bm25_benchmark import score_ranked_results


ReviewRetriever = Callable[..., list[dict[str, Any]]]


def _review_validation_cases(
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for case in cases:
        if case["split"] != "validation" or "reviews" not in case["expectedSources"]:
            continue
        review_filter = case.get("reviewFilter")
        if not isinstance(review_filter, dict):
            continue
        selected.append(
            {
                **case,
                "relevantReviewIds": [
                    chunk_id.removeprefix("review:")
                    for chunk_id in case["relevantChunkIds"]
                    if chunk_id.startswith("review:")
                ],
            }
        )
    return selected


def build_metadata_filter_validation_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    retrieve: ReviewRetriever = search_reviews,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    validation_cases = _review_validation_cases(
        cases if cases is not None else load_retrieval_quality_cases()
    )
    if not validation_cases:
        raise ValueError("no filtered review validation cases found")

    unfiltered_ranked: dict[str, list[dict[str, Any]]] = {}
    filtered_ranked: dict[str, list[dict[str, Any]]] = {}
    filter_violations: list[dict[str, Any]] = []
    for case in validation_cases:
        review_filter = case["reviewFilter"]
        source = str(review_filter["source"])
        shop_id = int(review_filter["shopId"])
        unfiltered_ranked[case["id"]] = retrieve(
            case["question"],
            top_k,
            source=source,
        )
        filtered = retrieve(
            case["question"],
            top_k,
            source=source,
            shop_id=shop_id,
        )
        filtered_ranked[case["id"]] = filtered
        wrong_shop_ids = sorted(
            {
                int(review["shopId"])
                for review in filtered
                if int(review["shopId"]) != shop_id
            }
        )
        if wrong_shop_ids:
            filter_violations.append(
                {
                    "caseId": case["id"],
                    "expectedShopId": shop_id,
                    "unexpectedShopIds": wrong_shop_ids,
                }
            )

    unfiltered = score_ranked_results(
        validation_cases,
        unfiltered_ranked,
        top_k=top_k,
    )
    filtered = score_ranked_results(
        validation_cases,
        filtered_ranked,
        top_k=top_k,
    )
    unfiltered_by_id = {
        item["caseId"]: item for item in unfiltered["details"]
    }
    filtered_by_id = {
        item["caseId"]: item for item in filtered["details"]
    }
    recovered_case_ids = [
        case["id"]
        for case in validation_cases
        if not unfiltered_by_id[case["id"]]["hit"]
        and filtered_by_id[case["id"]]["hit"]
    ]
    regressed_case_ids = [
        case["id"]
        for case in validation_cases
        if unfiltered_by_id[case["id"]]["hit"]
        and not filtered_by_id[case["id"]]["hit"]
    ]

    return {
        "methodology": {
            "split": "validation only",
            "testRead": False,
            "topK": top_k,
            "baseline": "Qdrant vector search filtered only by source=yelp",
            "candidate": "same vector search filtered by source=yelp and labeled shopId",
            "caveat": "shopId is supplied by evaluation labels; entity resolution is not implemented",
        },
        "caseCount": len(validation_cases),
        "unfilteredYelp": unfiltered,
        "filteredByShop": filtered,
        "delta": {
            "hits": filtered["hits"] - unfiltered["hits"],
            "hitsAt1": filtered["hitsAt1"] - unfiltered["hitsAt1"],
            "mrr": filtered["mrr"] - unfiltered["mrr"],
            "recoveredCaseIds": recovered_case_ids,
            "regressedCaseIds": regressed_case_ids,
        },
        "filterViolations": filter_violations,
        "cases": [
            {
                "caseId": case["id"],
                "category": case["category"],
                "shopId": case["reviewFilter"]["shopId"],
                "question": case["question"],
            }
            for case in validation_cases
        ],
    }
