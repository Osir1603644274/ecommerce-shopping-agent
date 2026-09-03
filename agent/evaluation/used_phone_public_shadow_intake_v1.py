"""Validate and freeze unlabeled, self-declared human used-phone queries.

This module is deliberately not a benchmark generator.  It accepts only direct
human entries with explicit provenance/privacy declarations, rejects labels and
AI rewriting, and emits a canonical public shadow bundle.  A successful freeze
proves intake integrity, not that the author identity was independently verified.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


SCHEMA_VERSION = "used-phone-public-shadow-bundle-v1"
ROW_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "used_phone_public_shadow_intake_v1.schema.json"
)
CASE_SCHEMA_VERSION = "used-phone-public-shadow-case-v1"
LABEL_BOUNDARY = (
    "unlabeled self-declared human-original input; author identity not "
    "independently verified"
)
_DIRECT_IDENTIFIER_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
)


class ShadowIntakeError(ValueError):
    """Fail-closed public shadow intake rejection."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(ROW_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _load_rows(path: Path) -> tuple[list[dict[str, Any]], bytes]:
    resolved = path.resolve()
    if resolved.name != "submissions.jsonl" or not resolved.is_file():
        raise ShadowIntakeError("input must be a submissions.jsonl file")
    payload = resolved.read_bytes()
    if not payload or not payload.endswith(b"\n"):
        raise ShadowIntakeError("submissions must be non-empty newline-terminated JSONL")
    try:
        rows = [json.loads(line) for line in payload.splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowIntakeError("submissions must be valid UTF-8 JSONL") from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise ShadowIntakeError("every submission must be a JSON object")
    return rows, payload


def _validate_semantics(rows: list[dict[str, Any]]) -> None:
    validator = _schema_validator()
    submission_ids: set[str] = set()
    content_ids: set[str] = set()
    for index, row in enumerate(rows, 1):
        errors = sorted(validator.iter_errors(row), key=lambda item: list(item.path))
        if errors:
            first = errors[0]
            path = ".".join(map(str, first.path)) or "$"
            raise ShadowIntakeError(f"row {index} schema error at {path}: {first.message}")
        submission_id = row["submissionId"]
        if submission_id in submission_ids:
            raise ShadowIntakeError(f"duplicate submissionId: {submission_id}")
        submission_ids.add(submission_id)

        try:
            captured = datetime.fromisoformat(row["source"]["capturedAt"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ShadowIntakeError(f"row {index} capturedAt is invalid") from exc
        if captured.tzinfo is None or captured.utcoffset() is None:
            raise ShadowIntakeError(f"row {index} capturedAt must include a timezone")

        turns = row["turns"]
        expected_turn_ids = [f"turn-{turn_index}" for turn_index in range(1, len(turns) + 1)]
        if [turn["turnId"] for turn in turns] != expected_turn_ids:
            raise ShadowIntakeError(f"row {index} turnIds must be contiguous and ordered")
        texts = [turn["text"] for turn in turns]
        if any(text != text.strip() for text in texts):
            raise ShadowIntakeError(f"row {index} turn text must not have outer whitespace")
        if any("\x00" in text or "\r" in text or "\n" in text for text in texts):
            raise ShadowIntakeError(f"row {index} turn text must be one logical line")
        joined = "\n".join(texts)
        if any(pattern.search(joined) for pattern in _DIRECT_IDENTIFIER_PATTERNS):
            raise ShadowIntakeError(f"row {index} contains a direct identifier pattern")
        content_id = sha256_bytes(canonical_bytes(texts))
        if content_id in content_ids:
            raise ShadowIntakeError(f"row {index} duplicates another turn sequence")
        content_ids.add(content_id)


def materialize_bundle(input_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, source_bytes = _load_rows(input_path)
    _validate_semantics(rows)
    cases: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        cases.append({
            "schemaVersion": CASE_SCHEMA_VERSION,
            "caseId": f"UPSH-H{index:04d}",
            "sourceSubmissionId": row["submissionId"],
            "sourceClass": "self_declared_human_original",
            "sourceRelationship": row["source"]["relationship"],
            "transformation": row["source"]["transformation"],
            "capturedAt": row["source"]["capturedAt"],
            "labelBoundary": LABEL_BOUNDARY,
            "turns": row["turns"],
        })
    case_payload = b"".join(canonical_bytes(case) for case in cases)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "status": "FROZEN_UNLABELED_PUBLIC_SHADOW_INPUT",
        "labelBoundary": LABEL_BOUNDARY,
        "caseCount": len(cases),
        "orderedCaseIds": [case["caseId"] for case in cases],
        "input": {
            "path": "submissions.jsonl",
            "sha256": sha256_bytes(source_bytes),
        },
        "cases": {
            "path": "cases_public.jsonl",
            "sha256": sha256_bytes(case_payload),
        },
        "sourceRelationshipCounts": dict(sorted(Counter(
            row["source"]["relationship"] for row in rows
        ).items())),
        "transformationCounts": dict(sorted(Counter(
            row["source"]["transformation"] for row in rows
        ).items())),
        "containsLabels": False,
        "containsExpectedOutcomes": False,
        "independentlyVerifiedHumanIdentity": False,
    }
    return cases, manifest


def freeze_bundle(input_path: Path, output_dir: Path) -> dict[str, Any]:
    resolved_output = output_dir.resolve()
    if resolved_output.exists():
        raise ShadowIntakeError("output directory already exists; frozen bundles are immutable")
    cases, manifest = materialize_bundle(input_path)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{resolved_output.name}.staging-",
        dir=resolved_output.parent,
    ) as staging_name:
        staging = Path(staging_name)
        (staging / "cases_public.jsonl").write_bytes(
            b"".join(canonical_bytes(case) for case in cases)
        )
        (staging / "manifest.json").write_bytes(canonical_bytes(manifest))
        # Same-volume directory rename publishes both files as one immutable
        # bundle; an existing target still fails instead of being overwritten.
        os.replace(staging, resolved_output)
    return manifest


__all__ = [
    "LABEL_BOUNDARY", "ShadowIntakeError", "canonical_bytes", "freeze_bundle",
    "materialize_bundle", "sha256_bytes",
]
