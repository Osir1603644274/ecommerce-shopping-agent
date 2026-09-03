from __future__ import annotations

import argparse
import time

from app.knowledge import knowledge_chunk_to_point, load_merchant_doc_chunks
from app.rag import INGEST_BATCH_SIZE, get_embedding_model, get_qdrant_client
from app.settings import settings
from scripts.reindex_translated_yelp_reviews import (
    reindex_translated_yelp_reviews,
)


def reindex_merchant_docs(batch_size: int = INGEST_BATCH_SIZE) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    chunks = load_merchant_doc_chunks()
    client = get_qdrant_client()
    collection_name = settings.knowledge_collection_name
    if not client.collection_exists(collection_name):
        raise RuntimeError(f"Qdrant collection does not exist: {collection_name}")

    embedder = get_embedding_model()
    indexed = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = list(embedder.embed([chunk.content for chunk in batch]))
        if len(vectors) != len(batch):
            raise ValueError("embedder returned a different number of vectors")
        client.upsert(
            collection_name=collection_name,
            points=[
                knowledge_chunk_to_point(chunk, vector)
                for chunk, vector in zip(batch, vectors, strict=True)
            ],
            wait=True,
        )
        indexed += len(batch)
        print(f"merchant_docs_indexed={indexed}/{len(chunks)}", flush=True)
    return indexed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize Beijing-localized Yelp reviews and merchant docs to Qdrant."
        )
    )
    parser.add_argument("--batch-size", type=int, default=INGEST_BATCH_SIZE)
    parser.add_argument("--start-offset", type=int, default=0)
    args = parser.parse_args()

    started_at = time.perf_counter()
    reviews = reindex_translated_yelp_reviews(
        batch_size=args.batch_size,
        start_offset=args.start_offset,
    )
    merchant_docs = reindex_merchant_docs(args.batch_size)
    print(
        f"updated_reviews={reviews} updated_merchant_docs={merchant_docs} "
        f"elapsed={time.perf_counter() - started_at:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
