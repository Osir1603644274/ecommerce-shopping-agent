"""Injectable, bounded semantic ranking over already resolved commerce facts."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ...settings import settings


@dataclass(frozen=True)
class CrossEncoderRequest:
    query: str
    pairs: tuple[tuple[int, str], ...]
    model_sha256: str
    input_template: str = "stage1-title-brand-categories-v1"


CrossEncoderProvider = Callable[[CrossEncoderRequest], Awaitable[dict[int, float]]]
_provider: ContextVar[tuple[str, CrossEncoderProvider] | None] = ContextVar(
    "commerce_cross_encoder_provider", default=None
)


@contextmanager
def use_commerce_cross_encoder(model_sha256: str, provider: CrossEncoderProvider) -> Iterator[None]:
    token = _provider.set((model_sha256, provider))
    try:
        yield
    finally:
        _provider.reset(token)


def commerce_search_text(product: dict[str, Any]) -> str:
    """Same visible-field template as stage1 catalog.text; never add commerce facts."""
    def clean(value: Any) -> str:
        value = " ".join(str(value or "").split())
        return "" if value.upper() in {"UNKNOWN", "NULL", "NONE", "N/A"} else value
    values = [clean(product.get(key)) for key in (
        "title", "brand", "categoryL1", "categoryL2", "categoryL3"
    )]
    return " ".join(dict.fromkeys(value for value in values if value))


def logits_to_rank_scores(logits: dict[int, float], candidate_ids: list[int]) -> tuple[dict[int, float], list[int]]:
    if (not candidate_ids or len(candidate_ids) != len(set(candidate_ids))
            or any(type(i) is not int or i <= 0 for i in candidate_ids)
            or any(type(i) is not int for i in logits) or set(logits) != set(candidate_ids)):
        raise ValueError("cross-encoder candidate IDs differ from authoritative pool")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in logits.values()):
        raise ValueError("cross-encoder non-finite score")
    ordered = sorted(candidate_ids, key=lambda item: (-float(logits[item]), item))
    count = len(ordered)
    # Strictly positive and monotonic even when every raw logit is negative.
    return {item: (count-rank)/count for rank, item in enumerate(ordered)}, ordered


async def cross_encoder_rank(query: str, products: list[dict[str, Any]]) -> tuple[dict[int, float], dict]:
    registered = _provider.get()
    expected = settings.product_cross_encoder_model_sha256
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("cross-encoder model binding missing")
    if registered is None or registered[0] != expected:
        raise ValueError("cross-encoder provider missing or model binding mismatch")
    if not 1 <= len(products) <= 50:
        raise ValueError("cross-encoder requires one authoritative Top50 pool")
    pairs = tuple((product["id"], commerce_search_text(product)) for product in products)
    if any(not text for _, text in pairs):
        raise ValueError("cross-encoder empty product text")
    request = CrossEncoderRequest(query=query, pairs=pairs, model_sha256=expected)
    logits = await asyncio.wait_for(registered[1](request), timeout=settings.product_cross_encoder_timeout_seconds)
    score_map, ordered = logits_to_rank_scores(logits, [item for item, _ in pairs])
    return score_map, {
        "status": "active", "modelCalled": True, "modelSha256": expected,
        "candidateCount": len(pairs), "orderedProductIds": ordered,
        "inputTemplate": request.input_template,
        "scoreTransform": "stable_raw_logit_desc_product_id_asc_then_(N-rank+1)/N",
        "scores": [{"productId": item, "rawLogit": float(logits[item]), "rankScore": score_map[item]} for item in ordered],
        "inputTextSha256": {str(item): hashlib.sha256(text.encode()).hexdigest() for item, text in pairs},
    }
