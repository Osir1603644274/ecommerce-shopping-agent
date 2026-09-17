import logging
import time
from collections.abc import Callable
from typing import Any

from .knowledge import SearchKnowledgeResult, search_knowledge
from .settings import settings


RuntimeRetrieve = Callable[..., SearchKnowledgeResult]

SOURCE_AWARE_RUNTIME_SMOKE_CASES = [
    {
        "id": "reviews-discovery",
        "query": "哪家咖啡店适合安静办公？",
        "sources": ["reviews"],
        "expectedSourceType": "review",
        "expectedRetriever": "qdrant_unified_vector",
    },
    {
        "id": "named-review-shop-filter",
        "query": "这家店适合安静办公吗？",
        "sources": ["reviews"],
        "shopId": 100011,
        "expectedSourceType": "review",
        "expectedRetriever": "qdrant_unified_vector",
    },
    {
        "id": "merchant-doc-lexical",
        "query": "St Honore Pastries 有 WiFi 吗？",
        "sources": ["merchant_docs"],
        "expectedSourceType": "merchant_doc",
        "expectedRetriever": "qdrant_payload_lexical",
        "expectedTopSourceId": "MTSW4McQd7CbVtyjqoe9mw",
        "requiredTopTerms": ["St Honore Pastries", "WiFi：免费"],
    },
    {
        "id": "policy-semantic-top3",
        "query": "个性化推荐会如何使用我的个人信息？",
        "sources": ["policy_docs"],
        "expectedSourceType": "policy_doc",
        "expectedRetriever": "qdrant_unified_vector",
        "expectedSourceInTopK": "privacy-and-recommendation",
        "requiredMatchingTerms": ["个性化推荐", "个人信息"],
    },
]


def _runtime_route(result: SearchKnowledgeResult) -> dict[str, Any]:
    for step in result.trace.steps:
        if step.name == "runtime_search_route":
            return step.detail
    return {}


def _retrievers(result: SearchKnowledgeResult) -> list[str]:
    return [
        str(step.detail["retriever"])
        for step in result.trace.steps
        if step.detail.get("retriever")
    ]


def _chunk_summary(result: SearchKnowledgeResult) -> list[dict[str, Any]]:
    citation_scores = {
        citation.chunk_id: citation.score
        for citation in result.citations
    }
    return [
        {
            "chunkId": chunk.chunk_id,
            "sourceType": chunk.source_type,
            "sourceId": chunk.source_id,
            "title": chunk.title,
            "shopId": chunk.metadata.get("shopId"),
            "score": citation_scores.get(chunk.chunk_id),
            "contentPreview": chunk.content[:240],
        }
        for chunk in result.chunks
    ]


def evaluate_source_aware_smoke_case(
    case: dict[str, Any],
    result: SearchKnowledgeResult,
    *,
    elapsed_ms: float,
    latency_ceiling_ms: float,
) -> dict[str, Any]:
    route = _runtime_route(result)
    retrievers = _retrievers(result)
    chunks = result.chunks
    citations_aligned = (
        len(chunks) == len(result.citations)
        and [chunk.chunk_id for chunk in chunks]
        == [citation.chunk_id for citation in result.citations]
    )
    checks: dict[str, bool] = {
        "sourceAwareEffective": (
            route.get("requestedMode") == "source_aware"
            and route.get("effectiveMode") == "source_aware"
            and route.get("fallback") is False
        ),
        "returnedEvidence": bool(chunks),
        "sourceTypeExact": bool(chunks)
        and all(chunk.source_type == case["expectedSourceType"] for chunk in chunks),
        "retrieverExpected": case["expectedRetriever"] in retrievers,
        "citationsAligned": citations_aligned,
        "withinSmokeLatencyCeiling": elapsed_ms <= latency_ceiling_ms,
    }

    if "shopId" in case:
        checks["shopFilterExact"] = bool(chunks) and all(
            chunk.metadata.get("shopId") == case["shopId"]
            for chunk in chunks
        )
    if "expectedTopSourceId" in case:
        checks["expectedTopSource"] = bool(chunks) and (
            chunks[0].source_id == case["expectedTopSourceId"]
        )
        checks["requiredTopTerms"] = bool(chunks) and all(
            term in chunks[0].content
            for term in case.get("requiredTopTerms", [])
        )
    if "expectedSourceInTopK" in case:
        matching_chunks = [
            chunk
            for chunk in chunks
            if chunk.source_id == case["expectedSourceInTopK"]
        ]
        checks["expectedSourceInTopK"] = bool(matching_chunks)
        checks["requiredMatchingTerms"] = bool(matching_chunks) and any(
            all(term in chunk.content for term in case.get("requiredMatchingTerms", []))
            for chunk in matching_chunks
        )

    return {
        "caseId": case["id"],
        "query": case["query"],
        "sources": case["sources"],
        **({"shopId": case["shopId"]} if "shopId" in case else {}),
        "passed": all(checks.values()),
        "checks": checks,
        "elapsedMs": elapsed_ms,
        "runtimeRoute": route,
        "retrievers": retrievers,
        "returnedCount": len(chunks),
        "chunks": _chunk_summary(result),
    }


