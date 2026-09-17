import hashlib
import json
from pathlib import Path

import pytest

from evaluation.shopping_task_state_context_ab_v4_blind_scorer import score_reviews


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review(pref: str, a: int, b: int) -> dict:
    names = ("constraintFidelity", "evidenceDiscipline", "taskProgression", "usefulness")
    return {
        "reviewerId": "reviewer",
        "candidateA": {name: a for name in names},
        "candidateB": {name: b for name in names},
        "overallPreference": pref,
    }


def _inputs(tmp_path: Path) -> dict[str, Path]:
    public_one = tmp_path / "public-one.jsonl"
    public_two = tmp_path / "public-two.jsonl"
    completed_one = tmp_path / "completed-one.jsonl"
    completed_two = tmp_path / "completed-two.jsonl"
    mapping = tmp_path / "mapping.json"
    attestation = tmp_path / "attestation.json"
    base = {"itemId": "blind-01", "candidateA": "x", "candidateB": "y", "review": None}
    mirrored = {"itemId": "blind-01", "candidateA": "y", "candidateB": "x", "review": None}
    _write_jsonl(public_one, [base])
    _write_jsonl(public_two, [mirrored])
    _write_jsonl(completed_one, [{**base, "review": _review("B", 2, 5)}])
    _write_jsonl(completed_two, [{**mirrored, "review": _review("A", 5, 2)}])
    _write_json(mapping, {"items": [{
        "itemId": "blind-01",
        "source": {"scenarioId": "S1", "turnId": "T1"},
        "reviewer01": {"A": "control", "B": "treatment"},
        "reviewer02": {"A": "treatment", "B": "control"},
    }]})
    _write_json(attestation, {
        "experimentId": "experiment",
        "humanReviewerAttested": True,
        "independentReviewAttested": True,
        "mappingBlindAttested": True,
        "reviewer01CompletedSha256": _sha(completed_one),
        "reviewer02CompletedSha256": _sha(completed_two),
    })
    return {
        "reviewer_one_public": public_one,
        "reviewer_one_completed": completed_one,
        "reviewer_two_public": public_two,
        "reviewer_two_completed": completed_two,
        "sealed_mapping": mapping,
        "attestation": attestation,
        "output": tmp_path / "score.json",
    }


def test_scores_mirrored_reviews_by_underlying_arm(tmp_path: Path) -> None:
    result = score_reviews(**_inputs(tmp_path))
    assert result["dimensionMeans"]["control"]["usefulness"] == 2
    assert result["dimensionMeans"]["treatment"]["usefulness"] == 5
    assert result["preferenceCountsAcrossReviews"]["treatment"] == 2
    assert result["preferenceAgreement"]["rate"] == 1
    assert result["agreedItemPreferenceCounts"]["treatment"] == 1
    assert result["evidenceInterpretation"].startswith("MIXED_DESCRIPTIVE_SIGNAL")
    assert result["productionDecision"] == "HOLD"


def test_fails_closed_without_complete_attestation(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    value = json.loads(paths["attestation"].read_text(encoding="utf-8"))
    value["mappingBlindAttested"] = False
    _write_json(paths["attestation"], value)
    with pytest.raises(ValueError, match="attestations are required"):
        score_reviews(**paths)
