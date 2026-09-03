import asyncio
import time
from collections.abc import Callable
from typing import Any

from app.rag_bm25 import search_yelp_reviews_bm25
from app.rag_content_reranker import DEFAULT_BATCH_SIZE
from app.rag_fusion import fuse_by_normalized_score
from .rag_fuzzy_discovery_evaluation import (
    FUZZY_QREL_VERSION,
    load_fuzzy_shop_discovery_validation_cases,
    score_fuzzy_discovery_case,
    summarize_fuzzy_discovery_details,
)
from app.rag_quality import search_yelp_reviews
from app.rag_query_decomposed_reranker import (
    CONDITION_JUDGE_PROMPT_VERSION,
    ConditionBatchJudge,
    rerank_reviews_by_shop_condition_coverage,
    rerank_reviews_with_query_decomposition,
)
from app.rag_query_decomposition import (
    QUERY_DECOMPOSITION_PROMPT_VERSION,
    QueryDecomposer,
    decompose_query_conditions,
)
from app.settings import settings


Retriever = Callable[[str, int], list[dict[str, Any]]]


def _first_review_per_shop(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for review in reviews:
        shop_id = int(review["shopId"])
        if shop_id not in seen:
            seen.add(shop_id)
            result.append(review)
    return result


async def build_query_decomposed_reranker_validation_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    vector_retrieve: Retriever = search_yelp_reviews,
    bm25_retrieve: Retriever = search_yelp_reviews_bm25,
    decompose_query: QueryDecomposer = decompose_query_conditions,
    judge_batch: ConditionBatchJudge | None = None,
    candidate_limit: int = 30,
    top_k: int = 5,
    evidence_per_shop: int = 3,
    vector_weight: float = 0.20,
    bm25_weight: float = 0.80,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    selected_cases = (
        cases
        if cases is not None
        else load_fuzzy_shop_discovery_validation_cases()
    )
    hybrid_details: list[dict[str, Any]] = []
    reranker_details: list[dict[str, Any]] = []
    shop_coverage_details: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    timing: list[dict[str, float | int | str]] = []

    for case in selected_cases:
        question = str(case["question"])
        retrieval_start = time.perf_counter()
        vector, bm25 = await asyncio.gather(
            asyncio.to_thread(vector_retrieve, question, candidate_limit),
            asyncio.to_thread(bm25_retrieve, question, candidate_limit),
        )
        retrieval_ms = (time.perf_counter() - retrieval_start) * 1000
        hybrid = fuse_by_normalized_score(
            vector,
            bm25,
            normalization="min_max",
            vector_weight=vector_weight,
            bm25_weight=bm25_weight,
        )

        reranker_start = time.perf_counter()
        kwargs: dict[str, Any] = {
            "decompose_query": decompose_query,
            "batch_size": batch_size,
        }
        if judge_batch is not None:
            kwargs["judge_batch"] = judge_batch
        decomposition, reranked = await rerank_reviews_with_query_decomposition(
            question,
            hybrid,
            **kwargs,
        )
        shop_coverage_reranked = rerank_reviews_by_shop_condition_coverage(
            decomposition,
            reranked,
        )
        reranker_ms = (time.perf_counter() - reranker_start) * 1000

        hybrid_details.append(
            score_fuzzy_discovery_case(
                case,
                hybrid,
                top_k=top_k,
                evidence_per_shop=evidence_per_shop,
            )
        )
        reranker_details.append(
            score_fuzzy_discovery_case(
                case,
                reranked,
                top_k=top_k,
                evidence_per_shop=evidence_per_shop,
            )
        )
        shop_coverage_details.append(
            score_fuzzy_discovery_case(
                case,
                shop_coverage_reranked,
                top_k=top_k,
                evidence_per_shop=evidence_per_shop,
            )
        )
        audit.append(
            {
                "caseId": str(case["id"]),
                "question": question,
                "decomposition": decomposition,
                "rankedCandidates": [
                    {
                        "rank": rank,
                        "reviewId": str(item["reviewId"]),
                        "shopId": int(item["shopId"]),
                        "shopName": item.get("shopName"),
                        "originalHybridRank": int(item["originalRank"]),
                        "relation": str(item["contentRelation"]),
                        "score": int(item["contentScore"]),
                        "conditionAssessments": item["conditionAssessments"],
                        "reason": str(item["contentReason"]),
                    }
                    for rank, item in enumerate(reranked, start=1)
                ],
                "shopCoverageRanking": [
                    {
                        "shopRank": int(item["shopConditionRank"]),
                        "shopId": int(item["shopId"]),
                        "shopName": item.get("shopName"),
                        "relation": str(item["shopConditionRelation"]),
                        "score": int(item["shopConditionScore"]),
                        "conditionAssessments": item["shopConditionAssessments"],
                    }
                    for item in _first_review_per_shop(shop_coverage_reranked)
                ],
            }
        )
        timing.append(
            {
                "caseId": str(case["id"]),
                "retrievalMs": retrieval_ms,
                "decompositionAndRerankerMs": reranker_ms,
                "candidateReviewCount": len(hybrid),
            }
        )

    return {
        "methodology": {
            "split": "validation only",
            "testRead": False,
            "candidatePool": "independent Vector/BM25 Top-N union",
            "candidateLimitPerRetriever": candidate_limit,
            "inputOrdering": "Min-Max score fusion with frozen Vector:BM25 weights",
            "hybridWeights": {"vector": vector_weight, "bm25": bm25_weight},
            "queryDecompositionPromptVersion": QUERY_DECOMPOSITION_PROMPT_VERSION,
            "conditionJudgePromptVersion": CONDITION_JUDGE_PROMPT_VERSION,
            "shopCoverageRule": (
                "combine per-condition evidence across reviews from the same shop; "
                "support wins over conflict when both are present"
            ),
            "contentJudgeModel": settings.deepseek_model,
            "qrelVersion": FUZZY_QREL_VERSION,
            "topKShops": top_k,
            "labelCaveat": "temporary pooled validation; no production or test claim",
        },
        "caseCount": len(selected_cases),
        "timing": timing,
        "audit": audit,
        "methods": {
            "hybridV02B08": {
                "metrics": summarize_fuzzy_discovery_details(
                    hybrid_details,
                    top_k=top_k,
                ),
                "details": hybrid_details,
            },
            "queryDecomposedContentReranker": {
                "metrics": summarize_fuzzy_discovery_details(
                    reranker_details,
                    top_k=top_k,
                ),
                "details": reranker_details,
            },
            "queryDecomposedShopCoverageReranker": {
                "metrics": summarize_fuzzy_discovery_details(
                    shop_coverage_details,
                    top_k=top_k,
                ),
                "details": shop_coverage_details,
            },
        },
    }
