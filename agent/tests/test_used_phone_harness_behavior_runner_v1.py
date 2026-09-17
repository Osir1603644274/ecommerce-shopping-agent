"""Runner/scorer contract tests for the ReAct V0 behavior pilot."""

import inspect
import json

from evaluation import used_phone_harness_behavior_runner_v1 as runner
from evaluation.used_phone_harness_behavior_scorer_v1 import score_receipts
from evaluation.used_phone_react_sequence_scorer_v1 import (
    score_receipts as score_react_sequences,
)


def test_runner_derives_fixed_v1_actions_from_public_response() -> None:
    assert runner.derive_authoritative_action({
        "tool_trace": [{"tool": "search_products", "ok": True}],
    }) == {"kind": "CALL_TOOL", "toolName": "search_products"}
    assert runner.derive_authoritative_action({
        "tool_trace": [],
        "traceSummary": {"finalAction": "ask_user"},
    }) == {"kind": "ASK_CLARIFICATION", "toolName": None}
    assert runner.derive_authoritative_action({
        "tool_trace": [],
        "traceSummary": {"finalAction": "task_completed"},
    }) == {"kind": "ANSWER", "toolName": None}
    assert runner._percentile([8588.18, 25045.35], 0.95) == 25045.35


def test_runner_rejects_http_200_application_failure() -> None:
    assert runner._response_failure_reason({
        "answer": "调用大模型失败",
        "trace": {
            "status": "error",
            "failureClass": "request_failure",
            "agentStatus": "not_run",
        },
    }) == "request_trace_error:request_failure:not_run"
    assert runner._response_failure_reason({
        "trace": {"status": "ok", "agentStatus": "ok"},
    }) is None


def test_runner_extracts_only_redacted_shadow_action() -> None:
    selected = runner.extract_shadow_action({
        "reactDecisions": [{
            "status": "accepted",
            "decisionSource": "deterministic_policy",
            "actionKind": "CALL_TOOL",
            "toolName": "compare_products",
            "reasonCode": "compare_visible",
            "viewHash": "hash",
            "viewTokenCount": 123,
            "durationMs": 4.5,
        }]
    })
    assert selected == {
        "status": "accepted",
        "decisionSource": "deterministic_policy",
        "kind": "CALL_TOOL",
        "toolName": "compare_products",
        "reasonCode": "compare_visible",
        "errorCode": None,
        "durationMs": 4.5,
        "viewTokenCount": 123,
        "viewHash": "hash",
    }
    assert "question" not in selected
    assert "arguments" not in selected


def test_runner_extracts_redacted_live_react_sequence() -> None:
    sequence = runner.extract_react_sequence({
        "reactDecisions": [
            {
                "status": "accepted",
                "decisionSource": "deterministic_policy",
                "actionKind": "CALL_TOOL",
                "toolName": "search_products",
                "reasonCode": "server_broad_catalog_discovery",
                "viewHash": "view-1",
                "viewTokenCount": 150,
                "durationMs": 2.0,
                "actionId": "redacted-action-id",
            },
            {
                "status": "accepted",
                "decisionSource": "deterministic_policy",
                "actionKind": "ANSWER",
                "toolName": None,
                "reasonCode": "answer_after_validated_action",
                "viewHash": "view-2",
                "viewTokenCount": 180,
                "durationMs": 1.0,
            },
        ],
        "reactOutcomes": [
            {
                "actionId": "redacted-action-id",
                "status": "SUCCEEDED",
                "validatorOutcome": "PASSED",
                "stateRevisionAfter": 8,
                "retryable": False,
                "errorCode": None,
                "observationRefHash": "scope-hash",
            },
            {
                "status": "SUCCEEDED",
                "validatorOutcome": "PASSED",
                "stateRevisionAfter": 8,
                "retryable": False,
                "errorCode": None,
                "observationRefHash": "scope-hash",
            },
        ],
    })

    assert sequence["status"] == "observed"
    assert [item["kind"] for item in sequence["actions"]] == [
        "CALL_TOOL", "ANSWER",
    ]
    assert sequence["terminalKind"] == "ANSWER"
    assert [item["status"] for item in sequence["outcomes"]] == [
        "SUCCEEDED", "SUCCEEDED",
    ]
    assert "actionId" not in sequence["actions"][0]
    assert "actionId" not in sequence["outcomes"][0]


def test_runner_source_cannot_read_private_oracle() -> None:
    source = inspect.getsource(runner)
    assert "expectations.jsonl" not in source
    assert "PRIVATE_EXPECTATIONS" not in source


