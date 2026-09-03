"""Recommendation ranking metrics for the FunRec milestone."""

from __future__ import annotations

import math
from typing import Iterable, Mapping


def _deduplicate_preserve_order(item_ids: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        result.append(item_id)
    return result


def precision_at_k(recommended_ids: Iterable[int], relevant_ids: Iterable[int], k: int) -> float:
    """Return the fraction of top-k recommendations that are relevant."""
    if k <= 0:
        raise ValueError("k must be positive")
    top_k = _deduplicate_preserve_order(recommended_ids)[:k]
    if not top_k:
        return 0.0
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    hits = sum(1 for item_id in top_k if item_id in relevant)
    return hits / k


def recall_at_k(recommended_ids: Iterable[int], relevant_ids: Iterable[int], k: int) -> float:
    """Return the fraction of relevant items recovered by top-k recommendations."""
    if k <= 0:
        raise ValueError("k must be positive")
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    top_k = _deduplicate_preserve_order(recommended_ids)[:k]
    hits = sum(1 for item_id in top_k if item_id in relevant)
    return hits / len(relevant)


def reciprocal_rank(recommended_ids: Iterable[int], relevant_ids: Iterable[int]) -> float:
    """Return 1/rank of the first relevant recommendation, or 0 if no hit exists."""
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    for rank, item_id in enumerate(_deduplicate_preserve_order(recommended_ids), start=1):
        if item_id in relevant:
            return 1 / rank
    return 0.0


def mrr(cases: Iterable[tuple[Iterable[int], Iterable[int]]]) -> float:
    """Return mean reciprocal rank over multiple recommendation cases."""
    values = [reciprocal_rank(recommended, relevant) for recommended, relevant in cases]
    return sum(values) / len(values) if values else 0.0


def ndcg_at_k(
    recommended_ids: Iterable[int],
    relevance_by_id: Mapping[int, float],
    k: int,
) -> float:
    """Return normalized discounted cumulative gain at k.

    ``relevance_by_id`` can represent binary relevance or graded relevance. In
    the MovieLens conversion, it is derived from target interaction scores.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    if not relevance_by_id:
        return 0.0

    top_k = _deduplicate_preserve_order(recommended_ids)[:k]
    dcg = 0.0
    for index, item_id in enumerate(top_k):
        relevance = relevance_by_id.get(item_id, 0.0)
        dcg += (2**relevance - 1) / math.log2(index + 2)

    ideal_relevances = sorted(relevance_by_id.values(), reverse=True)[:k]
    idcg = sum(
        (2**relevance - 1) / math.log2(index + 2)
        for index, relevance in enumerate(ideal_relevances)
    )
    if idcg == 0:
        return 0.0
    return dcg / idcg


def relevance_from_target_interactions(target_interactions: Iterable[dict]) -> dict[int, float]:
    """Build item relevance scores from target interactions.

    If the same item appears multiple times, keep the strongest observed score.
    """
    relevance: dict[int, float] = {}
    for interaction in target_interactions:
        item_id = int(interaction["itemId"])
        score = float(interaction.get("score", 1.0))
        relevance[item_id] = max(relevance.get(item_id, 0.0), score)
    return relevance