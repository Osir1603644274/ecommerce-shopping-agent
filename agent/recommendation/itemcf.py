"""ItemCF similarity-table utilities for the FunRec milestone."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SIMILARITY_PATH = Path(__file__).parent / "data" / "processed" / "item_similarity.json"


def _positive_unique_item_ids(
    interactions: Iterable[dict[str, Any]],
    *,
    positive_score_threshold: float = 4.0,
) -> list[int]:
    """Return unique positive item ids from one user's visible interactions."""
    item_ids: set[int] = set()
    for interaction in interactions:
        score = float(interaction.get("rating", interaction.get("score", 0.0)))
        if score < positive_score_threshold:
            continue
        item_ids.add(int(interaction["itemId"]))
    return sorted(item_ids)


def build_item_similarity_table(
    cases: Iterable[dict[str, Any]],
    *,
    positive_score_threshold: float = 4.0,
    top_n: int = 50,
) -> dict[str, Any]:
    """Build an item-item cosine cooccurrence similarity table.

    Each case contributes unique positive history items. If item A and item B
    appear in the same user's positive history, their cooccurrence count grows.
    Similarity is normalized by item popularity:

        sim(A, B) = cooccur(A, B) / sqrt(pop(A) * pop(B))
    """
    if top_n <= 0:
        raise ValueError("top_n must be positive")

    case_list = list(cases)
    item_popularity: Counter[int] = Counter()
    cooccurrence: dict[int, Counter[int]] = defaultdict(Counter)
    contributing_case_count = 0

    for case in case_list:
        item_ids = _positive_unique_item_ids(
            case.get("history", []),
            positive_score_threshold=positive_score_threshold,
        )
        if not item_ids:
            continue
        contributing_case_count += 1

        for item_id in item_ids:
            item_popularity[item_id] += 1

        for left_index, left_item_id in enumerate(item_ids):
            for right_item_id in item_ids[left_index + 1 :]:
                cooccurrence[left_item_id][right_item_id] += 1
                cooccurrence[right_item_id][left_item_id] += 1

    similarity_table: dict[str, list[dict[str, Any]]] = {}
    for item_id in sorted(cooccurrence):
        neighbors: list[dict[str, Any]] = []
        for neighbor_id, together_count in cooccurrence[item_id].items():
            denominator = math.sqrt(item_popularity[item_id] * item_popularity[neighbor_id])
            similarity = together_count / denominator if denominator else 0.0
            neighbors.append(
                {
                    "itemId": neighbor_id,
                    "similarity": similarity,
                    "cooccurrence": together_count,
                    "itemPopularity": item_popularity[item_id],
                    "neighborPopularity": item_popularity[neighbor_id],
                }
            )
        similarity_table[str(item_id)] = sorted(
            neighbors,
            key=lambda item: (-item["similarity"], -item["cooccurrence"], item["itemId"]),
        )[:top_n]

    return {
        "algorithm": "itemcf_cosine_cooccurrence",
        "positiveScoreThreshold": positive_score_threshold,
        "topN": top_n,
        "caseCount": len(case_list),
        "contributingCaseCount": contributing_case_count,
        "itemCount": len(similarity_table),
        "similarityTable": similarity_table,
    }


def load_similarity_table(similarity_path: Path = DEFAULT_SIMILARITY_PATH) -> dict[str, Any]:
    """Load a previously generated ItemCF similarity table."""
    if not similarity_path.exists():
        raise FileNotFoundError(
            f"ItemCF similarity table not found: {similarity_path}. "
            "Run `python -m scripts.prepare_item_similarity` first."
        )
    return json.loads(similarity_path.read_text(encoding="utf-8"))


def itemcf_candidate_scores(
    history: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    *,
    positive_score_threshold: float = 4.0,
) -> dict[int, float]:
    """Return unseen ItemCF candidate scores for one user's history."""
    history_list = list(history)
    seen_ids = {int(interaction["itemId"]) for interaction in history_list}
    source_item_ids = _positive_unique_item_ids(
        history_list,
        positive_score_threshold=positive_score_threshold,
    )
    raw_table = similarity_table.get("similarityTable", similarity_table)
    candidate_scores: defaultdict[int, float] = defaultdict(float)

    for source_item_id in source_item_ids:
        for neighbor in raw_table.get(str(source_item_id), []):
            neighbor_id = int(neighbor["itemId"])
            if neighbor_id in seen_ids:
                continue
            candidate_scores[neighbor_id] += float(neighbor.get("similarity", 0.0))

    return dict(candidate_scores)


def itemcf_recommendations(
    history: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    *,
    positive_score_threshold: float = 4.0,
    limit: int = 10,
) -> list[int]:
    """Recommend unseen items by summing similarities from positive history items."""
    if limit <= 0:
        raise ValueError("limit must be positive")

    candidate_scores = itemcf_candidate_scores(
        history,
        similarity_table,
        positive_score_threshold=positive_score_threshold,
    )
    return [
        item_id
        for item_id, _ in sorted(
            candidate_scores.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )[:limit]
    ]


def save_similarity_table(
    table: dict[str, Any],
    output_path: Path = DEFAULT_SIMILARITY_PATH,
) -> None:
    """Save an item similarity table as readable UTF-8 JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(table, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )