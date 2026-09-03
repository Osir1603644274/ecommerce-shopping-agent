"""Validate completed independent review and write a non-gold disagreement audit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from agent.evaluation.used_phone_human_qrel_phase_v1 import (
    audit_completed_second_human_reviews,
    write_json,
)
from agent.evaluation.used_phone_human_qrel_v1 import read_jsonl


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    annotation = repository / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v1"
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--first-review",
        type=Path,
        default=annotation / "review_batches" / "batch_001.jsonl",
    )
    parser.add_argument(
        "--second-review",
        type=Path,
        default=annotation / "review_batches" / "second_human_completed_batch_001.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=annotation / "artifacts" / "second_human_agreement_audit_v1.json",
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite existing audit: {args.output}")
    try:
        second_rows = json.loads(args.second_review.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read completed second review: {exc}")
    if not isinstance(second_rows, list):
        parser.error("completed second review must be a JSON array")
    schema_path = (
        repository / "agent" / "evaluation" / "schemas"
        / "used_phone_human_qrel_second_review_completed_v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = [
        error
        for row in second_rows
        for error in validator.iter_errors(row)
    ]
    if errors:
        first = errors[0]
        parser.error(
            "completed second review failed schema validation: "
            f"{list(first.absolute_path)}: {first.message}"
        )
    audit = audit_completed_second_human_reviews(
        read_jsonl(args.first_review),
        second_rows,
    )
    write_json(args.output, audit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
