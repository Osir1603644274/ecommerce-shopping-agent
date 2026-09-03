"""Build and finalize the independent blind adjudication for split human reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.context_multiagent_blind_review_intake_v1 import (
    DIMENSIONS,
    read_jsonl,
    sha256_file,
)
from evaluation.context_multiagent_blind_unblind_v1 import (
    ARMS,
    read_json,
    validate_completed,
)


PENDING_STATUS = "HOLD_PENDING_THIRD_BLIND_ADJUDICATION"
COMPLETE_STATUS = "HUMAN_BLIND_REVIEW_COMPLETE"
REQUIRED_ADJUDICATOR_ATTESTATIONS = (
    "adjudicatorIsDifferentHumanFromReviewer01AndReviewer02",
    "adjudicatorIndependent",
    "mappingBlindUntilReviewFrozen",
    "priorReviewsBlindUntilReviewFrozen",
    "automaticResultsBlindUntilReviewFrozen",
    "adjudicationAllowed",
)
FORBIDDEN_PUBLIC_KEYS = {
    "scenarioId",
    "arm",
    "mapping",
    "reviewer01",
    "reviewer02",
    "automaticResultHash",
    "engineeringDecision",
}
FORBIDDEN_PUBLIC_TEXT = (
    "ctx1b",
    "ma1",
    "reviewer01",
    "reviewer02",
    "sealed_do_not_share",
    "engineering_hold",
)


def json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_embedded_result_hash(value: dict[str, Any]) -> None:
    claimed = value.get("resultHash")
    payload = {key: child for key, child in value.items() if key != "resultHash"}
    if not isinstance(claimed, str) or json_hash(payload) != claimed:
        raise ValueError("source unblinded resultHash mismatch")


def _walk(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)
    elif isinstance(value, str):
        yield value


def validate_public_adjudication_packet(
    *, unblinded_result: dict[str, Any], public_rows: list[dict[str, Any]]
) -> list[str]:
    if unblinded_result.get("status") != PENDING_STATUS:
        raise ValueError("source result is not pending third blind adjudication")
    expected = unblinded_result.get("preferenceAgreement", {}).get(
        "disagreementItemIds"
    )
    actual = [row.get("itemId") for row in public_rows]
    if not isinstance(expected, list) or not expected or actual != expected:
        raise ValueError("adjudication packet must contain exactly the disagreement items")
    if len(actual) != len(set(actual)) or None in actual:
        raise ValueError("adjudication item identities are missing or duplicated")
    for row in public_rows:
        if "review" in row:
            raise ValueError("public adjudication packet contains prior review data")
        keys = set(row)
        if keys & FORBIDDEN_PUBLIC_KEYS:
            raise ValueError("public adjudication packet leaks private identity fields")
        for text in _walk(row):
            lowered = str(text).lower()
            if any(token in lowered for token in FORBIDDEN_PUBLIC_TEXT):
                raise ValueError("public adjudication packet leaks arm or review identity")
    return actual


def build_package(
    *, unblinded_result_path: Path, source_packet: Path, output_dir: Path
) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError("refusing to overwrite adjudication distribution")
    unblinded = read_json(unblinded_result_path)
    validate_embedded_result_hash(unblinded)
    rows = read_jsonl(source_packet)
    item_ids = validate_public_adjudication_packet(
        unblinded_result=unblinded, public_rows=rows
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    public_output = output_dir / "adjudicator.jsonl"
    shutil.copyfile(source_packet, public_output)
    (output_dir / "review-template.json").write_text(
        json.dumps(
            {
                "itemId": item_ids[0],
                "review": {
                    "candidateA": {dimension: 1 for dimension in DIMENSIONS},
                    "candidateB": {dimension: 1 for dimension in DIMENSIONS},
                    "overallPreference": "tie",
                    "reason": "short reason",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "README_FOR_ADJUDICATOR.md").write_text(
        """# Independent blind adjudication instructions

