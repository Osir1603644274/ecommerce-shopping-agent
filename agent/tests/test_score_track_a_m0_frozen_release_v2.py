import pytest

from agent.scripts.score_track_a_m0_frozen_release_v2 import paired_bootstrap, validate_bootstrap_contract


def test_paired_bootstrap_recomputes_both_ndcg_metrics() -> None:
    bm25 = {"q1": {"NDCG@5": 0.1, "NDCG@10": 0.4}, "q2": {"NDCG@5": 0.3, "NDCG@10": 0.2}}
    rrf = {"q1": {"NDCG@5": 0.4, "NDCG@10": 0.5}, "q2": {"NDCG@5": 0.5, "NDCG@10": 0.6}}
    ndcg5 = paired_bootstrap(rrf, bm25, metric="NDCG@5", iterations=100, seed=20260810)
    ndcg10 = paired_bootstrap(rrf, bm25, metric="NDCG@10", iterations=100, seed=20260810)
    assert ndcg5["absoluteDelta"] == pytest.approx(0.25)
    assert ndcg10["absoluteDelta"] == pytest.approx(0.25)
    assert ndcg5["bootstrap95CI"] != ndcg10["bootstrap95CI"]


def test_paired_bootstrap_rejects_unfrozen_metric() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        paired_bootstrap({"q": {"MRR": 1.0}}, {"q": {"MRR": 0.0}}, metric="MRR", iterations=10, seed=1)


def _contract() -> dict:
    return {
        metric: {
            "comparison": "RRF-vs-BM25", "metric": metric, "iterations": 10000, "seed": 20260810,
            "absoluteDelta": 0.1, "bootstrap95CI": [-0.1, 0.2], "pairedSignFlipP": 0.1,
        }
        for metric in ("NDCG@5", "NDCG@10")
    }


def test_bootstrap_contract_requires_both_metrics_and_frozen_parameters() -> None:
    validate_bootstrap_contract(_contract())
    missing = _contract(); del missing["NDCG@10"]
    with pytest.raises(ValueError, match="must freeze"):
        validate_bootstrap_contract(missing)
    wrong_seed = _contract(); wrong_seed["NDCG@10"]["seed"] = 7
    with pytest.raises(ValueError, match="configuration mismatch"):
        validate_bootstrap_contract(wrong_seed)


def test_no_stable_claim_requires_both_intervals_cross_zero() -> None:
    stable = _contract(); stable["NDCG@5"]["bootstrap95CI"] = [0.01, 0.2]
    with pytest.raises(ValueError, match="claim boundary"):
        validate_bootstrap_contract(stable)
