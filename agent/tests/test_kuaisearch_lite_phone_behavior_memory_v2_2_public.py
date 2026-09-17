import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from run_kuaisearch_lite_phone_behavior_memory_v2_2_public import (  # noqa: E402
    build_records,
    partition_bucket,
    public_gate,
    route_recall_line,
)


SALT = "kuaisearch-memory-v2-2-confirmation-20260830"


def authority_user():
    return next(user_id for user_id in range(1, 10000) if partition_bucket(user_id, SALT) >= 50)


def public_user():
    return next(user_id for user_id in range(1, 10000) if partition_bucket(user_id, SALT) < 50)


def test_partition_is_deterministic_and_routes_before_decode():
    sealed_id = authority_user()

    def fail_if_called(_):
        raise AssertionError("authority row was decoded")

    raw = f'{{"user_id": {sealed_id},"clicked_item_ids":[1]}}'.encode()
    route, user_id, row = route_recall_line(
        raw,
        set(),
        salt=SALT,
        public_bucket_upper_exclusive=50,
        loads=fail_if_called,
    )
    assert (route, user_id, row) == ("authority", sealed_id, None)
    assert partition_bucket(sealed_id, SALT) == partition_bucket(sealed_id, SALT)


def test_consumed_user_is_never_decoded_even_if_public_bucket():
    user_id = public_user()

    def fail_if_called(_):
        raise AssertionError("consumed row was decoded")

    route, routed_id, row = route_recall_line(
        f'{{"user_id": {user_id},"clicked_item_ids":[1]}}'.encode(),
        {user_id},
        salt=SALT,
        public_bucket_upper_exclusive=50,
        loads=fail_if_called,
    )
    assert (route, routed_id, row) == ("consumed", user_id, None)


def test_public_user_is_decoded_after_routing():
    user_id = public_user()
    calls = []

    def loads(raw):
        calls.append(raw)
        return {"user_id": user_id, "clicked_item_ids": [1]}

    route, routed_id, row = route_recall_line(
        f'{{"user_id": {user_id},"clicked_item_ids":[1]}}'.encode(),
        set(),
        salt=SALT,
        public_bucket_upper_exclusive=50,
        loads=loads,
    )
    assert route == "public" and routed_id == user_id and row["clicked_item_ids"] == [1]
    assert len(calls) == 1


def test_eligibility_does_not_require_future_click_outcome():
    raw_sessions = {
        7: [
            {"sessionId": 1, "timeIndex": 1, "candidates": [], "clicked": [11]},
            {"sessionId": 2, "timeIndex": 2, "candidates": [12, 13], "clicked": []},
        ],
        8: [
            {"sessionId": 1, "timeIndex": 1, "candidates": [12, 13], "clicked": []},
        ],
    }
    records, eligible = build_records(raw_sessions, minimum_history=1)
    assert eligible == [7]
    assert records[7][0]["futureStrictClicks"] == set()
    assert records[7][0]["history"] == [11]


def valid_result():
    return {
        "requestedUsers": 200,
        "candidateAtLeast2Coverage": 0.9,
        "futurePositiveSessionCoverage": 0.5,
        "evaluableUserCoverage": 0.6,
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
    "minimumEligibleUsers": 150,
    "minimumCandidateAtLeast2Coverage": 0.8,
    "minimumFuturePositiveSessionCoverage": 0.4,
    "minimumPositiveLabelUserCoverage": 0.4,
    "conditionalNdcgLowerGreaterThan": 0.0,
    "conditionalHitLowerAtLeast": -0.01,
    "conditionalMrrLowerAtLeast": -0.01,
    "demonstratedNdcgLowerGreaterThan": 0.0,
    "demonstratedHitLowerAtLeast": -0.01,
    "demonstratedMrrLowerAtLeast": -0.01,
    "minimumChangeRate": 0.05,
    "maximumChangeRate": 0.8,
    "maximumRerankP95Ms": 10.0,
    "maximumProfileP95Bytes": 4096,
}


def test_public_gate_requires_all_user_demonstrated_signal_and_coverage():
    result = valid_result()
    assert public_gate(result, GATES)["decision"] == "ACCEPT"
    result["demonstratedCMinusABootstrap95AllRequestedUsers"]["ndcgAt10"] = [-0.001, 0.01]
    assert public_gate(result, GATES)["decision"] == "HOLD"
    result = valid_result()
    result["futurePositiveSessionCoverage"] = 0.39
    assert public_gate(result, GATES)["decision"] == "HOLD"
