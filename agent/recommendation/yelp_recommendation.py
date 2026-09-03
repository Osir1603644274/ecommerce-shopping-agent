"""Build Yelp recommendation evaluation cases from raw user-business ratings.

The regular Yelp import sample is optimized for the local-life demo database.
This module is different: it builds an offline recommendation benchmark where
each case has enough positive user history to evaluate Popular / ItemCF /
Hybrid recommenders with a history-to-future split.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from recommendation.yelp import (
    DEFAULT_RAW_DIR,
    DEFAULT_START_SHOP_ID,
    build_address,
    estimate_avg_price,
    iter_json_lines,
    match_shop_type,
)


DEFAULT_YELP_RECOMMENDATION_CASES_PATH = (
    Path(__file__).parent / "data" / "processed" / "yelp_recommendation_cases.json"
)
DEFAULT_YELP_RECOMMENDATION_ITEMS_PATH = (
    Path(__file__).parent / "data" / "processed" / "yelp_recommendation_items.json"
)


def build_yelp_recommendation_cases(
    businesses: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    *,
    city: str | None = "Philadelphia",
    positive_score_threshold: float = 4.0,
    min_positive_interactions: int = 5,
    min_history_interactions: int = 3,
    train_ratio: float = 0.7,
    max_cases: int = 1000,
    min_business_review_count: int = 5,
    start_item_id: int = DEFAULT_START_SHOP_ID,
) -> dict[str, Any]:
    """Create recommendation cases from Yelp rows.

    A case corresponds to one user. Positive interactions are sorted by time;
    the earlier part becomes ``history`` and the later part becomes
    ``targetInteractions``. The ``relevantItemIds`` are the target item ids.
    """
    if positive_score_threshold <= 0:
        raise ValueError("positive_score_threshold must be positive")
    if min_positive_interactions < 2:
        raise ValueError("min_positive_interactions must be at least 2")
    if min_history_interactions <= 0:
        raise ValueError("min_history_interactions must be positive")
    if not 0 < train_ratio < 1:
        raise ValueError("train_ratio must be between 0 and 1")
    if max_cases <= 0:
        raise ValueError("max_cases must be positive")
    if min_business_review_count < 0:
        raise ValueError("min_business_review_count cannot be negative")

    item_by_business_id = build_candidate_items(
        businesses,
        city=city,
        min_business_review_count=min_business_review_count,
        start_item_id=start_item_id,
    )
    interactions_by_user = collect_positive_interactions(
        reviews,
        item_by_business_id,
        positive_score_threshold=positive_score_threshold,
    )
    cases = build_cases_from_user_interactions(
        interactions_by_user,
        min_positive_interactions=min_positive_interactions,
        min_history_interactions=min_history_interactions,
        train_ratio=train_ratio,
        max_cases=max_cases,
    )
    items = sorted(item_by_business_id.values(), key=lambda item: item["itemId"])
    return {
        "metadata": {
            "sourceDataset": "Yelp Open Dataset",
            "city": city,
            "positiveScoreThreshold": positive_score_threshold,
            "minPositiveInteractions": min_positive_interactions,
            "minHistoryInteractions": min_history_interactions,
            "trainRatio": train_ratio,
            "maxCases": max_cases,
            "minBusinessReviewCount": min_business_review_count,
            "candidateItemCount": len(items),
            "positiveInteractionCount": sum(len(items) for items in interactions_by_user.values()),
            "positiveUserCount": len(interactions_by_user),
            "caseCount": len(cases),
        },
        "items": items,
        "cases": cases,
    }


def build_candidate_items(
    businesses: Iterable[dict[str, Any]],
    *,
    city: str | None,
    min_business_review_count: int,
    start_item_id: int,
) -> dict[str, dict[str, Any]]:
    """Map Yelp business ids to local recommendation item metadata."""
    selected_city = city.casefold() if city else None
    item_by_business_id: dict[str, dict[str, Any]] = {}
    for business in businesses:
        if selected_city and str(business.get("city", "")).casefold() != selected_city:
            continue
        if business.get("is_open") == 0:
            continue
        if int(business.get("review_count") or 0) < min_business_review_count:
            continue

        shop_type = match_shop_type(business.get("categories"))
        if shop_type is None:
            continue

        business_id = str(business.get("business_id", "")).strip()
        name = str(business.get("name", "")).strip()
        if not business_id or not name:
            continue

        item_by_business_id[business_id] = {
            "itemId": start_item_id + len(item_by_business_id),
            "sourceBusinessId": business_id,
            "name": name,
            "typeId": shop_type["typeId"],
            "typeName": shop_type["typeName"],
            "address": build_address(business),
            "avgPrice": estimate_avg_price(business.get("attributes")),
            "longitude": float(business.get("longitude", 0.0)),
            "latitude": float(business.get("latitude", 0.0)),
            "reviewCount": int(business.get("review_count") or 0),
        }
    return item_by_business_id


def collect_positive_interactions(
    reviews: Iterable[dict[str, Any]],
    item_by_business_id: dict[str, dict[str, Any]],
    *,
    positive_score_threshold: float,
) -> dict[str, list[dict[str, Any]]]:
    """Collect positive user-item interactions from Yelp reviews."""
    interactions_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        business_id = str(review.get("business_id", "")).strip()
        item = item_by_business_id.get(business_id)
        if item is None:
            continue

        score = float(review.get("stars") or 0.0)
        if score < positive_score_threshold:
            continue

        user_id = str(review.get("user_id", "")).strip()
        review_id = str(review.get("review_id", "")).strip()
        occurred_at = str(review.get("date", "")).strip()
        if not user_id or not review_id or not occurred_at:
            continue

        interactions_by_user[user_id].append(
            {
                "itemId": item["itemId"],
                "rating": score,
                "score": score,
                "timestamp": occurred_at,
                "source": "yelp_review",
                "sourceReviewId": review_id,
                "sourceBusinessId": business_id,
                "itemName": item["name"],
                "typeId": item["typeId"],
                "typeName": item["typeName"],
            }
        )
    return interactions_by_user


def build_cases_from_user_interactions(
    interactions_by_user: dict[str, list[dict[str, Any]]],
    *,
    min_positive_interactions: int,
    min_history_interactions: int,
    train_ratio: float,
    max_cases: int,
) -> list[dict[str, Any]]:
    """Split each eligible user's positive interactions into history and target."""
    candidates: list[tuple[str, list[dict[str, Any]]]] = []
    for user_id, interactions in interactions_by_user.items():
        unique_interactions = dedupe_user_item_interactions(interactions)
        if len(unique_interactions) < min_positive_interactions:
            continue
        split_index = int(len(unique_interactions) * train_ratio)
        split_index = max(split_index, min_history_interactions)
        split_index = min(split_index, len(unique_interactions) - 1)
        if split_index < min_history_interactions:
            continue
        candidates.append((user_id, unique_interactions))

    candidates.sort(key=lambda item: (-len(item[1]), item[0]))
    cases: list[dict[str, Any]] = []
    for index, (user_id, interactions) in enumerate(candidates[:max_cases], start=1):
        split_index = int(len(interactions) * train_ratio)
        split_index = max(split_index, min_history_interactions)
        split_index = min(split_index, len(interactions) - 1)
        history = interactions[:split_index]
        target = interactions[split_index:]
        relevant_item_ids = sorted({int(item["itemId"]) for item in target})
        if not history or not relevant_item_ids:
            continue
        cases.append(
            {
                "caseId": f"yelp-rec-{index:04d}",
                "userId": f"yelp-user-{user_id}"[:64],
                "sourceUserId": user_id,
                "history": history,
                "targetInteractions": target,
                "relevantItemIds": relevant_item_ids,
                "historyCount": len(history),
                "targetCount": len(target),
            }
        )
    return cases


