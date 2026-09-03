"""Finalize the V6 two-human mirrored blind review without changing frozen inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from shopping_task_state_context_ab_v4_blind_scorer import DIMENSIONS, score_reviews


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: expected object")
        rows.append(row)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    )
    path.write_text(payload, encoding="utf-8", newline="\n")


def _complete_review(
    public_path: Path,
    sidecar_path: Path,
    completed_path: Path,
) -> None:
    public_rows = _read_jsonl(public_path)
    sidecar_rows = _read_jsonl(sidecar_path)
    if len(public_rows) != len(sidecar_rows) or not public_rows:
        raise ValueError("sidecar must preserve every public item")
    public_by_id = {row.get("itemId"): row for row in public_rows}
    sidecar_by_id = {row.get("itemId"): row for row in sidecar_rows}
    if (
        len(public_by_id) != len(public_rows)
        or len(sidecar_by_id) != len(sidecar_rows)
        or set(public_by_id) != set(sidecar_by_id)
    ):
        raise ValueError("review item IDs are missing or duplicated")
    public_sha = _sha256(public_path)
    completed: list[dict[str, Any]] = []
    for public_row in public_rows:
        item_id = public_row["itemId"]
        sidecar = sidecar_by_id[item_id]
        if sidecar.get("sourcePublicSha256") != public_sha:
            raise ValueError(f"{item_id}: sidecar source hash mismatch")
        review = sidecar.get("review")
        if not isinstance(review, dict):
            raise ValueError(f"{item_id}: missing review")
        if review.get("overallPreference") not in {"A", "B", "tie", "unjudgeable"}:
            raise ValueError(f"{item_id}: invalid preference")
        for side in ("candidateA", "candidateB"):
            scores = review.get(side)
            if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
                raise ValueError(f"{item_id}: invalid {side} scores")
            if any(type(scores[name]) is not int or not 1 <= scores[name] <= 5 for name in DIMENSIONS):
                raise ValueError(f"{item_id}: score outside 1..5")
        row = dict(public_row)
        row["review"] = review
        completed.append(row)
    _write_jsonl(completed_path, completed)


def _validate_attestation(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    reviewers = value.get("reviewers")
    if not isinstance(reviewers, list) or len(reviewers) != 2:
        raise ValueError("exactly two reviewer attestations are required")
    if {row.get("reviewerSlot") for row in reviewers if isinstance(row, dict)} != {"01", "02"}:
        raise ValueError("reviewer slots must be 01 and 02")
    required = (
        "humanReviewer",
        "independentReview",
        "assignedJsonlOnlyBeforeScoring",
        "sealedMappingNotViewedBeforeScoring",
    )
    if any(not isinstance(row, dict) or not all(row.get(key) is True for key in required) for row in reviewers):
        raise ValueError("human, independent, assigned-only and mapping-blind attestations are required")
    if value.get("unblindingAllowed") is not True:
        raise ValueError("attestation does not allow unblinding")
    return value


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    _validate_attestation(args.attestation)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    completed_one = args.output_dir / "reviewer-01.jsonl"
    completed_two = args.output_dir / "reviewer-02.jsonl"
    _complete_review(args.reviewer_one_public, args.reviewer_one_sidecar, completed_one)
    _complete_review(args.reviewer_two_public, args.reviewer_two_sidecar, completed_two)

    scorer_attestation = args.output_dir / "scorer-attestation.json"
    _write_json(scorer_attestation, {
        "schemaVersion": "shopping-task-state-context-blind-scorer-attestation-v1",
        "experimentId": "shopping-task-state-context-ab-v6-20260829",
        "humanReviewerAttested": True,
        "independentReviewAttested": True,
        "mappingBlindAttested": True,
        "reviewer01CompletedSha256": _sha256(completed_one),
        "reviewer02CompletedSha256": _sha256(completed_two),
        "sourceAttestationSha256": _sha256(args.attestation),
    })
    score_path = args.output_dir / "unblinded-score.json"
    result = score_reviews(
        reviewer_one_public=args.reviewer_one_public,
        reviewer_one_completed=completed_one,
        reviewer_two_public=args.reviewer_two_public,
        reviewer_two_completed=completed_two,
        sealed_mapping=args.sealed_mapping,
        attestation=scorer_attestation,
        output=score_path,
    )
    result["schemaVersion"] = "shopping-task-state-context-blind-score-v2"
    result["experimentId"] = "shopping-task-state-context-ab-v6-20260829"
    result["productionDecision"] = "HOLD_NO_PREREGISTERED_HUMAN_WIN_THRESHOLD"
    result["productionDecisionReason"] = (
        "The two valid human reviews are descriptive. V6 names a preregistered human "
        "threshold but the frozen manifest does not define a numeric win threshold; "
        "therefore unblinding cannot itself authorize the V2 authority migration."
    )
    result["hashes"]["v6FinalizerSha256"] = _sha256(Path(__file__).resolve())
    result["hashes"]["sourceAttestationSha256"] = _sha256(args.attestation)
    _write_json(score_path, result)
    final_manifest = {
        "schemaVersion": "shopping-task-state-context-blind-final-manifest-v1",
        "experimentId": result["experimentId"],
        "status": result["status"],
        "productionDecision": result["productionDecision"],
        "files": {
            "reviewer01CompletedSha256": _sha256(completed_one),
            "reviewer02CompletedSha256": _sha256(completed_two),
            "scorerAttestationSha256": _sha256(scorer_attestation),
            "unblindedScoreSha256": _sha256(score_path),
        },
    }
    _write_json(args.output_dir / "final-manifest.json", final_manifest)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewer-one-public", required=True, type=Path)
    parser.add_argument("--reviewer-one-sidecar", required=True, type=Path)
    parser.add_argument("--reviewer-two-public", required=True, type=Path)
    parser.add_argument("--reviewer-two-sidecar", required=True, type=Path)
    parser.add_argument("--sealed-mapping", required=True, type=Path)
    parser.add_argument("--attestation", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(finalize(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
