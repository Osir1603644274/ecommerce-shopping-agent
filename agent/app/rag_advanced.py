import asyncio
import time
from typing import Any

from .knowledge.models import SearchKnowledgeResult
from .knowledge.review_hybrid_search import search_review_hybrid
from .rag import DEFAULT_TOP_K
from .rag_answer import (
    NO_EVIDENCE_ANSWER,
    RagGenerationError,
    RagRetrievalError,
    knowledge_result_to_reviews,
    render_review_evidence_fallback,
)
from .rag_content_reranker import rerank_reviews_with_content_judge
from .rag_context_selection import select_reranked_review_context
from .rag_grounded_answer import (
    MAX_RECOMMENDATIONS,
    audit_grounded_answer,
    render_grounded_answer,
)
from .settings import settings


DEFAULT_CANDIDATE_LIMIT = 30


def _hybrid_result_to_reviews(
    result: SearchKnowledgeResult,
) -> list[dict[str, Any]]:
    return knowledge_result_to_reviews(result)


ADVANCED_PREFERENCE_MARKERS = (
    "推荐",
    "适合",
    "最好",
    "希望",
    "想找",
    "哪家",
    "安静",
    "氛围",
    "舒服",
    "友善",
    "耐心",
    "不会",
    "不希望",
    "不太",
    "约会",
    "办公",
    "学习",
    "聊天",
    "带孩子",
    "人情味",
)


def requires_advanced_rag(question: str) -> bool:
    normalized = question.strip().lower()
    return any(marker in normalized for marker in ADVANCED_PREFERENCE_MARKERS)


def _selected_sources(
    selection: dict[str, Any],
    review_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "reviewId": str(item["reviewId"]),
            "shopId": int(item["shopId"]),
            "shopName": str(item.get("shopName") or ""),
            "text": str(item["contextText"]),
            "score": float(item.get("fusionScore") or item.get("score") or 0.0),
        }
        for item in selection["selectedReviews"]
        if review_ids is None or str(item["reviewId"]) in review_ids
    ]


