"""Public-only loader for the manually authored Shopping Task State V2 MVP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


DATASET = Path(__file__).resolve().parent / "assets" / "shopping_task_state_v2_mvp_20260822"
PUBLIC_PATH = DATASET / "public" / "scenarios.jsonl"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "shopping_task_state_public_v2.schema.json"


def load_scenarios(path: Path = PUBLIC_PATH) -> list[dict[str, Any]]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        errors = sorted(validator.iter_errors(row), key=lambda item: list(item.path))
        if errors:
            raise ValueError(f"{path.name}:{line_number}: {errors[0].message}")
        rows.append(row)
    if len({row["scenarioId"] for row in rows}) != len(rows):
        raise ValueError("scenarioId values must be unique")
    return rows
