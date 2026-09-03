from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


PACKAGE_ROOT = Path(__file__).resolve().parent
SCENARIOS_PATH = PACKAGE_ROOT / "public" / "scenarios.jsonl"
SCHEMA_PATH = PACKAGE_ROOT / "scenario.schema.json"
CHECKSUM_PATH = PACKAGE_ROOT / "SHA256SUMS.txt"

SUPPORTED = {"phone", "laptop", "headphones"}
SCOPE_NEGATIVE = {"tablet", "camera", "smartwatch", "monitor", "e_reader"}
EXPECTED_PAIR_BINDING = {
    "modelConfig": "runner_locked_same_config",
    "toolProfile": "shopping_read_only_v1",
    "publicDataSnapshot": "runner_locked_same_snapshot",
    "freshTask": True,
}
FORBIDDEN_KEY_MARKERS = {
    "arm",
    "armmapping",
    "control",
    "expectedaction",
    "expectedanswer",
    "expectedoutcome",
    "expectedtool",
    "expectedtoolpath",
    "expectedwinner",
    "gold",
    "hiddenlabel",
    "implementationpath",
    "mapping",
    "oracle",
    "preferredruntime",
    "score",
    "sealedmapping",
    "threshold",
    "treatment",
    "winner",
}
FORBIDDEN_VALUE_MARKERS = (
    "expected winner",
    "preferred runtime",
    "arm mapping",
    "sealed mapping",
    "private oracle",
    "hidden label",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _audit_leaks(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized in FORBIDDEN_KEY_MARKERS:
                raise AssertionError(f"forbidden public key at {location}.{key}")
            _audit_leaks(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _audit_leaks(child, f"{location}[{index}]")
    elif isinstance(value, str):
        folded = value.casefold()
        for marker in FORBIDDEN_VALUE_MARKERS:
            if marker in folded:
                raise AssertionError(f"forbidden public value marker at {location}: {marker}")


def _load_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(
        SCENARIOS_PATH.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            raise AssertionError(f"blank JSONL line: {line_number}")
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"invalid JSONL line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise AssertionError(f"JSONL line {line_number} is not an object")
        records.append(record)
    return records


def _validate_schema_and_counts(records: list[dict[str, Any]]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for record in records:
        errors = sorted(validator.iter_errors(record), key=lambda item: list(item.path))
        if errors:
            rendered = "; ".join(
                f"{record.get('scenarioId', '<unknown>')}:{'.'.join(map(str, error.path))}: {error.message}"
                for error in errors
            )
            raise AssertionError(rendered)
        _audit_leaks(record, str(record["scenarioId"]))

    if len(records) != 24:
        raise AssertionError(f"scenario count must be 24, got {len(records)}")
    scenario_ids = [record["scenarioId"] for record in records]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise AssertionError("duplicate scenarioId")

    strata = Counter(record["behaviorClass"] for record in records)
    if strata != Counter({"adaptive": 8, "deterministic": 8, "negative": 8}):
        raise AssertionError(f"wrong behavior counts: {dict(strata)}")

    all_turn_ids: list[str] = []
    for record in records:
        if record["pairBinding"] != EXPECTED_PAIR_BINDING:
            raise AssertionError(f"pair binding drift: {record['scenarioId']}")
        turn_ids = [turn["turnId"] for turn in record["turns"]]
        if turn_ids != [f"t{index}" for index in range(1, len(turn_ids) + 1)]:
            raise AssertionError(f"non-contiguous turn IDs: {record['scenarioId']}")
        all_turn_ids.extend(f"{record['scenarioId']}:{turn_id}" for turn_id in turn_ids)

    if len(all_turn_ids) != 51 or len(set(all_turn_ids)) != 51:
        raise AssertionError(f"turn count/identity mismatch: {len(all_turn_ids)}")

    seen_categories = {record["category"] for record in records}
    if seen_categories != SUPPORTED | SCOPE_NEGATIVE:
        raise AssertionError(f"category coverage mismatch: {sorted(seen_categories)}")

    scope_negative_records = [
        record for record in records if record["category"] in SCOPE_NEGATIVE
    ]
    if len(scope_negative_records) != 5:
        raise AssertionError(f"scope-negative record count must be 5, got {len(scope_negative_records)}")
    for record in scope_negative_records:
        if (
            record["behaviorClass"] != "negative"
            or record["currentCapability"] != "scope_negative"
            or record["executionBoundary"] != "no_product_execution"
        ):
            raise AssertionError(f"scope boundary violation: {record['scenarioId']}")

    for record in records:
        if record["behaviorClass"] in {"adaptive", "deterministic"}:
            if record["category"] not in SUPPORTED:
                raise AssertionError(f"positive behavior outside current scope: {record['scenarioId']}")


def _validate_checksums() -> None:
    expected: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        CHECKSUM_PATH.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", raw_line)
        if not match:
            raise AssertionError(f"invalid checksum line {line_number}")
        digest, relative_name = match.groups()
        if relative_name in expected:
            raise AssertionError(f"duplicate checksum entry: {relative_name}")
        expected[relative_name] = digest

    actual_files = {
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file()
        and path != CHECKSUM_PATH
        and "__pycache__" not in path.parts
    }
    if set(expected) != actual_files:
        missing = sorted(actual_files - set(expected))
        extra = sorted(set(expected) - actual_files)
        raise AssertionError(f"checksum coverage mismatch missing={missing} extra={extra}")

    for relative_name, expected_digest in expected.items():
        actual_digest = _sha256(PACKAGE_ROOT / relative_name)
        if actual_digest != expected_digest:
            raise AssertionError(
                f"checksum mismatch: {relative_name} expected={expected_digest} actual={actual_digest}"
            )


def main() -> None:
    records = _load_records()
    _validate_schema_and_counts(records)
    _validate_checksums()
    counts = Counter(record["behaviorClass"] for record in records)
    print(
        "ACCEPT_PUBLIC_PACKAGE "
        f"scenarios={len(records)} turns={sum(len(record['turns']) for record in records)} "
        f"adaptive={counts['adaptive']} deterministic={counts['deterministic']} "
        f"negative={counts['negative']} scope_negative=5"
    )


if __name__ == "__main__":
    main()
