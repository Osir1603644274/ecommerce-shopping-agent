from typing import Any

from .knowledge.review_sync import (
    delete_review_knowledge_chunk,
    upsert_review_knowledge_chunk,
)
from .knowledge.reviews import review_to_chunk
from .rag import delete_review_vector, get_embedding_model, upsert_review_vector
from .review_index_events import publish_review_index_event


def upsert_review_search_indexes(review: dict[str, Any]) -> None:
    chunk = review_to_chunk(review)
    vector = next(get_embedding_model().embed([chunk.content])).tolist()
    upsert_review_vector(review, vector=vector)
    upsert_review_knowledge_chunk(review, vector=vector)
    publish_review_index_event(
        {
            "operation": "upsert",
            "review": review,
        }
    )


def delete_review_search_indexes(review_id: str) -> None:
    delete_review_vector(review_id)
    delete_review_knowledge_chunk(review_id)
    publish_review_index_event(
        {
            "operation": "delete",
            "reviewId": review_id,
        }
    )
