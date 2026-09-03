"""Merge completed human review records into the product qrel dataset.

The command fails closed: pending, AI-authored, or UNKNOWN judgments cannot be
converted to numeric relevance grades.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


AGENT_ROOT = Path(__file__).resolve().parents[1]
GRADE_BY_RELEVANCE = {
    "perfect_match": 3,
    "acceptable_alternative": 2,
    "partial_match": 1,
    "not_relevant": 0,
}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def merge_reviews(
    qrels: list[dict[str, Any]], reviews: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    qrels_by_id = {row["queryId"]: dict(row) for row in qrels}
    errors: list[str] = []
    for review in reviews:
        query_id = review.get("queryId")
        if query_id not in qrels_by_id:
            errors.append(f"{query_id}: query does not exist in qrels")
            continue
        if review.get("reviewStatus") != "reviewed" or review.get("humanConfirmed") is not True:
            errors.append(f"{query_id}: review is not human-confirmed")
            continue
        if not review.get("reviewerId") or not review.get("reviewedAt"):
            errors.append(f"{query_id}: reviewerId and reviewedAt are required")
            continue
        judgments = review.get("judgments") or []
        if not judgments:
            errors.append(f"{query_id}: at least one judgment is required")
            continue
        converted = []
        for judgment in judgments:
            relevance = judgment.get("relevance")
            if relevance == "unknown":
                errors.append(
                    f"{query_id}/{judgment.get('productId')}: UNKNOWN is a data blocker, not a grade"
                )
                continue
            if relevance not in GRADE_BY_RELEVANCE:
                errors.append(
                    f"{query_id}/{judgment.get('productId')}: invalid relevance {relevance!r}"
                )
                continue
            converted.append({
                "productId": int(judgment["productId"]),
                "grade": GRADE_BY_RELEVANCE[relevance],
                "labelSource": "human",
                "note": str(judgment.get("note") or ""),
            })
        if len(converted) != len(judgments):
            continue
        qrel = qrels_by_id[query_id]
        qrel["reviewStatus"] = "human_confirmed"
        qrel["reviewedBy"] = review["reviewerId"]
        qrel["reviewedAt"] = review["reviewedAt"]
        qrel["judgments"] = converted

    if errors:
        raise ValueError("\n".join(errors))
    return [qrels_by_id[row["queryId"]] for row in qrels]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--qrels",
        type=Path,
        default=AGENT_ROOT / "evaluation/product_qrel_draft.jsonl",
    )
    parser.add_argument(
        "--reviews",
        type=Path,
        default=AGENT_ROOT / "evaluation/product_qrel_review_batch_01.jsonl",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    merged = merge_reviews(_jsonl(args.qrels), _jsonl(args.reviews))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in merged) + "\n",
        encoding="utf-8",
    )
    confirmed = sum(row["reviewStatus"] == "human_confirmed" for row in merged)
    print(json.dumps({"output": str(args.output), "humanConfirmedCount": confirmed}))


if __name__ == "__main__":
    main()
