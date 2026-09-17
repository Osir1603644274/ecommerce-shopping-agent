from __future__ import annotations

from agent.evaluation.used_phone_439_retrieval_review_package_v2 import (
    HARD_REQUIREMENTS,
    SOFT_PREFERENCES,
    _review_contract,
    _taskstate_rows,
)


def _queries() -> list[dict]:
    return [
        {
            "scenarioId": query_id,
            "rawQuery": query_id,
            "split": "development",
        }
        for query_id in HARD_REQUIREMENTS
    ]


def test_frozen_intent_maps_have_exact_24_query_coverage() -> None:
    assert len(HARD_REQUIREMENTS) == 24
    assert set(HARD_REQUIREMENTS) == set(SOFT_PREFERENCES)
    rows = _taskstate_rows(_queries())
    assert len(rows) == 24
    assert all(row["status"] == "FROZEN_EXPERIMENT_INPUT" for row in rows)


def test_ambiguous_around_prices_are_soft_not_hard() -> None:
    assert all(
        item["key"] != "price_minor"
        for item in HARD_REQUIREMENTS["uphqv2-008"]
    )
    assert any(
        item["key"] == "price_target_minor"
        for item in SOFT_PREFERENCES["uphqv2-008"]
    )
    assert all(
        item["key"] != "price_minor"
        for item in HARD_REQUIREMENTS["uphqv2-012"]
    )


def test_review_contract_uses_pooled_recall_and_holds_default() -> None:
    contract = _review_contract()
    assert "pooledRecallAt50" in contract["metrics"]["names"]
    assert contract["metrics"]["directoryRecallClaimForbidden"] is True
    assert contract["selection"]["productionDefault"].startswith("HOLD")
