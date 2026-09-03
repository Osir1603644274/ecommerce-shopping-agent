import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.knowledge import SearchKnowledgeResult
from app.knowledge.source_aware_search import search_knowledge_source_aware
from app.knowledge_retrieval_quality import (
    evaluate_retrieval_quality_cases,
    load_retrieval_quality_cases,
)
from app.schemas import ToolTrace
from app.shop_entity import resolve_unique_shop
from app.tools import search_shops


ShopSearch = Callable[[int | None, str | None], Awaitable[ToolTrace]]
SourceAwareRetrieve = Callable[..., SearchKnowledgeResult]


async def build_source_aware_validation_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    find_shops: ShopSearch = search_shops,
    retrieve: SourceAwareRetrieve = search_knowledge_source_aware,
    top_k_per_source: int = 3,
) -> dict[str, Any]:
    validation_cases = [
        case
        for case in (cases if cases is not None else load_retrieval_quality_cases())
        if case["split"] == "validation"
    ]
    if not validation_cases:
        raise ValueError("no validation cases found")

    results_by_question: dict[str, SearchKnowledgeResult] = {}
    resolution_details: list[dict[str, Any]] = []
    for case in validation_cases:
        shop_ids: list[int] | None = None
        shop_entity = case.get("shopEntity")
        if isinstance(shop_entity, dict):
            requested_name = str(shop_entity["name"])
            shop_trace = await find_shops(None, requested_name)
            shops = (
                shop_trace.detail.get("shops", [])
                if shop_trace.ok and isinstance(shop_trace.detail, dict)
                else []
            )
            candidates = [shop for shop in shops if isinstance(shop, dict)]
            resolved_shop, resolution_error = resolve_unique_shop(
                candidates,
                requested_name,
            )
            resolved_shop_id = (
                int(resolved_shop["id"]) if resolved_shop is not None else None
            )
            expected_shop_id = int(case["reviewFilter"]["shopId"])
            resolution_details.append(
                {
                    "caseId": case["id"],
                    "requestedShopName": requested_name,
                    "candidateCount": len(candidates),
                    "resolvedShopId": resolved_shop_id,
                    "expectedShopId": expected_shop_id,
                    "resolutionError": resolution_error,
                    "resolutionCorrect": resolved_shop_id == expected_shop_id,
                }
            )
            shop_ids = [resolved_shop_id] if resolved_shop_id is not None else []

        results_by_question[case["question"]] = await asyncio.to_thread(
            retrieve,
            case["question"],
            list(case["expectedSources"]),
            top_k_per_source,
            shop_ids=shop_ids,
        )

    retrieval = evaluate_retrieval_quality_cases(
        validation_cases,
        retrieve=lambda question, _sources, _limit: results_by_question[question],
        top_k_per_source=top_k_per_source,
    )
    return {
        "methodology": {
            "split": "validation only",
            "testRead": False,
            "routerEvaluated": False,
            "sourceSelection": "explicit expectedSources",
            "entityInput": "annotated shopEntity.name; expected shopId used only for scoring",
            "topKPerSource": top_k_per_source,
        },
        "entityResolution": {
            "caseCount": len(resolution_details),
            "resolved": sum(item["resolvedShopId"] is not None for item in resolution_details),
            "correct": sum(item["resolutionCorrect"] for item in resolution_details),
            "details": resolution_details,
        },
        "retrieval": retrieval,
    }