def evaluate_legacy_fallback_smoke(
    result: SearchKnowledgeResult,
    *,
    elapsed_ms: float,
    latency_ceiling_ms: float,
) -> dict[str, Any]:
    route = _runtime_route(result)
    retrievers = _retrievers(result)
    checks = {
        "legacyEffective": (
            route.get("requestedMode") == "source_aware"
            and route.get("effectiveMode") == "legacy"
            and route.get("fallback") is True
        ),
        "errorTypeRecorded": bool(route.get("errorType")),
        "returnedEvidence": bool(result.chunks),
        "reviewEvidenceOnly": bool(result.chunks)
        and all(chunk.source_type == "review" for chunk in result.chunks),
        "legacyRetrieverUsed": "qdrant_vector" in retrievers,
        "withinSmokeLatencyCeiling": elapsed_ms <= latency_ceiling_ms,
    }
    return {
        "caseId": "forced-missing-unified-collection-fallback",
        "passed": all(checks.values()),
        "checks": checks,
        "elapsedMs": elapsed_ms,
        "runtimeRoute": route,
        "retrievers": retrievers,
        "returnedCount": len(result.chunks),
        "chunks": _chunk_summary(result),
    }


def build_source_aware_runtime_smoke_report(
    *,
    retrieve: RuntimeRetrieve = search_knowledge,
    cases: list[dict[str, Any]] | None = None,
    latency_ceiling_ms: float = 5000.0,
    fallback_latency_ceiling_ms: float = 8000.0,
) -> dict[str, Any]:
    selected_cases = cases or SOURCE_AWARE_RUNTIME_SMOKE_CASES
    configured_default = settings.knowledge_source_aware_enabled
    original_collection = settings.knowledge_collection_name
    runtime_logger = logging.getLogger("app.knowledge.runtime_search")
    original_logger_disabled = runtime_logger.disabled
    details: list[dict[str, Any]] = []

    settings.knowledge_source_aware_enabled = True
    try:
        for case in selected_cases:
            start = time.perf_counter()
            result = retrieve(
                case["query"],
                case["sources"],
                3,
                shop_id=case.get("shopId"),
            )
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            details.append(
                evaluate_source_aware_smoke_case(
                    case,
                    result,
                    elapsed_ms=elapsed_ms,
                    latency_ceiling_ms=latency_ceiling_ms,
                )
            )

        settings.knowledge_collection_name = (
            f"{original_collection}__missing_runtime_smoke"
        )
        fallback_start = time.perf_counter()
        runtime_logger.disabled = True
        try:
            fallback_result = retrieve(
                "哪家咖啡店适合安静办公？",
                ["reviews"],
                3,
            )
        finally:
            runtime_logger.disabled = original_logger_disabled
        fallback_elapsed_ms = round(
            (time.perf_counter() - fallback_start) * 1000,
            2,
        )
        fallback = evaluate_legacy_fallback_smoke(
            fallback_result,
            elapsed_ms=fallback_elapsed_ms,
            latency_ceiling_ms=fallback_latency_ceiling_ms,
        )
    finally:
        runtime_logger.disabled = original_logger_disabled
        settings.knowledge_collection_name = original_collection
        settings.knowledge_source_aware_enabled = configured_default

    passed_count = sum(detail["passed"] for detail in details)
    overall_passed = passed_count == len(details) and fallback["passed"]
    return {
        "experiment": "RAG-PROD-01 stage 2b source-aware runtime smoke",
        "methodology": {
            "sealedDataRead": False,
            "publicRuntimeEntry": "knowledge/runtime_search.py::search_knowledge",
            "configuredDefaultBeforeSmoke": configured_default,
            "temporarySourceAwareOverride": True,
            "canaryScope": "retrieval-only limited canary; not default rollout",
            "defaultRestoredAfterSmoke": (
                settings.knowledge_source_aware_enabled == configured_default
            ),
            "sourceAwareLatencyCeilingMs": latency_ceiling_ms,
            "fallbackLatencyCeilingMs": fallback_latency_ceiling_ms,
            "latencyCeilingsAreProductionSla": False,
        },
        "summary": {
            "passed": overall_passed,
            "eligibleForCanary": overall_passed,
            "sourceAwareCases": len(details),
            "sourceAwarePassed": passed_count,
            "fallbackPassed": fallback["passed"],
        },
        "details": details,
        "fallback": fallback,
        "failures": [detail for detail in details if not detail["passed"]]
        + ([] if fallback["passed"] else [fallback]),
    }