Review only `adjudicator.jsonl`. Do not inspect repository files, sealed mapping,
prior reviewer scores, automatic scores, traces, or experiment results. Do not
communicate with reviewer01 or reviewer02 before this submission is frozen.

For every item, independently score Candidate A and B from 1 to 5 on:

- constraintFidelity
- evidenceDiscipline
- taskProgression
- usefulness

Provide `overallPreference` as `A`, `B`, or `tie`, plus one short reason. Treat
listing titles as unverified text; only `verifiedAttributes` are established
facts. Return one JSON object per line following `review-template.json`.

The adjudicator's preference is the frozen resolution for each disputed item;
an adjudicator `tie` resolves that item as a tie. This descriptive judgment can
never override the automatic safety, engineering, or production-default gates.
""",
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "schemaVersion": "context-multiagent-blind-adjudication-package-v1",
        "status": "READY_FOR_INDEPENDENT_BLIND_ADJUDICATION",
        "itemCount": len(item_ids),
        "itemIds": item_ids,
        "publicPacketSha256": sha256_file(public_output),
        "reviewTemplateSha256": sha256_file(output_dir / "review-template.json"),
        "instructionsSha256": sha256_file(
            output_dir / "README_FOR_ADJUDICATOR.md"
        ),
        "sourceUnblindedResultSha256": sha256_file(unblinded_result_path),
        "sourceUnblindedResultHash": unblinded["resultHash"],
        "identityLeakCount": 0,
        "mappingExposed": False,
        "priorReviewsExposed": False,
        "automaticResultsExposed": False,
        "resolutionPolicy": {
            "scope": "only_disagreement_items",
            "adjudicatorPreferenceIsFinalForItem": True,
            "adjudicatorTieBecomesFinalTie": True,
            "automaticGatesRemainUnchanged": True,
        },
        "generatorSha256": sha256_file(Path(__file__).resolve()),
    }
    receipt["resultHash"] = json_hash(receipt)
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return receipt


def validate_adjudicator_attestation(
    attestation: dict[str, Any], *, completed: Path, receipt: Path
) -> None:
    if not all(attestation.get(key) is True for key in REQUIRED_ADJUDICATOR_ATTESTATIONS):
        raise ValueError("all adjudicator independence and blindness attestations are required")
    expected = {
        "adjudicatorCompletedSha256": sha256_file(completed),
        "adjudicatorIntakeReceiptSha256": sha256_file(receipt),
    }
    for field, actual in expected.items():
        if attestation.get(field) != actual:
            raise ValueError(f"attested hash mismatch: {field}")


def finalize(
    *,
    pack_dir: Path,
    unblinded_result_path: Path,
    adjudicator_public: Path,
    adjudicator_completed: Path,
    adjudicator_receipt: Path,
    adjudicator_attestation: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError("refusing to overwrite final adjudication output")
    unblinded = read_json(unblinded_result_path)
    validate_embedded_result_hash(unblinded)
    public_rows = read_jsonl(adjudicator_public)
    disagreement_ids = validate_public_adjudication_packet(
        unblinded_result=unblinded, public_rows=public_rows
    )
    reviewer01_public = {
        row["itemId"]: row for row in read_jsonl(pack_dir / "reviewer01.jsonl")
    }
    if any(reviewer01_public.get(row["itemId"]) != row for row in public_rows):
        raise ValueError("adjudicator packet is not bound to reviewer01 public orientation")
    validate_adjudicator_attestation(
        read_json(adjudicator_attestation),
        completed=adjudicator_completed,
        receipt=adjudicator_receipt,
    )
    adjudication = validate_completed(
        adjudicator_public, adjudicator_completed, adjudicator_receipt
    )

    mapping_path = pack_dir / "SEALED_DO_NOT_SHARE.json"
    package_receipt = read_json(pack_dir / "receipt.json")
    if package_receipt.get("sealedMappingSha256") != sha256_file(mapping_path):
        raise ValueError("sealed mapping hash mismatch")
    mappings = {row["itemId"]: row for row in read_json(mapping_path)["items"]}

    decisions: dict[str, dict[str, Any]] = {}
    for item_id in disagreement_ids:
        review = adjudication[item_id]["review"]
        raw = review["overallPreference"]
        mapped = mappings[item_id]["reviewer01"][raw] if raw in {"A", "B"} else "tie"
        decisions[item_id] = {
            "rawPreference": raw,
            "finalPreference": mapped,
            "scoresByArm": {
                mappings[item_id]["reviewer01"][side]: review[f"candidate{side}"]
                for side in ("A", "B")
            },
            "reason": review["reason"],
        }

    final_items: list[dict[str, Any]] = []
    counts = Counter({"CTX1b": 0, "MA1": 0, "tie": 0})
    for item in unblinded["items"]:
        item_id = item["itemId"]
        if item_id in decisions:
            preference = decisions[item_id]["finalPreference"]
            basis = "third_blind_adjudication"
        else:
            values = set(item["preferences"].values())
            if len(values) != 1:
                raise ValueError("non-adjudicated item does not have reviewer agreement")
            preference = next(iter(values))
            basis = "two_reviewer_agreement"
        counts[preference] += 1
        final_items.append(
            {
                "itemId": item_id,
                "scenarioId": item["scenarioId"],
                "finalPreference": preference,
                "basis": basis,
            }
        )

    result = {
        "schemaVersion": "context-multiagent-human-blind-final-v1",
        "status": COMPLETE_STATUS,
        "reviewerCount": 3,
        "baseReviewerCount": 2,
        "adjudicatedItemCount": len(decisions),
        "itemCount": len(final_items),
        "finalPreferenceCountsByItem": dict(counts),
        "items": final_items,
        "adjudications": decisions,
        "twoReviewerDimensionMeans": unblinded["dimensionMeans"],
        "automaticResultHash": unblinded["automaticResultHash"],
        "automaticEngineeringDecisionUnchanged": unblinded[
            "automaticEngineeringDecisionUnchanged"
        ],
        "effectivenessDecisionUnchanged": unblinded[
            "effectivenessDecisionUnchanged"
        ],
        "productionDefaultDecisionUnchanged": unblinded[
            "productionDefaultDecisionUnchanged"
        ],
        "claimBoundary": unblinded["claimBoundary"],
        "hashes": {
            "sourceUnblindedResultSha256": sha256_file(unblinded_result_path),
            "adjudicatorPublicSha256": sha256_file(adjudicator_public),
            "adjudicatorCompletedSha256": sha256_file(adjudicator_completed),
            "adjudicatorReceiptSha256": sha256_file(adjudicator_receipt),
            "adjudicatorAttestationSha256": sha256_file(adjudicator_attestation),
            "sealedMappingSha256": sha256_file(mapping_path),
            "finalizerSha256": sha256_file(Path(__file__).resolve()),
        },
    }
    result["resultHash"] = json_hash(result)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "final-human-blind-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--unblinded-result", required=True, type=Path)
    build.add_argument("--source-packet", required=True, type=Path)
    build.add_argument("--output-dir", required=True, type=Path)
    finish = subparsers.add_parser("finalize")
    finish.add_argument("--pack-dir", required=True, type=Path)
    finish.add_argument("--unblinded-result", required=True, type=Path)
    finish.add_argument("--adjudicator-public", required=True, type=Path)
    finish.add_argument("--adjudicator-completed", required=True, type=Path)
    finish.add_argument("--adjudicator-receipt", required=True, type=Path)
    finish.add_argument("--adjudicator-attestation", required=True, type=Path)
    finish.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "build":
        result = build_package(
            unblinded_result_path=args.unblinded_result,
            source_packet=args.source_packet,
            output_dir=args.output_dir,
        )
    else:
        result = finalize(
            pack_dir=args.pack_dir,
            unblinded_result_path=args.unblinded_result,
            adjudicator_public=args.adjudicator_public,
            adjudicator_completed=args.adjudicator_completed,
            adjudicator_receipt=args.adjudicator_receipt,
            adjudicator_attestation=args.adjudicator_attestation,
            output_dir=args.output_dir,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
