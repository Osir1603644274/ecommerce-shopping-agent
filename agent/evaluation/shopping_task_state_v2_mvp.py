"""Read-only loader and contract validator for the manually authored STS V2 MVP."""

from __future__ import annotations

import json
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
DATASET = Path(__file__).resolve().parent / "assets" / "shopping_task_state_v2_mvp_20260822"
PUBLIC_PATH = DATASET / "public" / "scenarios.jsonl"
PRIVATE_PATH = DATASET / "private" / "state_oracle.private.jsonl"
MANIFEST_PATH = DATASET / "manifest.json"
PUBLIC_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "shopping_task_state_public_v2.schema.json"
PRIVATE_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "shopping_task_state_oracle_private_v2.schema.json"

REQUIRED_COVERAGE = frozenset({
    "inherit", "override", "revoke", "negative", "priority",
    "minimal_clarification", "no_unnecessary_clarification", "compare",
    "candidate_scope_reuse", "dynamic_environment", "coupon_policy",
    "long_term_memory_write", "long_term_memory_read",
    "long_term_memory_update", "long_term_memory_revoke",
    "memory_scope_isolation", "task_only_memory", "intent_shift",
})
PRIVATE_PUBLIC_FORBIDDEN = (
    "routeclass", "activerequirementids", "memoryevents",
    "requirementdefinitions", "coveragetags", "private oracle",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{line_number} must be an object")
        rows.append(value)
    return rows


def _validate_schema(rows: list[dict[str, Any]], schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for row in rows:
        errors = sorted(validator.iter_errors(row), key=lambda item: list(item.path))
        if errors:
            raise ValueError(f"{row.get('scenarioId')}: {errors[0].message}")


def load_and_validate() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    public_rows = _load_jsonl(PUBLIC_PATH)
    private_rows = _load_jsonl(PRIVATE_PATH)
    _validate_schema(public_rows, PUBLIC_SCHEMA_PATH)
    _validate_schema(private_rows, PRIVATE_SCHEMA_PATH)
    if len(public_rows) != 14 or len(private_rows) != 14:
        raise ValueError("MVP must contain exactly 14 aligned scenarios")

    public_by_id = {row["scenarioId"]: row for row in public_rows}
    private_by_id = {row["scenarioId"]: row for row in private_rows}
    if len(public_by_id) != len(public_rows) or len(private_by_id) != len(private_rows):
        raise ValueError("scenarioId values must be unique")
    if set(public_by_id) != set(private_by_id):
        raise ValueError("public/private scenario identity mismatch")

    raw_public = PUBLIC_PATH.read_text(encoding="utf-8").casefold()
    for token in PRIVATE_PUBLIC_FORBIDDEN:
        if token in raw_public:
            raise ValueError(f"private contract token leaked into public data: {token}")

    all_texts: list[str] = []
    coverage: set[str] = set()
    route_counts: Counter[str] = Counter()
    memory_ops: Counter[str] = Counter()
    for scenario_id in sorted(public_by_id):
        public = public_by_id[scenario_id]
        oracle = private_by_id[scenario_id]
        public_turns = public["turns"]
        annotations = oracle["turnAnnotations"]
        public_turn_ids = [turn["turnId"] for turn in public_turns]
        private_turn_ids = [turn["turnId"] for turn in annotations]
        if public_turn_ids != [f"T{index}" for index in range(1, len(public_turns) + 1)]:
            raise ValueError(f"{scenario_id}: turn IDs must be contiguous")
        if private_turn_ids != public_turn_ids:
            raise ValueError(f"{scenario_id}: public/private turn mismatch")
        all_texts.extend(turn["text"].strip() for turn in public_turns)
        coverage.update(oracle["coverageTags"])

        definitions = {row["requirementId"]: row for row in oracle["requirementDefinitions"]}
        if len(definitions) != len(oracle["requirementDefinitions"]):
            raise ValueError(f"{scenario_id}: duplicate requirement definition")
        active: set[str] = set()
        introduced: set[str] = set()
        profile_memory: set[str] = set()
        for annotation in annotations:
            turn_id = annotation["turnId"]
            route_counts[annotation["routeClass"]] += 1
            sufficiency = annotation["informationSufficiency"]
            clarification = annotation["clarification"]
            if annotation["routeClass"] == "CLARIFY":
                if sufficiency["status"] != "insufficient" or not sufficiency["blockingUnknowns"]:
                    raise ValueError(f"{scenario_id}/{turn_id}: CLARIFY requires a blocker")
                if not clarification["required"] or clarification["maxQuestions"] != 1:
                    raise ValueError(f"{scenario_id}/{turn_id}: clarification must be minimal")
            elif clarification["required"] or clarification["maxQuestions"] != 0:
                raise ValueError(f"{scenario_id}/{turn_id}: unnecessary clarification")

            for delta in annotation["delta"]:
                requirement_id = delta["requirementId"]
                if requirement_id not in definitions:
                    raise ValueError(f"{scenario_id}/{turn_id}: unknown requirement {requirement_id}")
                op = delta["op"]
                if op == "add":
                    if definitions[requirement_id]["introducedTurnId"] != turn_id:
                        raise ValueError(f"{scenario_id}/{turn_id}: add turn mismatch")
                    active.add(requirement_id)
                    introduced.add(requirement_id)
                elif op == "retain":
                    if requirement_id not in active:
                        raise ValueError(f"{scenario_id}/{turn_id}: cannot retain inactive requirement")
                elif op == "override":
                    replaced = delta.get("replacesRequirementId")
                    if replaced not in active or requirement_id not in definitions:
                        raise ValueError(f"{scenario_id}/{turn_id}: invalid override")
                    if definitions[requirement_id].get("supersedes") != replaced:
                        raise ValueError(f"{scenario_id}/{turn_id}: supersedes mismatch")
                    active.remove(replaced)
                    active.add(requirement_id)
                    introduced.add(requirement_id)
                elif op in {"revoke", "suppress"}:
                    if requirement_id not in active:
                        raise ValueError(f"{scenario_id}/{turn_id}: cannot {op} inactive requirement")
                    active.remove(requirement_id)
            if set(annotation["activeRequirementIds"]) != active:
                raise ValueError(f"{scenario_id}/{turn_id}: effective requirement replay mismatch")
            if not active.issubset(introduced):
                raise ValueError(f"{scenario_id}/{turn_id}: active requirement was never introduced")

            for event in annotation["memoryEvents"]:
                op = event["op"]
                key = event["memoryKey"]
                memory_ops[op] += 1
                if op == "write":
                    profile_memory.add(key)
                elif op in {"read", "update", "revoke", "suppress"} and key not in profile_memory:
                    raise ValueError(f"{scenario_id}/{turn_id}: memory {op} without stored key")
                if op == "revoke":
                    profile_memory.remove(key)

    if len(all_texts) != 56 or len(set(all_texts)) != 56:
        raise ValueError("MVP must contain 56 unique manually authored turns")
    missing_coverage = REQUIRED_COVERAGE - coverage
    if missing_coverage:
        raise ValueError(f"missing coverage: {sorted(missing_coverage)}")
    for required_route in ("CLARIFY", "FAST", "PAE", "BOUNDED_REACT", "ANSWER_ONLY"):
        if route_counts[required_route] == 0:
            raise ValueError(f"route class is uncovered: {required_route}")
    for required_op in ("write", "read", "revoke", "suppress", "do_not_write"):
        if memory_ops[required_op] == 0:
            raise ValueError(f"memory operation is uncovered: {required_op}")
    return public_rows, private_rows


def validate_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("status") != "READY_FOR_HUMAN_LANGUAGE_REVIEW":
        raise ValueError("dataset must remain pending human language review")
    if manifest.get("strategyRunStatus") != "NOT_RUN":
        raise ValueError("MVP manifest cannot claim a strategy run")
    for artifact in manifest.get("artifacts", []):
        path = DATASET / artifact["path"]
        payload = path.read_bytes()
        if len(payload) != artifact["byteLength"]:
            raise ValueError(f"byte length mismatch: {artifact['path']}")
        if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {artifact['path']}")
        if sum(1 for line in payload.decode("utf-8").splitlines() if line.strip()) != artifact["recordCount"]:
            raise ValueError(f"record count mismatch: {artifact['path']}")
    for schema in manifest.get("schemas", []):
        path = ROOT / schema["path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != schema["sha256"]:
            raise ValueError(f"schema SHA-256 mismatch: {schema['path']}")
    return manifest


def dataset_summary() -> dict[str, Any]:
    public_rows, private_rows = load_and_validate()
    validate_manifest()
    return {
        "scenarioCount": len(public_rows),
        "turnCount": sum(len(row["turns"]) for row in public_rows),
        "sessionCount": len({turn["sessionId"] for row in public_rows for turn in row["turns"]}),
        "coverageTags": sorted({tag for row in private_rows for tag in row["coverageTags"]}),
    }
