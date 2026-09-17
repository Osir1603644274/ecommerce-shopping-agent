import asyncio
import json
import logging
from functools import lru_cache
from typing import Any

import redis
import redis.asyncio as async_redis

from .rag_bm25 import (
    delete_review_bm25,
    rebuild_review_bm25_indexes,
    upsert_review_bm25,
)
from .settings import settings


REVIEW_INDEX_EVENT_CHANNEL = "agent:review-index-events:v1"
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _publisher() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def publish_review_index_event(event: dict[str, Any]) -> None:
    payload = {"version": 1, **event}
    _publisher().publish(
        REVIEW_INDEX_EVENT_CHANNEL,
        json.dumps(payload, ensure_ascii=False),
    )


def apply_review_index_event(event: dict[str, Any]) -> None:
    if event.get("version") != 1:
        raise ValueError("unsupported review index event version")
    operation = event.get("operation")
    if operation == "upsert":
        review = event.get("review")
        if not isinstance(review, dict):
            raise ValueError("upsert review index event requires review")
        upsert_review_bm25(review)
        return
    if operation == "delete":
        review_id = str(event.get("reviewId") or "").strip()
        if not review_id:
            raise ValueError("delete review index event requires reviewId")
        delete_review_bm25(review_id)
        return
    raise ValueError("unsupported review index event operation")


async def listen_for_review_index_events(
    ready: asyncio.Event,
    stop: asyncio.Event,
) -> None:
    first_connection = True
    while not stop.is_set():
        client = async_redis.from_url(settings.redis_url, decode_responses=True)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(REVIEW_INDEX_EVENT_CHANNEL)
            ready.set()
            if not first_connection:
                await asyncio.to_thread(rebuild_review_bm25_indexes)
            first_connection = False
            while not stop.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if not message:
                    continue
                try:
                    event = json.loads(message["data"])
                    if not isinstance(event, dict):
                        raise ValueError("review index event must be an object")
                    await asyncio.to_thread(apply_review_index_event, event)
                except Exception:
                    logger.exception("Failed to apply review index event")
                    await asyncio.to_thread(rebuild_review_bm25_indexes)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Review index event listener disconnected; retrying")
            await asyncio.sleep(1.0)
        finally:
            await pubsub.aclose()
            await client.aclose()
