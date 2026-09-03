"""Hybrid recommendation utilities for the FunRec milestone."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from recommendation.itemcf import itemcf_candidate_scores


def _normalize_scores(scores: Mapping[int, float]) -> dict[int, float]:
    """Scale positive scores into [0, 1] by dividing by the max score."""
    positive_scores = {int(item_id): float(score) for item_id, score in scores.items() if score > 0}
    if not positive_scores:
        return {}
    max_score = max(positive_scores.values())
    if max_score <= 0:
        return {}
    return {item_id: score / max_score for item_id, score in positive_scores.items()}


def build_item_type_lookup(items: Iterable[dict[str, Any]]) -> dict[int, int]:
    """Build ``itemId -> typeId`` lookup from recommendation item metadata."""
    lookup: dict[int, int] = {}
    for item in items:
        if item.get("itemId") is None or item.get("typeId") is None:
            continue
        lookup[int(item["itemId"])] = int(item["typeId"])
    return lookup


def type_preference_candidate_scores(
    history: Iterable[dict[str, Any]],
    item_type_lookup: Mapping[int, int],
    *,
    positive_score_threshold: float = 4.0,
) -> dict[int, float]:
    """Score unseen candidate items by the user's positive type preferences.

    Yelp has no order / favorite / click behaviors, so the rating score itself
    is used as preference strength. A 5-star interaction contributes more than
    a 4-star interaction to that item's type.
    """
    history_list = list(history)
    seen_ids = {int(interaction["itemId"]) for interaction in history_list}
    raw_type_scores: defaultdict[int, float] = defaultdict(float)

    for interaction in history_list:
        score = float(interaction.get("rating", interaction.get("score", 0.0)))
        if score < positive_score_threshold:
            continue

        item_id = int(interaction["itemId"])
        type_id = interaction.get("typeId", item_type_lookup.get(item_id))
        if type_id is None:
            continue
        raw_type_scores[int(type_id)] += score

    normalized_type_scores = _normalize_scores(raw_type_scores)
    if not normalized_type_scores:
        return {}

    return {
        int(item_id): normalized_type_scores[type_id]
        for item_id, type_id in item_type_lookup.items()
        if int(item_id) not in seen_ids and type_id in normalized_type_scores
    }


def hybrid_recommendations(
    history: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    popular_scores: Mapping[int, float],
    *,
    popular_weight: float = 1.0,
    itemcf_weight: float = 1.0,
    limit: int = 10,
) -> list[int]:
    """Blend global popularity and ItemCF personalized scores.

    Both score sources are normalized independently before blending:

        final_score = popular_weight * normalized_popular_score
                    + itemcf_weight * normalized_itemcf_score
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if popular_weight < 0 or itemcf_weight < 0:
        raise ValueError("weights must be non-negative")
    if popular_weight == 0 and itemcf_weight == 0:
        raise ValueError("at least one weight must be positive")

    history_list = list(history)
    seen_ids = {int(interaction["itemId"]) for interaction in history_list}
    normalized_popular = _normalize_scores(popular_scores)
    normalized_itemcf = _normalize_scores(
        itemcf_candidate_scores(history_list, similarity_table)
    )

    candidate_ids: set[int] = set()
    if popular_weight > 0:
        candidate_ids.update(normalized_popular)
    if itemcf_weight > 0:
        candidate_ids.update(normalized_itemcf)
    candidate_ids -= seen_ids

    ranked_items = sorted(
        candidate_ids,
        key=lambda item_id: (
            -(
                popular_weight * normalized_popular.get(item_id, 0.0)
                + itemcf_weight * normalized_itemcf.get(item_id, 0.0)
            ),
            -normalized_itemcf.get(item_id, 0.0),
            -normalized_popular.get(item_id, 0.0),
            item_id,
        ),
    )
    return ranked_items[:limit]


def three_way_hybrid_recommendations(
    history: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    popular_scores: Mapping[int, float],
    item_type_lookup: Mapping[int, int],
    *,
    popular_weight: float = 1.0,
    itemcf_weight: float = 1.0,
    type_weight: float = 1.0,
    limit: int = 10,
) -> list[int]:
    """Blend Popular, ItemCF, and TypeId preference scores."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if popular_weight < 0 or itemcf_weight < 0 or type_weight < 0:
        raise ValueError("weights must be non-negative")
    if popular_weight == 0 and itemcf_weight == 0 and type_weight == 0:
        raise ValueError("at least one weight must be positive")

    history_list = list(history)
    seen_ids = {int(interaction["itemId"]) for interaction in history_list}
    normalized_popular = _normalize_scores(popular_scores)
    normalized_itemcf = _normalize_scores(
        itemcf_candidate_scores(history_list, similarity_table)
    )
    normalized_type = _normalize_scores(
        type_preference_candidate_scores(history_list, item_type_lookup)
    )

    candidate_ids: set[int] = set()
    if popular_weight > 0:
        candidate_ids.update(normalized_popular)
    if itemcf_weight > 0:
        candidate_ids.update(normalized_itemcf)
    if type_weight > 0:
        candidate_ids.update(normalized_type)
    candidate_ids -= seen_ids

    ranked_items = sorted(
        candidate_ids,
        key=lambda item_id: (
            -(
                popular_weight * normalized_popular.get(item_id, 0.0)
                + itemcf_weight * normalized_itemcf.get(item_id, 0.0)
                + type_weight * normalized_type.get(item_id, 0.0)
            ),
            -normalized_itemcf.get(item_id, 0.0),
            -normalized_type.get(item_id, 0.0),
            -normalized_popular.get(item_id, 0.0),
            item_id,
        ),
    )
    return ranked_items[:limit]