def test_runner_custom_dataset_is_explicit_and_default_safe() -> None:
    parameter = inspect.signature(runner.run_public_scenarios).parameters[
        "dataset_path"
    ]
    assert parameter.default == runner.PUBLIC_SCENARIOS


def test_private_scorer_scores_only_first_live_action_gate(tmp_path) -> None:
    receipts = [
        {
            "scenarioId": "UPHB-V1-001", "turnId": "T1",
            "executionTier": "live_439", "provenanceKind": "real_web",
            "status": "ok", "expectedRuntime": "fixed_v1",
            "enteredRuntime": "fixed_v1", "runtimeMatches": True,
            "selectedAction": {
                "status": "derived", "kind": "CALL_TOOL",
                "toolName": "search_products",
            },
            "authoritativeAction": {
                "kind": "CALL_TOOL", "toolName": "search_products",
            },
        },
        {
            "scenarioId": "UPHB-V1-007", "turnId": "T2",
            "executionTier": "live_439", "provenanceKind": "real_web",
            "status": "ok", "expectedRuntime": "react_v0_shadow",
            "enteredRuntime": "react_v0_shadow", "runtimeMatches": True,
            "selectedAction": {
                "status": "accepted", "kind": "CALL_TOOL",
                "toolName": "compare_products", "durationMs": 10,
                "viewTokenCount": 200,
                "decisionSource": "deterministic_policy",
            },
            "authoritativeAction": {
                "kind": "CALL_TOOL", "toolName": "compare_products",
            },
        },
        {
            "scenarioId": "UPHB-V1-006", "turnId": "T1",
            "executionTier": "live_439", "provenanceKind": "real_web",
            "status": "ok", "expectedRuntime": "react_v0_shadow",
            "enteredRuntime": "react_v0_shadow", "runtimeMatches": True,
            "selectedAction": {
                "status": "accepted", "kind": "ASK_CLARIFICATION",
                "toolName": None,
            },
            "authoritativeAction": {
                "kind": "CALL_TOOL", "toolName": "search_products",
            },
        },
        {
            "scenarioId": "UPHB-V1-002", "turnId": "T1",
            "executionTier": "live_439", "provenanceKind": "real_web",
            "status": "ok", "expectedRuntime": "react_v0_shadow",
            "enteredRuntime": "react_v0_shadow", "runtimeMatches": True,
            "selectedAction": {
                "status": "rejected", "kind": None, "toolName": None,
            },
            "authoritativeAction": {
                "kind": "CALL_TOOL", "toolName": "search_products",
            },
        },
        {
            "scenarioId": "UPHB-V1-016", "turnId": "T1",
            "executionTier": "harness_target", "provenanceKind": "real_web_derived",
            "status": "ok", "expectedRuntime": "react_v0_shadow",
            "enteredRuntime": "react_v0_shadow", "runtimeMatches": True,
            "selectedAction": {
                "status": "accepted", "kind": "NEEDS_REVIEW", "toolName": None,
            },
        },
    ]
    path = tmp_path / "receipts.jsonl"
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in receipts),
        encoding="utf-8",
    )

    report = score_receipts(path)

    assert report["evaluatedTurns"] == 4
    assert report["passedTurns"] == 2
    assert report["actionGateAccuracy"] == 0.5
    assert report["selectedStatusCounts"]["rejected"] == 1
    assert report["decisionSourceCounts"] == {
        "unknown": 3,
        "deterministic_policy": 1,
    }
    assert report["excludedCounts"] == {"non_live_439_tier": 1}
    assert report["authoritativeAgreement"] == 0.5
    assert report["claimBoundary"]["scoresAnswerQuality"] is False


