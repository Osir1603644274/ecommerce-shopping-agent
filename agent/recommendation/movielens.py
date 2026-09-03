"""Convert MovieLens ratings into the project's FunRec evaluation contract.

The raw MovieLens files are not committed to this repository. Download
``ml-latest-small.zip`` from GroupLens and extract it under:

    agent/recommendation/data/raw/ml-latest-small/
"""

from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_RAW_DIR = Path(__file__).parent / "data" / "raw" / "ml-latest-small"
DEFAULT_OUTPUT_PATH = Path(__file__).parent / "data" / "processed" / "movielens_cases.json"
DEFAULT_SPLITS_OUTPUT_PATH = Path(__file__).parent / "data" / "processed" / "movielens_case_splits.json"


def _rating_to_score(rating: float) -> int:
    """Map MovieLens 0.5-5.0 ratings to a compact integer relevance score."""
    if rating >= 4.5:
        return 5
    if rating >= 4.0:
        return 4
    if rating >= 3.0:
        return 2
    return 1


def _rating_to_action(rating: float) -> str:
    """Represent rating strength as a recommendation-style interaction."""
    if rating >= 4.0:
        return "positive_rating"
    if rating >= 3.0:
        return "neutral_rating"
    return "low_rating"


def load_ratings(raw_dir: Path = DEFAULT_RAW_DIR) -> list[dict[str, Any]]:
    """Load MovieLens ratings.csv as normalized interaction dictionaries."""
    ratings_path = raw_dir / "ratings.csv"
    if not ratings_path.exists():
        raise FileNotFoundError(
            f"MovieLens ratings file not found: {ratings_path}. "
            "Download ml-latest-small.zip from GroupLens first."
        )

    ratings: list[dict[str, Any]] = []
    with ratings_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            rating = float(row["rating"])
            ratings.append(
                {
                    "userId": int(row["userId"]),
                    "movieId": int(row["movieId"]),
                    "rating": rating,
                    "action": _rating_to_action(rating),
                    "score": _rating_to_score(rating),
                    "timestamp": int(row["timestamp"]),
                }
            )
    return ratings


def build_cases(
    ratings: list[dict[str, Any]],
    *,
    min_history: int = 5,
    max_history: int = 20,
    target_rating_threshold: float = 4.0,
    max_cases: int | None = 100,
) -> list[dict[str, Any]]:
    """Build time-split recommendation evaluation cases.

    For each user, interactions are sorted by time. Earlier interactions become
    ``history``. Later positive interactions become ``targetInteractions`` and
    their item ids form ``relevantItemIds``.

    The contract uses generic ``itemId`` so it can represent MovieLens movies now
    and local-life merchants later, while ``sourceItemType`` records that this
    first real dataset is MovieLens movies.
    """
    ratings_by_user: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in ratings:
        ratings_by_user[item["userId"]].append(item)

    cases: list[dict[str, Any]] = []
    for user_id in sorted(ratings_by_user):
        user_ratings = sorted(ratings_by_user[user_id], key=lambda item: item["timestamp"])
        if len(user_ratings) <= min_history:
            continue

        split_index = max(min_history, int(len(user_ratings) * 0.8))
        history_items = user_ratings[:split_index][-max_history:]
        future_items = user_ratings[split_index:]
        target_items = [
            item
            for item in future_items
            if item["rating"] >= target_rating_threshold
        ]
        if not target_items:
            continue

        relevant_ids = sorted({item["movieId"] for item in target_items})
        cases.append(
            {
                "caseId": f"ml-small-{len(cases) + 1:04d}",
                "userId": f"ml-user-{user_id}",
                "sourceDataset": "MovieLens ml-latest-small",
                "sourceItemType": "movie",
                "difficulty": "real",
                "challengeTypes": ["real_user_behavior", "time_split"],
                "history": [
                    {
                        "itemId": item["movieId"],
                        "action": item["action"],
                        "rating": item["rating"],
                        "score": item["score"],
                        "timestamp": item["timestamp"],
                    }
                    for item in history_items
                ],
                "targetInteractions": [
                    {
                        "itemId": item["movieId"],
                        "action": item["action"],
                        "rating": item["rating"],
                        "score": item["score"],
                        "timestamp": item["timestamp"],
                    }
                    for item in target_items
                ],
                "relevantItemIds": relevant_ids,
            }
        )
        if max_cases is not None and len(cases) >= max_cases:
            break

    return cases


def split_cases(
    cases: list[dict[str, Any]],
    *,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    random_seed: int = 42,
) -> dict[str, Any]:
    """Split cases into deterministic train / validation / test partitions."""
    if not cases:
        raise ValueError("cases must not be empty")
    if train_ratio <= 0 or validation_ratio <= 0:
        raise ValueError("train_ratio and validation_ratio must be positive")
    if train_ratio + validation_ratio >= 1:
        raise ValueError("train_ratio + validation_ratio must be less than 1")

    shuffled_cases = list(cases)
    random.Random(random_seed).shuffle(shuffled_cases)

    total = len(shuffled_cases)
    train_count = int(total * train_ratio)
    validation_count = int(total * validation_ratio)
    if train_count <= 0 or validation_count <= 0 or train_count + validation_count >= total:
        raise ValueError("split ratios produce an empty partition")

    train_cases = shuffled_cases[:train_count]
    validation_cases = shuffled_cases[train_count : train_count + validation_count]
    test_cases = shuffled_cases[train_count + validation_count :]

    return {
        "splitMethod": "deterministic_random_case_split",
        "randomSeed": random_seed,
        "ratios": {
            "train": train_ratio,
            "validation": validation_ratio,
            "test": 1 - train_ratio - validation_ratio,
        },
        "counts": {
            "train": len(train_cases),
            "validation": len(validation_cases),
            "test": len(test_cases),
            "total": total,
        },
        "train": train_cases,
        "validation": validation_cases,
        "test": test_cases,
    }


def save_cases(cases: list[dict[str, Any]], output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def save_case_splits(
    case_splits: dict[str, Any],
    output_path: Path = DEFAULT_SPLITS_OUTPUT_PATH,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(case_splits, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def convert_movielens(
    raw_dir: Path = DEFAULT_RAW_DIR,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    max_cases: int | None = 100,
) -> list[dict[str, Any]]:
    ratings = load_ratings(raw_dir)
    cases = build_cases(ratings, max_cases=max_cases)
    save_cases(cases, output_path)
    return cases


def convert_movielens_splits(
    raw_dir: Path = DEFAULT_RAW_DIR,
    cases_output_path: Path = DEFAULT_OUTPUT_PATH,
    splits_output_path: Path = DEFAULT_SPLITS_OUTPUT_PATH,
    max_cases: int | None = 100,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    random_seed: int = 42,
) -> dict[str, Any]:
    cases = convert_movielens(raw_dir, cases_output_path, max_cases)
    case_splits = split_cases(
        cases,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
        random_seed=random_seed,
    )
    save_case_splits(case_splits, splits_output_path)
    return case_splits


if __name__ == "__main__":
    generated_cases = convert_movielens()
    print(f"Generated {len(generated_cases)} MovieLens recommendation cases.")