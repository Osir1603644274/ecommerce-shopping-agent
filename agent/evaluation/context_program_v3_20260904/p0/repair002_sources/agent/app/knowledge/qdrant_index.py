import math
import uuid
from collections.abc import Sequence
from typing import Any

from qdrant_client import QdrantClient, models

from ..rag import EMBEDDING_DIMENSION
from ..settings import settings
from .models import KnowledgeChunk


KNOWLEDGE_POINT_NAMESPACE = uuid.UUID("013f21c8-871f-4fe6-a654-5d19b8f4c1b7")
KNOWLEDGE_PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "sourceType": models.PayloadSchemaType.KEYWORD,
    "sourceId": models.PayloadSchemaType.KEYWORD,
    "visibility": models.PayloadSchemaType.KEYWORD,
    "ownerUserId": models.PayloadSchemaType.KEYWORD,
    "language": models.PayloadSchemaType.KEYWORD,
    "tags": models.PayloadSchemaType.KEYWORD,
    "metadata.shopId": models.PayloadSchemaType.INTEGER,
    "metadata.typeId": models.PayloadSchemaType.INTEGER,
}


def knowledge_point_id(chunk_id: str) -> str:
    """Map a stable external chunk ID to the UUID format accepted by Qdrant."""

    return str(uuid.uuid5(KNOWLEDGE_POINT_NAMESPACE, chunk_id))


def knowledge_chunk_payload(chunk: KnowledgeChunk) -> dict[str, Any]:
    """Serialize one chunk into the common Qdrant payload contract."""

    return chunk.model_dump(by_alias=True, mode="json")


def knowledge_chunk_to_point(
    chunk: KnowledgeChunk,
    vector: Sequence[float],
) -> models.PointStruct:
    normalized_vector = [float(value) for value in vector]
    if len(normalized_vector) != EMBEDDING_DIMENSION:
        raise ValueError(
            f"knowledge vector must have {EMBEDDING_DIMENSION} dimensions"
        )
    if not all(math.isfinite(value) for value in normalized_vector):
        raise ValueError("knowledge vector values must be finite")
    return models.PointStruct(
        id=knowledge_point_id(chunk.chunk_id),
        vector=normalized_vector,
        payload=knowledge_chunk_payload(chunk),
    )


def ensure_knowledge_collection(client: QdrantClient) -> None:
    """Create the unified collection and its filter indexes when absent."""

    collection_name = settings.knowledge_collection_name
    if not client.collection_exists(collection_name):
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=EMBEDDING_DIMENSION,
                distance=models.Distance.COSINE,
            ),
        )

    existing_indexes = set(client.get_collection(collection_name).payload_schema)
    for field_name, field_schema in KNOWLEDGE_PAYLOAD_INDEXES.items():
        if field_name in existing_indexes:
            continue
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
            wait=True,
        )
