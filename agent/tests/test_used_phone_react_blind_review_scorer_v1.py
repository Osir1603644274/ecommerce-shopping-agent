import json
from pathlib import Path

import pytest

from evaluation.used_phone_react_blind_review_scorer_v1 import score_reviews


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_blind_scorer_unblinds_scores_and_rejects_content_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    completed = tmp_path / "completed.jsonl"
    mapping = tmp_path / "mapping.json"
    output = tmp_path / "score.json"
    base = {
        "schemaVersion": "used-phone-blind-review-item-v1", "itemId": "item-1",
        "scenarioId": "UPHB-V1-015", "candidateA": [], "candidateB": [],
        "rubric": {}, "review": None,
    }
    _write_jsonl(source, [base])
    review = {
        **base,
        "review": {
            "reviewerId": "reviewer-1",
            "candidateA": {
                "constraintFidelity": 5, "evidenceDiscipline": 5,
                "taskProgression": 4, "usefulness": 4,
            },
            "candidateB": {
                "constraintFidelity": 3, "evidenceDiscipline": 3,
                "taskProgression": 2, "usefulness": 2,
            },
            "overallPreference": "A",
        },
    }
    _write_jsonl(completed, [review])
    mapping.write_text(json.dumps({
        "items": [{"itemId": "item-1", "labels": {
            "A": "react_v0", "B": "fixed_v1",
        }}]
    }), encoding="utf-8")

    result = score_reviews(
        source_public=source, completed_review=completed,
        sealed_mapping=mapping, output=output,
    )
    assert result["preferenceCounts"]["react_v0"] == 1
    assert result["dimensionMeans"]["react_v0"]["constraintFidelity"] == 5.0
    assert result["claimBoundary"]["provesGeneralSuperiority"] is False

    mutated = {**review, "scenarioId": "changed"}
    _write_jsonl(completed, [mutated])
    with pytest.raises(ValueError, match="content changed"):
        score_reviews(
            source_public=source, completed_review=completed,
            sealed_mapping=mapping, output=output,
        )
