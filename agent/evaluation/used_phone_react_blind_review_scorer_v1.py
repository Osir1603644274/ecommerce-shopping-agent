"""Validate, unblind, and aggregate completed ReAct/fixed A/B reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "constraintFidelity", "evidenceDiscipline", "taskProgression", "usefulness"
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected object")
            rows.append(value)
    return rows


def _content_without_review(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "review"}


def _validate_scores(value: object, item_id: str, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(DIMENSIONS):
        raise ValueError(f"{item_id}: {label} must score the exact rubric dimensions")
    if any(type(value[name]) is not int or not 1 <= value[name] <= 5 for name in DIMENSIONS):
        raise ValueError(f"{item_id}: {label} scores must be integers from 1 to 5")
    return {name: value[name] for name in DIMENSIONS}


def score_reviews(
    *, source_public: Path, completed_review: Path, sealed_mapping: Path,
    output: Path,
) -> dict[str, Any]:
    source = _read_jsonl(source_public)
    completed = _read_jsonl(completed_review)
    if len(source) != len(completed) or not source:
        raise ValueError("completed review must contain every source item exactly once")
    source_by_id = {row.get("itemId"): row for row in source}
    completed_by_id = {row.get("itemId"): row for row in completed}
    if len(source_by_id) != len(source) or set(source_by_id) != set(completed_by_id):
        raise ValueError("review item IDs are missing or duplicated")
    for item_id, source_row in source_by_id.items():
        if _content_without_review(source_row) != _content_without_review(
            completed_by_id[item_id]
        ):
            raise ValueError(f"{item_id}: blind item content changed during review")

    mapping_value = json.loads(sealed_mapping.read_text(encoding="utf-8"))
    mapping_rows = mapping_value.get("items") if isinstance(mapping_value, dict) else None
    if not isinstance(mapping_rows, list):
        raise ValueError("sealed mapping has invalid items")
    mapping = {row.get("itemId"): row for row in mapping_rows if isinstance(row, dict)}
    if len(mapping) != len(mapping_rows) or set(mapping) != set(source_by_id):
        raise ValueError("sealed mapping does not match review items")

    dimension_totals = {
        runtime: {dimension: 0 for dimension in DIMENSIONS}
        for runtime in ("react_v0", "fixed_v1")
    }
    preferences = {"react_v0": 0, "fixed_v1": 0, "tie": 0, "unjudgeable": 0}
    reviewer_ids: set[str] = set()
    scored_items: list[dict[str, Any]] = []
    for item_id in source_by_id:
        review = completed_by_id[item_id].get("review")
        if not isinstance(review, dict):
            raise ValueError(f"{item_id}: review is incomplete")
        reviewer_id = review.get("reviewerId")
        if type(reviewer_id) is not str or not reviewer_id.strip():
            raise ValueError(f"{item_id}: reviewerId is required")
        reviewer_ids.add(reviewer_id.strip())
        scores = {
            "A": _validate_scores(review.get("candidateA"), item_id, "candidateA"),
            "B": _validate_scores(review.get("candidateB"), item_id, "candidateB"),
        }
        preference = review.get("overallPreference")
        if preference not in {"A", "B", "tie", "unjudgeable"}:
            raise ValueError(f"{item_id}: invalid overallPreference")
        labels = mapping[item_id].get("labels")
        if not isinstance(labels, dict) or set(labels) != {"A", "B"} or set(labels.values()) != {
            "react_v0", "fixed_v1"
        }:
            raise ValueError(f"{item_id}: invalid sealed labels")
        for side in ("A", "B"):
            runtime = labels[side]
            for dimension in DIMENSIONS:
                dimension_totals[runtime][dimension] += scores[side][dimension]
        unblinded_preference = labels[preference] if preference in {"A", "B"} else preference
        preferences[unblinded_preference] += 1
        scored_items.append({
            "itemId": item_id,
            "reviewerId": reviewer_id.strip(),
            "preference": unblinded_preference,
        })

    item_count = len(source)
    result = {
        "schemaVersion": "used-phone-blind-review-score-v1",
        "status": "EVALUATED",
        "itemCount": item_count,
        "reviewerCount": len(reviewer_ids),
        "dimensionMeans": {
            runtime: {
                dimension: round(total / item_count, 4)
                for dimension, total in totals.items()
            }
            for runtime, totals in dimension_totals.items()
        },
        "preferenceCounts": preferences,
        "items": scored_items,
        "sourcePublicSha256": hashlib.sha256(source_public.read_bytes()).hexdigest(),
        "completedReviewSha256": hashlib.sha256(completed_review.read_bytes()).hexdigest(),
        "sealedMappingSha256": hashlib.sha256(sealed_mapping.read_bytes()).hexdigest(),
        "claimBoundary": {
            "scoresOnlySubmittedReviews": True,
            "provesReviewerIndependence": False,
            "provesStatisticalSignificance": False,
            "provesGeneralSuperiority": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-public", required=True, type=Path)
    parser.add_argument("--completed-review", required=True, type=Path)
    parser.add_argument("--sealed-mapping", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = score_reviews(
        source_public=args.source_public,
        completed_review=args.completed_review,
        sealed_mapping=args.sealed_mapping,
        output=args.output,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
