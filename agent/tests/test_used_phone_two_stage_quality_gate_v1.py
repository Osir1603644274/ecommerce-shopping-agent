from agent.evaluation.used_phone_two_stage_quality_gate_v1 import evaluate_gate_metrics


def _row(case_id, pool=30, final=20):
    return {
        "caseId": case_id,
        "relevantTotal": 60,
        "candidatePoolRelevantHitCount": pool,
        "finalRelevantHitCount": final,
        "candidatePoolCeilingUtilizationAt50": pool / 50,
        "finalCeilingUtilizationAt20": final / 20,
        "sameCutoffRecallDeltaAt20": 0.1,
        "ndcgAt10": 1.0,
        "orderingEfficiencyAt10": 1.0,
        "citationCorrectCount": 2,
        "citationCount": 2,
        "top10HardViolationRate": 0.0,
    }


def test_gate_uses_candidate_conditioned_ceiling_not_raw_retention():
    rows = [_row(f"UPV2-RK-D{i:02d}") for i in range(1, 11)]
    thresholds = {
        "retrievalMissTotalMaximum": 300,
        "candidatePoolCeilingUtilizationAt50Minimum": 0.6,
        "d10PoolRelevantHitCountMinimum": 6,
        "maximumPerCasePoolRelevantHitCountRegression": 2,
        "sameCutoffRecallDeltaAt20Minimum": 0,
        "candidateConditionedRetentionCeilingUtilizationAt20Minimum": 0.95,
        "finalCeilingUtilizationAt20Minimum": 0.78,
        "orderingEfficiencyAt10Minimum": 0.99,
        "ndcgAt10Minimum": 0.86,
        "citationAccuracyExact": 1.0,
        "top10HardViolationRateExact": 0.0,
    }
    manifest = {
        "execution": {row["caseId"]: True for row in rows},
        "failedCaseIds": [], "modelCallCount": 0, "modelNetworkCallCount": 0,
        "hiddenArtifactsRead": False, "businessWriteNetworkUsed": False,
    }
    metrics, checks = evaluate_gate_metrics(
        diagnostics=rows, manifest=manifest, thresholds=thresholds,
        baseline_pool_hits={row["caseId"]: 30 for row in rows},
    )
    assert metrics["candidateConditionedRetentionCeilingUtilizationAt20"] == 1.0
    assert all(checks.values())


def test_gate_fails_execution_boundary_and_per_case_regression():
    rows = [_row(f"UPV2-RK-D{i:02d}") for i in range(1, 11)]
    rows[0]["candidatePoolRelevantHitCount"] = 20
    thresholds = {
        "retrievalMissTotalMaximum": 400,
        "candidatePoolCeilingUtilizationAt50Minimum": 0.5,
        "d10PoolRelevantHitCountMinimum": 6,
        "maximumPerCasePoolRelevantHitCountRegression": 2,
        "sameCutoffRecallDeltaAt20Minimum": 0,
        "candidateConditionedRetentionCeilingUtilizationAt20Minimum": 0.9,
        "finalCeilingUtilizationAt20Minimum": 0.78,
        "orderingEfficiencyAt10Minimum": 0.99,
        "ndcgAt10Minimum": 0.86,
        "citationAccuracyExact": 1.0,
        "top10HardViolationRateExact": 0.0,
    }
    manifest = {
        "execution": {row["caseId"]: True for row in rows},
        "failedCaseIds": [], "modelCallCount": 1, "modelNetworkCallCount": 0,
        "hiddenArtifactsRead": False, "businessWriteNetworkUsed": False,
    }
    _, checks = evaluate_gate_metrics(
        diagnostics=rows, manifest=manifest, thresholds=thresholds,
        baseline_pool_hits={row["caseId"]: 30 for row in rows},
    )
    assert checks["noModel"] is False
    assert checks["perCasePoolNonRegression"] is False
