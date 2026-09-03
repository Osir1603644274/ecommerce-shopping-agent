"""Unblind and score multiple independent held-out ReAct/fixed human reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "constraintFidelity",
    "evidenceDiscipline",
    "taskProgression",
    "usefulness",
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{path}: expected non-empty JSON objects")
    return rows


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def score_human_gate(
    *,
    source_public: Path,
    packet: Path,
    dataset: Path,
    preregistration: Path,
    sealed_mapping: Path,
    ratings_paths: list[Path],
    output: Path,
) -> dict[str, Any]:
    source_rows = _read_jsonl(source_public)
    source_ids = [row.get("itemId") for row in source_rows]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("source item IDs are missing or duplicated")

    scenario_classes = {
        row["scenarioId"]: row["generalizationClass"] for row in _read_jsonl(dataset)
    }
    prereg = _read_json(preregistration)
    mapping_value = _read_json(sealed_mapping)
    mapping = {row["itemId"]: row["labels"] for row in mapping_value.get("items", [])}
    if set(mapping) != set(source_ids):
        raise ValueError("sealed mapping does not match public items")

    packet_sha = hashlib.sha256(packet.read_bytes()).hexdigest()
    dimension_values: dict[str, dict[str, list[int]]] = {
        runtime: {dimension: [] for dimension in DIMENSIONS}
        for runtime in ("react_v0", "fixed_v1")
    }
    class_dimension_values: dict[str, dict[str, dict[str, list[int]]]] = defaultdict(
        lambda: {
            runtime: {dimension: [] for dimension in DIMENSIONS}
            for runtime in ("react_v0", "fixed_v1")
        }
    )
    preferences = {"react_v0": 0, "fixed_v1": 0, "tie": 0, "unjudgeable": 0}
    class_preferences: dict[str, dict[str, int]] = defaultdict(
        lambda: {"react_v0": 0, "fixed_v1": 0, "tie": 0, "unjudgeable": 0}
    )
    reviewer_ids: list[str] = []
    item_preferences: dict[str, list[str]] = defaultdict(list)

    for ratings_path in ratings_paths:
        ratings = _read_json(ratings_path)
        reviewer_id = ratings.get("reviewerId")
        if not isinstance(reviewer_id, str) or not reviewer_id.strip() or reviewer_id in reviewer_ids:
            raise ValueError(f"{ratings_path}: reviewerId is missing or duplicated")
        if ratings.get("protocolStatus") != "VALID_BLIND_REVIEW":
            raise ValueError(f"{ratings_path}: review is not marked VALID_BLIND_REVIEW")
        if ratings.get("packetSha256") != packet_sha:
            raise ValueError(f"{ratings_path}: packet hash mismatch")
        rows = ratings.get("items")
        if not isinstance(rows, list) or [row.get("itemId") for row in rows] != source_ids:
            raise ValueError(f"{ratings_path}: items must match public order exactly")
        reviewer_ids.append(reviewer_id)

        for source_row, row in zip(source_rows, rows, strict=True):
            item_id = source_row["itemId"]
            labels = mapping[item_id]
            if set(labels) != {"A", "B"} or set(labels.values()) != {"react_v0", "fixed_v1"}:
                raise ValueError(f"{item_id}: invalid sealed labels")
            scenario_class = scenario_classes[source_row["scenarioId"]]
            for side in ("A", "B"):
                scores = row.get(f"candidate{side}")
                if (
                    not isinstance(scores, list)
                    or len(scores) != len(DIMENSIONS)
                    or any(type(score) is not int or not 1 <= score <= 5 for score in scores)
                ):
                    raise ValueError(f"{ratings_path}:{item_id}: invalid {side} scores")
                runtime = labels[side]
                for dimension, score in zip(DIMENSIONS, scores, strict=True):
                    dimension_values[runtime][dimension].append(score)
                    class_dimension_values[scenario_class][runtime][dimension].append(score)
            preference = row.get("overallPreference")
            if preference not in {"A", "B", "tie", "unjudgeable"}:
                raise ValueError(f"{ratings_path}:{item_id}: invalid preference")
            unblinded = labels[preference] if preference in {"A", "B"} else preference
            preferences[unblinded] += 1
            class_preferences[scenario_class][unblinded] += 1
            item_preferences[item_id].append(unblinded)

    dimension_means = {
        runtime: {dimension: _mean(values) for dimension, values in dimensions.items()}
        for runtime, dimensions in dimension_values.items()
    }
    class_means = {
        scenario_class: {
            runtime: {dimension: _mean(values) for dimension, values in dimensions.items()}
            for runtime, dimensions in runtimes.items()
        }
        for scenario_class, runtimes in sorted(class_dimension_values.items())
    }
    judgeable = preferences["react_v0"] + preferences["fixed_v1"]
    react_share = round(preferences["react_v0"] / judgeable, 4) if judgeable else 0.0
    human_gate = prereg["humanDirectionGate"]
    class_noninferiority = {
        scenario_class: {
            dimension: means["react_v0"][dimension] >= means["fixed_v1"][dimension]
            for dimension in ("constraintFidelity", "evidenceDiscipline")
        }
        for scenario_class, means in class_means.items()
    }
    failures: list[str] = []
    if len(reviewer_ids) < human_gate["minimumDirectFullPacketReviewers"]:
        failures.append("insufficient_valid_reviewers")
    if react_share < human_gate["minimumReactPreferenceShareAmongJudgeable"]:
        failures.append("react_preference_share_below_threshold")
    if human_gate["requireNoLowerConstraintFidelityMeanByClass"] and any(
        not checks["constraintFidelity"] for checks in class_noninferiority.values()
    ):
        failures.append("react_constraint_fidelity_lower_in_at_least_one_class")
    if human_gate["requireNoLowerEvidenceDisciplineMeanByClass"] and any(
        not checks["evidenceDiscipline"] for checks in class_noninferiority.values()
    ):
        failures.append("react_evidence_discipline_lower_in_at_least_one_class")

    agreement_count = sum(
        1 for values in item_preferences.values() if len(set(values)) == 1
    )
    result = {
        "schemaVersion": "used-phone-react-generalization-human-gate-v1",
        "status": "ACCEPT_LIMITED_DIRECTION" if not failures else "HOLD",
        "reviewerIds": reviewer_ids,
        "reviewerCount": len(reviewer_ids),
        "itemCountPerReviewer": len(source_ids),
        "preferenceCounts": preferences,
        "judgeablePreferenceCount": judgeable,
        "reactPreferenceShareAmongJudgeable": react_share,
        "requiredReactPreferenceShare": human_gate["minimumReactPreferenceShareAmongJudgeable"],
        "dimensionMeans": dimension_means,
        "classDimensionMeans": class_means,
        "classPreferenceCounts": dict(sorted(class_preferences.items())),
        "classNoninferiority": class_noninferiority,
        "exactPreferenceAgreement": {
            "itemCount": agreement_count,
            "rate": round(agreement_count / len(source_ids), 4),
        },
        "failures": failures,
        "sourcePublicSha256": hashlib.sha256(source_public.read_bytes()).hexdigest(),
        "packetSha256": packet_sha,
        "datasetSha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "preregistrationSha256": hashlib.sha256(preregistration.read_bytes()).hexdigest(),
        "sealedMappingSha256": hashlib.sha256(sealed_mapping.read_bytes()).hexdigest(),
        "ratingsSha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in ratings_paths
        },
        "claimBoundary": prereg["claimBoundary"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-public", required=True, type=Path)
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--preregistration", required=True, type=Path)
    parser.add_argument("--sealed-mapping", required=True, type=Path)
    parser.add_argument("--ratings", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = score_human_gate(
        source_public=args.source_public,
        packet=args.packet,
        dataset=args.dataset,
        preregistration=args.preregistration,
        sealed_mapping=args.sealed_mapping,
        ratings_paths=args.ratings,
        output=args.output,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
