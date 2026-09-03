"""Explicit one-shot offline freeze for human-adjudicated sealed Qrels."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from agent.evaluation.used_phone_human_qrel_phase_v1 import (
    freeze_adjudicated_sealed_human_qrels,
    write_json,
)
from agent.evaluation.used_phone_human_qrel_v1 import read_jsonl


def _validate(schema_path: Path, values: list[object]) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = [error for value in values for error in validator.iter_errors(value)]
    if errors:
        first = errors[0]
        raise ValueError(f"{list(first.absolute_path)}: {first.message}")


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    annotation = repository / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v1"
    schemas = repository / "agent" / "evaluation" / "schemas"
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-one-shot-sealed-freeze", action="store_true")
    parser.add_argument("--first-review", type=Path, default=annotation / "review_batches" / "batch_001.jsonl")
    parser.add_argument("--second-review", type=Path, default=annotation / "review_batches" / "second_human_completed_batch_001.json")
    parser.add_argument("--adjudication", type=Path, default=annotation / "review_batches" / "human_adjudication_batch_001.json")
    parser.add_argument("--output", type=Path, default=annotation / "sealed" / "adjudicated_sealed_qrels_v1.json")
    args = parser.parse_args()
    if not args.authorize_one_shot_sealed_freeze:
        parser.error("explicit --authorize-one-shot-sealed-freeze is required")
    if args.output.exists():
        parser.error(f"refusing to overwrite existing sealed freeze: {args.output}")
    try:
        second = json.loads(args.second_review.read_text(encoding="utf-8"))
        adjudication = json.loads(args.adjudication.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read sealed human inputs: {exc}")
    if not isinstance(second, list) or not isinstance(adjudication, dict):
        parser.error("sealed human inputs have invalid top-level types")
    try:
        _validate(
            schemas / "used_phone_human_qrel_second_review_completed_v1.schema.json",
            second,
        )
        _validate(
            schemas / "used_phone_human_qrel_adjudication_v1.schema.json",
            [adjudication],
        )
        frozen = freeze_adjudicated_sealed_human_qrels(
            read_jsonl(args.first_review), second, adjudication
        )
    except ValueError as exc:
        parser.error(str(exc))
    write_json(args.output, frozen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
