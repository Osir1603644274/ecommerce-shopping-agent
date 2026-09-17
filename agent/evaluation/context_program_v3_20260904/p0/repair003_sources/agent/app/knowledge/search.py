import json
import re
import time
from pathlib import Path

from ..rag import DEFAULT_TOP_K, search_reviews as vector_search_reviews
from ..settings import settings
from .merchant_docs import (
    MERCHANT_DOC_SOURCE_NAME,
    load_merchant_doc_chunks,
)
from .models import Citation, KnowledgeChunk, RetrievalStep, RetrievalTrace, SearchKnowledgeResult
from .policy_docs import POLICY_DOC_SOURCE_NAME, load_policy_markdown_chunks
from .reviews import REVIEW_SOURCE_NAME, review_to_chunk, reviews_to_citations
from .router import route_knowledge_sources


DEFAULT_POLICY_DOCS_DIR = (
    Path(__file__).resolve().parents[2] / "knowledge_data" / "raw" / "policy_docs"
)
SUPPORTED_SOURCES = {
    REVIEW_SOURCE_NAME,
    MERCHANT_DOC_SOURCE_NAME,
    POLICY_DOC_SOURCE_NAME,
}


def _normalize_sources(sources: list[str] | None, query: str) -> tuple[list[str], bool]:
    if sources is None:
        return route_knowledge_sources(query), True

    normalized_sources: list[str] = []
    for source in sources:
        normalized_source = source.strip()
        if not normalized_source:
            continue
        if normalized_source not in SUPPORTED_SOURCES:
            raise ValueError(f"unsupported knowledge source: {normalized_source}")
        if normalized_source not in normalized_sources:
            normalized_sources.append(normalized_source)
    return normalized_sources or [REVIEW_SOURCE_NAME], False


def _query_terms(query: str) -> list[str]:
    lowered = query.lower()
    terms = [term for term in re.findall(r"[a-z0-9]+", lowered) if len(term) >= 3]
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.extend(
        "".join(chinese_chars[index : index + 2])
        for index in range(max(len(chinese_chars) - 1, 0))
    )
    synonyms = {
        "类别": ["分类", "类型", "类目"],
        "类型": ["分类", "类别", "类目"],
        "类目": ["分类", "类别", "类型"],
    }
    for term in list(terms):
        terms.extend(synonyms.get(term, []))
    return list(dict.fromkeys(term for term in terms if len(term) >= 2))


def _chunk_search_text(chunk: KnowledgeChunk) -> str:
    return " ".join(
        [
            chunk.title or "",
            chunk.content,
            json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True),
            " ".join(chunk.tags),
        ]
    ).lower()


def _score_chunk(chunk: KnowledgeChunk, terms: list[str]) -> float:
    if not terms:
        return 0.0
    haystack = _chunk_search_text(chunk)
    return float(sum(haystack.count(term) for term in terms))


def keyword_search_chunks(
    query: str,
    chunks: list[KnowledgeChunk],
    *,
    limit: int,
) -> tuple[list[KnowledgeChunk], list[Citation], int]:
    terms = _query_terms(query)
    scored = [
        (chunk, score)
        for chunk in chunks
        if (score := _score_chunk(chunk, terms)) > 0
    ]
    scored.sort(key=lambda item: (-item[1], item[0].chunk_id))
    top_scored = scored[:limit]
    returned_chunks = [chunk for chunk, _score in top_scored]
    citations = [
        chunk.to_citation(score=score, quote=chunk.content)
        for chunk, score in top_scored
    ]
    return returned_chunks, citations, len(scored)


