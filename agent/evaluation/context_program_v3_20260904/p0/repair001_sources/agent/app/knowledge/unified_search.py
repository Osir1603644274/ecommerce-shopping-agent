import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from qdrant_client import QdrantClient, models

from ..rag import DEFAULT_TOP_K, get_embedding_model, get_qdrant_client
from ..settings import settings
from .merchant_docs import MERCHANT_DOC_SOURCE_NAME, MERCHANT_DOC_SOURCE_TYPE
from .models import KnowledgeChunk, RetrievalStep, RetrievalTrace, SearchKnowledgeResult
from .policy_docs import POLICY_DOC_SOURCE_NAME, POLICY_DOC_SOURCE_TYPE
from .reviews import REVIEW_SOURCE_NAME, REVIEW_SOURCE_TYPE
from .router import route_knowledge_sources


SOURCE_NAME_TO_TYPE = {
    REVIEW_SOURCE_NAME: REVIEW_SOURCE_TYPE,
    MERCHANT_DOC_SOURCE_NAME: MERCHANT_DOC_SOURCE_TYPE,
    POLICY_DOC_SOURCE_NAME: POLICY_DOC_SOURCE_TYPE,
}


def _normalize_sources(sources: list[str] | None, query: str) -> tuple[list[str], bool]:
    if sources is None:
        return route_knowledge_sources(query), True

    normalized: list[str] = []
    for source in sources:
        value = source.strip()
        if not value:
            continue
        if value not in SOURCE_NAME_TO_TYPE:
            raise ValueError(f"unsupported knowledge source: {value}")
        if value not in normalized:
            normalized.append(value)
    return normalized or [REVIEW_SOURCE_NAME], False


def _query_filter(
    source_type: str,
    shop_ids: list[int] | None,
    excluded_review_sources: list[str] | tuple[str, ...] | None = None,
) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(
            key="visibility",
            match=models.MatchValue(value="public"),
        ),
        models.FieldCondition(
            key="sourceType",
            match=models.MatchValue(value=source_type),
        ),
    ]
    if shop_ids is not None:
        must.append(
            models.FieldCondition(
                key="metadata.shopId",
                match=models.MatchAny(any=shop_ids),
            )
        )
    must_not: list[models.Condition] = []
    if source_type == REVIEW_SOURCE_TYPE and excluded_review_sources:
        must_not.append(
            models.FieldCondition(
                key="metadata.source",
                match=models.MatchAny(any=list(excluded_review_sources)),
            )
        )
    return models.Filter(must=must, must_not=must_not or None)


def _as_vector(values: Sequence[float] | Any) -> list[float]:
    if hasattr(values, "tolist"):
        values = values.tolist()
    return [float(value) for value in values]


def search_knowledge_index(
    query: str,
    sources: list[str] | None = None,
    limit: int = DEFAULT_TOP_K,
    *,
    shop_ids: list[int] | None = None,
    excluded_review_sources: list[str] | tuple[str, ...] | None = None,
    client: QdrantClient | None = None,
    embedder: Callable[[Iterable[str]], Iterable[Sequence[float]]] | None = None,
) -> SearchKnowledgeResult:
    """Search the unified Qdrant index without replacing the current read path."""

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

    start = time.perf_counter()
    selected_sources, used_router = _normalize_sources(sources, normalized_query)
    steps = [
        RetrievalStep(
            name="router" if used_router else "source_override",
            detail={
                "selectedSources": selected_sources,
                "mode": "auto" if used_router else "explicit",
            },
        )
    ]

    chunks: list[KnowledgeChunk] = []
    citations = []
    selected_client = client or get_qdrant_client()
    selected_embedder = embedder or get_embedding_model().embed

    # An empty hard-filter candidate set must stay empty instead of falling
    # back to an unrestricted knowledge search.
    query_vector = None
    if normalized_shop_ids != []:
        query_vector = _as_vector(next(iter(selected_embedder([normalized_query]))))

    for source_name in selected_sources:
        source_start = time.perf_counter()
        points = []
        if query_vector is not None:
            response = selected_client.query_points(
                collection_name=settings.knowledge_collection_name,
                query=query_vector,
                query_filter=_query_filter(
                    SOURCE_NAME_TO_TYPE[source_name],
                    normalized_shop_ids,
                    excluded_review_sources,
                ),
                limit=limit,
                with_payload=True,
            )
            points = list(response.points)

        for point in points:
            chunk = KnowledgeChunk.model_validate(point.payload)
            chunks.append(chunk)
            citations.append(chunk.to_citation(score=float(point.score)))

        steps.append(
            RetrievalStep(
                name="vector_search",
                durationMs=round((time.perf_counter() - source_start) * 1000, 2),
                detail={
                    "source": source_name,
                    "retriever": "qdrant_unified_vector",
                    "collection": settings.knowledge_collection_name,
                    "topK": limit,
                    "returned": len(points),
                },
            )
        )

    filters: dict[str, Any] = {"visibility": "public"}
    if normalized_shop_ids is not None:
        filters["shopIds"] = normalized_shop_ids
    if excluded_review_sources:
        filters["excludedReviewSources"] = list(excluded_review_sources)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=citations,
        trace=RetrievalTrace(
            query=normalized_query,
            selectedSources=selected_sources,
            filters=filters,
            candidateCount=len(chunks),
            returnedCount=len(chunks),
            durationMs=duration_ms,
            citations=citations,
            steps=steps,
        ),
    )
