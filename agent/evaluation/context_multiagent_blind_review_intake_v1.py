"""Validate one Context/Multi-Agent human review without reading sealed mapping."""

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
PREFERENCES = {"A", "B", "tie"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: expected object")
        rows.append(value)
    return rows


def validate_submission(
    *, public_packet: Path, submission: Path, reviewer_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    public_rows = read_jsonl(public_packet)
    submitted_rows = read_jsonl(submission)
    if not public_rows or len(public_rows) != len(submitted_rows):
        raise ValueError("submission must preserve the complete non-empty item set")
    public = {row.get("itemId"): row for row in public_rows}
    submitted = {row.get("itemId"): row for row in submitted_rows}
    if (
        None in public
        or None in submitted
        or len(public) != len(public_rows)
        or len(submitted) != len(submitted_rows)
        or set(public) != set(submitted)
    ):
        raise ValueError("blind item identities are missing, duplicated, or changed")

    normalized: list[dict[str, Any]] = []
    preferences = {value: 0 for value in sorted(PREFERENCES)}
    for source in public_rows:
        item_id = source["itemId"]
        row = submitted[item_id]
        if set(row) != {"itemId", "review"}:
            raise ValueError(f"{item_id}: only review-only overlays are accepted")
        review = row.get("review")
        if not isinstance(review, dict):
            raise ValueError(f"{item_id}: review is missing")
        for side in ("candidateA", "candidateB"):
            scores = review.get(side)
            if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
                raise ValueError(f"{item_id}: {side} dimensions are invalid")
            if any(
                type(scores[name]) is not int or not 1 <= scores[name] <= 5
                for name in DIMENSIONS
            ):
                raise ValueError(f"{item_id}: scores must be integer 1..5")
        preference = review.get("overallPreference")
        if preference not in PREFERENCES:
            raise ValueError(f"{item_id}: overallPreference is invalid")
        reason = review.get("reason", review.get("rationale"))
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{item_id}: reason is required")
        preferences[preference] += 1
        normalized.append(
            {
                **source,
                "review": {
                    "candidateA": review["candidateA"],
                    "candidateB": review["candidateB"],
                    "overallPreference": preference,
                    "reason": reason.strip(),
                },
            }
        )

    receipt = {
        "schemaVersion": "context-multiagent-blind-review-intake-v1",
        "status": "ACCEPTED_BLIND_REVIEW_SUBMISSION",
        "reviewerId": reviewer_id,
        "validatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "itemCount": len(normalized),
        "publicContentUnchanged": True,
        "sealedMappingRead": False,
        "preferenceCountsStillBlinded": preferences,
        "publicPacketSha256": sha256_file(public_packet),
        "submissionSha256": sha256_file(submission),
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
    receipt["normalizedOutputSha256"] = sha256_file(args.normalized_output)
    args.receipt_output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
