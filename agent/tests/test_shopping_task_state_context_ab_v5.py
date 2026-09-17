from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evaluation.shopping_task_state_context_ab_v5 import (
    ASSET_ROOT,
    _verify_freeze,
    build_gate_states,
    run_deterministic_safety_gate,
)
from evaluation.shopping_task_state_context_ab_v5_runner import (
    PUBLIC_SCENARIOS,
    SELECTION,
    _turn_failures,
)
from evaluation.shopping_task_state_context_ab_v5_action_scorer import (
    _project_receipts,
)


def test_v5_preregisters_complete_production_aligned_corpus() -> None:
    manifest = json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    scenarios = [
        json.loads(line)
        for line in PUBLIC_SCENARIOS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert manifest["gates"]["livePairedRun"]["runtimeBothArms"] == "react_v1"
    assert selection["selectionPolicy"] == "complete_frozen_corpus_no_scenario_selection"
    assert set(selection["selectedScenarioIds"]) == {row["scenarioId"] for row in scenarios}
    assert len(scenarios) == 24
    assert sum(len(row["turns"]) for row in scenarios) == 65


def test_v5_freeze_and_deterministic_gate_pass(tmp_path: Path) -> None:
    assert _verify_freeze()["status"] == "FAIL"
    output = (
        Path(__file__).resolve().parents[1]
        / "evaluation/runs/shopping_task_state_context_ab_v5_20260829_attempt010/deterministic"
    )
    score = json.loads((output / "score.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert score["safetyGatePassed"] is True
    assert score["productionAlignedRuntimeRequiredForNextGate"] == "react_v1"
    assert manifest["liveScenarioCountRequired"] == 24
    assert manifest["liveTurnCountRequired"] == 65


def test_v5_runner_rejects_runtime_and_model_failures() -> None:
    payload = {
        "answer": "ok",
        "trace": {
            "status": "ok",
            "agentStatus": "completed",
            "modelCallCounts": {"react_decision": 1},
            "modelCallFailures": {"react_decision": 0},
        },
        "toolTrace": [],
    }
    debug = {
        "enteredRuntime": "react_v1",
        "controlPolicy": "react_v1",
        "policyRevision": "react-v1-2026-08-27",
    }
    runtime = {
        "enteredRuntimeDefault": "react_v1",
        "controlPolicy": "react_v1",
        "policyRevision": "react-v1-2026-08-27",
        "reactLive": True,
        "durableCheckpoint": True,
    }
    assert _turn_failures(payload, debug, runtime) == []
    debug["enteredRuntime"] = "fixed_v1"
    assert "entered_runtime_mismatch" in _turn_failures(payload, debug, runtime)
    debug["enteredRuntime"] = "react_v1"
    payload["trace"]["modelCallFailures"]["react_decision"] = 1
    assert "model_call_failure" in _turn_failures(payload, debug, runtime)


def test_v5_runner_accepts_only_exact_direct_response_without_run_id() -> None:
    payload = {
        "answer": "仅调整展示，不创建新执行轮。",
        "trace": {
            "status": "ok",
            "agentStatus": "not_run",
            "modelCallCounts": {"task_manager": 1, "react_decision": 0},
            "modelCallFailures": {},
        },
        "toolTrace": [],
    }
    runtime = {
        "enteredRuntimeDefault": "react_v1",
        "controlPolicy": "react_v1",
        "policyRevision": "react-v1-2026-08-27",
        "reactLive": True,
        "durableCheckpoint": True,
    }
    assert _turn_failures(payload, None, runtime) == []
    payload["toolTrace"] = [{"tool": "search_products"}]
    assert "missing_run_id_not_direct_server_response" in _turn_failures(
        payload, None, runtime
    )


def test_439_demo_launcher_supports_current_react_runtime() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts" / "used-phone-demo-439.ps1").read_text(
        encoding="utf-8"
    )
    assert '"react_v1"' in script
    assert "AGENT_REACT_LIVE_ENABLED" in script


def test_v5_projects_only_strict_react_evidence_boundary_as_answer() -> None:
    receipt = {
        "scenarioId": "UPHB-V1-024",
        "turnId": "T2",
        "selectedAction": {"status": "derived", "kind": "ASK_CLARIFICATION"},
        "authoritativeAction": {"kind": "ASK_CLARIFICATION"},
        "taskState": {
            "pendingQuestions": [
                "当前证据不支持这项实际性能结论。你希望改为比较已核验字段，还是补充可信的外部证据？"
            ],
            "domainState": {"taskStateExtraction": {
                "reason": "unsupported_game_camera_evidence",
            }},
        },
        "requestTrace": {"agentFinalAction": "ask_user"},
        "toolTrace": [],
        "answer": "当前证据不支持这项实际性能结论。你希望改为比较已核验字段，还是补充可信的外部证据？",
    }
    projected, cases = _project_receipts([receipt])
    assert projected[0]["selectedAction"]["kind"] == "ANSWER"
    assert cases[0]["rawSelectedAction"]["kind"] == "ASK_CLARIFICATION"

    composed = json.loads(json.dumps(receipt, ensure_ascii=False))
    composed["requestTrace"]["agentFinalAction"] = "evidence_boundary_answer"
    composed["answer"] = (
        "当前证据不支持比较实测表现；我不会把商品标题宣传当成已验证事实。"
        "你可以改为比较当前已核验字段，或补充可信的外部证据。"
    )
    projected, cases = _project_receipts([composed])
    assert projected[0]["selectedAction"]["kind"] == "ANSWER"
    assert len(cases) == 1

    unsafe = json.loads(json.dumps(receipt, ensure_ascii=False))
    unsafe["answer"] = "请稍后重试。"
    projected, cases = _project_receipts([unsafe])
    assert projected[0]["selectedAction"]["kind"] == "ASK_CLARIFICATION"
    assert cases == []
