import time
from collections.abc import Callable
from typing import Any

from app.knowledge.models import SearchKnowledgeResult
from app.knowledge.review_hybrid_search import search_review_hybrid
from app.knowledge.unified_search import search_knowledge_index
from app.rag_bm25 import prepare_review_bm25_indexes
from .rag_fuzzy_discovery_evaluation import (
    FUZZY_QREL_VERSION,
    load_fuzzy_shop_discovery_validation_cases,
    score_fuzzy_discovery_case,
    summarize_fuzzy_discovery_details,
)


Retriever = Callable[[str, int], SearchKnowledgeResult]
Preparer = Callable[[], Any]


def _reviews(result: SearchKnowledgeResult) -> list[dict[str, Any]]:
    scores = {citation.chunk_id: citation.score for citation in result.citations}
    return [
        {
            "reviewId": chunk.metadata.get("reviewId") or chunk.source_id,
            "shopId": chunk.metadata.get("shopId"),
            "shopName": chunk.metadata.get("shopName"),
            "text": chunk.content,
            "score": scores.get(chunk.chunk_id),
        }
        for chunk in result.chunks
    ]


def _timing(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "averageMs": sum(ordered) / len(ordered) if ordered else 0.0,
        "maxMs": max(ordered) if ordered else 0.0,
    }


def build_review_hybrid_validation_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    vector_retrieve: Retriever | None = None,
    hybrid_retrieve: Retriever | None = None,
    hybrid_prepare: Preparer | None = None,
    candidate_limit: int = 30,
    top_k: int = 5,
    evidence_per_shop: int = 3,
    warning_latency_ms: float = 3000.0,
    stop_latency_ms: float = 5000.0,
) -> dict[str, Any]:
    selected_cases = (
        cases
        if cases is not None
        else load_fuzzy_shop_discovery_validation_cases()
    )
    if not selected_cases:
        raise ValueError("validation cases must not be empty")
    if candidate_limit <= 0 or top_k <= 0:
        raise ValueError("candidate_limit and top_k must be positive")

    vector = vector_retrieve or (
        lambda query, limit: search_knowledge_index(
            query,
            sources=["reviews"],
            limit=limit,
        )
    )
    hybrid = hybrid_retrieve or (
        lambda query, limit: search_review_hybrid(
            query,
            limit,
            candidate_limit=candidate_limit,
        )
    )
    prepare = hybrid_prepare
    if prepare is None and hybrid_retrieve is None:
        prepare = prepare_review_bm25_indexes
    initialization_start = time.perf_counter()
    if prepare is not None:
        prepare()
    initialization_ms = (time.perf_counter() - initialization_start) * 1000
    ranked: dict[str, dict[str, list[dict[str, Any]]]] = {
        "sourceAwareVector": {},
        "reviewHybrid": {},
    }
    durations: dict[str, list[float]] = {
        "sourceAwareVector": [],
        "reviewHybrid": [],
    }
    durations_by_case: dict[str, dict[str, float]] = {
        "sourceAwareVector": {},
        "reviewHybrid": {},
    }

    for case in selected_cases:
        case_id = str(case["id"])
        question = str(case["question"])
        for method_name, retrieve in (
            ("sourceAwareVector", vector),
            ("reviewHybrid", hybrid),
        ):
            start = time.perf_counter()
            result = retrieve(question, candidate_limit)
            duration_ms = (time.perf_counter() - start) * 1000
            durations[method_name].append(duration_ms)
            durations_by_case[method_name][case_id] = duration_ms
            ranked[method_name][case_id] = _reviews(result)

    methods: dict[str, Any] = {}
    for method_name, by_case in ranked.items():
        details = [
            score_fuzzy_discovery_case(
                case,
                by_case[str(case["id"])],
                top_k=top_k,
                evidence_per_shop=evidence_per_shop,
            )
            for case in selected_cases
        ]
        for detail in details:
            detail["retrievalDurationMs"] = durations_by_case[method_name][
                str(detail["caseId"])
            ]
        methods[method_name] = {
            "metrics": summarize_fuzzy_discovery_details(details, top_k=top_k),
            "timing": _timing(durations[method_name]),
            "details": details,
        }

    vector_metrics = methods["sourceAwareVector"]["metrics"]
    hybrid_metrics = methods["reviewHybrid"]["metrics"]
    hybrid_timing = methods["reviewHybrid"]["timing"]
    checks = {
        "ndcgNonRegression": hybrid_metrics[f"meanNdcgAt{top_k}"]
        >= vector_metrics[f"meanNdcgAt{top_k}"],
        "recallNonRegression": hybrid_metrics[f"meanRecallAt{top_k}"]
        >= vector_metrics[f"meanRecallAt{top_k}"],
        "evidenceRecallNonRegression": hybrid_metrics["meanEvidenceShopRecall"]
        >= vector_metrics["meanEvidenceShopRecall"],
        "averageLatencyWithinWarningLine": hybrid_timing["averageMs"]
        <= warning_latency_ms,
        "maxLatencyWithinStopLine": hybrid_timing["maxMs"] <= stop_latency_ms,
    }
    return {
        "experiment": "RAG-PROD-01 stage 3 review Hybrid validation gate",
        "methodology": {
            "split": "validation only",
            "testRead": False,
            "sealedReportRead": False,
            "datasetRole": "frozen reused diagnostic set; not an unbiased production benchmark",
            "qrelVersion": FUZZY_QREL_VERSION,
            "bm25Corpus": "all review sources for generic and shop-filtered retrieval",
            "mixedCorpusLabelStatus": (
                "seed and Yelp strata measured separately; cross-source candidates remain unjudged"
            ),
            "candidateLimitPerRetriever": candidate_limit,
            "topKShops": top_k,
            "weights": {"vector": 0.20, "bm25": 0.80},
            "weightsRetuned": False,
            "warningLatencyMs": warning_latency_ms,
            "stopLatencyMs": stop_latency_ms,
            "hybridInitialization": {
                "performedBeforeTimedRequests": prepare is not None,
                "durationMs": initialization_ms,
                "productionEquivalent": "FastAPI startup prewarm when Hybrid flag is enabled",
            },
        },
        "summary": {
            "passed": all(checks.values()),
            "eligibleForHybridAgentCanary": all(checks.values()),
            "checks": checks,
        },
        "methods": methods,
    }