def search_knowledge(
    query: str,
    sources: list[str] | None = None,
    limit: int = DEFAULT_TOP_K,
    *,
    shop_id: int | None = None,
) -> SearchKnowledgeResult:
    """Search one or more Agent knowledge sources and return a unified result.

    Runtime calls may omit ``sources`` and let the rule router choose sources.
    Evaluation calls should pass explicit sources, for example
    ``sources=["reviews"]``, to isolate one knowledge source from router errors.
    """

    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be blank")
    if limit <= 0:
        raise ValueError("limit must be positive")
    if shop_id is not None and shop_id <= 0:
        raise ValueError("shop_id must be positive")

    start = time.perf_counter()
    selected_sources, used_router = _normalize_sources(sources, normalized_query)
    if shop_id is not None and selected_sources != [REVIEW_SOURCE_NAME]:
        raise ValueError("shop_id filtering currently supports reviews only")
    chunks: list[KnowledgeChunk] = []
    citations: list[Citation] = []
    legacy_reviews: list[dict] = []
    steps: list[RetrievalStep] = [
        RetrievalStep(
            name="router" if used_router else "source_override",
            detail={
                "selectedSources": selected_sources,
                "mode": "auto" if used_router else "explicit",
            },
        )
    ]
    candidate_count = 0

    if REVIEW_SOURCE_NAME in selected_sources:
        review_start = time.perf_counter()
        if shop_id is None:
            reviews = vector_search_reviews(normalized_query, limit)
        else:
            reviews = vector_search_reviews(
                normalized_query,
                limit,
                shop_id=shop_id,
            )
        review_duration_ms = round((time.perf_counter() - review_start) * 1000, 2)
        legacy_reviews.extend(reviews)
        review_chunks = [review_to_chunk(review) for review in reviews]
        review_citations = reviews_to_citations(reviews)
        chunks.extend(review_chunks)
        citations.extend(review_citations)
        candidate_count += len(review_chunks)
        steps.append(
            RetrievalStep(
                name="vector_search",
                duration_ms=review_duration_ms,
                detail={
                    "source": REVIEW_SOURCE_NAME,
                    "retriever": "qdrant_vector",
                    "collection": settings.rag_collection_name,
                    "topK": limit,
                    "returned": len(review_chunks),
                },
            )
        )

    if MERCHANT_DOC_SOURCE_NAME in selected_sources:
        merchant_start = time.perf_counter()
        merchant_chunks, merchant_citations, merchant_candidate_count = keyword_search_chunks(
            normalized_query,
            load_merchant_doc_chunks(),
            limit=limit,
        )
        merchant_duration_ms = round((time.perf_counter() - merchant_start) * 1000, 2)
        chunks.extend(merchant_chunks)
        citations.extend(merchant_citations)
        candidate_count += merchant_candidate_count
        steps.append(
            RetrievalStep(
                name="keyword_search",
                duration_ms=merchant_duration_ms,
                detail={
                    "source": MERCHANT_DOC_SOURCE_NAME,
                    "retriever": "in_memory_keyword",
                    "candidateCount": merchant_candidate_count,
                    "returned": len(merchant_chunks),
                    "topK": limit,
                },
            )
        )

    if POLICY_DOC_SOURCE_NAME in selected_sources:
        policy_start = time.perf_counter()
        policy_chunks, policy_citations, policy_candidate_count = keyword_search_chunks(
            normalized_query,
            load_policy_markdown_chunks(DEFAULT_POLICY_DOCS_DIR),
            limit=limit,
        )
        policy_duration_ms = round((time.perf_counter() - policy_start) * 1000, 2)
        chunks.extend(policy_chunks)
        citations.extend(policy_citations)
        candidate_count += policy_candidate_count
        steps.append(
            RetrievalStep(
                name="keyword_search",
                duration_ms=policy_duration_ms,
                detail={
                    "source": POLICY_DOC_SOURCE_NAME,
                    "retriever": "in_memory_keyword",
                    "candidateCount": policy_candidate_count,
                    "returned": len(policy_chunks),
                    "topK": limit,
                },
            )
        )

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    trace = RetrievalTrace(
        query=normalized_query,
        selectedSources=selected_sources,
        filters={
            "visibility": "public",
            **({"shopId": shop_id} if shop_id is not None else {}),
        },
        candidateCount=candidate_count,
        returnedCount=len(chunks),
        durationMs=duration_ms,
        citations=citations,
        steps=steps,
    )
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=citations,
        trace=trace,
        legacy_reviews=legacy_reviews,
    )
