from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent.evaluation.react_v1_architecture_24_blind_pack_v1 import (
    _hash_text,
    _hash_value,
    build_pack,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    pair = tmp_path / "paired"
    dataset = tmp_path / "scenarios.jsonl"
    scenarios = []
    fixed_rows = []
    react_rows = []
    for number in range(1, 25):
        scenario_id = f"scenario-{number:03d}"
        turns = [{"text": "第一问"}]
        scenarios.append({"scenarioId": scenario_id, "turns": turns})
        fixed_rows.append({"scenarioId": scenario_id, "turnIndex": 1, "answer": "相同", "requestId": f"f-{number}"})
        react_rows.append({"scenarioId": scenario_id, "turnIndex": 1, "answer": "相同", "requestId": f"r-{number}"})
    for extra in range(2, 11):
        scenario_id = f"scenario-{extra:03d}"
        scenarios[extra - 1]["turns"].append({"text": "第二问"})
        fixed_rows.append({"scenarioId": scenario_id, "turnIndex": 2, "answer": "旧回答", "requestId": f"f-{extra}-2"})
        react_rows.append({"scenarioId": scenario_id, "turnIndex": 2, "answer": "新回答", "requestId": f"r-{extra}-2"})
    _write_jsonl(dataset, scenarios)
    fixed_receipts = pair / "fixed" / "receipts.jsonl"
    react_receipts = pair / "react" / "receipts.jsonl"
    _write_jsonl(fixed_receipts, fixed_rows)
    _write_jsonl(react_receipts, react_rows)
    fixed_manifest = {"status": "ACCEPT", "scenarioCount": 24, "receiptsSha256": _sha(fixed_receipts)}
    react_manifest = {"status": "ACCEPT", "scenarioCount": 24, "receiptsSha256": _sha(react_receipts)}
    fixed_manifest_path = pair / "fixed" / "manifest.json"
    react_manifest_path = pair / "react" / "manifest.json"
    _write_json(fixed_manifest_path, fixed_manifest)
    _write_json(react_manifest_path, react_manifest)
    binding = {"packageFilesSha256": {"public/scenarios.jsonl": _sha(dataset)}}
    binding["bindingSha256"] = _hash_value(binding)
    _write_json(pair / "run_binding.json", binding)
    candidates = [{
        "scenarioId": "scenario-002",
        "turnIndex": 2,
        "fixedAnswerSha256": _hash_text("旧回答"),
        "reactAnswerSha256": _hash_text("新回答"),
    }]
    _write_json(pair / "paired_result.json", {
        "status": "ACCEPT", "blindPackEligible": True, "bothArmsSafetyAccept": True,
        "coverage": "full_suite", "selectedScenarioCount": 24, "packageScenarioCount": 24,
        "bindingSha256": binding["bindingSha256"],
        "fixedManifestSha256": _sha(fixed_manifest_path),
        "reactManifestSha256": _sha(react_manifest_path),
        "behaviorDifferenceCandidates": candidates,
    })
    return pair, dataset


def test_builds_mirrored_anonymous_full_dialogues(tmp_path: Path) -> None:
    pair, dataset = _fixture(tmp_path)
    output = tmp_path / "blind"
    manifest = build_pack(pair_dir=pair, dataset=dataset, output_dir=output, seed="secret")
    one = json.loads((output / "reviewer01.jsonl").read_text(encoding="utf-8"))
    two = json.loads((output / "reviewer02.jsonl").read_text(encoding="utf-8"))
    assert manifest["status"] == "AWAITING_TWO_HUMAN_BLIND_REVIEWS"
    assert len(one["candidateA"]["dialogue"]) == 4
    assert one["candidateA"] == two["candidateB"]
    assert one["candidateB"] == two["candidateA"]
    public_text = (output / "reviewer01.jsonl").read_text(encoding="utf-8")
    assert "react_v1" not in public_text and "fixed_v1" not in public_text
    assert "requestId" not in public_text and "scenario-002" not in public_text


def test_rejects_hold_pair(tmp_path: Path) -> None:
    pair, dataset = _fixture(tmp_path)
    paired_path = pair / "paired_result.json"
    paired = json.loads(paired_path.read_text(encoding="utf-8"))
    paired["status"] = "HOLD"
    _write_json(paired_path, paired)
    with pytest.raises(ValueError, match="safety gate"):
        build_pack(pair_dir=pair, dataset=dataset, output_dir=tmp_path / "blind", seed="secret")
