from __future__ import annotations

import pytest

from agent.evaluation.used_phone_439_retrieval_grid_v1 import (
    active_final_weights,
    final_rerank_score,
    non_hard_structured_completeness,
    weighted_rrf,
)


def test_weighted_rrf_respects_weight_and_deduplicates() -> None:
    ranked = weighted_rrf([[1, 2, 2], [2, 3]], [3.0, 1.0], k=60)
    assert [item[0] for item in ranked] == [2, 1, 3]
    assert ranked[0][1] == pytest.approx(3.0 / 62 + 1.0 / 61)
    assert ranked[1][1] == pytest.approx(3.0 / 61)


def test_weighted_rrf_rejects_invalid_grid() -> None:
    with pytest.raises(ValueError):
        weighted_rrf([[1]], [0.0])


def test_final_weights_renormalize_when_no_soft_preference() -> None:
    weights = active_final_weights(soft_preference_count=0)
    assert weights == pytest.approx({"retrieval": 0.55 / 0.65, "completeness": 0.10 / 0.65})
    score, used = final_rerank_score(
        normalized_rrf=1.0,
        explicit_soft_match=0.0,
        completeness=0.5,
        soft_preference_count=0,
    )
    assert score == pytest.approx(used["retrieval"] + 0.5 * used["completeness"])


def test_completeness_excludes_hard_fields() -> None:
    row = {
        "attributes": {
            "os": {"status": "known"},
            "battery_health": {"status": "known"},
            "screen_originality": {"status": "unknown"},
            "motherboard_repair": {"status": "known"},
            "battery_originality": {"status": "unknown"},
            "scratch_level": {"status": "known"},
            "shell_condition": {"status": "unknown"},
        }
    }
    assert non_hard_structured_completeness(row) == pytest.approx(4 / 7)
    assert non_hard_structured_completeness(row, hard_keys={"os"}) == pytest.approx(3 / 6)
