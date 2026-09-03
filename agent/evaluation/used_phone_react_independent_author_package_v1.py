"""Validate an independently authored ReAct blind-gate submission.

This validator checks freeze integrity and contamination risks before any live
execution.  It never supplies scenario content and does not judge model output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


EXPECTED_CLASSES = {
    "adaptive_needed": 3,
    "deterministic_control": 3,
    "negative_control": 3,
}
PLACEHOLDER_RE = re.compile(r"__(?:REPLACE|FILL|TODO)[^_]*__|<[^>]+>|\bTBD\b", re.I)
FORBIDDEN_TEXT_RE = re.compile(
    r"react(?:_v0)?|fixed_v1|option\s*id|request\s*id|UPRG-V1|"
    r"(?:^|\W)(?:A/B|variant\s*[AB]|arm\s*[AB])(?:$|\W)",
    re.I,
)
PRODUCT_ID_RE = re.compile(r"(?<!\d)\d{6,}(?!\d)")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCENARIO_ID_RE = re.compile(r"^BLIND-V1-\d{3}$")
TURN_ID_RE = re.compile(r"^T[1-9]\d*$")
FORBIDDEN_KEYS = {
    "abmapping",
    "variantmapping",
    "runtimeassignment",
    "answerassignment",
    "arma",
    "armb",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        rows.append(value)
    return rows


def _normalize(text: str) -> str:
    return "".join(character.lower() for character in text if character.isalnum())


def _walk_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            keys.append(str(key))
            keys.extend(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_walk_keys(child))
    return keys


def _scenario_texts(rows: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        scenario_id = row.get("scenarioId")
        turns = row.get("turns")
        if isinstance(scenario_id, str) and isinstance(turns, list):
            text = " ".join(
                str(turn.get("text", ""))
                for turn in turns
                if isinstance(turn, dict)
            )
            result[scenario_id] = text
    return result


def _turn_texts(rows: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        scenario_id = row.get("scenarioId")
        turns = row.get("turns")
        if not isinstance(scenario_id, str) or not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_id = turn.get("turnId")
            text = turn.get("text")
            if isinstance(turn_id, str) and isinstance(text, str):
                result[f"{scenario_id}:{turn_id}"] = text
    return result


def _near_duplicate(left: str, right: str) -> bool:
    normalized_left = _normalize(left)
    normalized_right = _normalize(right)
    if min(len(normalized_left), len(normalized_right)) < 16:
        return normalized_left == normalized_right
    return SequenceMatcher(None, normalized_left, normalized_right).ratio() >= 0.92


def validate_submission(
    *, package_root: Path, forbidden_dataset: Path | None = None
) -> dict[str, Any]:
    scenarios_path = package_root / "public" / "scenarios.jsonl"
    preregistration_path = package_root / "private" / "preregistration.json"
    failures: list[dict[str, Any]] = []

    def fail(code: str, detail: str) -> None:
        failures.append({"code": code, "detail": detail})

    for path in (scenarios_path, preregistration_path):
        if not path.is_file():
            fail("required_file_missing", str(path))
    if failures:
        return _report(package_root, failures)

    try:
        scenarios = _read_jsonl(scenarios_path)
        preregistration = _read_json(preregistration_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        fail("invalid_json", str(error))
        return _report(package_root, failures)
    if not isinstance(preregistration, dict):
        fail("preregistration_not_object", str(preregistration_path))
        return _report(package_root, failures)

    serialized_package = json.dumps(
        {"scenarios": scenarios, "preregistration": preregistration},
        ensure_ascii=False,
    )
    if PLACEHOLDER_RE.search(serialized_package):
        fail("placeholder_present", "submission still contains template placeholders")

    if len(scenarios) != 9:
        fail("scenario_count_invalid", f"expected 9, got {len(scenarios)}")
    ids = [row.get("scenarioId") for row in scenarios]
    if len(set(map(str, ids))) != len(ids):
        fail("duplicate_scenario_id", "scenarioId values must be unique")
    classes: Counter[str] = Counter()
    turn_count = 0
    all_turn_ids: set[tuple[str, str]] = set()
    for index, row in enumerate(scenarios, 1):
        scenario_id = row.get("scenarioId")
        if not isinstance(scenario_id, str) or not SCENARIO_ID_RE.fullmatch(scenario_id):
            fail("scenario_id_invalid", f"row {index}: {scenario_id!r}")
        if row.get("schemaVersion") != "used-phone-react-independent-blind-public-v1":
            fail("scenario_schema_invalid", f"row {index}")
        if row.get("language") != "zh-CN":
            fail("scenario_language_invalid", f"row {index}")
        if row.get("provenanceKind") != "independent_blind_author":
            fail("scenario_provenance_invalid", f"row {index}")
        scenario_class = row.get("generalizationClass")
        if isinstance(scenario_class, str):
            classes[scenario_class] += 1
        else:
            fail("scenario_class_invalid", f"row {index}")
        tags = row.get("tags")
        if (
            not isinstance(tags, list)
            or len(tags) < 2
            or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
        ):
            fail("scenario_tags_invalid", f"row {index}")
        turns = row.get("turns")
        if not isinstance(turns, list) or not 2 <= len(turns) <= 5:
            fail("turn_count_per_scenario_invalid", f"row {index}")
            continue
        for turn in turns:
            turn_count += 1
            if not isinstance(turn, dict):
                fail("turn_invalid", f"row {index}")
                continue
            turn_id = turn.get("turnId")
            text = turn.get("text")
            if not isinstance(turn_id, str) or not TURN_ID_RE.fullmatch(turn_id):
                fail("turn_id_invalid", f"row {index}: {turn_id!r}")
            elif (str(scenario_id), turn_id) in all_turn_ids:
                fail("duplicate_turn_id", f"{scenario_id}:{turn_id}")
            else:
                all_turn_ids.add((str(scenario_id), turn_id))
            if not isinstance(text, str) or len(text.strip()) < 4:
                fail("turn_text_invalid", f"{scenario_id}:{turn_id}")
                continue
            if PLACEHOLDER_RE.search(text):
                fail("placeholder_present", f"{scenario_id}:{turn_id}")
            if FORBIDDEN_TEXT_RE.search(text):
                fail("implementation_or_mapping_leak", f"{scenario_id}:{turn_id}")
            if PRODUCT_ID_RE.search(text):
                fail("product_id_present", f"{scenario_id}:{turn_id}")

    if dict(classes) != EXPECTED_CLASSES:
        fail("class_balance_invalid", f"expected {EXPECTED_CLASSES}, got {dict(classes)}")

    scenario_texts = _scenario_texts(scenarios)
    turn_texts = _turn_texts(scenarios)
    text_items = list(scenario_texts.items())
    for position, (left_id, left_text) in enumerate(text_items):
        for right_id, right_text in text_items[position + 1 :]:
            if _near_duplicate(left_text, right_text):
                fail("within_package_near_duplicate", f"{left_id} ~ {right_id}")

    if forbidden_dataset is not None:
        try:
            forbidden_rows = _read_jsonl(forbidden_dataset)
            forbidden_texts = _scenario_texts(forbidden_rows)
            forbidden_turn_texts = _turn_texts(forbidden_rows)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            fail("forbidden_dataset_invalid", str(error))
        else:
            for new_id, new_text in scenario_texts.items():
                for old_id, old_text in forbidden_texts.items():
                    if _near_duplicate(new_text, old_text):
                        fail("forbidden_dataset_near_duplicate", f"{new_id} ~ {old_id}")
            for new_id, new_text in turn_texts.items():
                for old_id, old_text in forbidden_turn_texts.items():
                    if _near_duplicate(new_text, old_text):
                        fail("forbidden_turn_near_duplicate", f"{new_id} ~ {old_id}")

    for key in _walk_keys({"scenarios": scenarios, "preregistration": preregistration}):
        if re.sub(r"[^a-z]", "", key.lower()) in FORBIDDEN_KEYS:
            fail("mapping_key_present", key)

    expected_prereg = {
        "schemaVersion": "used-phone-react-independent-blind-preregistration-v1",
        "status": "FROZEN_BEFORE_EXECUTION",
        "dataset": "../public/scenarios.jsonl",
        "scenarioCount": 9,
        "pairedRunCount": 2,
    }
    for key, expected in expected_prereg.items():
        if preregistration.get(key) != expected:
            fail("preregistration_field_invalid", f"{key}: expected {expected!r}")
    frozen_date = preregistration.get("frozenDate")
    if not isinstance(frozen_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", frozen_date):
        fail("frozen_date_invalid", "frozenDate must use YYYY-MM-DD")
    if preregistration.get("turnCount") != turn_count:
        fail("preregistration_turn_count_mismatch", f"expected {turn_count}")
    if preregistration.get("classCounts") != EXPECTED_CLASSES:
        fail("preregistration_class_counts_invalid", "must match balanced classes")
    if preregistration.get("datasetSha256") != _sha256(scenarios_path):
        fail("dataset_hash_mismatch", "datasetSha256 does not match scenarios.jsonl")

    source_freeze = preregistration.get("applicationSourceFreeze")
    if not isinstance(source_freeze, dict) or not source_freeze:
        fail("source_freeze_missing", "applicationSourceFreeze must be non-empty")
    else:
        for path, digest in source_freeze.items():
            if (
                not isinstance(path, str)
                or not path.startswith("agent/app/")
                or not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest)
            ):
                fail("source_freeze_invalid", f"{path}: {digest}")

    execution_freeze = preregistration.get("executionFreeze")
    if not isinstance(execution_freeze, dict):
        fail("execution_freeze_missing", "executionFreeze required")
    else:
        config_hash = execution_freeze.get("modelConfigurationSha256")
        timeout = execution_freeze.get("requestTimeoutSeconds")
        ports = execution_freeze.get("reservedPorts")
        if not isinstance(config_hash, str) or not SHA256_RE.fullmatch(config_hash):
            fail("model_configuration_hash_invalid", str(config_hash))
        if not isinstance(timeout, int) or not 1 <= timeout <= 300:
            fail("request_timeout_invalid", str(timeout))
        if (
            not isinstance(ports, list)
            or len(ports) < 2
            or len(set(ports)) != len(ports)
            or any(not isinstance(port, int) or not 1024 <= port <= 65535 for port in ports)
        ):
            fail("reserved_ports_invalid", str(ports))

    adaptive_targets = preregistration.get("adaptiveTargets")
    adaptive_ids = {
        row.get("scenarioId")
        for row in scenarios
        if row.get("generalizationClass") == "adaptive_needed"
    }
    if not isinstance(adaptive_targets, dict) or not adaptive_targets:
        fail("adaptive_targets_missing", "private targets required")
    else:
        target_ids: set[str] = set()
        for target, contract in adaptive_targets.items():
            if not isinstance(target, str) or ":" not in target:
                fail("adaptive_target_key_invalid", str(target))
                continue
            scenario_id, turn_id = target.split(":", 1)
            target_ids.add(scenario_id)
            if (scenario_id, turn_id) not in all_turn_ids:
                fail("adaptive_target_unknown_turn", target)
            if not isinstance(contract, dict) or not contract:
                fail("adaptive_target_contract_invalid", target)
        if target_ids != adaptive_ids:
            fail("adaptive_target_coverage_invalid", f"expected {sorted(adaptive_ids)}")

    hard_gates = preregistration.get("automatedHardGates")
    required_hard_gates = {
        "runnerErrors": 0,
        "runtimeMismatches": 0,
        "publishedOptionViolations": 0,
        "sequenceReceiptCompletenessRate": 1.0,
        "reactDecisionCallsOutsideAdaptiveTargets": 0,
        "fixedAndReactAnswerCompletenessRate": 1.0,
    }
    if not isinstance(hard_gates, dict) or any(
        hard_gates.get(key) != value for key, value in required_hard_gates.items()
    ):
        fail("automated_hard_gates_invalid", "required hard gates changed or missing")

    human_gate = preregistration.get("humanDirectionGate")
    if not isinstance(human_gate, dict) or (
        human_gate.get("minimumDirectFullPacketReviewers") != 2
        or human_gate.get("minimumJudgeablePreferenceShare") != 0.6
        or human_gate.get("requireNoLowerConstraintFidelityMeanByClass") is not True
        or human_gate.get("requireNoLowerEvidenceDisciplineMeanByClass") is not True
    ):
        fail("human_gate_invalid", "fixed reviewer and direction thresholds required")

    freeze_policy = preregistration.get("freezePolicy")
    if not isinstance(freeze_policy, dict) or any(
        freeze_policy.get(key) is not True
        for key in (
            "noApplicationSourceChangesAfterFreeze",
            "failedRunsMustBePreserved",
            "noScenarioRemovalAfterSeeingOutputs",
            "privateOracleHiddenUntilRunsComplete",
        )
    ):
        fail("freeze_policy_invalid", "strict freeze policy required")

    claim_boundary = preregistration.get("claimBoundary")
    if not isinstance(claim_boundary, dict) or (
        claim_boundary.get("canProveStatisticalSignificance") is not False
        or claim_boundary.get("canProveGeneralSuperiority") is not False
        or claim_boundary.get("canAuthorizeDefaultRuntimeSwitch") is not False
    ):
        fail("claim_boundary_invalid", "broad claims and default switch must remain false")

    return _report(package_root, failures, scenarios_path=scenarios_path)


def _report(
    package_root: Path,
    failures: list[dict[str, Any]],
    *,
    scenarios_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": "used-phone-react-independent-author-intake-v1",
        "status": "ACCEPT_FROZEN_INTAKE" if not failures else "HOLD",
        "packageRoot": str(package_root),
        "datasetSha256": _sha256(scenarios_path) if scenarios_path else None,
        "failureCount": len(failures),
        "failures": failures,
        "claimBoundary": {
            "authoringIntegrityOnly": True,
            "provesHeldoutGeneralization": False,
            "provesReactSuperiority": False,
            "authorizesExecution": not failures,
            "authorizesDefaultRuntimeSwitch": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", required=True, type=Path)
    parser.add_argument("--forbidden-dataset", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite output: {args.output}")
    report = validate_submission(
        package_root=args.package_root,
        forbidden_dataset=args.forbidden_dataset,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "ACCEPT_FROZEN_INTAKE" else 1)


if __name__ == "__main__":
    main()
