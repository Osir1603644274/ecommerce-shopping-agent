"""Prediction-first Dev10 quality gate for post-baseline v3 attempts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import used_phone_two_stage_ranking_scorer_v3 as base
from .used_phone_two_stage_ranking_metric_audit_v1 import _case_diagnostics


SCHEMA_VERSION = "used-phone-two-stage-ranking-public-dev-quality-gate-v1"
PREREGISTRATION_FILENAME = "public_dev_quality_gate_preregistration_v1.json"
PREREGISTRATION_SHA256 = (
    "805464b348ae3b74b4f3d6efab19d5342f5be7373f958611c853e036b7d70496"
)
EVALUATOR_FILES = {
    "agent/evaluation/used_phone_two_stage_ranking_scorer_v3.py": (
        base.REPO_ROOT / "agent" / "evaluation"
        / "used_phone_two_stage_ranking_scorer_v3.py"
    ),
    "agent/evaluation/used_phone_two_stage_ranking_metric_audit_v1.py": (
        base.REPO_ROOT / "agent" / "evaluation"
        / "used_phone_two_stage_ranking_metric_audit_v1.py"
    ),
    "agent/evaluation/used_phone_two_stage_quality_gate_v1.py": Path(__file__).resolve(),
    "agent/scripts/audit_used_phone_two_stage_quality_gate_v1.py": (
        base.REPO_ROOT / "agent" / "scripts"
        / "audit_used_phone_two_stage_quality_gate_v1.py"
    ),
}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _load_preregistration(path: Path, attempt_id: str) -> dict[str, Any]:
    resolved = path.resolve()
    if (
        resolved.parent != base.EVALUATOR_ASSET_DIR.resolve()
        or resolved.name != PREREGISTRATION_FILENAME
    ):
        raise ValueError("quality-gate preregistration path mismatch")
    payload = resolved.read_bytes()
    if hashlib.sha256(payload).hexdigest() != PREREGISTRATION_SHA256:
        raise ValueError("quality-gate preregistration SHA mismatch")
    value = json.loads(payload)
    if payload != base.canonical_bytes(value):
        raise ValueError("quality-gate preregistration must be canonical JSON")
    if (
        value.get("schemaVersion")
        != "used-phone-two-stage-ranking-public-dev-quality-gate-preregistration-v1"
        or value.get("split") != "dev"
        or value.get("frozenBeforeProductionModification") is not True
        or attempt_id not in value.get("authorizedAttemptIds", [])
    ):
        raise ValueError("quality-gate preregistration semantic mismatch")
    return value


def _load_bound_score(path: Path, expected: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if payload != base.canonical_bytes(value) or value != expected:
        raise ValueError("score is not the canonical result for the authenticated prediction")
    return value, hashlib.sha256(payload).hexdigest()


def evaluate_gate_metrics(
    *, diagnostics: list[dict[str, Any]], manifest: Mapping[str, Any],
    thresholds: Mapping[str, Any], baseline_pool_hits: Mapping[str, int],
) -> tuple[dict[str, Any], dict[str, bool]]:
    retrieval_misses = sum(
        row["relevantTotal"] - row["candidatePoolRelevantHitCount"]
        for row in diagnostics
    )
    conditioned = _mean([
        row["finalRelevantHitCount"]
        / min(20, row["candidatePoolRelevantHitCount"])
        if row["candidatePoolRelevantHitCount"] else 0.0
        for row in diagnostics
    ])
    metrics = {
        "candidateConditionedRetentionCeilingUtilizationAt20": conditioned,
        "candidatePoolCeilingUtilizationAt50": _mean([
            row["candidatePoolCeilingUtilizationAt50"] for row in diagnostics
        ]),
        "citationAccuracy": (
            sum(row["citationCorrectCount"] for row in diagnostics)
            / sum(row["citationCount"] for row in diagnostics)
            if sum(row["citationCount"] for row in diagnostics) else None
        ),
        "d10PoolRelevantHitCount": next(
            row["candidatePoolRelevantHitCount"]
            for row in diagnostics if row["caseId"] == "UPV2-RK-D10"
        ),
        "finalCeilingUtilizationAt20": _mean([
            row["finalCeilingUtilizationAt20"] for row in diagnostics
        ]),
        "ndcgAt10": _mean([row["ndcgAt10"] for row in diagnostics]),
        "orderingEfficiencyAt10": _mean([
            row["orderingEfficiencyAt10"] for row in diagnostics
        ]),
        "retrievalMissTotal": retrieval_misses,
        "sameCutoffRecallDeltaAt20": _mean([
            row["sameCutoffRecallDeltaAt20"] for row in diagnostics
        ]),
        "top10HardViolationRate": _mean([
            row["top10HardViolationRate"] for row in diagnostics
        ]),
    }
    execution = manifest.get("execution", {})
    checks = {
        "allTenTerminalSuccess": (
            isinstance(execution, Mapping)
            and len(execution) == 10
            and all(value is True for value in execution.values())
            and manifest.get("failedCaseIds") == []
        ),
        "noModel": manifest.get("modelCallCount") == 0
        and manifest.get("modelNetworkCallCount") == 0,
        "noHidden": manifest.get("hiddenArtifactsRead") is False,
        "noBusinessWrite": manifest.get("businessWriteNetworkUsed") is False,
        "retrievalMissImproved": metrics["retrievalMissTotal"]
        <= thresholds["retrievalMissTotalMaximum"],
        "poolCeilingImproved": metrics["candidatePoolCeilingUtilizationAt50"]
        >= thresholds["candidatePoolCeilingUtilizationAt50Minimum"],
        "d10Improved": metrics["d10PoolRelevantHitCount"]
        >= thresholds["d10PoolRelevantHitCountMinimum"],
        "perCasePoolNonRegression": all(
            row["candidatePoolRelevantHitCount"]
            >= baseline_pool_hits[row["caseId"]]
            - thresholds["maximumPerCasePoolRelevantHitCountRegression"]
            for row in diagnostics
        ),
        "sameCutoffNonHarm": metrics["sameCutoffRecallDeltaAt20"]
        >= thresholds["sameCutoffRecallDeltaAt20Minimum"],
        "candidateConditionedRetention": metrics[
            "candidateConditionedRetentionCeilingUtilizationAt20"
        ] >= thresholds["candidateConditionedRetentionCeilingUtilizationAt20Minimum"],
        "finalCeilingNonRegression": metrics["finalCeilingUtilizationAt20"]
        >= thresholds["finalCeilingUtilizationAt20Minimum"],
        "orderingEfficiency": metrics["orderingEfficiencyAt10"]
        >= thresholds["orderingEfficiencyAt10Minimum"],
        "ndcgNonRegression": metrics["ndcgAt10"] >= thresholds["ndcgAt10Minimum"],
        "citationExact": metrics["citationAccuracy"] == thresholds["citationAccuracyExact"],
        "hardViolationExact": metrics["top10HardViolationRate"]
        == thresholds["top10HardViolationRateExact"],
    }
    return metrics, checks


def audit_quality_gate(
    *, predictions_path: Path, manifest_path: Path, score_path: Path,
    public_dev_judgments_path: Path, public_dev_preregistration_path: Path,
    quality_gate_preregistration_path: Path,
) -> dict[str, Any]:
    predictions, prediction_sha, manifest_sha, contract_sha = (
        base._authenticate_prediction_before_labels(
            predictions_path=predictions_path, manifest_path=manifest_path
        )
    )
    manifest, _ = base._load_canonical_json_snapshot(
        manifest_path.resolve(), document="manifest"
    )
    attempt_id = manifest_path.resolve().parent.name
    prereg = _load_preregistration(quality_gate_preregistration_path, attempt_id)
    expected_score = base.score_public_dev(
        predictions_path=predictions_path,
        manifest_path=manifest_path,
        public_dev_judgments_path=public_dev_judgments_path,
        public_dev_preregistration_path=public_dev_preregistration_path,
    )
    _score, score_sha = _load_bound_score(score_path, expected_score)
    judgments = base._load_public_dev_judgments(public_dev_judgments_path)
    diagnostics = [
        _case_diagnostics(row, judgments[row["caseId"]]) for row in predictions
    ]
    metrics, checks = evaluate_gate_metrics(
        diagnostics=diagnostics,
        manifest=manifest,
        thresholds=prereg["comparisonRules"],
        baseline_pool_hits=prereg["baseline"]["perCasePoolRelevantHitCount"],
    )
    return {
        "checks": checks,
        "evaluatorIdentity": {
            "evaluatorCodeSha256": base._code_hashes(EVALUATOR_FILES),
            "productionCodeScopeSha256": base.PRODUCTION_CODE_SCOPE_SHA256,
            "protocolVersion": base.PROTOCOL_VERSION,
        },
        "gatePassed": all(checks.values()),
        "inputs": {
            "manifestSha256": manifest_sha,
            "predictionSha256": prediction_sha,
            "qualityGatePreregistrationSha256": PREREGISTRATION_SHA256,
            "runContractSha256": contract_sha,
            "scoreSha256": score_sha,
        },
        "labelBoundary": "AI-designed product-grounded, not human gold",
        "metrics": metrics,
        "perCase": diagnostics,
        "schemaVersion": SCHEMA_VERSION,
        "split": "dev",
    }
