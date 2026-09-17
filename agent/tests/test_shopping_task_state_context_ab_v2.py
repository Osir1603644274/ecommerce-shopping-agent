import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.context_pack import build_context_pack
from app.domains.ecommerce.shopping_task_state_v2 import ShoppingTaskStateV2
from evaluation.shopping_task_state_context_ab_v1 import build_gate_states
from evaluation.shopping_task_state_context_ab_v2 import (
    ASSET_ROOT,
    V2TaskSemanticProjectionError,
    _state_hash,
    _verify_freeze,
    build_context_pack_from_v2_task_semantics,
    project_v2_task_semantics,
    run_deterministic_safety_gate,
)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_superseded_v2_freeze_rejects_v3_production_contract_but_dataset_holds() -> None:
    freeze = _verify_freeze()
    assert freeze["status"] == "FAIL"
    assert {
        "agent/app/domains/ecommerce/shopping_state_update.py",
        "agent/app/domains/ecommerce/shopping_task_state_v2.py",
    }.issubset({item["path"] for item in freeze["mismatches"]})
    assert freeze["datasetSha256"] == {
        "manifest": "dabd9dc098515a92739ae137e067c313bc5c519b2804142aa77ea91cc401e299",
        "publicScenarios": "44ed3b8995cedd067e2c891fedd8f82c775fc42844dc9c02aad48b030c9e761e",
        "privateExpectations": "7ad739d6c49c5740a802177e5348d05e140d2bf32a3c3b2b604ac578609645ab",
    }


def test_selection_is_read_only_subset_of_original_24_scenarios_65_turns() -> None:
    selection = json.loads((ASSET_ROOT / "selection.json").read_text(encoding="utf-8"))
    original = _read_jsonl(
        ASSET_ROOT.parent
        / "used_phone_harness_behavior_v1_20260825"
        / "public"
        / "scenarios.jsonl"
    )
    assert len(original) == 24
    assert sum(len(row["turns"]) for row in original) == 65
    selected = selection["selectedScenarioIds"]
    assert len(selected) == len(set(selected)) == 8
    assert set(selected).issubset({row["scenarioId"] for row in original})


def test_both_arms_build_and_share_exact_candidate_scope_without_mutation() -> None:
    score = json.loads((
        ASSET_ROOT.parent.parent
        / "runs/shopping_task_state_context_ab_v2_20260827/attempt002/score.json"
    ).read_text(encoding="utf-8"))
    assert score["controlPassed"] == score["treatmentPassed"] == 8
    assert score["sharedCandidateScopePreserved"] is True
    assert score["readOnlyStatePreserved"] is True


def test_treatment_requirements_come_from_v2_not_legacy_compatibility_values() -> None:
    state = dict(build_gate_states())["initial_search"].model_copy(deep=True)
    snapshot = ShoppingTaskStateV2.model_validate(
        deepcopy(state.domain_state["shoppingTaskStateV2"])
    )
    v2_price = next(item.value for item in snapshot.requirements if item.key == "price_minor")
    state.domain_state["shoppingGuide"]["requirements"] = [
        {
            **item,
            "value": 999_999 if item["key"] == "price_minor" else item["value"],
        }
        for item in state.domain_state["shoppingGuide"]["requirements"]
    ]

    projected = project_v2_task_semantics(state)
    requirements = projected.domain_state["shoppingGuide"]["requirements"]
    projected_price = next(item for item in requirements if item["key"] == "price_minor")
    assert projected_price["value"] == v2_price == 220_000


def test_scope_identity_mismatch_fails_closed() -> None:
    state = dict(build_gate_states())["active_candidate_scope"].model_copy(deep=True)
    state.domain_state["candidateScope"]["scopeId"] = "scope-forged"
    with pytest.raises(V2TaskSemanticProjectionError) as error:
        project_v2_task_semantics(state)
    assert error.value.code == "SHARED_SCOPE_MISMATCH"


def test_superseded_v2_gate_writes_hold_evidence_and_blocks_live(tmp_path: Path) -> None:
    output = (
        ASSET_ROOT.parent.parent
        / "runs/shopping_task_state_context_ab_v2_20260827/attempt002"
    )
    score = json.loads((output / "score.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert score["verdict"] == "ACCEPT_DETERMINISTIC_SAFETY"
    assert score["safetyGatePassed"] is True
    assert score["controlPassed"] == score["treatmentPassed"] == score["caseCount"] == 8
    assert score["readOnlyStatePreserved"] is True
    assert score["sharedCandidateScopePreserved"] is True
    assert score["freezeStatus"] == "PASS"
    assert score["nextGate"] == "LIVE_PAIRED_RUN"
    assert manifest["productionContractChanged"] is False
    assert manifest["livePairedRunExecuted"] is False
    assert manifest["humanReviewPacketGenerated"] is False

    receipts = output / "receipts.jsonl"
    persisted_score = output / "score.json"
    assert manifest["receiptSha256"] == hashlib.sha256(receipts.read_bytes()).hexdigest()
    assert manifest["scoreSha256"] == hashlib.sha256(persisted_score.read_bytes()).hexdigest()
