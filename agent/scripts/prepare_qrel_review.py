#!/usr/bin/env python
"""Prepare a human-review batch from the qrel draft pool.

Reads evaluation/product_qrel_draft.jsonl, selects the first 30 validation-split
queries (10 phone, 10 laptop, 10 headphones), and writes a review workbook at
evaluation/product_qrel_review_batch_01.jsonl.

The output is a flat JSONL file where each line is one review record containing:
  - The original qrel queryId, category, query text, and requirements
  - An empty review template (judgments, humanConfirmed, reviewerNotes)
  - reviewBatch "001" to avoid confusing AI-prep with human labels

This script does NOT write to the original qrel file and does NOT mark anything
as humanConfirmed.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


REVIEW_BATCH_SIZE = 30
REVIEW_BATCH = "001"


def load_qrel_drafts(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def select_review_candidates(drafts: list[dict], batch_size: int = REVIEW_BATCH_SIZE) -> list[dict]:
    """Select the first batch_size validation-split queries, balanced across categories.

    Each category gets at most batch_size // num_categories queries, with remaining
    slots distributed round-robin.
    """
    validation = [d for d in drafts if d.get("split") == "validation"]

    # Group by category
    by_category: dict[str, list[dict]] = {}
    for d in validation:
        cat = d.get("category", "other")
        by_category.setdefault(cat, []).append(d)

    # Sort each group by queryId for stable selection
    for cat in by_category:
        by_category[cat].sort(key=lambda d: d.get("queryId", ""))

    num_categories = len(by_category)
    per_category = batch_size // num_categories  # 10 each for 3 categories
    remainder = batch_size % num_categories

    selected: list[dict] = []
    for cat in sorted(by_category):  # deterministic order
        limit = per_category + (1 if remainder > 0 else 0)
        remainder = max(0, remainder - 1)
        selected.extend(by_category[cat][:limit])

    return selected


def build_review_record(original: dict) -> dict:
    """Wrap one qrel draft into a review record with blank judgment fields."""
    return {
        "schemaVersion": "product-qrel-review-v1",
        "queryId": original["queryId"],
        "reviewBatch": REVIEW_BATCH,
        "language": original.get("language", "zh-CN"),
        "category": original.get("category", ""),
        "split": original.get("split", ""),
        "originalQuery": original["query"],
        "originalRequirements": original.get("requirements", []),
        "catalogSource": original.get("catalogSource", "KuaiSearch"),
        # Review fields — ALL blank, ready for human fill-in
        "reviewStatus": "pending_review",
        "humanConfirmed": False,
        "reviewerId": "",
        "reviewedAt": None,
        "reviewerNotes": "",
        "judgments": [],  # human fills this in
        "systemHint": (
            "请审核本查询的需求是否准确，并逐条添加 judgments（productId + relevance grade）。"
            "relevance 取值: perfect_match / partial_match / not_relevant / unknown。"
            "确认后设 humanConfirmed=true, reviewStatus=reviewed 并填写 reviewerId 和 reviewedAt。"
        ),
        "preparedAt": datetime.now(timezone.utc).isoformat(),
        "preparedBy": "AI — deterministic selection, no human labels claimed",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a human-review batch from qrel drafts"
    )
    parser.add_argument(
        "--input", type=Path,
        default=Path(__file__).parents[1] / "evaluation/product_qrel_draft.jsonl",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).parents[1] / "evaluation/product_qrel_review_batch_01.jsonl",
    )
    parser.add_argument(
        "--batch-size", type=int, default=REVIEW_BATCH_SIZE,
    )
    args = parser.parse_args()

    drafts = load_qrel_drafts(args.input)
    selected = select_review_candidates(drafts, batch_size=args.batch_size)
    review_records = [build_review_record(d) for d in selected]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in review_records) + "\n",
        encoding="utf-8",
    )

    categories = {}
    for r in review_records:
        cat = r["category"]
        categories[cat] = categories.get(cat, 0) + 1

    print(json.dumps(
        {
            "output": str(args.output),
            "totalSelected": len(review_records),
            "byCategory": categories,
            "reviewBatch": REVIEW_BATCH,
            "warning": "DO NOT mark any record as humanConfirmed — review must be done by a human",
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
