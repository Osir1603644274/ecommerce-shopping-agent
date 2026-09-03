"""Public-only loader seam for the Commerce Benchmark V1 runner.

This module deliberately has no import path into the scorer, oracle, fault,
prediction, receipt, or authoring modules.  It accepts exactly one public
JSONL filename and reads only that file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class PublicOnlyLoadError(ValueError):
    """Raised when a runner-facing path is not the public input artifact."""


def _assert_public_input_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.name != "scenario.input.jsonl":
        raise PublicOnlyLoadError("public loader accepts only scenario.input.jsonl")
    blocked = {"private", "run", "oracle", "fault", "receipt", "prediction", "authoring"}
    if any(part.lower() in blocked for part in resolved.parts):
        raise PublicOnlyLoadError("public loader refuses private or run artifact paths")
    return resolved


def load_public_inputs(path: Path | str) -> tuple[Mapping[str, Any], ...]:
    """Read and minimally shape-check only the public scenario JSONL."""
    resolved = _assert_public_input_path(Path(path))
    rows: list[Mapping[str, Any]] = []
    with resolved.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PublicOnlyLoadError(f"invalid public JSON at line {line_number}") from exc
            if not isinstance(value, Mapping):
                raise PublicOnlyLoadError(f"public row {line_number} is not an object")
            if not isinstance(value.get("scenarioId"), str) or not isinstance(value.get("turns"), list) or not value["turns"]:
                raise PublicOnlyLoadError(f"public row {line_number} has invalid scenario shape")
            rows.append(value)
    if not rows:
        raise PublicOnlyLoadError("public input file is empty")
    return tuple(rows)
