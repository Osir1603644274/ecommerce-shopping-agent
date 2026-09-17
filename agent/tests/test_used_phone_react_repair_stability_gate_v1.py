import json
from pathlib import Path

from evaluation.used_phone_react_repair_stability_gate_v1 import (
    evaluate_repair_stability,
)


SCENARIOS = {
    "UPRG-V1-002": ("T1", "T2", "T3"),
    "UPRG-V1-003": ("T1", "T2", "T3", "T4"),
    "UPRG-V1-006": ("T1", "T2"),
}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    dataset = tmp_path / "dataset.jsonl"
    _write_jsonl(dataset, [
        {
            "scenarioId": scenario_id,
            "turns": [{"turnId": turn_id, "text": "x"} for turn_id in turns],
        }
        for scenario_id, turns in SCENARIOS.items()
    ])
    run_root = tmp_path / "run-1"
    rows: list[dict] = []
    for scenario_id, turns in SCENARIOS.items():
        for turn_id in turns:
            route = "deterministic_complete"
            reason = "complete_controlled_coverage"
            kinds = ["CALL_TOOL", "ANSWER"]
            answer = "safe"
            if (scenario_id, turn_id) == ("UPRG-V1-002", "T2"):
                route = "deterministic_capability_boundary"
                reason = "unsupported_game_camera_evidence"
                kinds = ["ANSWER"]
                answer = "没有帧率和散热证据，不能根据标题判断。"
            elif (scenario_id, turn_id) == ("UPRG-V1-002", "T3"):
                route = "deterministic_validated_scope_answer"
                reason = "validated_scope_answer"
                kinds = ["ANSWER"]
            elif (scenario_id, turn_id) == ("UPRG-V1-003", "T4"):
                route = "deterministic_stale_scope_clarification"
                reason = "stale_candidate_reference"
                kinds = ["ASK_CLARIFICATION"]
                answer = "这两个来自旧筛选范围，请重新指定。"
            price = 160_000 if (scenario_id, turn_id) == ("UPRG-V1-006", "T2") else 220_000
            actions = [
                {
                    "status": "accepted",
                    "kind": kind,
                    "optionId": f"option.{index}",
                    "publishedOptionIds": [f"option.{index}"],
                }
                for index, kind in enumerate(kinds)
            ]
            rows.append({
                "scenarioId": scenario_id,
                "turnId": turn_id,
                "requestId": f"req-{scenario_id}-{turn_id}",
                "status": "ok",
                "enteredRuntime": "react_v0",
                "runtimeMatches": True,
                "answer": answer,
                "runnerDurationMs": 10,
                "modelAttribution": {"modelCallCounts": {}},
                "reactSequence": {
                    "actions": actions,
                    "outcomes": [{"status": "SUCCEEDED"} for _ in actions],
                },
                "taskState": {"domainState": {
                    "taskStateExtraction": {"route": route, "reason": reason},
                    "shoppingGuide": {"requirements": [{
                        "key": "price_minor", "value": price,
                    }]},
                }},
                "guideResult": {"products": [{"product": {
                    "syntheticReferencePriceMinor": min(price, 150_000),
                }}]},
            })
    receipts = run_root / "run" / "receipts.jsonl"
    _write_jsonl(receipts, rows)
    import hashlib

    _write_json(run_root / "run" / "manifest.json", {
        "status": "COMPLETE",
        "expectedRuntime": "react_v0",
        "datasetSha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "errorCount": 0,
        "runtimeMismatchCount": 0,
    })
    return dataset, run_root


def test_repair_stability_gate_accepts_complete_deterministic_replay(tmp_path: Path) -> None:
    dataset, run_root = _fixture(tmp_path)

    report = evaluate_repair_stability(dataset_path=dataset, run_roots=[run_root])

    assert report["status"] == "ACCEPT_REPAIR_STABILITY"
    assert report["turnCount"] == 9
    assert report["failureCount"] == 0
    assert report["claimBoundary"]["provesReactSuperiority"] is False


def test_repair_stability_gate_holds_model_call_and_budget_regression(tmp_path: Path) -> None:
    dataset, run_root = _fixture(tmp_path)
    receipts = run_root / "run" / "receipts.jsonl"
    rows = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines()]
    target = next(
        row for row in rows
        if row["scenarioId"] == "UPRG-V1-006" and row["turnId"] == "T2"
    )
    target["modelAttribution"]["modelCallCounts"] = {"final_answer": 1}
    target["taskState"]["domainState"]["shoppingGuide"]["requirements"][0]["value"] = 220_000
    _write_jsonl(receipts, rows)

    report = evaluate_repair_stability(dataset_path=dataset, run_roots=[run_root])

    assert report["status"] == "HOLD"
    codes = {failure["code"] for failure in report["failures"]}
    assert {"unexpected_model_call", "budget_replacement_failed"} <= codes
