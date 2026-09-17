import hashlib
import json
from pathlib import Path

from evaluation.used_phone_react_generalization_scorer_v1 import (
    score_generalization,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    dataset = tmp_path / "dataset.jsonl"
    scenarios = [
        {
            "scenarioId": "G-A",
            "generalizationClass": "adaptive_needed",
            "turns": [{"turnId": "T1", "text": "zero"}],
        },
        {
            "scenarioId": "G-C",
            "generalizationClass": "deterministic_control",
            "turns": [{"turnId": "T1", "text": "search"}],
        },
        {
            "scenarioId": "G-N",
            "generalizationClass": "negative_control",
            "turns": [{"turnId": "T1", "text": "clarify"}],
        },
    ]
    _write_jsonl(dataset, scenarios)
    preregistration = tmp_path / "preregistration.json"
    preregistration.write_text(json.dumps({
        "datasetSha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "pairedRunCount": 1,
        "adaptiveTargets": {
            "G-A:T1": {
                "requiredPrefix": ["CALL_TOOL"],
                "allowedTerminalKinds": ["ANSWER"],
                "requiredToolErrorCode": "product_candidates_missing",
                "requiredReactDecisionCalls": 1,
                "requiredModelAfterObservation": True,
            }
        },
        "reactDecisionForbiddenClasses": [
            "deterministic_control", "negative_control",
        ],
    }), encoding="utf-8")
    react = tmp_path / "react.jsonl"
    base = {
        "status": "ok",
        "enteredRuntime": "react_v0",
        "runtimeMatches": True,
        "answer": "safe answer",
        "runnerDurationMs": 10,
    }
    _write_jsonl(react, [
        {
            **base, "scenarioId": "G-A", "turnId": "T1",
            "answer": "没有帧率和散热数据，不能判断",
            "modelAttribution": {
                "modelCallCounts": {"react_decision": 1},
                "llmDurationByStageMs": {"react_decision": 5},
            },
            "reactSequence": {
                "actions": [
                    {
                        "status": "accepted",
                        "decisionSource": "deterministic_policy",
                        "kind": "CALL_TOOL",
                        "optionId": "tool.search",
                        "publishedOptionIds": ["tool.search"],
                    },
                    {
                        "status": "accepted",
                        "decisionSource": "model",
                        "kind": "ANSWER",
                        "optionId": "answer.zero",
                        "publishedOptionIds": ["answer.zero", "clarify.zero"],
                    },
                ],
                "outcomes": [
                    {"errorCode": "product_candidates_missing"},
                    {"status": "SUCCEEDED"},
                ],
            },
        },
        {
            **base, "scenarioId": "G-C", "turnId": "T1",
            "modelAttribution": {"modelCallCounts": {}},
            "reactSequence": {
                "actions": [{
                    "status": "accepted", "kind": "CALL_TOOL",
                    "optionId": "tool.search",
                    "publishedOptionIds": ["tool.search"],
                }],
                "outcomes": [{"status": "SUCCEEDED"}],
            },
        },
        {
            **base, "scenarioId": "G-N", "turnId": "T1",
            "modelAttribution": {"modelCallCounts": {}},
            "reactSequence": {
                "actions": [{
                    "status": "accepted", "kind": "ASK_CLARIFICATION",
                    "optionId": "clarify.pending.0",
                    "publishedOptionIds": ["clarify.pending.0"],
                }],
                "outcomes": [{"status": "INTERRUPTED"}],
            },
        },
    ])
    fixed = tmp_path / "fixed.jsonl"
    _write_jsonl(fixed, [
        {
            "scenarioId": scenario_id,
            "turnId": "T1",
            "status": "ok",
            "runtimeMatches": True,
            "answer": "fixed answer",
            "runnerDurationMs": 5,
        }
        for scenario_id in ("G-A", "G-C", "G-N")
    ])
    return dataset, preregistration, react, fixed


def test_generalization_scorer_accepts_preregistered_contract(tmp_path: Path) -> None:
    dataset, preregistration, react, fixed = _fixture(tmp_path)

    report = score_generalization(
        dataset_path=dataset,
        preregistration_path=preregistration,
        react_paths=[react],
        fixed_paths=[fixed],
    )

    assert report["status"] == "ACCEPT"
    assert report["publishedOptionViolationCount"] == 0
    assert report["reactDecisionCallsOutsideAdaptiveTargets"] == 0
    assert report["humanReviewStatus"] == "HOLD_PENDING_INDEPENDENT_REVIEW"


def test_generalization_scorer_holds_model_call_in_control(tmp_path: Path) -> None:
    dataset, preregistration, react, fixed = _fixture(tmp_path)
    rows = [json.loads(line) for line in react.read_text(encoding="utf-8").splitlines()]
    rows[1]["modelAttribution"] = {
        "modelCallCounts": {"react_decision": 1},
    }
    _write_jsonl(react, rows)

    report = score_generalization(
        dataset_path=dataset,
        preregistration_path=preregistration,
        react_paths=[react],
        fixed_paths=[fixed],
    )

    assert report["status"] == "HOLD"
    assert report["reactDecisionCallsOutsideAdaptiveTargets"] == 1
    assert any(
        failure["code"] == "react_decision_in_forbidden_class"
        for failure in report["failures"]
    )
