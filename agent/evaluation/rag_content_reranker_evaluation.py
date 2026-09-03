import asyncio
import time
from collections.abc import Callable
from typing import Any

from app.rag_bm25 import search_yelp_reviews_bm25
from app.rag_content_reranker import (
    CONTENT_RERANKER_PROMPT_VERSION,
    MAX_REVIEW_CHARS,
    ReviewBatchJudge,
    rerank_reviews_with_content_judge,
)
from app.rag_fusion import fuse_by_normalized_score
from .rag_fuzzy_discovery_evaluation import (
    FUZZY_QREL_VERSION,
    load_fuzzy_shop_discovery_validation_cases,
    score_fuzzy_discovery_case,
    summarize_fuzzy_discovery_details,
)
from app.rag_quality import search_yelp_reviews
from app.settings import settings


Retriever = Callable[[str, int], list[dict[str, Any]]]


async def build_content_reranker_validation_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    vector_retrieve: Retriever = search_yelp_reviews,
    bm25_retrieve: Retriever = search_yelp_reviews_bm25,
    judge_batch: ReviewBatchJudge | None = None,
    candidate_limit: int = 30,
    top_k: int = 5,
    evidence_per_shop: int = 3,
    vector_weight: float = 0.20,
    bm25_weight: float = 0.80,
    batch_size: int = 20,
) -> dict[str, Any]:
    selected_cases = (
        cases
        if cases is not None
        else load_fuzzy_shop_discovery_validation_cases()
    )
    hybrid_details: list[dict[str, Any]] = []
    reranker_details: list[dict[str, Any]] = []
    reranker_audit: list[dict[str, Any]] = []
    timing: list[dict[str, float]] = []

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
        rerank_kwargs: dict[str, Any] = {"batch_size": batch_size}
        if judge_batch is not None:
            rerank_kwargs["judge_batch"] = judge_batch
        reranked = await rerank_reviews_with_content_judge(
            question,
            hybrid,
            **rerank_kwargs,
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
        reranker_audit.append(
            {
                "caseId": str(case["id"]),
                "rankedCandidates": [
                    {
                        "rank": rank,
                        "reviewId": str(item["reviewId"]),
                        "shopId": int(item["shopId"]),
                        "shopName": item.get("shopName"),
                        "originalHybridRank": int(item["originalRank"]),
                        "relation": str(item["contentRelation"]),
                        "score": int(item["contentScore"]),
                        "reason": str(item["contentReason"]),
                    }
                    for rank, item in enumerate(reranked, start=1)
                ],
            }
        )
        timing.append(
            {
                "caseId": str(case["id"]),
                "retrievalMs": retrieval_ms,
                "contentRerankerMs": reranker_ms,
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
            "contentJudgeLabels": ["support", "conflict", "irrelevant"],
            "contentJudgeModel": settings.deepseek_model,
            "contentJudgePromptVersion": CONTENT_RERANKER_PROMPT_VERSION,
            "maxReviewChars": MAX_REVIEW_CHARS,
            "contentJudgeBatchSize": batch_size,
            "qrelVersion": FUZZY_QREL_VERSION,
            "topKShops": top_k,
            "selectionUse": True,
            "labelCaveat": "temporary pooled validation; no production or test claim",
        },
        "caseCount": len(selected_cases),
        "timing": timing,
        "rerankerAudit": reranker_audit,
        "methods": {
            "hybridV02B08": {
                "metrics": summarize_fuzzy_discovery_details(
                    hybrid_details,
                    top_k=top_k,
                ),
                "details": hybrid_details,
            },
            "llmContentReranker": {
                "metrics": summarize_fuzzy_discovery_details(
                    reranker_details,
                    top_k=top_k,
                ),
                "details": reranker_details,
            },
        },
    }
