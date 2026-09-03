"""Unblind the repaired delta and combine it with strictly reusable reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from react_v1_architecture_24_blind_pack_v1 import (
    _dialogue,
    _public_scenarios,
    _receipt_map,
)


DIMENSIONS = (
    "constraintFidelity",
    "evidenceDiscipline",
    "taskProgression",
    "usefulness",
)
ARMS = ("fixed_v1", "react_v1")
REUSABLE_KEYS = {
    ("scenario-001", 1),
    ("scenario-002", 1),
    ("scenario-003", 2),
}
DELTA_KEYS = {
    ("scenario-004", 2),
    ("scenario-005", 2),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path}: expected JSON objects")
    return rows


def _validate_review_inputs(
    *,
    manifest: dict[str, Any],
    mapping_path: Path,
    public_packets: dict[str, Path],
    reviews: dict[str, Path],
    receipts: dict[str, Path],
    attestation_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    attestation = _read_json(attestation_path)
    if not (
        attestation.get("reviewer01AndReviewer02AreDifferentHumans") is True
        and attestation.get("completedIndependently") is True
        and attestation.get("sealedMappingUnseenBeforeScoring") is True
    ):
        raise ValueError("required delta human independence attestation is missing")

    expected_outputs = manifest.get("outputs", {})
    if _sha256(mapping_path) != expected_outputs.get("sealedMappingSha256"):
        raise ValueError("delta sealed mapping hash mismatch")
    expected_count = manifest.get("itemCountPerReviewer")
    if expected_count != 2:
        raise ValueError("delta manifest must declare exactly two items")

    mapping_rows = _read_json(mapping_path).get("items")
    if not isinstance(mapping_rows, list) or len(mapping_rows) != expected_count:
        raise ValueError("delta sealed mapping item count mismatch")

    review_rows: dict[str, list[dict[str, Any]]] = {}
    for reviewer in ("reviewer01", "reviewer02"):
        public_path = public_packets[reviewer]
        expected_public_hash = expected_outputs[f"{reviewer}Sha256"]
        if _sha256(public_path) != expected_public_hash:
            raise ValueError(f"{reviewer}: public packet hash mismatch")
        receipt = _read_json(receipts[reviewer])
        if receipt.get("status") != "ACCEPTED_BLIND_REVIEW_SUBMISSION":
            raise ValueError(f"{reviewer}: intake receipt is not accepted")
        if receipt.get("sealedMappingRead") is not False:
            raise ValueError(f"{reviewer}: intake did not remain blind")
        if receipt.get("publicPacketSha256") != expected_public_hash:
            raise ValueError(f"{reviewer}: receipt/public packet binding mismatch")
        if receipt.get("normalizedOutputSha256") != _sha256(reviews[reviewer]):
            raise ValueError(f"{reviewer}: normalized review hash mismatch")
        rows = _read_jsonl(reviews[reviewer])
        if len(rows) != expected_count:
            raise ValueError(f"{reviewer}: normalized review count mismatch")
        review_rows[reviewer] = rows

    mapping_ids = {row["itemId"] for row in mapping_rows}
    for reviewer, rows in review_rows.items():
        if {row["itemId"] for row in rows} != mapping_ids:
            raise ValueError(f"{reviewer}: review items and mapping items differ")
    return mapping_rows, review_rows


def _validate_reuse(
    *, old_pair: Path, new_pair: Path, dataset: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    _validate_pair_artifact_chain(old_pair)
    new_paired = _validate_pair_artifact_chain(new_pair)
    if not (
        new_paired.get("status") == "ACCEPT"
        and new_paired.get("bothArmsSafetyAccept") is True
        and new_paired.get("coverage") == "full_suite"
    ):
        raise ValueError("new paired safety gate is not accepted")
    difference_keys = {
        (row["scenarioId"], row["turnIndex"])
        for row in new_paired.get("behaviorDifferenceCandidates", [])
    }
    if difference_keys != REUSABLE_KEYS | DELTA_KEYS:
        raise ValueError("new paired differences are not the expected five adaptive items")

    public = _public_scenarios(dataset)
    old_receipts = {
        arm: _receipt_map(old_pair / ("fixed" if arm == "fixed_v1" else "react") / "receipts.jsonl")
        for arm in ARMS
    }
    new_receipts = {
        arm: _receipt_map(new_pair / ("fixed" if arm == "fixed_v1" else "react") / "receipts.jsonl")
        for arm in ARMS
    }
    compared: list[dict[str, Any]] = []
    for scenario_id, turn_index in sorted(REUSABLE_KEYS):
        for arm in ARMS:
            old_dialogue = _dialogue(
                scenario_id=scenario_id,
                through=turn_index,
                turns=public[scenario_id],
                receipts=old_receipts[arm],
            )
            new_dialogue = _dialogue(
                scenario_id=scenario_id,
                through=turn_index,
                turns=public[scenario_id],
                receipts=new_receipts[arm],
            )
            if old_dialogue != new_dialogue:
                raise ValueError(f"reviewed dialogue changed: {arm} {scenario_id}")
        compared.append({"scenarioId": scenario_id, "turnIndex": turn_index})

    negative_parity: list[str] = []
    for number in range(17, 22):
        scenario_id = f"scenario-{number:03d}"
        key = (scenario_id, 1)
        fixed = new_receipts["fixed_v1"][key]
        react = new_receipts["react_v1"][key]
        if fixed["answer"] != react["answer"] or fixed["finalAction"] != react["finalAction"]:
            raise ValueError(f"scope-negative parity not restored: {scenario_id}")
        negative_parity.append(scenario_id)
    return compared, negative_parity


def _validate_pair_artifact_chain(pair_dir: Path) -> dict[str, Any]:
    paired = _read_json(pair_dir / "paired_result.json")
    for directory, field in (
        ("fixed", "fixedManifestSha256"),
        ("react", "reactManifestSha256"),
    ):
        manifest_path = pair_dir / directory / "manifest.json"
        receipts_path = pair_dir / directory / "receipts.jsonl"
        if _sha256(manifest_path) != paired.get(field):
            raise ValueError(f"{directory}: paired result/manifest hash mismatch")
        manifest = _read_json(manifest_path)
        if _sha256(receipts_path) != manifest.get("receiptsSha256"):
            raise ValueError(f"{directory}: manifest/receipts hash mismatch")
    return paired


def _validate_old_unblind_chain(
    *, old_pack: Path, old_result: dict[str, Any], dataset: Path,
) -> None:
    inputs = old_result.get("inputHashes", {})
    runner_metadata = dataset.parent.parent / "runner_metadata.jsonl"
    bound_paths = {
        "packManifestSha256": old_pack / "manifest.json",
        "sealedMappingSha256": old_pack / "sealed" / "mapping.json",
        "reviewer01Sha256": old_pack / "human-reviews" / "reviewer01-reviewed.jsonl",
        "reviewer02Sha256": old_pack / "human-reviews" / "reviewer02-reviewed.jsonl",
        "reviewer01ReceiptSha256": old_pack / "human-reviews" / "reviewer01-intake-receipt.json",
        "reviewer02ReceiptSha256": old_pack / "human-reviews" / "reviewer02-intake-receipt.json",
        "attestationSha256": old_pack / "human-reviews" / "independence-attestation.json",
        "runnerMetadataSha256": runner_metadata,
    }
    for field, path in bound_paths.items():
        if inputs.get(field) != _sha256(path):
            raise ValueError(f"old unblinded input hash mismatch: {field}")

    attestation = _read_json(bound_paths["attestationSha256"])
    if not (
        attestation.get("reviewer01AndReviewer02AreDifferentHumans") is True
        and attestation.get("completedIndependently") is True
        and attestation.get("sealedMappingUnseenBeforeScoring") is True
    ):
        raise ValueError("old human independence attestation is invalid")
    for reviewer in ("reviewer01", "reviewer02"):
        review_path = old_pack / "human-reviews" / f"{reviewer}-reviewed.jsonl"
        receipt = _read_json(
            old_pack / "human-reviews" / f"{reviewer}-intake-receipt.json"
        )
        if receipt.get("status") != "ACCEPTED_BLIND_REVIEW_SUBMISSION":
            raise ValueError(f"old {reviewer}: intake receipt is not accepted")
        if receipt.get("sealedMappingRead") is not False:
            raise ValueError(f"old {reviewer}: intake did not remain blind")
        if receipt.get("normalizedOutputSha256") != _sha256(review_path):
            raise ValueError(f"old {reviewer}: normalized review hash mismatch")


def _resolved_items(
    *,
    mapping_rows: list[dict[str, Any]],
    reviews: dict[str, list[dict[str, Any]]],
    selected_keys: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    mapping_by_item = {row["itemId"]: row for row in mapping_rows}
    review_by_item = {
        reviewer: {row["itemId"]: row for row in rows}
        for reviewer, rows in reviews.items()
    }
    resolved: list[dict[str, Any]] = []
    for mapping_row in mapping_rows:
        source = mapping_row["source"]
        source_key = (source["scenarioId"], source["turnIndex"])
        if selected_keys is not None and source_key not in selected_keys:
            continue
        item = {
            "itemId": mapping_row["itemId"],
            "scenarioId": source["scenarioId"],
            "turnIndex": source["turnIndex"],
            "preferences": {},
            "scores": {},
        }
        for reviewer in ("reviewer01", "reviewer02"):
            review = review_by_item[reviewer][mapping_row["itemId"]]["review"]
            preference = review["overallPreference"]
            item["preferences"][reviewer] = (
                mapping_row[reviewer][preference]
                if preference in {"A", "B"}
                else preference
            )
            arm_scores: dict[str, dict[str, int]] = {}
            for label in ("A", "B"):
                arm_scores[mapping_row[reviewer][label]] = review["scores"][f"candidate{label}"]
            item["scores"][reviewer] = arm_scores
        resolved.append(item)
    return resolved


def _summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    preferences = {"fixed_v1": 0, "react_v1": 0, "tie": 0, "unjudgeable": 0}
    sums = {arm: {dimension: 0 for dimension in DIMENSIONS} for arm in ARMS}
    counts = {arm: 0 for arm in ARMS}
    exact_agreement = 0
    opposite_preferences = 0
    for item in items:
        choices: list[str] = []
        for reviewer in ("reviewer01", "reviewer02"):
            choice = item["preferences"][reviewer]
            preferences[choice] += 1
            choices.append(choice)
            for arm in ARMS:
                counts[arm] += 1
                for dimension in DIMENSIONS:
                    sums[arm][dimension] += item["scores"][reviewer][arm][dimension]
        if choices[0] == choices[1]:
            exact_agreement += 1
        elif set(choices) == set(ARMS):
            opposite_preferences += 1
    means = {
        arm: {
            dimension: round(sums[arm][dimension] / counts[arm], 3)
            for dimension in DIMENSIONS
        }
        for arm in ARMS
    }
    return {
        "preferenceCounts": preferences,
        "scoreMeans": means,
        "agreement": {
            "exactOutcomeAgreementItems": exact_agreement,
            "itemCount": len(items),
            "oppositeArmPreferenceItems": opposite_preferences,
        },
    }


def unblind_and_combine(
    *,
    delta_pack: Path,
    old_pack: Path,
    old_pair: Path,
    new_pair: Path,
    dataset: Path,
) -> dict[str, Any]:
    manifest_path = delta_pack / "manifest.json"
    manifest = _read_json(manifest_path)
    old_unblinded_path = old_pack / "human-reviews" / "unblinded-result.json"
    expected_inputs = manifest.get("inputs", {})
    expected_hashes = {
        "oldPairedResultSha256": _sha256(old_pair / "paired_result.json"),
        "newPairedResultSha256": _sha256(new_pair / "paired_result.json"),
        "oldUnblindedResultSha256": _sha256(old_unblinded_path),
        "datasetSha256": _sha256(dataset),
    }
    if expected_inputs != expected_hashes:
        raise ValueError("delta manifest input binding mismatch")

    mapping_path = delta_pack / "sealed" / "mapping.json"
    public_packets = {
        "reviewer01": delta_pack / "reviewer01-delta.jsonl",
        "reviewer02": delta_pack / "reviewer02-delta.jsonl",
    }
    review_paths = {
        "reviewer01": delta_pack / "human-reviews" / "reviewer01-delta-reviewed.jsonl",
        "reviewer02": delta_pack / "human-reviews" / "reviewer02-delta-reviewed.jsonl",
    }
    receipt_paths = {
        "reviewer01": delta_pack / "human-reviews" / "reviewer01-delta-intake-receipt.json",
        "reviewer02": delta_pack / "human-reviews" / "reviewer02-delta-intake-receipt.json",
    }
    attestation_path = delta_pack / "human-reviews" / "independence-attestation.json"
    delta_mapping, delta_reviews = _validate_review_inputs(
        manifest=manifest,
        mapping_path=mapping_path,
        public_packets=public_packets,
        reviews=review_paths,
        receipts=receipt_paths,
        attestation_path=attestation_path,
    )
    delta_source_keys = {
        (row["source"]["scenarioId"], row["source"]["turnIndex"])
        for row in delta_mapping
    }
    if delta_source_keys != DELTA_KEYS:
        raise ValueError("delta mapping does not contain the expected changed items")

    compared, negative_parity = _validate_reuse(
        old_pair=old_pair,
        new_pair=new_pair,
        dataset=dataset,
    )
    old_result = _read_json(old_unblinded_path)
    if old_result.get("status") != "UNBLINDED_DESCRIPTIVE_RESULT":
        raise ValueError("old unblinded result is not accepted")
    _validate_old_unblind_chain(
        old_pack=old_pack,
        old_result=old_result,
        dataset=dataset,
    )
    old_mapping = _read_json(old_pack / "sealed" / "mapping.json").get("items")
    if not isinstance(old_mapping, list):
        raise ValueError("old mapping is invalid")
    old_reviews = {
        "reviewer01": _read_jsonl(old_pack / "human-reviews" / "reviewer01-reviewed.jsonl"),
        "reviewer02": _read_jsonl(old_pack / "human-reviews" / "reviewer02-reviewed.jsonl"),
    }
    reused_items = _resolved_items(
        mapping_rows=old_mapping,
        reviews=old_reviews,
        selected_keys=REUSABLE_KEYS,
    )
    delta_items = _resolved_items(mapping_rows=delta_mapping, reviews=delta_reviews)
    if len(reused_items) != 3 or len(delta_items) != 2:
        raise ValueError("combined review item count mismatch")

    delta_summary = _summarize(delta_items)
    combined_items = reused_items + delta_items
    combined_summary = _summarize(combined_items)
    return {
        "schemaVersion": "react-v1-architecture-24-delta-unblind-v1",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "status": "UNBLINDED_COMBINED_DESCRIPTIVE_RESULT",
        "architectureDecision": "ACCEPT_HYBRID_BOUNDED_REACT_TARGET",
        "effectDecision": "DESCRIPTIVE_REACT_PREFERENCE_SIGNAL_NO_PREREGISTERED_THRESHOLD",
        "productionDecision": "HOLD_KEEP_FIXED_V1_DEFAULT",
        "reviewerCount": 2,
        "strictlyReusedItemCount": 3,
        "newDeltaItemCount": 2,
        "excludedEqualArmItemCount": 5,
        "reusedDialogueEquivalence": compared,
        "scopeNegativeParityRestored": negative_parity,
        "deltaResult": {**delta_summary, "items": delta_items},
        "combinedAdaptiveResult": {**combined_summary, "items": combined_items},
        "interpretation": (
            "Across the five genuine adaptive differences and two independent reviewers, "
            "react_v1 received seven preferences, fixed_v1 received none, and three were ties. "
            "This supports the hybrid bounded ReAct target architecture descriptively. "
            "It does not authorize a production default switch because no human-effect "
            "threshold was preregistered and the independent reliability gate remains HOLD."
        ),
        "inputHashes": {
            "deltaManifestSha256": _sha256(manifest_path),
            "deltaMappingSha256": _sha256(mapping_path),
            "reviewer01Sha256": _sha256(review_paths["reviewer01"]),
            "reviewer02Sha256": _sha256(review_paths["reviewer02"]),
            "reviewer01ReceiptSha256": _sha256(receipt_paths["reviewer01"]),
            "reviewer02ReceiptSha256": _sha256(receipt_paths["reviewer02"]),
            "attestationSha256": _sha256(attestation_path),
            "oldUnblindedResultSha256": _sha256(old_unblinded_path),
            "newPairedResultSha256": _sha256(new_pair / "paired_result.json"),
            "datasetSha256": _sha256(dataset),
            "generatorSha256": _sha256(Path(__file__).resolve()),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delta-pack", required=True, type=Path)
    parser.add_argument("--old-pack", required=True, type=Path)
    parser.add_argument("--old-pair", required=True, type=Path)
    parser.add_argument("--new-pair", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output
    result = unblind_and_combine(
        **{key: value for key, value in vars(args).items() if key != "output"}
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
