from agent.evaluation.used_phone_two_stage_validation_gate_v1 import evaluate_thresholds
from agent.evaluation.used_phone_two_stage_validation_scorer_v1 import VALIDATION_CASE_IDS


def _thresholds():
    return {
        "candidateConditionedRetentionCeilingUtilizationAt20Minimum": 0.95,
        "candidatePoolCeilingUtilizationAt50Minimum": 0.558,
        "citationAccuracyExact": 1.0,
        "finalCeilingUtilizationAt20Minimum": 0.785,
        "ndcgAt10Minimum": 0.8678886164857375,
        "orderingEfficiencyAt10Minimum": 0.99,
        "sameCutoffRecallDeltaAt20Minimum": 0.0,
        "top10HardViolationRateExact": 0.0,
    }


def _manifest():
    return {
        "businessWriteNetworkUsed": False,
        "execution": {case_id: True for case_id in VALIDATION_CASE_IDS},
        "failedCaseIds": [],
        "hiddenArtifactsRead": False,
        "modelCallCount": 0,
        "modelNetworkCallCount": 0,
        "modelNetworkUsed": False,
    }


def _score():
    return {"metrics": {
        "candidateConditionedRetentionCeilingUtilizationAt20": 0.95,
        "candidatePoolCeilingUtilizationAt50": 0.558,
        "citationAccuracy": 1.0,
        "finalCeilingUtilizationAt20": 0.785,
        "ndcgAt10": 0.8678886164857375,
        "orderingEfficiencyAt10": 0.99,
        "sameCutoffRecallDeltaAt20": 0.0,
        "top10HardViolationRate": 0.0,
    }}


def test_validation_gate_uses_frozen_inclusive_thresholds():
    checks = evaluate_thresholds(
        score=_score(), manifest=_manifest(), thresholds=_thresholds()
    )
    assert len(checks) == 12
    assert all(checks.values())


def test_validation_gate_fails_each_boundary_without_moving_thresholds():
    score = _score()
    score["metrics"]["sameCutoffRecallDeltaAt20"] = -1e-12
    manifest = _manifest()
    manifest["modelNetworkUsed"] = True
    checks = evaluate_thresholds(
        score=score, manifest=manifest, thresholds=_thresholds()
    )
    assert checks["sameCutoffNonHarm"] is False
    assert checks["noModel"] is False
