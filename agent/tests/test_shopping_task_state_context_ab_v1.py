import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app.context_pack import build_context_pack
from evaluation.shopping_task_state_context_ab_v1 import (
    ASSET_ROOT,
    V2ContextProjectionError,
    _state_hash,
    _verify_freeze,
    build_context_pack_from_v2,
    build_gate_states,
    run_deterministic_safety_gate,
)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_preregistered_source_freeze_and_original_dataset_identity_hold() -> None:
    freeze = _verify_freeze()
    assert freeze["status"] == "FAIL"
    assert freeze["mismatches"]
    assert freeze["datasetSha256"] == {
        "manifest": "dabd9dc098515a92739ae137e067c313bc5c519b2804142aa77ea91cc401e299",
        "publicScenarios": "44ed3b8995cedd067e2c891fedd8f82c775fc42844dc9c02aad48b030c9e761e",
        "privateExpectations": "7ad739d6c49c5740a802177e5348d05e140d2bf32a3c3b2b604ac578609645ab",
    }


def test_selection_is_a_read_only_subset_of_original_24_scenarios_65_turns() -> None:
    selection = json.loads((ASSET_ROOT / "selection.json").read_text(encoding="utf-8"))
    original = _read_jsonl(
        ASSET_ROOT.parent
        / "used_phone_harness_behavior_v1_20260825"
        / "public"
        / "scenarios.jsonl"
    )
    assert len(original) == 24
    assert sum(len(row["turns"]) for row in original) == 65
    original_ids = {row["scenarioId"] for row in original}
    selected = selection["selectedScenarioIds"]
    assert len(selected) == len(set(selected)) == 8
    assert set(selected).issubset(original_ids)


def test_control_context_builds_but_v2_treatment_fails_closed_without_mutation() -> None:
    score = json.loads((
        ASSET_ROOT.parent.parent
        / "runs/shopping_task_state_context_ab_v1_20260826/attempt002/score.json"
    ).read_text(encoding="utf-8"))
    assert score["controlPassed"] == score["caseCount"] == 8
    assert score["treatmentPassed"] == 0
    assert score["readOnlyStatePreserved"] is True


def test_gate_writes_immutable_evidence_and_blocks_live_and_human_stages(tmp_path: Path) -> None:
    output = (
        ASSET_ROOT.parent.parent
        / "runs/shopping_task_state_context_ab_v1_20260826/attempt002"
    )
    score = json.loads((output / "score.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert score["verdict"] == "HOLD_V2_CONTEXT_SCHEMA_INSUFFICIENT"
    assert score["safetyGatePassed"] is False
    assert score["controlPassed"] == score["caseCount"] == 8
    assert score["treatmentPassed"] == 0
    assert score["readOnlyStatePreserved"] is True
    assert score["callAttribution"]["status"] == "DETERMINISTIC_GATE_NO_MODEL_OR_TOOL"
    assert manifest["livePairedRunExecuted"] is False
    assert manifest["humanReviewPacketGenerated"] is False

    receipts = output / "receipts.jsonl"
    persisted_score = output / "score.json"
    assert manifest["receiptSha256"] == hashlib.sha256(receipts.read_bytes()).hexdigest()
    assert manifest["scoreSha256"] == hashlib.sha256(persisted_score.read_bytes()).hexdigest()
