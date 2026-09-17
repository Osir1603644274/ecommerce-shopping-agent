from typing import Any

from .knowledge import (
    REVIEW_SOURCE_NAME,
    SearchKnowledgeResult,
    build_review_retrieval_trace,
    review_to_chunk,
    reviews_to_citations,
)
from .rag import DEFAULT_TOP_K
from .schemas import ToolTrace
from .settings import settings


def build_search_reviews_tool_detail(
    query: str,
    reviews: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_TOP_K,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    """Build the detail payload shape returned by search_reviews_tool."""

    normalized_query = query.strip()
    retrieval_trace = build_review_retrieval_trace(
        normalized_query,
        reviews,
        limit=limit,
        duration_ms=duration_ms,
        collection_name=settings.rag_collection_name,
    )
    return {
        "query": normalized_query,
        "count": len(reviews),
        "reviews": reviews,
        "retrievalTrace": retrieval_trace.model_dump(by_alias=True),
    }


def build_search_knowledge_result_from_reviews(
    query: str,
    reviews: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_TOP_K,
    duration_ms: float | None = None,
) -> SearchKnowledgeResult:
    """Build the review-only SearchKnowledgeResult equivalent to search_knowledge_tool."""

    normalized_query = query.strip()
    chunks = [review_to_chunk(review) for review in reviews]
    citations = reviews_to_citations(reviews)
    retrieval_trace = build_review_retrieval_trace(
        normalized_query,
        reviews,
        limit=limit,
        duration_ms=duration_ms,
        collection_name=settings.rag_collection_name,
    )
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=citations,
        trace=retrieval_trace,
    )


def build_search_knowledge_tool_detail_from_reviews(
    query: str,
    reviews: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_TOP_K,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    """Build the detail payload shape returned by search_knowledge_tool in review-only mode."""

    normalized_query = query.strip()
    result = build_search_knowledge_result_from_reviews(
        normalized_query,
        reviews,
        limit=limit,
        duration_ms=duration_ms,
    )
    return {
        "query": normalized_query,
        "sources": result.trace.selected_sources,
        "count": len(result.chunks),
        "chunks": [chunk.model_dump(by_alias=True) for chunk in result.chunks],
        "citations": [
            citation.model_dump(by_alias=True)
            for citation in result.citations
        ],
        "retrievalTrace": result.trace.model_dump(by_alias=True),
    }


def _detail(output: ToolTrace | dict[str, Any]) -> Any:
    return output.detail if isinstance(output, ToolTrace) else output


def _is_ok(output: ToolTrace | dict[str, Any]) -> bool:
    return output.ok if isinstance(output, ToolTrace) else True


def extract_search_reviews_tool_review_ids(output: ToolTrace | dict[str, Any]) -> list[str]:
    """Extract TopK review ids from search_reviews_tool output."""

    detail = _detail(output)
    if not isinstance(detail, dict):
        return []
    return [
        str(review["reviewId"])
        for review in detail.get("reviews", [])
        if review.get("reviewId") is not None
    ]


def extract_search_knowledge_tool_review_ids(output: ToolTrace | dict[str, Any]) -> list[str]:
    """Extract TopK review ids from search_knowledge_tool review-only output."""

    detail = _detail(output)
    if not isinstance(detail, dict):
        return []
    ids: list[str] = []
    for citation in detail.get("citations", []):
        metadata = citation.get("metadata") or {}
        review_id = metadata.get("reviewId") or citation.get("sourceId")
        if review_id is not None:
            ids.append(str(review_id))
    return ids


def _selected_sources(output: ToolTrace | dict[str, Any]) -> list[str]:
    detail = _detail(output)
    if not isinstance(detail, dict):
        return []
    trace = detail.get("retrievalTrace") or {}
    sources = trace.get("selectedSources") or detail.get("sources") or []
    return [str(source) for source in sources]


def compare_review_tool_outputs(
    case: dict[str, Any],
    *,
    legacy_output: ToolTrace | dict[str, Any],
    candidate_output: ToolTrace | dict[str, Any],
) -> dict[str, Any]:
    """Compare search_reviews_tool with search_knowledge_tool in review-only mode."""

    legacy_ids = extract_search_reviews_tool_review_ids(legacy_output)
    candidate_ids = extract_search_knowledge_tool_review_ids(candidate_output)
    relevant_ids = set(case.get("relevantReviewIds", []))
    legacy_hit = bool(relevant_ids & set(legacy_ids))
    candidate_hit = bool(relevant_ids & set(candidate_ids))
    exact_match = legacy_ids == candidate_ids
    candidate_sources = _selected_sources(candidate_output)
    candidate_sources_ok = candidate_sources == [REVIEW_SOURCE_NAME]
    legacy_ok = _is_ok(legacy_output)
    candidate_ok = _is_ok(candidate_output)

    return {
        "caseId": case.get("id"),
        "question": case.get("question"),
        "expectedReviewIds": case.get("relevantReviewIds", []),
        "legacyOk": legacy_ok,
        "candidateOk": candidate_ok,
        "legacyReviewIds": legacy_ids,
        "candidateReviewIds": candidate_ids,
        "exactMatch": exact_match,
        "legacyHit": legacy_hit,
        "candidateHit": candidate_hit,
        "sameHit": legacy_hit == candidate_hit,
        "candidateRegression": legacy_hit and not candidate_hit,
        "candidateSources": candidate_sources,
        "candidateSourcesOk": candidate_sources_ok,
    }
