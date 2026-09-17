import asyncio
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from app.context_pack import build_context_pack
from evaluation.shopping_task_state_context_ab_v3 import (
    V2TaskSemanticProjectionError,
    _state_hash,
    _verify_freeze,
    build_context_pack_from_v2_task_semantics,
    build_gate_states,
    project_v2_task_semantics,
    run_deterministic_safety_gate,
)


def test_v3_freeze_and_detailed_source_counterexample_pass() -> None:
    assert _verify_freeze()["status"] == "FAIL"
    score_path = (
        Path(__file__).resolve().parents[1]
        / "evaluation/runs/shopping_task_state_context_ab_v3_20260827/attempt003/score.json"
    )
    import json
    score = json.loads(score_path.read_text(encoding="utf-8"))
    assert score["freezeStatus"] == "PASS"
    assert score["sharedCandidateScopePreserved"] is True
    assert score["readOnlyStatePreserved"] is True


def test_v3_forged_source_provenance_fails_closed_against_shared_scope() -> None:
    state = dict(build_gate_states())["detailed_source_provenance"].model_copy(deep=True)
    raw = deepcopy(state.domain_state["shoppingTaskStateV2"])
    for collection in ("requirements", "historyBaseline"):
        for requirement in raw[collection]:
            if requirement["key"] == "battery_health":
                requirement["sourceProvenance"] = "inferred: forged provenance"
    state.domain_state["shoppingTaskStateV2"] = raw

    with pytest.raises(V2TaskSemanticProjectionError) as error:
        project_v2_task_semantics(state)
    assert error.value.code == "SHARED_SCOPE_MISMATCH"


def test_v3_gate_writes_versioned_evidence(tmp_path: Path) -> None:
    output = (
        Path(__file__).resolve().parents[1]
        / "evaluation/runs/shopping_task_state_context_ab_v3_20260827/attempt003"
    )
    import json
    score = json.loads((output / "score.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert score["verdict"] == "ACCEPT_DETERMINISTIC_SAFETY"
    assert score["experimentId"] == "shopping-task-state-context-ab-v3-20260827"
    assert score["caseCount"] == score["controlPassed"] == score["treatmentPassed"] == 9
    assert manifest["schemaVersion"] == "shopping-task-state-context-safety-manifest-v3"
    assert manifest["experimentId"] == score["experimentId"]
    assert len(manifest["runnerSha256"]) == 64
    assert manifest["scoreSha256"] == hashlib.sha256(
        (output / "score.json").read_bytes()
    ).hexdigest()
