"""Mechanically unblind two attested, mirrored Context/Multi-Agent reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.context_multiagent_blind_review_intake_v1 import (
    DIMENSIONS,
    read_jsonl,
    sha256_file,
)


ARMS = ("CTX1b", "MA1")
REQUIRED_ATTESTATIONS = (
    "reviewer01AndReviewer02AreDifferentHumans",
    "reviewer01Independent",
    "reviewer02Independent",
    "bothMappingBlindUntilReviewsFrozen",
    "bothAutomaticResultsBlindUntilReviewsFrozen",
    "unblindingAllowed",
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_attestation(
    attestation: dict[str, Any],
    *,
    reviewer01_completed: Path,
    reviewer02_completed: Path,
    reviewer01_receipt: Path,
    reviewer02_receipt: Path,
) -> None:
    if not all(attestation.get(key) is True for key in REQUIRED_ATTESTATIONS):
        raise ValueError("all human, independence, blindness, and unblind attestations are required")
    expected = {
        "reviewer01CompletedSha256": sha256_file(reviewer01_completed),
        "reviewer02CompletedSha256": sha256_file(reviewer02_completed),
        "reviewer01IntakeReceiptSha256": sha256_file(reviewer01_receipt),
        "reviewer02IntakeReceiptSha256": sha256_file(reviewer02_receipt),
    }
    for field, actual in expected.items():
        if attestation.get(field) != actual:
            raise ValueError(f"attested hash mismatch: {field}")


def validate_completed(
    public_path: Path, completed_path: Path, receipt_path: Path
) -> dict[str, dict[str, Any]]:
    public_rows = read_jsonl(public_path)
    completed_rows = read_jsonl(completed_path)
    public = {row.get("itemId"): row for row in public_rows}
    completed = {row.get("itemId"): row for row in completed_rows}
    if (
        not public
        or len(public) != len(public_rows)
        or len(completed) != len(completed_rows)
        or set(public) != set(completed)
    ):
        raise ValueError("completed review does not preserve the public item set")
    for item_id, source in public.items():
        row = completed[item_id]
        if {key: value for key, value in row.items() if key != "review"} != source:
            raise ValueError(f"{item_id}: public blind content changed")
        review = row.get("review")
        if not isinstance(review, dict) or review.get("overallPreference") not in {"A", "B", "tie"}:
            raise ValueError(f"{item_id}: invalid review")
        for side in ("candidateA", "candidateB"):
            scores = review.get(side)
            if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
                raise ValueError(f"{item_id}: invalid scores")
            if any(type(scores[name]) is not int or not 1 <= scores[name] <= 5 for name in DIMENSIONS):
                raise ValueError(f"{item_id}: invalid score value")
        if not isinstance(review.get("reason"), str) or not review["reason"].strip():
            raise ValueError(f"{item_id}: reason is required")
    receipt = read_json(receipt_path)
    if (
        receipt.get("status") != "ACCEPTED_BLIND_REVIEW_SUBMISSION"
        or receipt.get("sealedMappingRead") is not False
        or receipt.get("publicPacketSha256") != sha256_file(public_path)
        or receipt.get("normalizedOutputSha256") != sha256_file(completed_path)
    ):
        raise ValueError("intake receipt does not bind the completed review")
    return completed


def unblind(
    *,
    pack_dir: Path,
    reviewer01_completed: Path,
    reviewer02_completed: Path,
    reviewer01_receipt: Path,
    reviewer02_receipt: Path,
    attestation_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError("refusing to overwrite unblind output")
    attestation = read_json(attestation_path)
    validate_attestation(
        attestation,
        reviewer01_completed=reviewer01_completed,
        reviewer02_completed=reviewer02_completed,
        reviewer01_receipt=reviewer01_receipt,
        reviewer02_receipt=reviewer02_receipt,
    )
    package_receipt = read_json(pack_dir / "receipt.json")
    public_paths = {
        "reviewer01": pack_dir / "reviewer01.jsonl",
        "reviewer02": pack_dir / "reviewer02.jsonl",
    }
    if package_receipt.get("reviewer01Sha256") != sha256_file(public_paths["reviewer01"]):
        raise ValueError("reviewer01 public packet hash mismatch")
    if package_receipt.get("reviewer02Sha256") != sha256_file(public_paths["reviewer02"]):
        raise ValueError("reviewer02 public packet hash mismatch")
    reviews = {
        "reviewer01": validate_completed(
            public_paths["reviewer01"], reviewer01_completed, reviewer01_receipt
        ),
        "reviewer02": validate_completed(
            public_paths["reviewer02"], reviewer02_completed, reviewer02_receipt
        ),
    }

    # The sealed mapping is intentionally read only after all attestations,
    # hashes, and completed-review receipts have passed.
    mapping_path = pack_dir / "SEALED_DO_NOT_SHARE.json"
    if package_receipt.get("sealedMappingSha256") != sha256_file(mapping_path):
        raise ValueError("sealed mapping hash mismatch")
    mapping_value = read_json(mapping_path)
    mapping_rows = mapping_value.get("items")
    if not isinstance(mapping_rows, list) or not mapping_rows:
        raise ValueError("sealed mapping has no items")
    mappings = {row.get("itemId"): row for row in mapping_rows}
    if len(mappings) != len(mapping_rows) or any(set(value) != set(mappings) for value in reviews.values()):
        raise ValueError("sealed mapping and completed item sets differ")

    totals = {arm: {dimension: 0 for dimension in DIMENSIONS} for arm in ARMS}
    counts = Counter({arm: 0 for arm in ARMS})
    preferences = Counter({"CTX1b": 0, "MA1": 0, "tie": 0})
    items: list[dict[str, Any]] = []
    disagreements: list[str] = []
    for item_id in sorted(mappings):
        mapping = mappings[item_id]
        labels: dict[str, dict[str, str]] = {}
        for reviewer in ("reviewer01", "reviewer02"):
            value = mapping.get(reviewer)
            if not isinstance(value, dict) or set(value) != {"A", "B"} or set(value.values()) != set(ARMS):
                raise ValueError(f"{item_id}: invalid mapping for {reviewer}")
            labels[reviewer] = value
        if any(
            labels["reviewer01"][side]
            != labels["reviewer02"]["B" if side == "A" else "A"]
            for side in ("A", "B")
        ):
            raise ValueError(f"{item_id}: packets are not mirrored")
        item_preferences: dict[str, str] = {}
        item_scores: dict[str, dict[str, dict[str, int]]] = {}
        for reviewer in ("reviewer01", "reviewer02"):
            review = reviews[reviewer][item_id]["review"]
            item_scores[reviewer] = {}
            for side in ("A", "B"):
                arm = labels[reviewer][side]
                score = review[f"candidate{side}"]
                item_scores[reviewer][arm] = score
                counts[arm] += 1
                for dimension in DIMENSIONS:
                    totals[arm][dimension] += score[dimension]
            raw = review["overallPreference"]
            preference = labels[reviewer][raw] if raw in {"A", "B"} else "tie"
            item_preferences[reviewer] = preference
            preferences[preference] += 1
        agreed = item_preferences["reviewer01"] == item_preferences["reviewer02"]
        if not agreed:
            disagreements.append(item_id)
        items.append(
            {
                "itemId": item_id,
                "scenarioId": mapping["scenarioId"],
                "preferences": item_preferences,
                "preferenceAgreement": agreed,
                "scoresByReviewerAndArm": item_scores,
            }
        )

    automatic_summary_path = pack_dir.parent / "summary.json"
    automatic_summary = read_json(automatic_summary_path)
    status = (
        "HOLD_PENDING_THIRD_BLIND_ADJUDICATION"
        if disagreements
        else "HUMAN_BLIND_REVIEW_COMPLETE"
    )
    result = {
        "schemaVersion": "context-multiagent-human-blind-unblind-v1",
        "status": status,
        "reviewerCount": 2,
        "itemCount": len(items),
        "preferenceCountsAcrossReviews": dict(preferences),
        "dimensionMeans": {
            arm: {
                dimension: round(totals[arm][dimension] / counts[arm], 4)
                for dimension in DIMENSIONS
            }
            for arm in ARMS
        },
        "preferenceAgreement": {
            "agreedItems": len(items) - len(disagreements),
            "itemCount": len(items),
            "rate": round((len(items) - len(disagreements)) / len(items), 4),
            "disagreementItemIds": disagreements,
        },
        "items": items,
        "automaticResultHash": automatic_summary["resultHash"],
        "automaticEngineeringDecisionUnchanged": automatic_summary["engineeringDecision"],
        "effectivenessDecisionUnchanged": automatic_summary["effectivenessDecision"],
        "productionDefaultDecisionUnchanged": automatic_summary["productionDefaultDecision"],
        "claimBoundary": {
            "descriptiveHumanPreferenceOnly": True,
            "overridesAutomaticSafetyOrEngineeringGate": False,
            "provesStatisticalSignificance": False,
            "authorizesProductionDefault": False,
        },
        "hashes": {
            "packageReceiptSha256": sha256_file(pack_dir / "receipt.json"),
            "sealedMappingSha256": sha256_file(mapping_path),
            "reviewer01CompletedSha256": sha256_file(reviewer01_completed),
            "reviewer02CompletedSha256": sha256_file(reviewer02_completed),
            "reviewer01ReceiptSha256": sha256_file(reviewer01_receipt),
            "reviewer02ReceiptSha256": sha256_file(reviewer02_receipt),
            "attestationSha256": sha256_file(attestation_path),
            "scorerSha256": sha256_file(Path(__file__).resolve()),
        },
    }
    result["resultHash"] = json_hash(result)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "unblinded-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if disagreements:
        reviewer01_public = {row["itemId"]: row for row in read_jsonl(public_paths["reviewer01"])}
        adjudication_rows = [reviewer01_public[item_id] for item_id in disagreements]
        (output_dir / "adjudicator.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in adjudication_rows),
            encoding="utf-8",
            newline="\n",
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", required=True, type=Path)
    parser.add_argument("--reviewer01-completed", required=True, type=Path)
    parser.add_argument("--reviewer02-completed", required=True, type=Path)
    parser.add_argument("--reviewer01-receipt", required=True, type=Path)
    parser.add_argument("--reviewer02-receipt", required=True, type=Path)
    parser.add_argument("--attestation", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = unblind(
        pack_dir=args.pack_dir,
        reviewer01_completed=args.reviewer01_completed,
        reviewer02_completed=args.reviewer02_completed,
        reviewer01_receipt=args.reviewer01_receipt,
        reviewer02_receipt=args.reviewer02_receipt,
        attestation_path=args.attestation,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
