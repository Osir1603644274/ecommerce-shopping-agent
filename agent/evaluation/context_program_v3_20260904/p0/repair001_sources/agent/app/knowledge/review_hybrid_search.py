import time
from collections.abc import Callable
from typing import Any

from ..rag import DEFAULT_TOP_K
from ..rag_bm25 import search_reviews_bm25
from ..rag_fusion import fuse_by_normalized_score
from ..settings import settings
from .models import KnowledgeChunk, RetrievalStep, RetrievalTrace, SearchKnowledgeResult
from .reviews import REVIEW_SOURCE_NAME, review_to_chunk
from .unified_search import search_knowledge_index


VectorRetrieve = Callable[..., SearchKnowledgeResult]
Bm25Retrieve = Callable[..., list[dict[str, Any]]]


def _vector_reviews(result: SearchKnowledgeResult) -> list[dict[str, Any]]:
    scores = {citation.chunk_id: citation.score for citation in result.citations}
    reviews: list[dict[str, Any]] = []
    for chunk in result.chunks:
        metadata = chunk.metadata
        reviews.append(
            {
                "reviewId": metadata.get("reviewId") or chunk.source_id,
                "shopId": metadata.get("shopId"),
                "shopName": metadata.get("shopName"),
                "text": chunk.content,
                "originalText": metadata.get("originalText"),
                "contentZh": metadata.get("contentZh"),
                "source": metadata.get("source"),
                "language": chunk.language,
                "sourceLanguage": metadata.get("sourceLanguage"),
                "translationStatus": metadata.get("translationStatus"),
                "score": scores.get(chunk.chunk_id),
            }
        )
    return reviews


def search_review_hybrid(
    query: str,
    limit: int = DEFAULT_TOP_K,
    *,
    shop_ids: list[int] | None = None,
    candidate_limit: int | None = None,
    vector_weight: float | None = None,
    bm25_weight: float | None = None,
    vector_retrieve: VectorRetrieve = search_knowledge_index,
    bm25_retrieve: Bm25Retrieve = search_reviews_bm25,
) -> SearchKnowledgeResult:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be blank")
    if limit <= 0:
        raise ValueError("limit must be positive")
    normalized_shop_ids = None
    if shop_ids is not None:
        normalized_shop_ids = list(dict.fromkeys(shop_ids))
        if any(shop_id <= 0 for shop_id in normalized_shop_ids):
            raise ValueError("shop_ids must contain only positive integers")

    selected_candidate_limit = (
        settings.knowledge_review_hybrid_candidate_limit
        if candidate_limit is None
        else candidate_limit
    )
    if selected_candidate_limit <= 0:
        raise ValueError("candidate_limit must be positive")
    if selected_candidate_limit < limit:
        raise ValueError("candidate_limit must be greater than or equal to limit")
    selected_vector_weight = (
        settings.knowledge_review_hybrid_vector_weight
        if vector_weight is None
        else vector_weight
    )
    selected_bm25_weight = (
        settings.knowledge_review_hybrid_bm25_weight
        if bm25_weight is None
        else bm25_weight
    )

    start = time.perf_counter()
    vector_start = time.perf_counter()
    vector_result = vector_retrieve(
        normalized_query,
        sources=[REVIEW_SOURCE_NAME],
        limit=selected_candidate_limit,
        shop_ids=normalized_shop_ids,
        excluded_review_sources=settings.knowledge_review_excluded_sources,
    )
    vector_duration_ms = round((time.perf_counter() - vector_start) * 1000, 2)
    vector_reviews = _vector_reviews(vector_result)

    bm25_start = time.perf_counter()
    bm25_reviews = bm25_retrieve(
        normalized_query,
        selected_candidate_limit,
        shop_ids=normalized_shop_ids,
    )
    bm25_duration_ms = round((time.perf_counter() - bm25_start) * 1000, 2)

    fusion_start = time.perf_counter()
    fused = fuse_by_normalized_score(
        vector_reviews,
        bm25_reviews,
        vector_weight=selected_vector_weight,
        bm25_weight=selected_bm25_weight,
        normalization="min_max",
    )
    returned = fused[:limit]
    fusion_duration_ms = round((time.perf_counter() - fusion_start) * 1000, 2)

    chunks: list[KnowledgeChunk] = []
    citations = []
    for item in returned:
        review = {**item, "score": float(item["fusionScore"])}
        chunk = review_to_chunk(review)
        chunk.metadata.update(
            {
                "vectorRank": item.get("vectorRank"),
                "bm25Rank": item.get("bm25Rank"),
                "vectorScore": item.get("vectorScore"),
                "bm25Score": item.get("bm25Score"),
                "fusionScore": item.get("fusionScore"),
            }
        )
        chunks.append(chunk)
        citations.append(chunk.to_citation(score=float(item["fusionScore"])))

    vector_ids = {str(item["reviewId"]) for item in vector_reviews}
    bm25_ids = {str(item["reviewId"]) for item in bm25_reviews}
    filters: dict[str, Any] = {"visibility": "public"}
    filters["excludedReviewSources"] = list(
        settings.knowledge_review_excluded_sources
    )
    if normalized_shop_ids is not None:
        filters["shopIds"] = normalized_shop_ids
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=citations,
        trace=RetrievalTrace(
            query=normalized_query,
            selectedSources=[REVIEW_SOURCE_NAME],
            filters=filters,
            candidateCount=len(vector_ids | bm25_ids),
            returnedCount=len(chunks),
            durationMs=duration_ms,
            citations=citations,
            steps=[
                RetrievalStep(
                    name="vector_recall",
                    durationMs=vector_duration_ms,
                    detail={
                        "retriever": "qdrant_unified_vector",
                        "candidateLimit": selected_candidate_limit,
                        "returned": len(vector_reviews),
                    },
                ),
                RetrievalStep(
                    name="bm25_recall",
                    durationMs=bm25_duration_ms,
                    detail={
                        "retriever": "python_bm25_all_reviews",
                        "corpus": "all_review_sources",
                        "candidateLimit": selected_candidate_limit,
                        "returned": len(bm25_reviews),
                    },
                ),
                RetrievalStep(
                    name="hybrid_fusion",
                    durationMs=fusion_duration_ms,
                    detail={
                        "normalization": "min_max",
                        "vectorWeight": selected_vector_weight,
                        "bm25Weight": selected_bm25_weight,
                        "vectorCandidates": len(vector_ids),
                        "bm25Candidates": len(bm25_ids),
                        "overlap": len(vector_ids & bm25_ids),
                        "union": len(vector_ids | bm25_ids),
                        "topK": limit,
                        "returned": len(returned),
                        "ranking": [
                            {
                                "reviewId": item["reviewId"],
                                "vectorRank": item.get("vectorRank"),
                                "bm25Rank": item.get("bm25Rank"),
                                "fusionScore": item.get("fusionScore"),
                            }
                            for item in returned
                        ],
                    },
                ),
            ],
        ),
    )
