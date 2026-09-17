from typing import Any

from .models import Citation, KnowledgeChunk, RetrievalStep, RetrievalTrace


REVIEW_SOURCE_NAME = "reviews"
REVIEW_SOURCE_TYPE = "review"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def review_to_chunk(review: dict[str, Any]) -> KnowledgeChunk:
    """Convert the legacy review retrieval dict into a generic knowledge chunk."""

    review_id = _clean_text(review.get("reviewId", review.get("id")))
    translated_content = _clean_text(
        review.get("contentZh") or review.get("content_zh")
    )
    content = _clean_text(
        translated_content
        or review.get("text")
        or review.get("originalText")
        or review.get("content")
    )
    source_language = _clean_text(
        review.get("sourceLanguage") or review.get("language")
    ) or "zh"
    shop_name = _clean_text(review.get("shopName")) or "未知商户"
    tags = _clean_tags(review.get("tags"))
    metadata: dict[str, Any] = {
        "reviewId": review_id,
        "shopName": shop_name,
        "sourceLanguage": source_language,
    }

    for source_key, output_key in (
        ("shopId", "shopId"),
        ("originalText", "originalText"),
        ("contentZh", "contentZh"),
        ("source", "source"),
        ("translationStatus", "translationStatus"),
        ("sourceReviewId", "sourceReviewId"),
        ("sourceUserId", "sourceUserId"),
        ("stars", "stars"),
        ("sourceShopName", "sourceShopName"),
        ("evidenceScope", "evidenceScope"),
        ("score", "score"),
    ):
        if review.get(source_key) is not None:
            metadata[output_key] = review[source_key]

    return KnowledgeChunk(
        chunk_id=f"review:{review_id}",
        source_type=REVIEW_SOURCE_TYPE,
        source_id=review_id,
        chunk_index=1,
        source_version=str(review.get("sourceVersion") or review.get("updatedAt") or "1"),
        title=f"{shop_name}评论",
        content=content,
        metadata=metadata,
        visibility=review.get("visibility", "public"),
        owner_user_id=review.get("ownerUserId"),
        language="zh" if translated_content else source_language,
        tags=tags,
        updated_at=review.get("updatedAt"),
    )


def review_to_citation(review: dict[str, Any]) -> Citation:
    """Convert one retrieved review into a structured citation."""

    chunk = review_to_chunk(review)
    return chunk.to_citation(score=_optional_score(review.get("score")), quote=chunk.content)


def reviews_to_citations(reviews: list[dict[str, Any]]) -> list[Citation]:
    return [review_to_citation(review) for review in reviews]


def build_review_retrieval_trace(
    query: str,
    reviews: list[dict[str, Any]],
    *,
    limit: int,
    duration_ms: float | None = None,
    collection_name: str | None = None,
    filters: dict[str, Any] | None = None,
) -> RetrievalTrace:
    """Build an Agent RAG trace around the existing Qdrant review search."""

    normalized_filters = {"visibility": "public"}
    if filters:
        normalized_filters.update(filters)

    step_detail: dict[str, Any] = {
        "retriever": "qdrant_vector",
        "sourceType": REVIEW_SOURCE_TYPE,
        "topK": limit,
        "returned": len(reviews),
    }
    if collection_name:
        step_detail["collection"] = collection_name

    return RetrievalTrace(
        query=query,
        selected_sources=[REVIEW_SOURCE_NAME],
        filters=normalized_filters,
        candidate_count=len(reviews),
        returned_count=len(reviews),
        duration_ms=duration_ms,
        citations=reviews_to_citations(reviews),
        steps=[
            RetrievalStep(
                name="vector_search",
                duration_ms=duration_ms,
                detail=step_detail,
            )
        ],
    )
