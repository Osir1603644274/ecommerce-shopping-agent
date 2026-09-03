from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from ..rag import (
    INGEST_BATCH_SIZE,
    get_embedding_model,
    get_qdrant_client,
    load_reviews,
)
from ..settings import settings
from .merchant_docs import load_merchant_doc_chunks
from .models import KnowledgeChunk
from .policy_docs import load_policy_markdown_chunks
from .qdrant_index import ensure_knowledge_collection, knowledge_chunk_to_point
from .reviews import review_to_chunk


DEFAULT_POLICY_DOCS_DIR = (
    Path(__file__).resolve().parents[2] / "knowledge_data" / "raw" / "policy_docs"
)
Embedder = Callable[[list[str]], Iterable[Sequence[float]]]


def load_all_knowledge_chunks() -> list[KnowledgeChunk]:
    chunks = [review_to_chunk(review) for review in load_reviews()]
    chunks.extend(load_merchant_doc_chunks())
    chunks.extend(load_policy_markdown_chunks(DEFAULT_POLICY_DOCS_DIR))
    validate_unique_chunk_ids(chunks)
    return chunks


def validate_unique_chunk_ids(chunks: list[KnowledgeChunk]) -> None:
    counts = Counter(chunk.chunk_id for chunk in chunks)
    duplicates = sorted(chunk_id for chunk_id, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError("duplicate knowledge chunk IDs: " + ", ".join(duplicates))


def rebuild_knowledge_index(
    chunks: list[KnowledgeChunk] | None = None,
    *,
    client: QdrantClient | None = None,
    embedder: Embedder | None = None,
    batch_size: int = INGEST_BATCH_SIZE,
) -> dict[str, Any]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    selected_chunks = chunks if chunks is not None else load_all_knowledge_chunks()
    validate_unique_chunk_ids(selected_chunks)
    selected_client = client or get_qdrant_client()
    selected_embedder = embedder or get_embedding_model().embed

    collection_name = settings.knowledge_collection_name
    if selected_client.collection_exists(collection_name):
        selected_client.delete_collection(collection_name=collection_name)
    ensure_knowledge_collection(selected_client)

    source_counts: Counter[str] = Counter()
    indexed_count = 0
    for start in range(0, len(selected_chunks), batch_size):
        batch = selected_chunks[start : start + batch_size]
        vectors = list(selected_embedder([chunk.content for chunk in batch]))
        if len(vectors) != len(batch):
            raise ValueError("embedder returned a different number of vectors")
        points = [
            knowledge_chunk_to_point(chunk, vector)
            for chunk, vector in zip(batch, vectors, strict=True)
        ]
        selected_client.upsert(
            collection_name=collection_name,
            points=points,
            wait=True,
        )
        indexed_count += len(points)
        source_counts.update(chunk.source_type for chunk in batch)

    return {
        "collection": collection_name,
        "indexedCount": indexed_count,
        "sourceCounts": dict(sorted(source_counts.items())),
    }
