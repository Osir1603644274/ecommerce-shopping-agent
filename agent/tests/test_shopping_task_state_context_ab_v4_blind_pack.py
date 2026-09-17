import hashlib
import json
from pathlib import Path

import pytest

from evaluation.shopping_task_state_context_ab_v4_blind_pack import build_pack


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path: Path) -> dict[str, Path]:
    dataset = tmp_path / "dataset.jsonl"
    control = tmp_path / "control.jsonl"
    treatment = tmp_path / "treatment.jsonl"
    score = tmp_path / "score.json"
    live_manifest = tmp_path / "live-manifest.json"
    _write_jsonl(dataset, [{
        "scenarioId": "S1",
        "turns": [
            {"turnId": "T1", "text": "first"},
            {"turnId": "T2", "text": "second"},
        ],
    }])
    _write_jsonl(control, [
        {"scenarioId": "S1", "turnId": "T1", "answer": "same"},
        {"scenarioId": "S1", "turnId": "T2", "answer": "answer one", "requestId": "c"},
    ])
    _write_jsonl(treatment, [
        {"scenarioId": "S1", "turnId": "T1", "answer": "same"},
        {"scenarioId": "S1", "turnId": "T2", "answer": "answer two", "requestId": "t"},
    ])
    _write_json(score, {
        "experimentId": "experiment",
        "verdict": "READY_FOR_BLIND_REVIEW",
        "liveSafetyPassed": True,
        "treatmentRegressions": [],
        "commonActionFailures": [],
        "userVisibleDifferenceTurns": 1,
    })
    _write_json(live_manifest, {
        "controlReceiptsSha256": _sha256(control),
        "treatmentReceiptsSha256": _sha256(treatment),
        "scoreSha256": _sha256(score),
    })
    return {
        "dataset": dataset,
        "control_receipts": control,
        "treatment_receipts": treatment,
        "live_score": score,
        "live_manifest": live_manifest,
        "reviewer_one_output": tmp_path / "reviewer-01.jsonl",
        "reviewer_two_output": tmp_path / "reviewer-02.jsonl",
        "sealed_mapping_output": tmp_path / "sealed-mapping.json",
        "manifest_output": tmp_path / "manifest.json",
    }


def test_blind_pack_is_label_free_and_exactly_mirrored(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    manifest = build_pack(**paths, seed="random-seed")

    one = json.loads(paths["reviewer_one_output"].read_text(encoding="utf-8"))
    two = json.loads(paths["reviewer_two_output"].read_text(encoding="utf-8"))
    assert one["candidateA"] == two["candidateB"]
    assert one["candidateB"] == two["candidateA"]
    assert one["userTurns"][-1] == {"turnId": "T2", "text": "second"}
    for public_path in (paths["reviewer_one_output"], paths["reviewer_two_output"]):
        public_text = public_path.read_text(encoding="utf-8")
        assert "control" not in public_text
        assert "treatment" not in public_text
        assert "requestId" not in public_text
        assert "scenarioId" not in public_text
    mapping = json.loads(paths["sealed_mapping_output"].read_text(encoding="utf-8"))
    assert mapping["items"][0]["reviewer01"]["A"] == mapping["items"][0]["reviewer02"]["B"]
    assert manifest["status"] == "AWAITING_TWO_HUMAN_BLIND_REVIEWS"
    assert manifest["answerQualityStatus"] == "HOLD_PENDING_TWO_HUMAN_BLIND_REVIEWS"
    assert len(manifest["generatorSha256"]) == 64


def test_blind_pack_fails_closed_when_live_score_hash_changes(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    paths["live_score"].write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not safe"):
        build_pack(**paths, seed="random-seed")
