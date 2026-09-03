"""Mechanically unblind two independently attested ReAct V1 review packets."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "constraintFidelity",
    "evidenceDiscipline",
    "taskProgression",
    "usefulness",
)
ARMS = ("fixed_v1", "react_v1")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path}: expected JSON objects")
    return rows


def unblind(
    *, pack_dir: Path, reviewer_one: Path, reviewer_two: Path,
    reviewer_one_receipt: Path, reviewer_two_receipt: Path,
    attestation: Path, runner_metadata: Path,
) -> dict[str, Any]:
    manifest = _read_json(pack_dir / "manifest.json")
    mapping_path = pack_dir / "sealed" / "mapping.json"
    mapping = _read_json(mapping_path)
    attest = _read_json(attestation)
    if not (
        attest.get("reviewer01AndReviewer02AreDifferentHumans") is True
        and attest.get("completedIndependently") is True
        and attest.get("sealedMappingUnseenBeforeScoring") is True
    ):
        raise ValueError("required human independence attestation is missing")
    if _sha256(mapping_path) != manifest.get("outputs", {}).get("sealedMappingSha256"):
        raise ValueError("sealed mapping hash mismatch")

    reviews = {
        "reviewer01": _read_jsonl(reviewer_one),
        "reviewer02": _read_jsonl(reviewer_two),
    }
    receipts = {
        "reviewer01": _read_json(reviewer_one_receipt),
        "reviewer02": _read_json(reviewer_two_receipt),
    }
    for reviewer, path in (("reviewer01", reviewer_one), ("reviewer02", reviewer_two)):
        receipt = receipts[reviewer]
        if receipt.get("status") != "ACCEPTED_BLIND_REVIEW_SUBMISSION":
            raise ValueError(f"{reviewer}: intake status is not accepted")
        if receipt.get("normalizedOutputSha256") != _sha256(path):
            raise ValueError(f"{reviewer}: normalized review hash mismatch")
        if len(reviews[reviewer]) != 10:
            raise ValueError(f"{reviewer}: expected 10 reviewed items")

    class_by_scenario = {
        row["scenarioId"]: row["behaviorClass"] for row in _read_jsonl(runner_metadata)
    }
    mapping_rows = mapping.get("items")
    if not isinstance(mapping_rows, list) or len(mapping_rows) != 10:
        raise ValueError("sealed mapping must contain 10 items")
    mapping_by_item = {row["itemId"]: row for row in mapping_rows}
    review_by_item = {
        reviewer: {row["itemId"]: row for row in rows}
        for reviewer, rows in reviews.items()
    }
    if any(set(rows) != set(mapping_by_item) for rows in review_by_item.values()):
        raise ValueError("review items and mapping items differ")

    preference_counts = {"fixed_v1": 0, "react_v1": 0, "tie": 0, "unjudgeable": 0}
    preference_by_class: dict[str, dict[str, int]] = {}
    score_sums = {arm: {dimension: 0 for dimension in DIMENSIONS} for arm in ARMS}
    score_counts = {arm: 0 for arm in ARMS}
    items: list[dict[str, Any]] = []
    exact_agreement = 0
    opposite_arm_preferences = 0
    for mapping_row in mapping_rows:
        item_id = mapping_row["itemId"]
        scenario_id = mapping_row["source"]["scenarioId"]
        behavior_class = class_by_scenario[scenario_id]
        preference_by_class.setdefault(
            behavior_class,
            {"fixed_v1": 0, "react_v1": 0, "tie": 0, "unjudgeable": 0},
        )
        item_preferences: dict[str, str] = {}
        for reviewer in ("reviewer01", "reviewer02"):
            row = review_by_item[reviewer][item_id]
            review = row["review"]
            preference = review["overallPreference"]
            if preference in {"A", "B"}:
                resolved = mapping_row[reviewer][preference]
            else:
                resolved = preference
            preference_counts[resolved] += 1
            preference_by_class[behavior_class][resolved] += 1
            item_preferences[reviewer] = resolved
            for label in ("A", "B"):
                arm = mapping_row[reviewer][label]
                scores = review["scores"][f"candidate{label}"]
                score_counts[arm] += 1
                for dimension in DIMENSIONS:
                    score_sums[arm][dimension] += scores[dimension]
        choices = list(item_preferences.values())
        if choices[0] == choices[1]:
            exact_agreement += 1
        elif set(choices) == set(ARMS):
            opposite_arm_preferences += 1
        items.append({
            "itemId": item_id,
            "scenarioId": scenario_id,
            "behaviorClass": behavior_class,
            "preferences": item_preferences,
        })

    score_means = {
        arm: {
            dimension: round(score_sums[arm][dimension] / score_counts[arm], 3)
            for dimension in DIMENSIONS
        }
        for arm in ARMS
    }
    return {
        "schemaVersion": "react-v1-architecture-24-blind-unblind-v1",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "status": "UNBLINDED_DESCRIPTIVE_RESULT",
        "effectDecision": "HOLD_NO_PREREGISTERED_HUMAN_EFFECT_THRESHOLD",
        "productionDecision": "HOLD_KEEP_FIXED_V1_DEFAULT",
        "reviewerCount": 2,
        "itemCountPerReviewer": 10,
        "preferenceCounts": preference_counts,
        "preferenceCountsByBehaviorClass": preference_by_class,
        "scoreMeans": score_means,
        "agreement": {
            "exactOutcomeAgreementItems": exact_agreement,
            "itemCount": 10,
            "oppositeArmPreferenceItems": opposite_arm_preferences,
        },
        "interpretation": (
            "react_v1 was preferred on adaptive differences; fixed_v1 was preferred on "
            "scope-negative differences. No preregistered human-effect threshold exists, "
            "so this is descriptive and does not authorize a production default switch."
        ),
        "items": items,
        "inputHashes": {
            "packManifestSha256": _sha256(pack_dir / "manifest.json"),
            "sealedMappingSha256": _sha256(mapping_path),
            "reviewer01Sha256": _sha256(reviewer_one),
            "reviewer02Sha256": _sha256(reviewer_two),
            "reviewer01ReceiptSha256": _sha256(reviewer_one_receipt),
            "reviewer02ReceiptSha256": _sha256(reviewer_two_receipt),
            "attestationSha256": _sha256(attestation),
            "runnerMetadataSha256": _sha256(runner_metadata),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", required=True, type=Path)
    parser.add_argument("--reviewer-one", required=True, type=Path)
    parser.add_argument("--reviewer-two", required=True, type=Path)
    parser.add_argument("--reviewer-one-receipt", required=True, type=Path)
    parser.add_argument("--reviewer-two-receipt", required=True, type=Path)
    parser.add_argument("--attestation", required=True, type=Path)
    parser.add_argument("--runner-metadata", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output
    result = unblind(**{key: value for key, value in vars(args).items() if key != "output"})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