def _evidence_quote(text: str, limit: int = 180) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _build_deterministic_grounded_answer(
    selection: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
    """Render exact review excerpts so the runtime answer cannot overstate them."""

    reviews_by_shop: dict[int, list[dict[str, Any]]] = {}
    for review in selection["selectedReviews"]:
        reviews_by_shop.setdefault(int(review["shopId"]), []).append(review)

    recommendations: list[dict[str, Any]] = []
    cited_review_ids: set[str] = set()
    for shop in selection["shops"]:
        shop_id = int(shop["shopId"])
        selected = reviews_by_shop.get(shop_id, [])
        support = [
            review for review in selected if review["selectedRole"] == "support"
        ]
        if not support:
            continue
        conflict = [
            review for review in selected if review["selectedRole"] == "conflict"
        ]
        support_ids = [str(review["reviewId"]) for review in support]
        conflict_ids = [str(review["reviewId"]) for review in conflict]
        cited_review_ids.update(support_ids)
        cited_review_ids.update(conflict_ids)
        recommendations.append(
            {
                "shopId": shop_id,
                "shopName": str(shop.get("shopName") or ""),
                "reason": "；".join(
                    f"评论原文：“{_evidence_quote(str(review['contextText']))}”"
                    for review in support
                ),
                "citationReviewIds": support_ids,
                "caveat": (
                    "；".join(
                        f"另有评论提到：“{_evidence_quote(str(review['contextText']))}”"
                        for review in conflict
                    )
                    if conflict
                    else ""
                ),
                "caveatCitationReviewIds": conflict_ids,
            }
        )
        if len(recommendations) >= MAX_RECOMMENDATIONS:
            break

    answer = {
        "recommendations": recommendations,
        "modelRecommendationCount": 0,
    }
    answer["answer"] = render_grounded_answer(answer)
    return answer, audit_grounded_answer(answer, selection), cited_review_ids


async def answer_with_advanced_rag_observed(
    question: str,
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    hybrid_retrieve=search_review_hybrid,
    content_rerank=rerank_reviews_with_content_judge,
    context_select=select_reranked_review_context,
) -> tuple[
    str,
    list[dict[str, Any]],
    dict[str, float],
    dict[str, Any],
]:
    """Run the opt-in quality path without changing the default RAG route."""

    normalized_question = question.strip()
    metrics: dict[str, float] = {}
    if not normalized_question:
        return NO_EVIDENCE_ANSWER, [], metrics, {
            "requestedMode": "advanced",
            "effectiveMode": "advanced",
            "stages": [],
        }
    if candidate_limit < DEFAULT_TOP_K:
        raise ValueError("candidate_limit must be at least DEFAULT_TOP_K")

    try:
        retrieval_start = time.perf_counter()
        hybrid_result = await asyncio.to_thread(
            hybrid_retrieve,
            normalized_question,
            candidate_limit,
            candidate_limit=candidate_limit,
        )
        metrics["retrievalDurationMs"] = (
            time.perf_counter() - retrieval_start
        ) * 1000
        candidates = _hybrid_result_to_reviews(hybrid_result)
    except Exception as exc:
        raise RagRetrievalError("advanced RAG retrieval failed") from exc

    if not candidates:
        return NO_EVIDENCE_ANSWER, [], metrics, {
            "requestedMode": "advanced",
            "effectiveMode": "advanced",
            "stages": ["hybrid_retrieval"],
            "candidateReviewCount": 0,
            "selectedReviewCount": 0,
            "retrievalTrace": hybrid_result.trace.model_dump(by_alias=True),
        }

    reranker_start = time.perf_counter()
    try:
        reranked = await asyncio.wait_for(
            content_rerank(normalized_question, candidates),
            timeout=settings.knowledge_advanced_reranker_timeout_seconds,
        )
        metrics["rerankerDurationMs"] = (
            time.perf_counter() - reranker_start
        ) * 1000
    except Exception as exc:
        metrics["rerankerDurationMs"] = (
            time.perf_counter() - reranker_start
        ) * 1000
        metrics["llmDurationMs"] = metrics["rerankerDurationMs"]
        fallback_sources = candidates[:DEFAULT_TOP_K]
        return (
            render_review_evidence_fallback(
                fallback_sources,
                prefix="内容判断暂时不可用，以下仅提供混合检索到的评论原文：",
            ),
            fallback_sources,
            metrics,
            {
                "requestedMode": "advanced",
                "effectiveMode": "hybrid_evidence_fallback",
                "fallback": True,
                "fallbackReason": type(exc).__name__,
                "stages": [
                    "hybrid_retrieval",
                    "llm_content_rerank_failed",
                    "deterministic_evidence_fallback",
                ],
                "candidateReviewCount": len(candidates),
                "selectedReviewCount": len(fallback_sources),
                "retrievalTrace": hybrid_result.trace.model_dump(by_alias=True),
            },
        )

    try:
        selection_start = time.perf_counter()
        selection = context_select(reranked)
        metrics["contextSelectionDurationMs"] = (
            time.perf_counter() - selection_start
        ) * 1000
        generated, audit, cited_review_ids = _build_deterministic_grounded_answer(
            selection
        )
        sources = _selected_sources(selection, cited_review_ids)
        if not generated["recommendations"]:
            metrics["llmDurationMs"] = metrics["rerankerDurationMs"]
            return NO_EVIDENCE_ANSWER, [], metrics, {
                "requestedMode": "advanced",
                "effectiveMode": "advanced",
                "stages": [
                    "hybrid_retrieval",
                    "llm_content_rerank",
                    "context_selection",
                ],
                "candidateReviewCount": len(candidates),
                "contentRerankerCandidateCount": len(candidates),
                "selectedReviewCount": 0,
                "retrievalTrace": hybrid_result.trace.model_dump(by_alias=True),
            }
        metrics["llmDurationMs"] = metrics["rerankerDurationMs"]
    except Exception as exc:
        raise RagGenerationError(
            _selected_sources(selection) if "selection" in locals() else [],
            metrics,
        ) from exc

    pipeline = {
        "requestedMode": "advanced",
        "effectiveMode": "advanced",
        "stages": [
            "hybrid_retrieval",
            "llm_content_rerank",
            "context_selection",
            "deterministic_evidence_answer",
            "deterministic_citation_audit",
        ],
        "candidateReviewCount": len(candidates),
        "contentRerankerCandidateCount": len(candidates),
        "selectedReviewCount": len(sources),
        "selectedCharacterCount": sum(len(item["text"]) for item in sources),
        "retrievalTrace": hybrid_result.trace.model_dump(by_alias=True),
        "citationAudit": audit,
    }
    return str(generated["answer"]), sources, metrics, pipeline