def dedupe_user_item_interactions(interactions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one latest positive interaction per item for one user."""
    latest_by_item_id: dict[int, dict[str, Any]] = {}
    for interaction in interactions:
        item_id = int(interaction["itemId"])
        previous = latest_by_item_id.get(item_id)
        if previous is None or str(interaction["timestamp"]) > str(previous["timestamp"]):
            latest_by_item_id[item_id] = interaction
    return sorted(
        latest_by_item_id.values(),
        key=lambda item: (str(item["timestamp"]), int(item["itemId"])),
    )


def convert_yelp_recommendation_cases(
    *,
    raw_dir: Path = DEFAULT_RAW_DIR,
    cases_output_path: Path = DEFAULT_YELP_RECOMMENDATION_CASES_PATH,
    items_output_path: Path = DEFAULT_YELP_RECOMMENDATION_ITEMS_PATH,
    city: str | None = "Philadelphia",
    positive_score_threshold: float = 4.0,
    min_positive_interactions: int = 5,
    min_history_interactions: int = 3,
    train_ratio: float = 0.7,
    max_cases: int = 1000,
    min_business_review_count: int = 5,
) -> dict[str, Any]:
    """Read raw Yelp files and save recommendation cases plus item metadata."""
    business_path = raw_dir / "yelp_academic_dataset_business.json"
    review_path = raw_dir / "yelp_academic_dataset_review.json"
    result = build_yelp_recommendation_cases(
        iter_json_lines(business_path),
        iter_json_lines(review_path),
        city=city,
        positive_score_threshold=positive_score_threshold,
        min_positive_interactions=min_positive_interactions,
        min_history_interactions=min_history_interactions,
        train_ratio=train_ratio,
        max_cases=max_cases,
        min_business_review_count=min_business_review_count,
    )
    cases_output_path.parent.mkdir(parents=True, exist_ok=True)
    cases_output_path.write_text(
        json.dumps(result["cases"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    items_output_path.parent.mkdir(parents=True, exist_ok=True)
    items_output_path.write_text(
        json.dumps(
            {"metadata": result["metadata"], "items": result["items"]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return result
