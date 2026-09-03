"""Prediction-first scorer binding for authoritative-state compiler runs."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

from agent.evaluation import shopping_mission_baseline_runner_v1 as base_runner
from agent.evaluation import shopping_mission_baseline_scorer_v1 as base_scorer
from agent.evaluation import shopping_mission_benchmark_v1 as benchmark
from agent.evaluation import shopping_mission_authoritative_state_runner_v1 as runner


class AuthoritativeScoreError(ValueError):
    """Invalid authoritative-state run."""


def authenticate_run(run_dir: Path):
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        case_bytes = (run_dir / "cases.jsonl").read_bytes()
        receipt_bytes = (run_dir / "receipts.jsonl").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthoritativeScoreError("cannot read run artifacts") from exc
    required = {
        "schemaVersion", "datasetId", "runId", "profile", "model", "endpointOrigin", "temperature",
        "startedAt", "completedAt", "scenarioCount", "completedCount", "failedCount", "publicSha256",
        "humanLanguageEvidenceSha256", "humanSemanticEvidenceSha256", "frozenBaselineRunnerSha256",
        "runnerSha256", "stateSchemaSha256", "predictionSchemaSha256", "preregistrationSha256",
        "casesSha256", "receiptsSha256", "compilerType", "modelRepairCalls", "toolCalls",
    }
    if type(manifest) is not dict or set(manifest) != required:
        raise AuthoritativeScoreError("manifest contract mismatch")
    if (
        manifest["schemaVersion"] != "shopping-mission-authoritative-state-run-manifest-v1"
        or manifest["datasetId"] != base_runner.DATASET_ID
        or manifest["profile"] != runner.PROFILE
        or manifest["endpointOrigin"] != "https://api.deepseek.com"
        or manifest["temperature"] != 0
        or manifest["compilerType"] != "DETERMINISTIC"
        or manifest["modelRepairCalls"] != 0
        or manifest["toolCalls"] != 0
    ):
        raise AuthoritativeScoreError("manifest identity mismatch")
    identities = {
        "publicSha256": base_runner._sha256_path(base_runner.PUBLIC_PATH),
        "humanLanguageEvidenceSha256": base_runner.EXPECTED_LANGUAGE_EVIDENCE_SHA,
        "humanSemanticEvidenceSha256": base_runner.EXPECTED_SEMANTIC_EVIDENCE_SHA,
        "frozenBaselineRunnerSha256": base_runner._sha256_path(Path(base_runner.__file__).resolve()),
        "runnerSha256": base_runner._sha256_path(Path(runner.__file__).resolve()),
        "stateSchemaSha256": base_runner._sha256_path(runner.STATE_SCHEMA_PATH),
        "predictionSchemaSha256": base_runner._sha256_path(base_runner.PREDICTION_SCHEMA_PATH),
        "preregistrationSha256": base_runner._sha256_path(runner.PREREGISTRATION_PATH),
        "casesSha256": base_runner._sha256_bytes(case_bytes),
        "receiptsSha256": base_runner._sha256_bytes(receipt_bytes),
    }
    if any(manifest[key] != value for key, value in identities.items()):
        raise AuthoritativeScoreError("run identity or digest drift")
    cases = base_scorer._read_jsonl_bytes(case_bytes, "case")
    receipts = base_scorer._read_jsonl_bytes(receipt_bytes, "receipt")
    public_rows = base_scorer._read_jsonl_bytes(base_runner.PUBLIC_PATH.read_bytes(), "public")
    expected_ids = {row["scenarioId"] for row in public_rows}
    case_index = base_scorer._index(cases, "case")
    receipt_index = base_scorer._index(receipts, "receipt")
    if set(case_index) != expected_ids or set(receipt_index) != expected_ids or len(expected_ids) != 18:
        raise AuthoritativeScoreError("scenario closure mismatch")
    completed = failed = 0
    for scenario_id in expected_ids:
        case, receipt = case_index[scenario_id], receipt_index[scenario_id]
        for item in (case, receipt):
            if item.get("runId") != manifest["runId"] or item.get("profile") != runner.PROFILE:
                raise AuthoritativeScoreError("run binding mismatch")
        if case.get("status") != receipt.get("status") or case.get("status") not in {"COMPLETED", "FAILED"}:
            raise AuthoritativeScoreError("status mismatch")
        calls = receipt.get("calls")
        if type(calls) is not list or len(calls) != 1 or receipt.get("modelCalls") != 1:
            raise AuthoritativeScoreError("one-call contract mismatch")
        if calls[0].get("phase") != "STATE_EXTRACT":
            raise AuthoritativeScoreError("call phase mismatch")
        for field in ("inputTokens", "outputTokens", "latencyMs"):
            if receipt.get(field) != calls[0].get(field):
                raise AuthoritativeScoreError(f"receipt {field} mismatch")
        if case["status"] == "COMPLETED":
            if case.get("errorCode") is not None or type(case.get("prediction")) is not dict:
                raise AuthoritativeScoreError("completed envelope mismatch")
            benchmark.validate_record(case["prediction"], "prediction")
            if case["prediction"]["scenarioId"] != scenario_id:
                raise AuthoritativeScoreError("prediction identity mismatch")
            completed += 1
        else:
            if case.get("prediction") is not None or not isinstance(case.get("errorCode"), str):
                raise AuthoritativeScoreError("failed envelope mismatch")
            failed += 1
    if (manifest["scenarioCount"], manifest["completedCount"], manifest["failedCount"]) != (18, completed, failed):
        raise AuthoritativeScoreError("completion count mismatch")
    return manifest, cases, receipts


def _translated_score(manifest, cases, receipts):
    translated_cases = copy.deepcopy(cases)
    translated_receipts = copy.deepcopy(receipts)
    for item in translated_cases:
        item["profile"] = "DIRECT_ONE_SHOT"
    for item in translated_receipts:
        item["profile"] = "DIRECT_ONE_SHOT"
    case_bytes = b"".join(base_runner._canonical_bytes(item) for item in translated_cases)
    receipt_bytes = b"".join(base_runner._canonical_bytes(item) for item in translated_receipts)
    translated_manifest = {
        "schemaVersion": "shopping-mission-baseline-run-manifest-v1",
        "datasetId": manifest["datasetId"], "runId": manifest["runId"], "profile": "DIRECT_ONE_SHOT",
        "model": manifest["model"], "endpointOrigin": manifest["endpointOrigin"],
        "temperature": manifest["temperature"], "startedAt": manifest["startedAt"],
        "completedAt": manifest["completedAt"], "scenarioCount": 18,
        "completedCount": manifest["completedCount"], "failedCount": manifest["failedCount"],
        "publicSha256": manifest["publicSha256"],
        "humanLanguageEvidenceSha256": manifest["humanLanguageEvidenceSha256"],
        "humanSemanticEvidenceSha256": manifest["humanSemanticEvidenceSha256"],
        "runnerSha256": manifest["runnerSha256"],
        "predictionSchemaSha256": manifest["predictionSchemaSha256"],
        "preregistrationSha256": manifest["preregistrationSha256"],
        "casesSha256": base_runner._sha256_bytes(case_bytes),
        "receiptsSha256": base_runner._sha256_bytes(receipt_bytes),
        "modelRepairCalls": 0, "toolCalls": 0,
    }
    temp_dir = Path(tempfile.mkdtemp(prefix="shopping-mission-authoritative-score-"))
    try:
        (temp_dir / "cases.jsonl").write_bytes(case_bytes)
        (temp_dir / "receipts.jsonl").write_bytes(receipt_bytes)
        (temp_dir / "manifest.json").write_bytes(base_runner._canonical_bytes(translated_manifest))
        old_runner, old_prereg = base_scorer.RUNNER_PATH, base_scorer.PREREGISTRATION_PATH
        base_scorer.RUNNER_PATH = Path(runner.__file__).resolve()
        base_scorer.PREREGISTRATION_PATH = runner.PREREGISTRATION_PATH
        try:
            report = base_scorer.score_run(temp_dir)
        finally:
            base_scorer.RUNNER_PATH, base_scorer.PREREGISTRATION_PATH = old_runner, old_prereg
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    report["profile"] = runner.PROFILE
    schema = json.loads(runner.SCORE_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(report))
    if errors:
        raise AuthoritativeScoreError(f"score schema mismatch: {errors[0].message}")
    return report


def score_run(run_dir: Path):
    manifest, cases, receipts = authenticate_run(run_dir)
    # Private oracle is opened only inside the frozen scorer after authentication.
    return _translated_score(manifest, cases, receipts)


def write_score_new(run_dir: Path, report):
    path = run_dir / "score.json"
    if path.exists():
        raise AuthoritativeScoreError("score output already exists")
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return path
