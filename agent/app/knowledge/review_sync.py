from collections.abc import Sequence
from typing import Any

from qdrant_client import models

from ..rag import get_embedding_model, get_qdrant_client
from ..settings import settings
from .qdrant_index import (
    ensure_knowledge_collection,
    knowledge_chunk_to_point,
    knowledge_point_id,
)
from .reviews import review_to_chunk


def upsert_review_knowledge_chunk(
    review: dict[str, Any],
    *,
    vector: Sequence[float] | None = None,
) -> None:
    chunk = review_to_chunk(review)
    selected_vector = vector
    if selected_vector is None:
        selected_vector = next(
            get_embedding_model().embed([chunk.content])
        ).tolist()
    client = get_qdrant_client()
    ensure_knowledge_collection(client)
    client.upsert(
        collection_name=settings.knowledge_collection_name,
        points=[knowledge_chunk_to_point(chunk, selected_vector)],
        wait=True,
    )


def delete_review_knowledge_chunk(review_id: str) -> None:
    client = get_qdrant_client()
    ensure_knowledge_collection(client)
    client.delete(
        collection_name=settings.knowledge_collection_name,
        points_selector=models.PointIdsList(
            points=[knowledge_point_id(f"review:{review_id}")]
        ),
        wait=True,
    )
