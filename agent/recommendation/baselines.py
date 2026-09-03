"""Simple recommendation baselines for the FunRec milestone."""

from __future__ import annotations

from collections import Counter
from typing import Iterable


def popular_item_scores(
    interactions: Iterable[dict],
    *,
    positive_score_threshold: float = 4.0,
) -> dict[int, float]:
    """Return global positive-interaction counts by item id."""
    counter: Counter[int] = Counter()
    for interaction in interactions:
        score = float(interaction.get("rating", interaction.get("score", 0.0)))
        if score < positive_score_threshold:
            continue
        counter[int(interaction["itemId"])] += 1
    return {item_id: float(count) for item_id, count in counter.items()}


def popular_recommendations(
    interactions: Iterable[dict],
    *,
    positive_score_threshold: float = 4.0,
    limit: int = 10,
) -> list[int]:
    """Recommend globally popular items from positive historical interactions.

    This baseline is intentionally non-personalized: every user receives the
    same ranking. It is useful as a floor that later personalized methods should
    beat.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    scores = popular_item_scores(
        interactions,
        positive_score_threshold=positive_score_threshold,
    )
    return [
        item_id
        for item_id, _ in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    ]