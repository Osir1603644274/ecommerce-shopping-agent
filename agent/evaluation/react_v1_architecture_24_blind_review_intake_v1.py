"""Validate and normalize one human blind-review submission without unblinding."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "constraintFidelity",
    "evidenceDiscipline",
    "taskProgression",
    "usefulness",
)
PREFERENCES = {"A", "B", "tie", "unjudgeable"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: expected object")
        rows.append(value)
    return rows


def _read_submission(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if blocks:
        values = [json.loads(block) for block in blocks]
        if not all(isinstance(value, dict) for value in values):
            raise ValueError("each fenced block must contain one JSON object")
        return values
    return _read_jsonl(path)


def _public_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "review"}


def validate_submission(
    *, public_packet: Path, submission: Path, reviewer_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    public_rows = _read_jsonl(public_packet)
    submitted_rows = _read_submission(submission)
    if not public_rows or len(submitted_rows) != len(public_rows):
        raise ValueError("submission must contain the same complete non-empty item set")
    public = {row.get("itemId"): row for row in public_rows}
    submitted = {row.get("itemId"): row for row in submitted_rows}
    if None in public or None in submitted or set(public) != set(submitted):
        raise ValueError("submission item identities do not match the public packet")
    if len(public) != len(public_rows) or len(submitted) != len(submitted_rows):
        raise ValueError("duplicate blind item identity")

    normalized: list[dict[str, Any]] = []
    preferences: dict[str, int] = {value: 0 for value in sorted(PREFERENCES)}
    submission_format = "full_packet"
    for source in public_rows:
        item_id = source["itemId"]
        row = submitted[item_id]
        if set(row) == {"itemId", "review"}:
            submission_format = "review_only_overlay"
        elif _public_projection(row) != _public_projection(source):
            raise ValueError(f"{item_id}: public candidate or rubric content changed")
        review = row.get("review")
        if not isinstance(review, dict):
            raise ValueError(f"{item_id}: review is missing")
        scores = review.get("scores")
        if scores is None and {"candidateA", "candidateB"} <= set(review):
            scores = {
                "candidateA": review.get("candidateA"),
                "candidateB": review.get("candidateB"),
            }
        if not isinstance(scores, dict) or set(scores) != {"candidateA", "candidateB"}:
            raise ValueError(f"{item_id}: invalid score candidates")
        for candidate in ("candidateA", "candidateB"):
            values = scores.get(candidate)
            if not isinstance(values, dict) or set(values) != set(DIMENSIONS):
                raise ValueError(f"{item_id}: invalid score dimensions")
            if any(type(values[name]) is not int or not 1 <= values[name] <= 5 for name in DIMENSIONS):
                raise ValueError(f"{item_id}: scores must be integers from 1 to 5")
        preference = review.get("overallPreference")
        if preference not in PREFERENCES:
            raise ValueError(f"{item_id}: invalid overallPreference")
        if type(review.get("rationale")) is not str or not review["rationale"].strip():
            raise ValueError(f"{item_id}: rationale is required")
        preferences[preference] += 1
        normalized_row = dict(source)
        normalized_row["review"] = {
            "scores": scores,
            "overallPreference": preference,
            "rationale": review["rationale"].strip(),
        }
        normalized.append(normalized_row)

    receipt = {
        "schemaVersion": "react-v1-architecture-24-blind-review-intake-v1",
        "reviewerId": reviewer_id,
        "validatedAt": datetime.now(timezone.utc).isoformat(),
        "status": "ACCEPTED_BLIND_REVIEW_SUBMISSION",
        "itemCount": len(normalized),
        "submissionFormat": submission_format,
        "publicContentUnchanged": True,
        "publicContentBoundByPacketHash": True,
        "sealedMappingRead": False,
        "preferenceCountsStillBlinded": preferences,
        "publicPacketSha256": _sha256(public_packet),
        "submissionSha256": _sha256(submission),
    }
    return normalized, receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-packet", required=True, type=Path)
    parser.add_argument("--submission", required=True, type=Path)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--normalized-output", required=True, type=Path)
    parser.add_argument("--receipt-output", required=True, type=Path)
    args = parser.parse_args()
    normalized, receipt = validate_submission(
        public_packet=args.public_packet,
        submission=args.submission,
        reviewer_id=args.reviewer_id,
    )
    args.normalized_output.parent.mkdir(parents=True, exist_ok=True)
    args.normalized_output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in normalized),
        encoding="utf-8",
        newline="\n",
    )
    receipt["normalizedOutputSha256"] = _sha256(args.normalized_output)
    args.receipt_output.parent.mkdir(parents=True, exist_ok=True)
    args.receipt_output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
