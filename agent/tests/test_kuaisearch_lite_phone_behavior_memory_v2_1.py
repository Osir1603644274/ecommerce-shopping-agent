import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from run_kuaisearch_lite_phone_behavior_memory_v2_1 import (  # noqa: E402
    evaluate,
    route_recall_line,
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
    "featureWeights": {"brand": 0.25, "seller": 0.1, "title": 0.65},
}


def test_sealed_route_never_calls_json_decoder():
    def fail_if_called(_):
        raise AssertionError("sealed row was decoded")

    route, user_id, row = route_recall_line(
        b'{"user_id": 9,"clicked_item_ids":[123]}', {1}, {9}, loads=fail_if_called,
    )
    assert (route, user_id, row) == ("sealed", 9, None)


def test_public_route_decodes_only_after_envelope_match():
    calls = []

    def loads(raw):
        calls.append(raw)
        return {"user_id": 1, "clicked_item_ids": [123]}

    route, user_id, row = route_recall_line(
        b'{"user_id": 1,"clicked_item_ids":[123]}', {1}, {9}, loads=loads,
    )
    assert route == "public" and user_id == 1 and row["clicked_item_ids"] == [123]
    assert len(calls) == 1


def test_demonstrated_utility_includes_all_requested_users_as_zero():
    items = {
        1: meta("a", "s", "华为 手机"),
        2: meta("a", "s", "华为 手机 pro"),
        3: meta("b", "t", "苹果 手机"),
    }
    records = {
        7: [{
            "history": [1], "candidates": [3, 2], "futureStrictClicks": {2},
        }],
        8: [{
            "history": [1], "candidates": [3, 2], "futureStrictClicks": set(),
        }],
    }
    result = evaluate(
        [7, 8, 9], records, items, CONFIG,
        bootstrap_seed=1, bootstrap_samples=100, include_per_user=True,
    )
    assert result["requestedUsers"] == 3
    assert result["evaluableUsers"] == 1
    assert result["futurePositiveSessionCoverage"] == 0.5
    assert len(result["perUser"]) == 3
    user9 = next(row for row in result["perUser"] if row["userId"] == 9)
    assert user9["demonstrated"]["C"] == {
        "ndcgAt10": 0.0, "hitAt3": 0.0, "mrr": 0.0,
    }
    assert result["demonstratedUtilityMacroAllRequestedUsers"]["C"]["hitAt3"] == 1 / 3
    assert len(result["demonstratedCMinusABootstrap95AllRequestedUsers"]["ndcgAt10"]) == 2


def valid_result():
    return {
        "evaluableUsers": 100,
        "candidateAtLeast2Coverage": 1.0,
        "futurePositiveSessionCoverage": 0.6,
        "evaluableUserCoverage": 1.0,
        "conditionalCMinusABootstrap95User": {
            "ndcgAt10": [0.01, 0.02], "hitAt3": [0.0, 0.01], "mrr": [0.0, 0.01],
        },
        "demonstratedCMinusABootstrap95AllRequestedUsers": {
            "ndcgAt10": [0.005, 0.01], "hitAt3": [0.0, 0.01], "mrr": [0.0, 0.01],
        },
        "candidateSetFailures": 0,
        "inputDuplicateCandidateSessions": 0,
        "hardFilteredCandidates": 0,
        "bCDivergenceSessions": 1,
        "changeRate": 0.2,
        "rerankLatencyMs": {"p95": 1.0},
        "profileBytes": {"p95": 100.0},
    }


GATES = {
    "minimumEvaluableUsers": 60,
    "minimumCandidateAtLeast2Coverage": 0.8,
    "minimumFuturePositiveSessionCoverage": 0.5,
    "minimumEvaluableUserCoverage": 0.8,
    "conditionalNdcgBootstrapLowerGreaterThan": 0.0,
    "conditionalHitBootstrapLowerAtLeast": -0.01,
    "conditionalMrrBootstrapLowerAtLeast": -0.01,
    "demonstratedNdcgBootstrapLowerGreaterThan": 0.0,
    "demonstratedHitBootstrapLowerAtLeast": -0.01,
    "demonstratedMrrBootstrapLowerAtLeast": -0.01,
    "minimumChangeRate": 0.05,
    "maximumChangeRate": 0.8,
    "maximumRerankP95Ms": 10.0,
    "maximumProfileP95Bytes": 4096,
}


def test_gate_requires_future_positive_coverage_and_demonstrated_ci():
    result = valid_result()
    assert validation_gate(result, GATES)["decision"] == "ACCEPT"
    result["futurePositiveSessionCoverage"] = 0.49
    assert validation_gate(result, GATES)["decision"] == "HOLD"
    result = valid_result()
    result["demonstratedCMinusABootstrap95AllRequestedUsers"]["ndcgAt10"] = [-0.001, 0.01]
    assert validation_gate(result, GATES)["decision"] == "HOLD"


def test_gate_fails_on_duplicate_input_candidates():
    result = valid_result()
    result["inputDuplicateCandidateSessions"] = 1
    assert validation_gate(result, GATES)["decision"] == "HOLD"