def test_react_sequence_scorer_checks_complete_validated_loop(tmp_path) -> None:
    base = {
        "executionTier": "live_439",
        "expectedRuntime": "react_v0",
        "enteredRuntime": "react_v0",
        "runtimeMatches": True,
        "status": "ok",
    }
    receipts = [
        {
            **base,
            "scenarioId": "UPHB-V1-002",
            "turnId": "T1",
            "reactSequence": {
                "status": "observed",
                "actions": [
                    {"status": "accepted", "kind": "CALL_TOOL"},
                    {"status": "accepted", "kind": "ANSWER"},
                ],
                "outcomes": [
                    {"status": "SUCCEEDED", "validatorOutcome": "PASSED"},
                    {"status": "SUCCEEDED", "validatorOutcome": "PASSED"},
                ],
            },
        },
        {
            **base,
            "scenarioId": "UPHB-V1-006",
            "turnId": "T1",
            "reactSequence": {
                "status": "observed",
                "actions": [{"status": "accepted", "kind": "CALL_TOOL"}],
                "outcomes": [
                    {
                        "status": "FAILED",
                        "validatorOutcome": "NOT_RUN",
                        "errorCode": "tool_timeout",
                    },
                ],
            },
        },
    ]
    path = tmp_path / "receipts.jsonl"
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in receipts),
        encoding="utf-8",
    )

    report = score_react_sequences(path)

    assert report["evaluatedTurns"] == 2
    assert report["completeSequenceTurns"] == 2
    assert report["sequenceReceiptCompletenessRate"] == 1.0
    assert report["successfulTurns"] == 1
    assert report["executionSuccessRate"] == 0.5
    assert report["failedClosedTurns"] == 1
    assert report["toolValidationPassRate"] == 0.5
    assert report["failureCounts"] == {
        "tool_failed_closed": 1,
    }
    assert report["claimBoundary"]["scoresAnswerQuality"] is False


def test_react_sequence_scorer_accepts_zero_result_model_clarification(tmp_path) -> None:
    receipt = {
        "executionTier": "live_439",
        "expectedRuntime": "react_v0",
        "enteredRuntime": "react_v0",
        "runtimeMatches": True,
        "status": "ok",
        "scenarioId": "UPHB-V1-015",
        "turnId": "T1",
        "requestId": "req-adaptive",
        "modelAttribution": {
            "modelCallCounts": {"react_decision": 1},
            "llmDurationByStageMs": {"react_decision": 123.4},
        },
        "reactSequence": {
            "status": "observed",
            "actions": [
                {
                    "status": "accepted",
                    "decisionSource": "deterministic_policy",
                    "kind": "CALL_TOOL",
                    "optionId": "tool.search_products",
                    "publishedOptionIds": ["tool.search_products"],
                },
                {
                    "status": "accepted",
                    "decisionSource": "model",
                    "kind": "ASK_CLARIFICATION",
                    "optionId": "clarify.pending.0",
                    "publishedOptionIds": [
                        "answer.zero_result",
                        "clarify.pending.0",
                    ],
                },
            ],
            "outcomes": [
                {
                    "status": "REJECTED",
                    "validatorOutcome": "REJECTED",
                    "errorCode": "product_candidates_missing",
                },
                {"status": "INTERRUPTED", "validatorOutcome": "NOT_RUN"},
            ],
        },
    }
    path = tmp_path / "receipts.jsonl"
    path.write_text(json.dumps(receipt, ensure_ascii=False) + "\n", encoding="utf-8")

    report = score_react_sequences(path)

    assert report["successfulTurns"] == 1
    assert report["modelDecisionCount"] == 1
    assert report["modelAfterObservationTurns"] == 1
    assert report["publishedOptionViolationCount"] == 0
    assert report["modelCallCounts"] == {"react_decision": 1}
    assert report["uphbV1015T1AdaptiveGate"]["status"] == "ACCEPT"


def test_react_sequence_scorer_preserves_decision_timeout_as_failed_closed(tmp_path) -> None:
    receipt = {
        "executionTier": "live_439",
        "expectedRuntime": "react_v0",
        "enteredRuntime": "react_v0",
        "runtimeMatches": True,
        "status": "ok",
        "scenarioId": "UPHB-V1-015",
        "turnId": "T1",
        "reactSequence": {
            "status": "observed",
            "actions": [
                {"status": "accepted", "kind": "CALL_TOOL"},
                {
                    "status": "failed",
                    "decisionSource": "model",
                    "kind": None,
                    "errorCode": "decision_timeout",
                    "publishedOptionIds": [
                        "answer.zero_result",
                        "clarify.pending.0",
                    ],
                },
            ],
            "outcomes": [{
                "status": "REJECTED",
                "validatorOutcome": "REJECTED",
                "errorCode": "product_candidates_missing",
            }],
        },
    }
    path = tmp_path / "receipts.jsonl"
    path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    report = score_react_sequences(path)

    assert report["completeSequenceTurns"] == 1
    assert report["failedClosedTurns"] == 1
    assert report["sequenceCounts"] == {"CALL_TOOL->DECISION_FAILED": 1}
    assert report["publishedOptionViolationCount"] == 0
