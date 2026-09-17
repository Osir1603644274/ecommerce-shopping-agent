import asyncio
import time
import uuid
from contextlib import suppress
from typing import Any

import httpx

from .knowledge.qdrant_index import knowledge_point_id
from .rag import get_qdrant_client, review_point_id
from .rag_bm25 import prepare_review_bm25_indexes, search_reviews_bm25
from .review_index_events import listen_for_review_index_events
from .settings import settings


async def _wait_for_bm25(
    term: str,
    review_id: str,
    *,
    present: bool,
    timeout_seconds: float = 10.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        results = await asyncio.to_thread(
            search_reviews_bm25,
            term,
            10,
            shop_ids=[3],
        )
        found = any(item.get("reviewId") == review_id for item in results)
        if found is present:
            return True
        await asyncio.sleep(0.1)
    return False


def _point_exists(collection_name: str, point_id: int | str) -> bool:
    points = get_qdrant_client().retrieve(
        collection_name=collection_name,
        ids=[point_id],
        with_payload=True,
        with_vectors=False,
    )
    return bool(points)


async def run_review_index_freshness_smoke(
    *,
    base_url: str = "http://localhost:8000",
) -> dict[str, Any]:
    review_id = f"review-freshness-{uuid.uuid4().hex[:12]}"
    unique_term = f"freshnessprobe{uuid.uuid4().hex[:10]}"
    review = {
        "shopId": 3,
        "shopName": "清晨手冲咖啡",
        "content": f"倒排索引同步验证 {unique_term}",
        "source": "user",
        "language": "zh",
        "tags": ["freshness-smoke"],
    }
    ready = asyncio.Event()
    stop = asyncio.Event()
    listener = asyncio.create_task(listen_for_review_index_events(ready, stop))
    created = False
    checks: dict[str, bool] = {}
    try:
        await asyncio.wait_for(ready.wait(), timeout=10.0)
        await asyncio.to_thread(prepare_review_bm25_indexes)
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            response = await client.put(
                f"/internal/review-vectors/{review_id}",
                json=review,
            )
            response.raise_for_status()
            created = True
            checks["syncEndpointUpsert"] = response.json().get("synced") is True
            checks["bm25UpsertVisible"] = await _wait_for_bm25(
                unique_term,
                review_id,
                present=True,
            )
            checks["legacyVectorUpsertVisible"] = await asyncio.to_thread(
                _point_exists,
                settings.rag_collection_name,
                review_point_id(review_id),
            )
            checks["unifiedChunkUpsertVisible"] = await asyncio.to_thread(
                _point_exists,
                settings.knowledge_collection_name,
                knowledge_point_id(f"review:{review_id}"),
            )

            response = await client.delete(f"/internal/review-vectors/{review_id}")
            response.raise_for_status()
            created = False
            checks["syncEndpointDelete"] = response.json().get("synced") is True
            checks["bm25DeleteVisible"] = await _wait_for_bm25(
                unique_term,
                review_id,
                present=False,
            )
            checks["legacyVectorDeleteVisible"] = not await asyncio.to_thread(
                _point_exists,
                settings.rag_collection_name,
                review_point_id(review_id),
            )
            checks["unifiedChunkDeleteVisible"] = not await asyncio.to_thread(
                _point_exists,
                settings.knowledge_collection_name,
                knowledge_point_id(f"review:{review_id}"),
            )
    finally:
        if created:
            with suppress(Exception):
                async with httpx.AsyncClient(
                    base_url=base_url,
                    timeout=30.0,
                ) as client:
                    await client.delete(f"/internal/review-vectors/{review_id}")
        stop.set()
        listener.cancel()
        with suppress(asyncio.CancelledError):
            await listener

    return {
        "experiment": "review index freshness multi-instance smoke",
        "sealedDataRead": False,
        "temporaryReviewId": review_id,
        "checks": checks,
        "passed": bool(checks) and all(checks.values()),
    }
