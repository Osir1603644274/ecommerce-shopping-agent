import pytest

from agent.evaluation.product_retrieval_architecture_experiment_v1 import (
    decide,
    percentile,
    validate_trace_grid,
)


def _score(*, ndcg: float, recall: float, p95: float, vector: int) -> dict:
    return {
        "queryCount": 5,
        "failedCallCount": 0,
        "repeatRankingMismatchQueryCount": 0,
        "metrics": {
            "pooledNdcgAt10": ndcg,
            "judgedRelevantPoolRecallAt50": recall,
            "judgedGradeAtLeast2HitAt3": 1.0,
        },
        "hardConstraint": {"top3ViolationCount": 0},
        "latencyMs": {"p95": p95},
        "vectorActiveCanonicalQueries": vector,
    }


def test_percentile_uses_observed_nearest_rank():
    assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.5) == 3.0
    assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.95) == 5.0


def test_decision_selects_hybrid_only_as_next_shadow_candidate():
    decision = decide(
        {
            "bm25": _score(ndcg=0.40, recall=0.60, p95=500.0, vector=0),
            "hybrid": _score(ndcg=0.41, recall=0.70, p95=700.0, vector=5),
        },
        prior_ce={"stableImprovement": False, "latencyMultiplierVsRrf": 1.6},
        prior_broad={
            "g1": {"rrfMrrAdvantageCiExcludesZero": True},
            "phaseA": {"processDecision": "NO_RETRIEVAL_WINNER"},
        },
    )
    assert decision["status"] == "HOLD_TARGET_ARCHITECTURE_EVIDENCE_REPAIR_REQUIRED"
    assert decision["selectedCandidateGeneration"] == "hybrid_rrf_bm25_dense_structured_next_shadow_candidate"
    assert decision["crossEncoder"] == "disabled_no_stable_gain"
    assert decision["onlineDefaultSwitch"] == "HOLD_NO_CHANGE_AUTHORIZED"


def test_decision_holds_when_hybrid_is_materially_worse():
    decision = decide(
        {
            "bm25": _score(ndcg=0.40, recall=0.70, p95=500.0, vector=0),
            "hybrid": _score(ndcg=0.35, recall=0.60, p95=700.0, vector=5),
        },
        prior_ce={"stableImprovement": False, "latencyMultiplierVsRrf": 1.6},
        prior_broad={
            "g1": {"rrfMrrAdvantageCiExcludesZero": False},
            "phaseA": {"processDecision": "NO_RETRIEVAL_WINNER"},
        },
    )
    assert decision["status"] == "HOLD_TARGET_ARCHITECTURE_EVIDENCE_REPAIR_REQUIRED"
    assert decision["selectedCandidateGeneration"] == "bm25_structured_current_safe_default"


def test_shadow_nomination_rejects_catastrophic_current_lane_regression():
    decision = decide(
        {
            "bm25": _score(ndcg=0.60, recall=0.80, p95=500.0, vector=0),
            "hybrid": _score(ndcg=0.30, recall=0.40, p95=700.0, vector=5),
        },
        prior_ce={"stableImprovement": False, "latencyMultiplierVsRrf": 1.6},
        prior_broad={"g1": {"rrfMrrAdvantageCiExcludesZero": True}, "phaseA": {}},
    )
    assert decision["selectedCandidateGeneration"] == "bm25_structured_current_safe_default"


def test_trace_grid_rejects_duplicate_measured_rows():
    query_ids = ["q1"]
    rows = [
        {"phase": "warmup", "mode": "bm25", "repeat": 0, "queryId": "q1"},
        {"phase": "warmup", "mode": "hybrid", "repeat": 0, "queryId": "q1"},
        {"phase": "measured", "mode": "bm25", "repeat": 1, "queryId": "q1"},
        {"phase": "measured", "mode": "hybrid", "repeat": 1, "queryId": "q1"},
        {"phase": "measured", "mode": "hybrid", "repeat": 1, "queryId": "q1"},
    ]
    with pytest.raises(ValueError, match="grid"):
        validate_trace_grid(rows, query_ids, 1)
