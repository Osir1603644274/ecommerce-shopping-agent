import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from run_kuaisearch_lite_phone_behavior_memory_v2 import (  # noqa: E402
    evaluate,
    rerank,
    title_tokens,
    validation_gate,
)


def meta(brand, seller, title):
    return {"brand": brand, "seller": seller, "titleTokens": title_tokens(title)}


CONFIG = {
    "alpha": 0.3,
    "repeatCap": 3,
    "titleHistoryCap": 5,
    "halfLifeClicks": 5,
    "featureWeights": {"brand": 0.4, "seller": 0.1, "title": 0.5},
}


def test_governed_behavior_profile_preserves_candidates_and_differs_from_raw_brand():
    items = {
        1: meta("a", "s1", "华为 mate 60 pro"),
        2: meta("a", "s2", "苹果 iphone 15"),
        3: meta("b", "s1", "华为 mate 60 pro"),
    }
    for item_id in range(4, 22):
        items[item_id] = meta("z", f"seller-{item_id}", f"unrelated-{item_id}")
    baseline = [2, 3, *range(4, 22)]
    raw, _ = rerank(baseline, [1], items, arm="B", config=CONFIG)
    governed, _ = rerank(baseline, [1], items, arm="C", config=CONFIG)
    assert set(raw) == set(governed) == set(baseline)
    assert raw != governed
    assert governed[0] == 3


def test_evaluation_keeps_no_positive_sessions_in_demonstrated_denominator():
    items = {1: meta("a", "s", "手机 a"), 2: meta("a", "s", "手机 a2"), 3: meta("b", "t", "手机 b")}
    records = {7: [
        {"history": [1], "candidates": [3, 2], "positives": {2}},
        {"history": [1], "candidates": [3, 2], "positives": set()},
    ]}
    result = evaluate([7], records, items, CONFIG, bootstrap_seed=1, bootstrap_samples=100)
    assert result["counts"]["candidateAtLeastTwo"] == 2
    assert result["counts"]["noFuturePositiveInCandidates"] == 1
    assert result["futurePositiveSessionCoverage"] == 0.5
    assert result["demonstratedUtilityMacroUser"]["C"]["hitAt3"] == 0.5


def test_validation_gate_fails_closed_on_candidate_set_failure():
    result = {
        "evaluableUsers": 100, "candidateAtLeast2Coverage": 1.0,
        "evaluableUserCoverage": 1.0,
        "cMinusABootstrap95User": {"ndcgAt10": [0.01, 0.02], "hitAt3": [0, 0.01], "mrr": [0, 0.01]},
        "candidateSetFailures": 1, "hardFilteredCandidates": 0,
        "bCDivergenceSessions": 1, "changeRate": 0.2,
        "rerankLatencyMs": {"p95": 1.0}, "profileBytes": {"p95": 100.0},
    }
    gates = {
        "minimumEvaluableUsers": 60, "minimumCandidateAtLeast2Coverage": 0.8,
        "minimumEvaluableUserCoverage": 0.8, "ndcgBootstrapLowerGreaterThan": 0,
        "hitBootstrapLowerAtLeast": -0.01, "mrrBootstrapLowerAtLeast": -0.01,
        "minimumChangeRate": 0.05, "maximumChangeRate": 0.8,
        "maximumRerankP95Ms": 10, "maximumProfileP95Bytes": 4096,
    }
    assert validation_gate(result, gates)["decision"] == "HOLD"
