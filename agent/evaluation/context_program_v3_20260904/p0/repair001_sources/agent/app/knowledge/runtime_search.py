import logging
import time
from typing import Any

from ..rag import DEFAULT_TOP_K
from ..settings import settings
from .models import Citation, KnowledgeChunk, RetrievalStep, SearchKnowledgeResult
from .reviews import REVIEW_SOURCE_NAME, REVIEW_SOURCE_TYPE
from .search import _normalize_sources, search_knowledge as search_knowledge_legacy
from .source_aware_search import search_knowledge_source_aware


logger = logging.getLogger(__name__)


def _review_chunk_to_legacy_dict(
    chunk: KnowledgeChunk,
    citation: Citation | None,
) -> dict[str, Any]:
    """Adapt a unified review chunk to the detail shape used by the old tool."""

    metadata = chunk.metadata
    return {
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
        "score": citation.score if citation is not None else metadata.get("score"),
    }


def _attach_legacy_reviews(result: SearchKnowledgeResult) -> None:
    citations_by_chunk_id = {
        citation.chunk_id: citation
        for citation in result.citations
    }
    result.legacy_reviews = [
        _review_chunk_to_legacy_dict(
            chunk,
            citations_by_chunk_id.get(chunk.chunk_id),
        )
        for chunk in result.chunks
        if chunk.source_type == REVIEW_SOURCE_TYPE
    ]


def _attach_runtime_trace(
    result: SearchKnowledgeResult,
    *,
    effective_mode: str,
    fallback: bool,
    duration_ms: float,
    error_type: str | None = None,
) -> None:
    detail: dict[str, Any] = {
        "requestedMode": "source_aware",
        "effectiveMode": effective_mode,
        "fallback": fallback,
    }
    if error_type is not None:
        detail["errorType"] = error_type
    result.trace.steps.insert(
        0,
        RetrievalStep(
            name="runtime_search_route",
            durationMs=duration_ms,
            detail=detail,
        ),
    )
    result.trace.duration_ms = duration_ms


def search_knowledge(
    query: str,
    sources: list[str] | None = None,
    limit: int = DEFAULT_TOP_K,
    *,
    shop_id: int | None = None,
) -> SearchKnowledgeResult:
    """Dispatch production knowledge reads with a safe legacy fallback."""

    if not settings.knowledge_source_aware_enabled:
        return search_knowledge_legacy(
            query,
            sources,
            limit,
            shop_id=shop_id,
        )

    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be blank")
    if limit <= 0:
        raise ValueError("limit must be positive")
    if shop_id is not None and shop_id <= 0:
        raise ValueError("shop_id must be positive")

    selected_sources, _used_router = _normalize_sources(sources, normalized_query)
    if shop_id is not None and selected_sources != [REVIEW_SOURCE_NAME]:
        raise ValueError("shop_id filtering currently supports reviews only")

    start = time.perf_counter()
    try:
        result = search_knowledge_source_aware(
            normalized_query,
            selected_sources,
            limit,
            shop_ids=[shop_id] if shop_id is not None else None,
        )
    except Exception as exc:
        logger.exception(
            "Source-aware knowledge retrieval failed; falling back to legacy",
        )
        result = search_knowledge_legacy(
            normalized_query,
            selected_sources,
            limit,
            shop_id=shop_id,
        )
        _attach_runtime_trace(
            result,
            effective_mode="legacy",
            fallback=True,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            error_type=type(exc).__name__,
        )
        return result

    _attach_legacy_reviews(result)
    _attach_runtime_trace(
        result,
        effective_mode="source_aware",
        fallback=False,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return result
