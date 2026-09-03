from __future__ import annotations

import argparse
import time
from collections.abc import Sequence

from qdrant_client import models

from app.knowledge import knowledge_chunk_to_point, review_to_chunk
from app.rag import (
    INGEST_BATCH_SIZE,
    get_embedding_model,
    get_qdrant_client,
    load_reviews,
    review_point_id,
)
from app.settings import settings


def merchant_review_point(
    review: dict,
    vector: Sequence[float],
) -> models.PointStruct:
    return models.PointStruct(
        id=review_point_id(str(review["reviewId"])),
        vector=[float(value) for value in vector],
        payload={
            "reviewId": review["reviewId"],
            "shopId": review["shopId"],
            "shopName": review["shopName"],
            "text": review["text"],
            "originalText": review["originalText"],
            "contentZh": review["contentZh"],
            "source": review["source"],
            "language": review["language"],
            "sourceLanguage": review["sourceLanguage"],
            "translationStatus": review["translationStatus"],
            "sourceReviewId": review.get("sourceReviewId"),
            "sourceUserId": review.get("sourceUserId"),
            "stars": review.get("stars"),
            "sourceShopName": review.get("sourceShopName"),
            "evidenceScope": review.get("evidenceScope"),
            "tags": review["tags"],
        },
    )


def reindex_translated_yelp_reviews(
    *,
    batch_size: int = INGEST_BATCH_SIZE,
    start_offset: int = 0,
) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if start_offset < 0:
        raise ValueError("start_offset must not be negative")

    reviews = [review for review in load_reviews() if review["source"] == "yelp"]
    invalid = [
        review["reviewId"]
        for review in reviews
        if review["translationStatus"] != "translated"
        or not review["contentZh"]
        or review["language"] != "zh"
    ]
    if invalid:
        raise ValueError(
            f"Yelp reviews are not ready for Chinese reindexing: {invalid[:5]}"
        )
    if start_offset > len(reviews):
        raise ValueError("start_offset exceeds Yelp review count")

    client = get_qdrant_client()
    for collection_name in (
        settings.rag_collection_name,
        settings.knowledge_collection_name,
    ):
        if not client.collection_exists(collection_name):
            raise RuntimeError(f"Qdrant collection does not exist: {collection_name}")

    embedder = get_embedding_model()
    indexed = start_offset
    started_at = time.perf_counter()
    for start in range(start_offset, len(reviews), batch_size):
        batch = reviews[start : start + batch_size]
        vectors = list(embedder.embed([review["text"] for review in batch]))
        if len(vectors) != len(batch):
            raise ValueError("embedder returned a different number of vectors")

        client.upsert(
            collection_name=settings.rag_collection_name,
            points=[
                merchant_review_point(review, vector)
                for review, vector in zip(batch, vectors, strict=True)
            ],
            wait=True,
        )
        client.upsert(
            collection_name=settings.knowledge_collection_name,
            points=[
                knowledge_chunk_to_point(review_to_chunk(review), vector)
                for review, vector in zip(batch, vectors, strict=True)
            ],
            wait=True,
        )
        indexed = start + len(batch)
        elapsed = time.perf_counter() - started_at
        print(
            f"indexed={indexed}/{len(reviews)} "
            f"next_offset={indexed} elapsed={elapsed:.1f}s",
            flush=True,
        )
    return indexed - start_offset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Embed translated Yelp reviews once and update both Qdrant collections."
        )
    )
    parser.add_argument("--batch-size", type=int, default=INGEST_BATCH_SIZE)
    parser.add_argument("--start-offset", type=int, default=0)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    count = reindex_translated_yelp_reviews(
        batch_size=args.batch_size,
        start_offset=args.start_offset,
    )
    print(f"updated={count}", flush=True)
