from __future__ import annotations

import pytest

from evaluation.shopping_memory_v14_public_dev_quality_v1 import (
    MEMORY_WEIGHT,
    cluster_bootstrap,
    rank_candidates,
    ranking_metrics,
)


def candidate(product_id: str, rank: int, base: float, memory: float, grade: int):
    return {
        "productId": product_id, "baseRank": rank, "baseScore": base,
        "memoryScore": memory, "relevanceGrade": grade,
    }


def test_frozen_weight_and_rerank_preserve_candidates():
    assert MEMORY_WEIGHT == 0.08
    rows = [candidate("a", 1, 1.0, 0.0, 0), candidate("b", 2, 0.95, 1.0, 3)]
    assert [row["productId"] for row in rank_candidates(rows, use_memory=False)] == ["a", "b"]
    reranked = rank_candidates(rows, use_memory=True)
    assert [row["productId"] for row in reranked] == ["b", "a"]
    assert {row["productId"] for row in reranked} == {"a", "b"}


def test_metrics_reward_graded_relevance_near_top():
    good = [candidate("a", 1, 1.0, 1.0, 3), candidate("b", 2, 0.9, 0.0, 0)]
    bad = list(reversed(good))
    assert ranking_metrics(good)["nDCGAt10"] > ranking_metrics(bad)["nDCGAt10"]
    assert ranking_metrics(good)["grade2HitAt10"] == 1.0


def test_cluster_bootstrap_is_deterministic():
    rows = [("a", 1.0), ("b", 0.0), ("c", 1.0)]
    first = cluster_bootstrap(rows, iterations=100, seed=7)
    second = cluster_bootstrap(rows, iterations=100, seed=7)
    assert first == second
    assert first["meanDelta"] == pytest.approx(2 / 3)
