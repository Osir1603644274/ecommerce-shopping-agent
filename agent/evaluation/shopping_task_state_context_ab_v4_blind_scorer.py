"""Validate, unblind, and descriptively aggregate two mirrored V4 human reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "constraintFidelity",
    "evidenceDiscipline",
    "taskProgression",
    "usefulness",
)
ARMS = ("control", "treatment")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


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


def _validate_completed(
    source_path: Path, completed_path: Path
) -> dict[str, dict[str, Any]]:
    source = _read_jsonl(source_path)
    completed = _read_jsonl(completed_path)
    if not source or len(source) != len(completed):
        raise ValueError("completed review must preserve every public item")
    source_by_id = {row.get("itemId"): row for row in source}
    completed_by_id = {row.get("itemId"): row for row in completed}
    if (
        len(source_by_id) != len(source)
        or len(completed_by_id) != len(completed)
        or set(source_by_id) != set(completed_by_id)
    ):
        raise ValueError("review item IDs are missing or duplicated")
    for item_id, source_row in source_by_id.items():
        completed_row = completed_by_id[item_id]
        if _content_without_review(source_row) != _content_without_review(completed_row):
            raise ValueError(f"{item_id}: blind content changed during review")
        review = completed_row.get("review")
        if not isinstance(review, dict):
            raise ValueError(f"{item_id}: review is incomplete")
        if review.get("overallPreference") not in {"A", "B", "tie", "unjudgeable"}:
            raise ValueError(f"{item_id}: invalid preference")
        for side in ("candidateA", "candidateB"):
            scores = review.get(side)
            if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
                raise ValueError(f"{item_id}: {side} must score the exact dimensions")
            if any(type(scores[name]) is not int or not 1 <= scores[name] <= 5 for name in DIMENSIONS):
                raise ValueError(f"{item_id}: scores must be integers from 1 to 5")
    return completed_by_id


def score_reviews(
    *,
    reviewer_one_public: Path,
    reviewer_one_completed: Path,
    reviewer_two_public: Path,
    reviewer_two_completed: Path,
    sealed_mapping: Path,
    attestation: Path,
    output: Path,
) -> dict[str, Any]:
    attested = _read_json(attestation)
    if not all(
        attested.get(key) is True
        for key in (
            "humanReviewerAttested",
            "independentReviewAttested",
            "mappingBlindAttested",
        )
    ):
        raise ValueError("human, independence, and mapping-blind attestations are required")
    expected_completed_hashes = {
        reviewer_one_completed: attested.get("reviewer01CompletedSha256"),
        reviewer_two_completed: attested.get("reviewer02CompletedSha256"),
    }
    for path, expected in expected_completed_hashes.items():
        if type(expected) is not str or _sha256(path) != expected:
            raise ValueError(f"attested completed-review hash mismatch: {path}")

    reviews = {
        "reviewer01": _validate_completed(reviewer_one_public, reviewer_one_completed),
        "reviewer02": _validate_completed(reviewer_two_public, reviewer_two_completed),
    }
    mapping_value = _read_json(sealed_mapping)
    mapping_rows = mapping_value.get("items")
    if not isinstance(mapping_rows, list) or not mapping_rows:
        raise ValueError("sealed mapping has no items")
    mappings = {row.get("itemId"): row for row in mapping_rows if isinstance(row, dict)}
    if len(mappings) != len(mapping_rows):
        raise ValueError("sealed mapping item IDs are missing or duplicated")
    if any(set(items) != set(mappings) for items in reviews.values()):
        raise ValueError("sealed mapping does not match both review packets")

    dimension_totals = {arm: {name: 0 for name in DIMENSIONS} for arm in ARMS}
    dimension_counts = {arm: 0 for arm in ARMS}
    preferences = {"control": 0, "treatment": 0, "tie": 0, "unjudgeable": 0}
    item_results: list[dict[str, Any]] = []
    agreement_count = 0
    agreed_item_preferences = {
        "control": 0,
        "treatment": 0,
        "tie": 0,
        "unjudgeable": 0,
        "disagreement": 0,
    }
    for item_id, mapping in mappings.items():
        item_preferences: dict[str, str] = {}
        item_scores: dict[str, dict[str, dict[str, int]]] = {}
        labels_by_reviewer: dict[str, dict[str, str]] = {}
        for reviewer in ("reviewer01", "reviewer02"):
            labels = mapping.get(reviewer)
            if (
                not isinstance(labels, dict)
                or set(labels) != {"A", "B"}
                or set(labels.values()) != set(ARMS)
            ):
                raise ValueError(f"{item_id}: invalid {reviewer} mapping")
            labels_by_reviewer[reviewer] = labels
        if any(
            labels_by_reviewer["reviewer01"][side]
            != labels_by_reviewer["reviewer02"]["B" if side == "A" else "A"]
            for side in ("A", "B")
        ):
            raise ValueError(f"{item_id}: reviewer assignments are not mirrored")

        for reviewer in ("reviewer01", "reviewer02"):
            review = reviews[reviewer][item_id]["review"]
            labels = labels_by_reviewer[reviewer]
            item_scores[reviewer] = {}
            for side in ("A", "B"):
                arm = labels[side]
                scores = review[f"candidate{side}"]
                item_scores[reviewer][arm] = scores
                dimension_counts[arm] += 1
                for name in DIMENSIONS:
                    dimension_totals[arm][name] += scores[name]
            raw_preference = review["overallPreference"]
            preference = labels[raw_preference] if raw_preference in {"A", "B"} else raw_preference
            item_preferences[reviewer] = preference
            preferences[preference] += 1
        agreed = item_preferences["reviewer01"] == item_preferences["reviewer02"]
        agreement_count += int(agreed)
        if agreed:
            agreed_item_preferences[item_preferences["reviewer01"]] += 1
        else:
            agreed_item_preferences["disagreement"] += 1
        item_results.append({
            "itemId": item_id,
            "source": mapping.get("source"),
            "preferences": item_preferences,
            "preferenceAgreement": agreed,
            "scoresByReviewerAndArm": item_scores,
        })

    result = {
        "schemaVersion": "shopping-task-state-context-blind-score-v1",
        "experimentId": attested.get("experimentId"),
        "status": "HUMAN_BLIND_REVIEW_COMPLETE",
        "reviewerCount": 2,
        "itemCount": len(item_results),
        "ratingCountPerArm": dimension_counts,
        "dimensionMeans": {
            arm: {
                name: round(dimension_totals[arm][name] / dimension_counts[arm], 4)
                for name in DIMENSIONS
            }
            for arm in ARMS
        },
        "preferenceCountsAcrossReviews": preferences,
        "preferenceAgreement": {
            "agreedItems": agreement_count,
            "itemCount": len(item_results),
            "rate": round(agreement_count / len(item_results), 4),
        },
        "agreedItemPreferenceCounts": agreed_item_preferences,
        "items": item_results,
        "evidenceInterpretation": "MIXED_DESCRIPTIVE_SIGNAL_TREATMENT_PREFERENCE_MAJORITY",
        "productionDecision": "HOLD",
        "productionDecisionReason": (
            "Preference counts favor treatment, while dimension means are mixed. There is no "
            "preregistered human-win threshold or generalization gate, and six selected differing "
            "turns cannot by themselves authorize production migration."
        ),
        "claimBoundary": {
            "humanAndIndependentAndMappingBlindAttested": True,
            "descriptiveOnly": True,
            "provesStatisticalSignificance": False,
            "provesGeneralSuperiority": False,
            "authorizesProductionMigration": False,
        },
        "hashes": {
            "reviewer01PublicSha256": _sha256(reviewer_one_public),
            "reviewer01CompletedSha256": _sha256(reviewer_one_completed),
            "reviewer02PublicSha256": _sha256(reviewer_two_public),
            "reviewer02CompletedSha256": _sha256(reviewer_two_completed),
            "sealedMappingSha256": _sha256(sealed_mapping),
            "attestationSha256": _sha256(attestation),
            "scorerSha256": _sha256(Path(__file__).resolve()),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewer-one-public", required=True, type=Path)
    parser.add_argument("--reviewer-one-completed", required=True, type=Path)
    parser.add_argument("--reviewer-two-public", required=True, type=Path)
    parser.add_argument("--reviewer-two-completed", required=True, type=Path)
    parser.add_argument("--sealed-mapping", required=True, type=Path)
    parser.add_argument("--attestation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(score_reviews(**vars(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
