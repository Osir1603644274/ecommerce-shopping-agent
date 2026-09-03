"""Frozen-threshold decision gate for an authenticated sealed Test10 score."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import used_phone_two_stage_test_scorer_v1 as scorer


SCHEMA_VERSION = "used-phone-two-stage-ranking-test10-gate-v1"
GATE_FILES = {
    **scorer.EVALUATOR_FILES,
    "agent/evaluation/used_phone_two_stage_test_gate_v1.py": Path(__file__).resolve(),
    "agent/scripts/audit_used_phone_two_stage_test_gate_v1.py": scorer.REPO_ROOT / "agent" / "scripts" / "audit_used_phone_two_stage_test_gate_v1.py",
}


def _load_canonical(path: Path, document: str) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {document}") from exc
    if not isinstance(value, dict) or payload != scorer.canonical_bytes(value):
        raise ValueError(f"{document} is not canonical JSON")
    return value, hashlib.sha256(payload).hexdigest()


def evaluate_thresholds(*, score: Mapping[str, Any], manifest: Mapping[str, Any],
                        thresholds: Mapping[str, Any]) -> dict[str, bool]:
    metrics = score.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("Test10 score metrics missing")
    execution = manifest.get("execution")
    return {
        "allTenTerminalSuccess": isinstance(execution, Mapping)
        and list(execution) == list(scorer.TEST_CASE_IDS)
        and all(value is True for value in execution.values())
        and manifest.get("failedCaseIds") == [],
        "noModel": manifest.get("modelCallCount") == 0
        and manifest.get("modelNetworkCallCount") == 0
        and manifest.get("modelNetworkUsed") is False,
        "noHidden": manifest.get("hiddenArtifactsRead") is False,
        "noBusinessWrite": manifest.get("businessWriteNetworkUsed") is False,
        "citationExact": metrics.get("citationAccuracy") == thresholds["citationAccuracyExact"],
        "hardViolationExact": metrics.get("top10HardViolationRate") == thresholds["top10HardViolationRateExact"],
        "sameCutoffNonHarm": metrics.get("sameCutoffRecallDeltaAt20") >= thresholds["sameCutoffRecallDeltaAt20Minimum"],
        "candidateConditionedRetention": metrics.get("candidateConditionedRetentionCeilingUtilizationAt20") >= thresholds["candidateConditionedRetentionCeilingUtilizationAt20Minimum"],
        "orderingEfficiency": metrics.get("orderingEfficiencyAt10") >= thresholds["orderingEfficiencyAt10Minimum"],
        "ndcg": metrics.get("ndcgAt10") >= thresholds["ndcgAt10Minimum"],
        "poolCeiling": metrics.get("candidatePoolCeilingUtilizationAt50") >= thresholds["candidatePoolCeilingUtilizationAt50Minimum"],
        "finalCeiling": metrics.get("finalCeilingUtilizationAt20") >= thresholds["finalCeilingUtilizationAt20Minimum"],
    }


def audit_test_gate(*, predictions_path: Path, manifest_path: Path, score_path: Path,
                    test_judgments_path: Path, test_preregistration_path: Path) -> dict[str, Any]:
    predictions, prediction_sha, manifest_sha, contract_sha = scorer._authenticate_prediction_before_labels(
        predictions_path=predictions_path, manifest_path=manifest_path,
    )
    del predictions
    manifest, _ = _load_canonical(manifest_path.resolve(), "manifest")
    prereg = scorer._load_preregistration(test_preregistration_path, manifest_path.resolve().parent.name)
    expected_score = scorer.score_test10(
        predictions_path=predictions_path, manifest_path=manifest_path,
        test_judgments_path=test_judgments_path,
        test_preregistration_path=test_preregistration_path,
    )
    score, score_sha = _load_canonical(score_path.resolve(), "Test10 score")
    if score != expected_score:
        raise ValueError("Test10 score is not the canonical evaluator result")
    inputs, identity = score.get("inputs"), score.get("evaluatorIdentity")
    if (
        score.get("schemaVersion") != scorer.SCHEMA_VERSION or score.get("split") != "test"
        or score.get("labelBoundary") != scorer.LABEL_BOUNDARY
        or not isinstance(inputs, Mapping) or inputs.get("predictionSha256") != prediction_sha
        or inputs.get("manifestSha256") != manifest_sha
        or inputs.get("runContractSha256") != contract_sha
        or inputs.get("testPreregistrationSha256") != scorer.PREREGISTRATION_SHA256
        or not isinstance(identity, Mapping)
        or identity.get("evaluatorCodeSha256") != scorer._code_hashes(scorer.EVALUATOR_FILES)
        or identity.get("testBlockSha256") != scorer.TEST_BLOCK_SHA256
    ):
        raise ValueError("Test10 score identity/binding mismatch")
    checks = evaluate_thresholds(score=score, manifest=manifest, thresholds=prereg["qualityThresholds"])
    return {
        "checks": checks,
        "evaluatorIdentity": {
            "gateCodeSha256": scorer._code_hashes(GATE_FILES),
            "productionCodeScopeSha256": scorer._base.PRODUCTION_CODE_SCOPE_SHA256,
            "protocolVersion": scorer.PROTOCOL_VERSION,
        },
        "gatePassed": all(checks.values()),
        "inputs": {
            "manifestSha256": manifest_sha, "predictionSha256": prediction_sha,
            "runContractSha256": contract_sha, "scoreSha256": score_sha,
            "testPreregistrationSha256": scorer.PREREGISTRATION_SHA256,
        },
        "labelBoundary": scorer.LABEL_BOUNDARY,
        "metrics": score["metrics"], "perCase": score["perCase"],
        "schemaVersion": SCHEMA_VERSION, "split": "test",
    }
