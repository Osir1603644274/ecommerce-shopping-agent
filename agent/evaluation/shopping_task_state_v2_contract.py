"""Public-safe schemas and validation helpers for Shopping Task State V2."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
SCHEMA_PATHS = {
    "prediction": SCHEMA_DIR / "shopping_task_state_prediction_v2.schema.json",
    "receipt": SCHEMA_DIR / "shopping_task_state_runner_receipt_v2.schema.json",
    "report": SCHEMA_DIR / "shopping_task_state_score_report_v2.schema.json",
}


def validator_for(kind: str) -> Draft202012Validator:
    try:
        path = SCHEMA_PATHS[kind]
    except KeyError as exc:
        raise ValueError(f"unknown Shopping Task State contract kind: {kind}") from exc
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_record(record: Mapping[str, Any], kind: str) -> None:
    errors = sorted(validator_for(kind).iter_errors(record), key=lambda item: list(item.path))
    if errors:
        path = "/".join(str(part) for part in errors[0].path) or "<root>"
        raise ValueError(f"invalid {kind} at {path}: {errors[0].message}")
