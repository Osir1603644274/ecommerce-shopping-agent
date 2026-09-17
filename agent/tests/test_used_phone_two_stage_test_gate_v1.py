from agent.evaluation.used_phone_two_stage_test_gate_v1 import evaluate_thresholds
from agent.evaluation.used_phone_two_stage_test_scorer_v1 import TEST_CASE_IDS


def test_test_gate_uses_unchanged_validation_thresholds():
    thresholds = {
        "candidateConditionedRetentionCeilingUtilizationAt20Minimum": 0.95,
        "candidatePoolCeilingUtilizationAt50Minimum": 0.558,
        "citationAccuracyExact": 1.0,
        "finalCeilingUtilizationAt20Minimum": 0.785,
        "ndcgAt10Minimum": 0.8678886164857375,
        "orderingEfficiencyAt10Minimum": 0.99,
        "sameCutoffRecallDeltaAt20Minimum": 0.0,
        "top10HardViolationRateExact": 0.0,
    }
    score = {"metrics": {
        "candidateConditionedRetentionCeilingUtilizationAt20": 0.95,
        "candidatePoolCeilingUtilizationAt50": 0.558,
        "citationAccuracy": 1.0, "finalCeilingUtilizationAt20": 0.785,
        "ndcgAt10": 0.8678886164857375, "orderingEfficiencyAt10": 0.99,
        "sameCutoffRecallDeltaAt20": 0.0, "top10HardViolationRate": 0.0,
    }}
    manifest = {
        "execution": {case_id: True for case_id in TEST_CASE_IDS},
        "failedCaseIds": [], "hiddenArtifactsRead": False,
        "businessWriteNetworkUsed": False, "modelCallCount": 0,
        "modelNetworkCallCount": 0, "modelNetworkUsed": False,
    }
    assert all(evaluate_thresholds(
        score=score, manifest=manifest, thresholds=thresholds
    ).values())
