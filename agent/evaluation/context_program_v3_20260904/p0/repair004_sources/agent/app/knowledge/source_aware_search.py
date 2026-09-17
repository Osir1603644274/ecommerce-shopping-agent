import logging
import time
from typing import Any

from qdrant_client import QdrantClient, models

from ..rag import DEFAULT_TOP_K, get_qdrant_client
from ..settings import settings
from .merchant_docs import MERCHANT_DOC_SOURCE_NAME, MERCHANT_DOC_SOURCE_TYPE
from .models import KnowledgeChunk, RetrievalStep, RetrievalTrace, SearchKnowledgeResult
from .policy_docs import POLICY_DOC_SOURCE_NAME
from .reviews import REVIEW_SOURCE_NAME
from .review_hybrid_search import search_review_hybrid
from .search import keyword_search_chunks
from .unified_search import SOURCE_NAME_TO_TYPE, search_knowledge_index


logger = logging.getLogger(__name__)


def _public_source_filter(source_type: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(
                key="visibility",
                match=models.MatchValue(value="public"),
            ),
            models.FieldCondition(
                key="sourceType",
                match=models.MatchValue(value=source_type),
            ),
        ]
    )


def _load_source_chunks_from_index(
    source_type: str,
    *,
    client: QdrantClient,
) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    offset: Any = None
    while True:
        points, offset = client.scroll(
            collection_name=settings.knowledge_collection_name,
            scroll_filter=_public_source_filter(source_type),
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        chunks.extend(KnowledgeChunk.model_validate(point.payload) for point in points)
        if offset is None:
            return chunks


def search_merchant_docs_lexical_index(
    query: str,
    limit: int = DEFAULT_TOP_K,
    *,
    client: QdrantClient | None = None,
) -> SearchKnowledgeResult:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be blank")
    if limit <= 0:
        raise ValueError("limit must be positive")
    start = time.perf_counter()
    selected_client = client or get_qdrant_client()
    chunks = _load_source_chunks_from_index(
        MERCHANT_DOC_SOURCE_TYPE,
        client=selected_client,
    )
    returned, citations, candidate_count = keyword_search_chunks(
        normalized_query,
        chunks,
        limit=limit,
    )
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    return SearchKnowledgeResult(
        chunks=returned,
        citations=citations,
        trace=RetrievalTrace(
            query=normalized_query,
            selectedSources=[MERCHANT_DOC_SOURCE_NAME],
            filters={"visibility": "public"},
            candidateCount=candidate_count,
            returnedCount=len(returned),
            durationMs=duration_ms,
            citations=citations,
            steps=[
                RetrievalStep(
                    name="lexical_search",
                    durationMs=duration_ms,
                    detail={
                        "source": MERCHANT_DOC_SOURCE_NAME,
                        "retriever": "qdrant_payload_lexical",
                        "collection": settings.knowledge_collection_name,
                        "scanned": len(chunks),
                        "candidateCount": candidate_count,
                        "topK": limit,
                        "returned": len(returned),
                    },
                )
            ],
        ),
    )


def _search_reviews(
    query: str,
    limit: int,
    *,
    shop_ids: list[int] | None,
) -> SearchKnowledgeResult:
    if not settings.knowledge_review_hybrid_enabled:
        return search_knowledge_index(
            query,
            sources=[REVIEW_SOURCE_NAME],
            limit=limit,
            shop_ids=shop_ids,
            excluded_review_sources=settings.knowledge_review_excluded_sources,
        )

    start = time.perf_counter()
    try:
        result = search_review_hybrid(
            query,
            limit,
            shop_ids=shop_ids,
        )
    except Exception as exc:
        logger.exception(
            "Review Hybrid retrieval failed; falling back to Source-aware Vector",
        )
        result = search_knowledge_index(
            query,
            sources=[REVIEW_SOURCE_NAME],
            limit=limit,
            shop_ids=shop_ids,
            excluded_review_sources=settings.knowledge_review_excluded_sources,
        )
        result.trace.steps.insert(
            0,
            RetrievalStep(
                name="review_hybrid_route",
                durationMs=round((time.perf_counter() - start) * 1000, 2),
                detail={
                    "requestedMode": "hybrid",
                    "effectiveMode": "vector",
                    "fallback": True,
                    "errorType": type(exc).__name__,
                },
            ),
        )
        return result

    result.trace.steps.insert(
        0,
        RetrievalStep(
            name="review_hybrid_route",
            durationMs=round((time.perf_counter() - start) * 1000, 2),
            detail={
                "requestedMode": "hybrid",
                "effectiveMode": "hybrid",
                "fallback": False,
            },
        ),
    )
    return result


def search_knowledge_source_aware(
    query: str,
    sources: list[str],
    limit: int = DEFAULT_TOP_K,
    *,
    shop_ids: list[int] | None = None,
) -> SearchKnowledgeResult:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be blank")
    if limit <= 0:
        raise ValueError("limit must be positive")
    normalized_sources = list(dict.fromkeys(source.strip() for source in sources if source.strip()))
    unsupported = [source for source in normalized_sources if source not in SOURCE_NAME_TO_TYPE]
    if unsupported:
        raise ValueError(f"unsupported knowledge source: {unsupported[0]}")
    if not normalized_sources:
        raise ValueError("sources must not be empty")

    start = time.perf_counter()
    partial_results: list[SearchKnowledgeResult] = []
    for source in normalized_sources:
        if source == REVIEW_SOURCE_NAME:
            partial_results.append(
                _search_reviews(
                    normalized_query,
                    limit,
                    shop_ids=shop_ids,
                )
            )
        elif source == MERCHANT_DOC_SOURCE_NAME:
            if shop_ids is None:
                partial_results.append(
                    search_merchant_docs_lexical_index(normalized_query, limit)
                )
            else:
                partial_results.append(
                    search_knowledge_index(
                        normalized_query,
                        sources=[MERCHANT_DOC_SOURCE_NAME],
                        limit=limit,
                        shop_ids=shop_ids,
                    )
                )
        elif source == POLICY_DOC_SOURCE_NAME:
            partial_results.append(
                search_knowledge_index(
                    normalized_query,
                    sources=[POLICY_DOC_SOURCE_NAME],
                    limit=limit,
                )
            )

    chunks = [chunk for result in partial_results for chunk in result.chunks]
    citations = [citation for result in partial_results for citation in result.citations]
    filters: dict[str, Any] = {"visibility": "public"}
    if shop_ids is not None:
        filters["shopIds"] = list(dict.fromkeys(shop_ids))
    if REVIEW_SOURCE_NAME in normalized_sources:
        filters["excludedReviewSources"] = list(
            settings.knowledge_review_excluded_sources
        )
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=citations,
        trace=RetrievalTrace(
            query=normalized_query,
            selectedSources=normalized_sources,
            filters=filters,
            candidateCount=sum(result.trace.candidate_count for result in partial_results),
            returnedCount=len(chunks),
            durationMs=round((time.perf_counter() - start) * 1000, 2),
            citations=citations,
            steps=[
                RetrievalStep(
                    name="source_aware_strategy",
                    detail={
                        "reviews": (
                            "vector_bm25_hybrid_with_optional_shop_filter"
                            if settings.knowledge_review_hybrid_enabled
                            else "qdrant_vector_with_optional_shop_filter"
                        ),
                        "merchantDocs": (
                            "qdrant_vector_shop_filter"
                            if shop_ids is not None
                            else "qdrant_payload_lexical"
                        ),
                        "policyDocs": "qdrant_vector",
                    },
                ),
                *(step for result in partial_results for step in result.trace.steps),
            ],
        ),
    )
