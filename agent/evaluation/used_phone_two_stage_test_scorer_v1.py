"""Prediction-first scorer for the single sealed Test10 decision set."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from unittest.mock import patch

from . import used_phone_two_stage_ranking_scorer_v3 as _base
from .used_phone_two_stage_ranking_metric_audit_v1 import _case_diagnostics


SCHEMA_VERSION = "used-phone-two-stage-ranking-test10-score-v1"
EVALUATOR_IDENTITY_VERSION = "test10-prediction-first-v1"
PROTOCOL_VERSION = "used-phone-public-production-agent-runner-v3-two-stage-ranking-test10"
TEST_CASE_IDS = tuple(f"UPV2-RK-T{index:02d}" for index in range(1, 11))
PREREGISTRATION_FILENAME = "test10_preregistration_v1.json"
PREREGISTRATION_SHA256 = (
    "98aab881d6b6552c03a625ea7cb0635b7b0df9cea220e21e2c2b91b8f0cefee8"
)
HIDDEN_FILENAME = "judgments_hidden.jsonl"
HIDDEN_SOURCE_BYTE_COUNT = 10_947_321
HIDDEN_SOURCE_ROW_COUNT = 7_560
ROWS_BEFORE_TEST_BLOCK = 5_040
TEST_BLOCK_ROW_COUNT = 2_520
TEST_BLOCK_BYTE_COUNT = 3_644_907
TEST_BLOCK_SHA256 = "cc974c2b633d7335cd7ba715af63f7ac03afb673af353d2c22c4779486531f3a"
ROWS_PER_CASE = 252
LABEL_BOUNDARY = "AI-designed product-grounded, not human gold"
REPO_ROOT = Path(__file__).resolve().parents[2]
PREREGISTRATION_DIR = (
    REPO_ROOT / ".agents" / "evaluation-assets"
    / "used-phone-two-stage-ranking-v3-20260813"
)
EVALUATOR_ASSET_DIR = Path(
    r"C:\Users\ming\.codex\evaluation-assets\used-phone-ranking-benchmark-v2-20260813"
)
PREDICTION_SCHEMA_PATH = _base.PREDICTION_SCHEMA_PATH
RUNNER_FILES = {
    "agent/evaluation/used_phone_public_agent_runner_v1.py": REPO_ROOT / "agent" / "evaluation" / "used_phone_public_agent_runner_v1.py",
    "agent/evaluation/used_phone_two_stage_ranking_runner_v3.py": REPO_ROOT / "agent" / "evaluation" / "used_phone_two_stage_ranking_runner_v3.py",
    "agent/evaluation/used_phone_two_stage_test_runner_v1.py": REPO_ROOT / "agent" / "evaluation" / "used_phone_two_stage_test_runner_v1.py",
    "agent/evaluation/schemas/used_phone_two_stage_ranking_prediction_v3.schema.json": PREDICTION_SCHEMA_PATH,
    "agent/evaluation/schemas/used_phone_ranking_prediction_v2.schema.json": REPO_ROOT / "agent" / "evaluation" / "schemas" / "used_phone_ranking_prediction_v2.schema.json",
    "agent/scripts/run_used_phone_two_stage_test_v1.py": REPO_ROOT / "agent" / "scripts" / "run_used_phone_two_stage_test_v1.py",
}
EVALUATOR_FILES = {
    "agent/evaluation/used_phone_two_stage_test_scorer_v1.py": Path(__file__).resolve(),
    "agent/evaluation/used_phone_two_stage_ranking_scorer_v3.py": Path(_base.__file__).resolve(),
    "agent/evaluation/used_phone_two_stage_ranking_metric_audit_v1.py": REPO_ROOT / "agent" / "evaluation" / "used_phone_two_stage_ranking_metric_audit_v1.py",
    "agent/evaluation/schemas/used_phone_two_stage_ranking_prediction_v3.schema.json": PREDICTION_SCHEMA_PATH,
    "agent/scripts/score_used_phone_two_stage_test_v1.py": REPO_ROOT / "agent" / "scripts" / "score_used_phone_two_stage_test_v1.py",
}


def canonical_bytes(value: Any) -> bytes:
    return _base.canonical_bytes(value)


def _code_hashes(paths: Mapping[str, Path]) -> dict[str, str]:
    return _base._code_hashes(paths)


@contextmanager
def _test_auth_identity():
    with (
        patch.object(_base, "PROTOCOL_VERSION", PROTOCOL_VERSION),
        patch.object(_base, "PUBLIC_DEV_CASE_IDS", TEST_CASE_IDS),
        patch.object(_base, "RUNNER_FILES", RUNNER_FILES),
        patch.object(_base, "_validate_run_identity", _validate_test_run_identity),
    ):
        yield


def _validate_test_run_identity(*, manifest: Mapping[str, Any],
                                run_contract: Mapping[str, Any],
                                prediction_sha256: str) -> dict[str, Any]:
    contract = run_contract.get("contract")
    if not isinstance(contract, dict):
        raise ValueError("run contract body is missing")
    contract_sha = hashlib.sha256(canonical_bytes(contract)).hexdigest()
    if (
        run_contract.get("schemaVersion") != _base.RUN_CONTRACT_SCHEMA_VERSION
        or run_contract.get("contractSha256") != contract_sha
        or manifest.get("contractSha256") != contract_sha
        or manifest.get("contract") != contract
    ):
        raise ValueError("run contract/manifest identity mismatch")
    if manifest.get("schemaVersion") != _base.MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest schema mismatch")
    if manifest.get("selectedSplits") != ["test"]:
        raise ValueError("Test10 scorer accepts only the test split")
    if manifest.get("selectedCaseIds") != list(TEST_CASE_IDS):
        raise ValueError("Test10 case identity/order mismatch")
    execution = manifest.get("execution")
    if (
        not isinstance(execution, dict)
        or list(execution) != list(TEST_CASE_IDS)
        or any(type(value) is not bool for value in execution.values())
    ):
        raise ValueError("Test10 execution identity mismatch")
    succeeded = [case_id for case_id, ok in execution.items() if ok]
    failed = [case_id for case_id, ok in execution.items() if not ok]
    if manifest.get("succeededCaseIds") != succeeded or manifest.get("failedCaseIds") != failed or failed:
        raise ValueError("Test10 requires ten terminal successes")
    prediction = manifest.get("prediction")
    if (
        not isinstance(prediction, dict)
        or prediction.get("path") != "predictions.jsonl"
        or prediction.get("rowCount") != len(TEST_CASE_IDS)
        or prediction.get("sha256") != prediction_sha256
    ):
        raise ValueError("manifest/prediction identity mismatch")
    if manifest.get("twoStagePredictionSchemaVersion") != _base.PREDICTION_SCHEMA_VERSION:
        raise ValueError("prediction schema identity mismatch")
    if (
        manifest.get("hiddenArtifactsRead") is not False
        or manifest.get("businessWriteNetworkUsed") is not False
        or manifest.get("modelCallCount") != 0
        or manifest.get("modelNetworkCallCount") != 0
        or manifest.get("modelNetworkUsed") is not False
    ):
        raise ValueError("Test10 run exceeded hidden/write/no-model boundary")
    expected = {
        "allowSealedTest": True,
        "attributeContractCodeSha256": _base.ATTRIBUTE_CONTRACT_CODE_SHA256,
        "attributeRuleset": _base.ATTRIBUTE_RULESET_VERSION,
        "caseCount": 30,
        "javaCatalog": _base._expected_java_catalog(),
        "model": _base.NO_MODEL_NAME,
        "modelEndpoint": _base.NO_MODEL_ENDPOINT,
        "productionCodeScopeSha256": _base.PRODUCTION_CODE_SCOPE_SHA256,
        "productionRuntimeConfig": _base.PRODUCTION_RUNTIME_CONFIG,
        "protocolVersion": PROTOCOL_VERSION,
        "publicCasesSha256": _base.PUBLIC_CASES_SHA256,
        "publicCatalogSha256": _base.PUBLIC_CATALOG_SHA256,
        "runnerCodeSha256": _code_hashes(RUNNER_FILES),
        "selectedCaseIds": list(TEST_CASE_IDS),
        "selectedSplits": ["test"],
    }
    if set(contract) != {*expected, "timeoutSeconds"}:
        raise ValueError("run contract fields mismatch")
    timeout = contract.get("timeoutSeconds")
    if type(timeout) not in {int, float} or timeout <= 0:
        raise ValueError("run timeout contract mismatch")
    if {key: contract.get(key) for key in expected} != expected:
        raise ValueError("run protocol/code/data contract mismatch")
    return contract


def _authenticate_prediction_before_labels(*, predictions_path: Path,
                                           manifest_path: Path):
    with _test_auth_identity():
        return _base._authenticate_prediction_before_labels(
            predictions_path=predictions_path, manifest_path=manifest_path,
        )


def _load_preregistration(path: Path, attempt_id: str) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved.parent != PREREGISTRATION_DIR.resolve() or resolved.name != PREREGISTRATION_FILENAME:
        raise ValueError("Test10 preregistration path mismatch")
    payload = resolved.read_bytes()
    if hashlib.sha256(payload).hexdigest() != PREREGISTRATION_SHA256:
        raise ValueError("Test10 preregistration SHA mismatch")
    value = json.loads(payload)
    if payload != canonical_bytes(value):
        raise ValueError("Test10 preregistration must be canonical JSON")
    if (
        value.get("schemaVersion") != "used-phone-two-stage-ranking-test10-preregistration-v1"
        or value.get("split") != "test"
        or value.get("caseIds") != list(TEST_CASE_IDS)
        or value.get("frozenBeforeTestPrediction") is not True
        or attempt_id not in value.get("authorizedRunDirectories", [])
    ):
        raise ValueError("Test10 preregistration semantic mismatch")
    return value


def _load_test_judgments(path: Path) -> dict[str, dict[str, Any]]:
    resolved = path.resolve()
    if resolved.parent != EVALUATOR_ASSET_DIR.resolve() or resolved.name != HIDDEN_FILENAME:
        raise ValueError("Test10 judgments must use the pinned evaluator asset")
    if resolved.stat().st_size != HIDDEN_SOURCE_BYTE_COUNT:
        raise ValueError("hidden judgment source byte count mismatch")
    judgments = {case_id: {} for case_id in TEST_CASE_IDS}
    test_hasher = hashlib.sha256()
    test_bytes = 0
    observed_order: list[str] = []
    with resolved.open("rb", buffering=1024 * 1024) as stream:
        for line_number in range(1, HIDDEN_SOURCE_ROW_COUNT + 1):
            raw = stream.readline()
            if not raw:
                raise ValueError("hidden judgment source ended before Test10 block")
            if line_number <= ROWS_BEFORE_TEST_BLOCK:
                continue
            test_hasher.update(raw)
            test_bytes += len(raw)
            if not raw.strip() or not raw.endswith(b"\n"):
                raise ValueError("invalid Test10 judgment JSONL boundary")
            try:
                row = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid Test10 judgment JSON") from exc
            if not isinstance(row, dict) or raw != canonical_bytes(row):
                raise ValueError("non-canonical Test10 judgment row")
            case_id, item_id = row.get("caseId"), row.get("itemId")
            if (
                case_id not in judgments or row.get("split") != "test"
                or row.get("schemaVersion") != "used-phone-ranking-judgment-v2"
                or not isinstance(item_id, str) or not item_id.isdigit()
                or item_id.startswith("0")
            ):
                raise ValueError("Test10 judgment identity mismatch")
            if not observed_order or observed_order[-1] != case_id:
                if case_id in observed_order:
                    raise ValueError("Test10 case block is non-contiguous")
                observed_order.append(case_id)
            if item_id in judgments[case_id]:
                raise ValueError("duplicate Test10 judgment")
            judgments[case_id][item_id] = row
    if test_bytes != TEST_BLOCK_BYTE_COUNT or test_hasher.hexdigest() != TEST_BLOCK_SHA256:
        raise ValueError("Test10 judgment block byte identity mismatch")
    if tuple(observed_order) != TEST_CASE_IDS or any(len(rows) != ROWS_PER_CASE for rows in judgments.values()):
        raise ValueError("Test10 judgment case/cardinality mismatch")
    item_universe = set(judgments[TEST_CASE_IDS[0]])
    if len(item_universe) != ROWS_PER_CASE or any(set(rows) != item_universe for rows in judgments.values()):
        raise ValueError("Test10 judgment item universe mismatch")
    return judgments


def _mean(values: Iterable[float]) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0


def _normalized_diagnostics(prediction: Mapping[str, Any],
                            judgments: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    row = _case_diagnostics(prediction, judgments)
    pool_hits, final_hits = row["candidatePoolRelevantHitCount"], row["finalRelevantHitCount"]
    row["candidateConditionedRetentionCeilingUtilizationAt20"] = (
        final_hits / min(20, pool_hits) if pool_hits else 0.0
    )
    row["orderingEfficiencyAt10"] = row["orderingEfficiencyAt10"] or 0.0
    return row


def score_test10(*, predictions_path: Path, manifest_path: Path,
                 test_judgments_path: Path, test_preregistration_path: Path) -> dict[str, Any]:
    predictions, prediction_sha, manifest_sha, contract_sha = _authenticate_prediction_before_labels(
        predictions_path=predictions_path, manifest_path=manifest_path,
    )
    prereg = _load_preregistration(test_preregistration_path, manifest_path.resolve().parent.name)
    judgments = _load_test_judgments(test_judgments_path)
    per_case = [_normalized_diagnostics(row, judgments[row["caseId"]]) for row in predictions]
    citation_count = sum(row["citationCount"] for row in per_case)
    citation_correct = sum(row["citationCorrectCount"] for row in per_case)
    metrics = {
        "candidateConditionedRetentionCeilingUtilizationAt20": _mean(row["candidateConditionedRetentionCeilingUtilizationAt20"] for row in per_case),
        "candidatePoolCeilingUtilizationAt50": _mean(row["candidatePoolCeilingUtilizationAt50"] for row in per_case),
        "citationAccuracy": citation_correct / citation_count if citation_count else None,
        "citationCorrectCount": citation_correct,
        "citationCount": citation_count,
        "finalCeilingUtilizationAt20": _mean(row["finalCeilingUtilizationAt20"] for row in per_case),
        "ndcgAt10": _mean(row["ndcgAt10"] for row in per_case),
        "orderingEfficiencyAt10": _mean(row["orderingEfficiencyAt10"] for row in per_case),
        "sameCutoffRecallDeltaAt20": _mean(row["sameCutoffRecallDeltaAt20"] for row in per_case),
        "top10HardViolationCount": sum(row["top10HardViolationCount"] for row in per_case),
        "top10HardViolationDenominator": sum(row["top10HardViolationDenominator"] for row in per_case),
        "top10HardViolationRate": _mean(row["top10HardViolationRate"] for row in per_case),
    }
    return {
        "evaluatorIdentity": {
            "evaluatorCodeSha256": _code_hashes(EVALUATOR_FILES),
            "evaluatorIdentityVersion": EVALUATOR_IDENTITY_VERSION,
            "predictionSchemaVersion": _base.PREDICTION_SCHEMA_VERSION,
            "productionCodeScopeSha256": _base.PRODUCTION_CODE_SCOPE_SHA256,
            "protocolVersion": PROTOCOL_VERSION,
            "rankingContractVersion": _base.RANKING_CONTRACT_VERSION,
            "testBlockByteCount": TEST_BLOCK_BYTE_COUNT,
            "testBlockRowCount": TEST_BLOCK_ROW_COUNT,
            "testBlockSha256": TEST_BLOCK_SHA256,
        },
        "inputs": {
            "manifestSha256": manifest_sha, "predictionSha256": prediction_sha,
            "runContractSha256": contract_sha,
            "testPreregistrationSha256": PREREGISTRATION_SHA256,
        },
        "labelBoundary": LABEL_BOUNDARY,
        "metricDefinitions": prereg["metricDefinitions"],
        "metrics": metrics, "perCase": per_case,
        "schemaVersion": SCHEMA_VERSION, "split": "test",
    }
