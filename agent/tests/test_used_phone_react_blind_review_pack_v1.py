import json
from pathlib import Path

from evaluation.used_phone_react_blind_review_pack_v1 import build_pack


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_blind_pack_hides_runtime_labels_and_preserves_sealed_mapping(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    scenarios = []
    react_rows = []
    fixed_rows = []
    for scenario_id in ("UPHB-V1-015", "UPHB-V1-022", "UPHB-V1-024"):
        scenarios.append({
            "scenarioId": scenario_id,
            "turns": [{"turnId": "T1", "text": f"question {scenario_id}"}],
        })
        react_rows.append({
            "scenarioId": scenario_id, "turnId": "T1",
            "answer": f"react answer {scenario_id}", "requestId": f"react-{scenario_id}",
        })
        fixed_rows.append({
            "scenarioId": scenario_id, "turnId": "T1",
            "answer": f"fixed answer {scenario_id}", "requestId": f"fixed-{scenario_id}",
        })
    _write_jsonl(dataset, scenarios)
    react = tmp_path / "react.jsonl"
    fixed = tmp_path / "fixed.jsonl"
    _write_jsonl(react, react_rows)
    _write_jsonl(fixed, fixed_rows)
    public = tmp_path / "public.jsonl"
    mapping = tmp_path / "mapping.json"
    manifest_path = tmp_path / "manifest.json"

    manifest = build_pack(
        dataset=dataset, react_paths=[react], fixed_paths=[fixed],
        public_output=public, mapping_output=mapping,
        manifest_output=manifest_path, seed="test-seed",
    )

    public_text = public.read_text(encoding="utf-8")
    assert '"react_v0"' not in public_text
    assert '"fixed_v1"' not in public_text
    assert "requestId" not in public_text
    assert len(public_text.splitlines()) == 3
    mapping_value = json.loads(mapping.read_text(encoding="utf-8"))
    assert len(mapping_value["items"]) == 3
    assert set(mapping_value["items"][0]["labels"].values()) == {
        "react_v0", "fixed_v1"
    }
    assert manifest["status"] == "READY_FOR_INDEPENDENT_REVIEW"
    assert manifest["answerQualityStatus"] == "HOLD_PENDING_INDEPENDENT_REVIEW"


def test_blind_pack_accepts_explicit_heldout_targets(tmp_path: Path) -> None:
    targets = ("UPRG-V1-001", "UPRG-V1-002")
    dataset = tmp_path / "dataset.jsonl"
    _write_jsonl(dataset, [
        {
            "scenarioId": scenario_id,
            "turns": [{"turnId": "T1", "text": f"question {scenario_id}"}],
        }
        for scenario_id in targets
    ])
    react = tmp_path / "react.jsonl"
    fixed = tmp_path / "fixed.jsonl"
    _write_jsonl(react, [
        {
            "scenarioId": scenario_id, "turnId": "T1",
            "answer": f"react {scenario_id}",
        }
        for scenario_id in targets
    ])
    _write_jsonl(fixed, [
        {
            "scenarioId": scenario_id, "turnId": "T1",
            "answer": f"fixed {scenario_id}",
        }
        for scenario_id in targets
    ])

    manifest = build_pack(
        dataset=dataset,
        react_paths=[react],
        fixed_paths=[fixed],
        public_output=tmp_path / "public.jsonl",
        mapping_output=tmp_path / "mapping.json",
        manifest_output=tmp_path / "manifest.json",
        seed="heldout-test",
        target_scenarios=targets,
    )

    assert manifest["scenarioIds"] == list(targets)
    assert manifest["itemCount"] == 2
