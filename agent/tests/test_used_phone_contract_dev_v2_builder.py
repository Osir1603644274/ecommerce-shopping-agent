import json

import pytest

from agent.evaluation.used_phone_contract_dev_v2_builder import (
    build,
    definitions,
    score_predictions,
)


def test_definitions_are_public_dev_only_and_cover_five_delta_operations():
    rows = definitions()
    assert len(rows) == 30
    cases = [case for case, _expected in rows]
    assert {case["split"] for case in cases} == {"dev"}
    assert sum(len(case["turns"]) == 2 for case in cases) == 12
    text = "\n".join(turn["text"] for case in cases for turn in case["turns"])
    for cue in ("保留", "改成", "再加", "取消", "改为优先", "改为硬条件"):
        assert cue in text


def test_build_is_deterministic_and_pins_public_assets(tmp_path):
    first = build(tmp_path / "first")
    second = build(tmp_path / "second")
    assert first == second
    assert first["sealed"] is False
    assert first["splitCounts"] == {"dev": 30}
    for name in ("cases_public.jsonl", "expected_public.jsonl", "manifest.json"):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()


def test_public_scorer_uses_frozen_predictions_and_requires_exact_state(tmp_path):
    output = tmp_path / "data"
    build(output)
    predictions = []
    for case, expected in definitions():
        predictions.append({
            "caseId": case["caseId"],
            "predictedAction": expected["expectedAction"],
            "predictedConstraints": expected["expectedState"],
            "rankedItemIds": [],
        })
    predictions_path = tmp_path / "predictions.jsonl"
    predictions_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in predictions),
        encoding="utf-8",
    )
    result = score_predictions(predictions_path, output / "expected_public.jsonl")
    assert result["passRate"] == 1.0

    predictions[0]["predictedConstraints"] = {"hard": [], "soft": []}
    predictions_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in predictions),
        encoding="utf-8",
    )
    result = score_predictions(predictions_path, output / "expected_public.jsonl")
    assert result["passCount"] == 29


def test_public_scorer_rejects_missing_prediction_identity(tmp_path):
    output = tmp_path / "data"
    build(output)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text('{"caseId":"UNKNOWN"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        score_predictions(predictions, output / "expected_public.jsonl")


def test_public_scorer_accepts_non_empty_registered_subset(tmp_path):
    output = tmp_path / "data"
    build(output)
    case, expected = definitions()[0]
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps({
        "caseId": case["caseId"],
        "predictedAction": expected["expectedAction"],
        "predictedConstraints": expected["expectedState"],
        "rankedItemIds": [],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    result = score_predictions(predictions, output / "expected_public.jsonl")
    assert result["caseCount"] == 1
    assert result["passRate"] == 1.0

    predictions.write_text(json.dumps({
        "caseId": case["caseId"],
        "predictedAction": expected["expectedAction"],
        "predictedConstraints": {
            "hard": list(reversed(expected["expectedState"]["hard"])),
            "soft": list(reversed(expected["expectedState"]["soft"])),
        },
        "rankedItemIds": [],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    assert score_predictions(
        predictions, output / "expected_public.jsonl"
    )["passRate"] == 1.0
