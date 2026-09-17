from functools import lru_cache
from threading import RLock
from typing import Any

from .bm25 import BM25Document, BM25Index
from .rag import load_reviews, search_reviews
from .settings import settings


YELP_SOURCE = "yelp"
_INDEX_LOCK = RLock()


def _review_text_for_bm25(review: dict[str, Any]) -> str:
    return str(
        review.get("text")
        or review.get("contentZh")
        or review.get("content_zh")
        or review.get("originalText")
        or review.get("content")
        or ""
    )


def _review_to_bm25_document(review: dict[str, Any]) -> BM25Document:
    return BM25Document(
        doc_id=str(review["reviewId"]),
        text=_review_text_for_bm25(review),
        payload={
            "reviewId": review["reviewId"],
            "shopId": review["shopId"],
            "shopName": review["shopName"],
            "text": _review_text_for_bm25(review),
            "originalText": review.get("originalText"),
            "contentZh": review.get("contentZh"),
            "source": review.get("source"),
            "language": review.get("language"),
            "translationStatus": review.get("translationStatus"),
        },
    )


@lru_cache(maxsize=1)
def _load_review_snapshot() -> list[dict[str, Any]]:
    return load_reviews()


@lru_cache(maxsize=1)
def _build_review_bm25_index() -> BM25Index:
    return BM25Index(
        [_review_to_bm25_document(review) for review in _load_review_snapshot()]
    )


@lru_cache(maxsize=1)
def _build_production_review_bm25_index() -> BM25Index:
    excluded_sources = set(settings.knowledge_review_excluded_sources)
    return BM25Index(
        [
            _review_to_bm25_document(review)
            for review in _load_review_snapshot()
            if review.get("source") not in excluded_sources
        ]
    )


@lru_cache(maxsize=1)
def _build_yelp_review_bm25_index() -> BM25Index:
    return BM25Index(
        [
            _review_to_bm25_document(review)
            for review in _load_review_snapshot()
            if review.get("source") == YELP_SOURCE
        ]
    )


def get_review_bm25_index() -> BM25Index:
    """Return the complete mixed corpus for explicit offline evaluations."""
    with _INDEX_LOCK:
        return _build_review_bm25_index()


def get_production_review_bm25_index() -> BM25Index:
    with _INDEX_LOCK:
        return _build_production_review_bm25_index()


def get_yelp_review_bm25_index() -> BM25Index:
    """Retain the frozen Yelp-only index for historical offline evaluations."""
    with _INDEX_LOCK:
        return _build_yelp_review_bm25_index()


def upsert_review_bm25(review: dict[str, Any]) -> None:
    with _INDEX_LOCK:
        _build_review_bm25_index().upsert(_review_to_bm25_document(review))
        if review.get("source") in settings.knowledge_review_excluded_sources:
            _build_production_review_bm25_index().remove(str(review["reviewId"]))
        else:
            _build_production_review_bm25_index().upsert(
                _review_to_bm25_document(review)
            )
        if _build_yelp_review_bm25_index.cache_info().currsize:
            if review.get("source") == YELP_SOURCE:
                _build_yelp_review_bm25_index().upsert(
                    _review_to_bm25_document(review)
                )
            else:
                _build_yelp_review_bm25_index().remove(str(review["reviewId"]))


def delete_review_bm25(review_id: str) -> None:
    with _INDEX_LOCK:
        _build_review_bm25_index().remove(review_id)
        _build_production_review_bm25_index().remove(review_id)
        if _build_yelp_review_bm25_index.cache_info().currsize:
            _build_yelp_review_bm25_index().remove(review_id)


def clear_review_bm25_indexes() -> None:
    with _INDEX_LOCK:
        _load_review_snapshot.cache_clear()
        _build_review_bm25_index.cache_clear()
        _build_production_review_bm25_index.cache_clear()
        _build_yelp_review_bm25_index.cache_clear()


def prepare_review_bm25_indexes() -> tuple[BM25Index, BM25Index]:
    with _INDEX_LOCK:
        _build_review_bm25_index()
        return (
            _build_production_review_bm25_index(),
            _build_yelp_review_bm25_index(),
        )


def rebuild_review_bm25_indexes() -> tuple[BM25Index, BM25Index]:
    with _INDEX_LOCK:
        _load_review_snapshot.cache_clear()
        _build_review_bm25_index.cache_clear()
        _build_production_review_bm25_index.cache_clear()
        _build_yelp_review_bm25_index.cache_clear()
        _build_review_bm25_index()
        return (
            _build_production_review_bm25_index(),
            _build_yelp_review_bm25_index(),
        )


def _search_review_index(
    index: BM25Index,
    question: str,
    limit: int,
    shop_ids: list[int] | None,
) -> list[dict[str, Any]]:
    normalized_shop_ids = None
    if shop_ids is not None:
        normalized_shop_ids = list(dict.fromkeys(shop_ids))
        if any(shop_id <= 0 for shop_id in normalized_shop_ids):
            raise ValueError("shop_ids must contain only positive integers")
        if not normalized_shop_ids:
            return []

    allowed_doc_ids = None
    if normalized_shop_ids is not None:
        allowed_shop_ids = set(normalized_shop_ids)
        allowed_doc_ids = index.matching_doc_ids(
            lambda document: int(document.payload["shopId"])
            in allowed_shop_ids
        )
    return index.search(question, limit=limit, doc_ids=allowed_doc_ids)


def search_reviews_bm25(
    question: str,
    limit: int = 3,
    *,
    shop_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Search production reviews while excluding synthetic evaluation seeds."""
    return _search_review_index(
        get_production_review_bm25_index(),
        question,
        limit,
        shop_ids,
    )


def search_all_reviews_bm25(
    question: str,
    limit: int = 3,
    *,
    shop_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Explicit all-source retriever for mixed-corpus evaluations."""
    return _search_review_index(
        get_review_bm25_index(),
        question,
        limit,
        shop_ids,
    )


def search_yelp_reviews_bm25(
    question: str,
    limit: int = 3,
    *,
    shop_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    return _search_review_index(
        get_yelp_review_bm25_index(),
        question,
        limit,
        shop_ids,
    )


def search_yelp_reviews_vector_then_bm25_rerank(
    question: str,
    limit: int = 3,
    *,
    candidate_limit: int = 10,
    bm25_weight: float = 0.1,
) -> list[dict[str, Any]]:
    candidates = search_reviews(question, candidate_limit, source=YELP_SOURCE)
    if not candidates:
        return []

    index = get_yelp_review_bm25_index()
    candidate_ids = {review["reviewId"] for review in candidates}
    bm25_by_review_id = index.score_by_doc_id(question, candidate_ids)
    reranked = []
    for original_rank, review in enumerate(candidates, start=1):
        vector_score = float(review.get("score") or 0.0)
        bm25_score = float(bm25_by_review_id.get(review["reviewId"], 0.0))
        reranked.append(
            {
                **review,
                "originalRank": original_rank,
                "vectorScore": vector_score,
                "bm25Score": bm25_score,
                "rerankScore": vector_score + bm25_weight * bm25_score,
            }
        )
    reranked.sort(
        key=lambda review: (
            -float(review["rerankScore"]),
            int(review["originalRank"]),
        )
    )
    return reranked[:limit]
