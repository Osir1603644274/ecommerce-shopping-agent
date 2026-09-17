from __future__ import annotations

from pathlib import Path

from agent.evaluation.react_v1_architecture_24_runner_v1 import (
    DEFAULT_PACKAGE,
    _behavior_difference_candidates,
    _load_projection,
    _paired_gate_decision,
    _read_jsonl,
    _reverify_data_invariants,
    _scenario_safety,
    _turn_safety,
    _validate_author_package,
)


def test_frozen_v3_package_and_price_invariants_validate() -> None:
    assert _validate_author_package(DEFAULT_PACKAGE)["status"] == "PASS"
    invariants = _read_jsonl(
        DEFAULT_PACKAGE / "public" / "data_invariants.jsonl"
    )
    observed = _reverify_data_invariants(DEFAULT_PACKAGE, invariants)
    assert len(observed) == 2
    assert all(item["zeroResultProven"] for item in observed)


def test_projection_exposes_only_ordered_turn_text() -> None:
    project = _load_projection(DEFAULT_PACKAGE)
    scenario = {
        "scenarioId": "neutral-id",
        "behaviorClass": "adaptive",
        "turns": [{"text": "第一轮"}, {"text": "第二轮"}],
    }
    assert project(scenario) == ["第一轮", "第二轮"]


def test_turn_safety_uses_explicit_policy_and_call_layers() -> None:
    payload = {
        "answer": "已按证据边界处理。",
        "trace": {
            "status": "ok",
            "transportStatus": "response_generated",
            "agentStatus": "ok",
            "modelCallCounts": {"react_decision": 1, "final_answer": 1},
        },
        "tool_trace": [],
    }
    debug = {
        "controlPolicy": "react_v1",
        "policyRevision": "react-v1-2026-08-27",
        "reactDecisions": [{
            "decisionSource": "model",
            "optionId": "answer.validated_context",
            "publishedOptionIds": ["answer.validated_context"],
            "toolName": None,
            "taskRevision": 3,
            "viewHash": "v" * 64,
            "actionId": "action-001",
            "actionKind": "ANSWER",
            "errorCode": None,
            "modelName": "deepseek-v4-flash",
            "modelCallId": "rmc-test-call-001",
            "decisionBindingHash": "a" * 64,
            "decisionViewHash": "v" * 64,
            "decisionTaskRevision": 3,
            "decisionActionId": "action-001",
            "decisionActionKind": "ANSWER",
            "decisionErrorCode": None,
            "toolName": None,
        }],
        "graphV2Events": [{
            "nodeName": "react_policy",
            "phase": "end",
            "modelName": "deepseek-v4-flash",
            "modelCallId": "rmc-test-call-001",
            "decisionBindingHash": "a" * 64,
            "decisionViewHash": "v" * 64,
            "decisionTaskRevision": 3,
            "decisionActionId": "action-001",
            "decisionActionKind": "ANSWER",
            "decisionErrorCode": None,
            "toolName": None,
        }],
    }
    assert _turn_safety(
        arm="react_v1",
        metadata={"behaviorClass": "adaptive", "boundaryContract": None},
        payload=payload,
        debug=debug,
        model_name="deepseek-v4-flash",
    ) == []
    debug["graphV2Events"][0]["modelName"] = "unexpected-model"
    assert "observed_model_name_mismatch" in _turn_safety(
        arm="react_v1",
        metadata={"behaviorClass": "adaptive", "boundaryContract": None},
        payload=payload,
        debug=debug,
        model_name="deepseek-v4-flash",
    )


def test_turn_safety_rejects_model_identity_on_non_react_node() -> None:
    payload = {
        "answer": "安全回答",
        "trace": {
            "status": "ok",
            "transportStatus": "response_generated",
            "agentStatus": "ok",
            "modelCallCounts": {"react_decision": 1},
            "modelCallFailures": {},
        },
        "tool_trace": [],
    }
    debug = {
        "controlPolicy": "react_v1",
        "policyRevision": "react-v1-2026-08-27",
        "reactDecisions": [{
            "decisionSource": "model",
            "optionId": "answer.validated_context",
            "publishedOptionIds": ["answer.validated_context"],
            "taskRevision": 3,
            "viewHash": "v" * 64,
            "actionId": "action-001",
            "actionKind": "ANSWER",
            "toolName": None,
            "errorCode": None,
            "modelName": "deepseek-v4-flash",
            "modelCallId": "rmc-test-call-001",
            "decisionBindingHash": "a" * 64,
            "decisionViewHash": "v" * 64,
            "decisionTaskRevision": 3,
            "decisionActionId": "action-001",
            "decisionActionKind": "ANSWER",
            "decisionErrorCode": None,
            "toolName": None,
        }],
        "graphV2Events": [{
            "nodeName": "final_answer",
            "phase": "end",
            "modelName": "deepseek-v4-flash",
            "modelCallId": "rmc-test-call-001",
            "decisionBindingHash": "a" * 64,
            "decisionViewHash": "v" * 64,
            "decisionTaskRevision": 3,
            "decisionActionId": "action-001",
            "decisionActionKind": "ANSWER",
            "decisionErrorCode": None,
            "toolName": None,
        }],
    }
    failures = _turn_safety(
        arm="react_v1",
        metadata={"behaviorClass": "adaptive", "boundaryContract": None},
        payload=payload,
        debug=debug,
        model_name="deepseek-v4-flash",
    )
    assert "react_model_event_not_exactly_bound" in failures


def test_turn_safety_rejects_cross_joined_model_call_ids() -> None:
    payload = {
        "answer": "安全回答",
        "trace": {
            "status": "ok",
            "transportStatus": "response_generated",
            "agentStatus": "ok",
            "modelCallCounts": {"react_decision": 1},
            "modelCallFailures": {},
        },
        "tool_trace": [],
    }
    debug = {
        "controlPolicy": "react_v1",
        "policyRevision": "react-v1-2026-08-27",
        "reactDecisions": [{
            "decisionSource": "model",
            "optionId": "answer.validated_context",
            "publishedOptionIds": ["answer.validated_context"],
            "taskRevision": 3,
            "viewHash": "v" * 64,
            "actionId": "action-001",
            "actionKind": "ANSWER",
            "toolName": None,
            "errorCode": None,
            "modelName": "deepseek-v4-flash",
            "modelCallId": "rmc-decision-call",
            "decisionBindingHash": "a" * 64,
            "decisionViewHash": "v" * 64,
            "decisionTaskRevision": 3,
            "decisionActionId": "action-001",
            "decisionActionKind": "ANSWER",
            "decisionErrorCode": None,
            "toolName": None,
        }],
        "graphV2Events": [{
            "nodeName": "react_policy",
            "phase": "end",
            "modelName": "deepseek-v4-flash",
            "modelCallId": "rmc-event-call",
            "decisionBindingHash": "a" * 64,
            "decisionViewHash": "v" * 64,
            "decisionTaskRevision": 3,
            "decisionActionId": "action-001",
            "decisionActionKind": "ANSWER",
            "decisionErrorCode": None,
            "toolName": None,
        }],
    }
    failures = _turn_safety(
        arm="react_v1",
        metadata={"behaviorClass": "adaptive", "boundaryContract": None},
        payload=payload,
        debug=debug,
        model_name="deepseek-v4-flash",
    )
    assert "react_model_call_binding_mismatch" in failures


def test_turn_safety_rejects_swapped_ids_with_same_call_id_set() -> None:
    payload = {
        "answer": "安全回答",
        "trace": {
            "status": "ok",
            "transportStatus": "response_generated",
            "agentStatus": "ok",
            "modelCallCounts": {"react_decision": 2},
            "modelCallFailures": {},
        },
        "tool_trace": [],
    }
    debug = {
        "controlPolicy": "react_v1",
        "policyRevision": "react-v1-2026-08-27",
        "reactDecisions": [
            {
                "decisionSource": "model",
                "optionId": "answer.first",
                "publishedOptionIds": ["answer.first"],
                "taskRevision": 3,
                "viewHash": "1" * 64,
                "actionId": "action-001",
                "actionKind": "CALL_TOOL",
                "toolName": "search_products",
                "errorCode": None,
                "modelName": "deepseek-v4-flash",
                "modelCallId": "rmc-call-001",
                "decisionBindingHash": "a" * 64,
            },
            {
                "decisionSource": "model",
                "optionId": "answer.second",
                "publishedOptionIds": ["answer.second"],
                "taskRevision": 4,
                "viewHash": "2" * 64,
                "actionId": "action-002",
                "actionKind": "ANSWER",
                "toolName": None,
                "errorCode": None,
                "modelName": "deepseek-v4-flash",
                "modelCallId": "rmc-call-002",
                "decisionBindingHash": "b" * 64,
            },
        ],
        "graphV2Events": [
            {
                "nodeName": "react_policy",
                "phase": "end",
                "modelName": "deepseek-v4-flash",
                "modelCallId": "rmc-call-002",
                "decisionBindingHash": "b" * 64,
                "decisionViewHash": "1" * 64,
                "decisionTaskRevision": 3,
                "decisionActionId": "action-001",
                "decisionActionKind": "CALL_TOOL",
                "decisionErrorCode": None,
                "toolName": "search_products",
            },
            {
                "nodeName": "react_policy",
                "phase": "end",
                "modelName": "deepseek-v4-flash",
                "modelCallId": "rmc-call-001",
                "decisionBindingHash": "a" * 64,
                "decisionViewHash": "2" * 64,
                "decisionTaskRevision": 4,
                "decisionActionId": "action-002",
                "decisionActionKind": "ANSWER",
                "decisionErrorCode": None,
                "toolName": None,
            },
        ],
    }
    failures = _turn_safety(
        arm="react_v1",
        metadata={"behaviorClass": "adaptive", "boundaryContract": None},
        payload=payload,
        debug=debug,
        model_name="deepseek-v4-flash",
    )
    assert "react_model_call_binding_mismatch" in failures


def test_scenario_safety_requires_server_owned_adaptive_trigger() -> None:
    metadata = {
        "behaviorClass": "adaptive",
        "triggerContract": {"adaptiveTrigger": "zero_result"},
        "expectedTrace": {"reactDecisionCalls": {"min": 1}},
    }
    receipt = {
        "taskRevision": 3,
        "safetyFailures": [],
        "modelCallCounts": {"react_decision": 1},
        "reactDecisions": [{
            "adaptiveTrigger": "zero_result",
            "status": "accepted",
            "optionId": "answer.zero_result",
            "publishedOptionIds": ["answer.zero_result", "clarify.pending.0"],
        }],
        "preRequestTaskState": {
            "kind": "NEW_SESSION_EMPTY",
            "revision": None,
            "taskStateSha256": (
                "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            ),
        },
        "receiptHashes": {"taskStateSha256": "post-state"},
    }
    assert _scenario_safety(
        arm="react_v1", metadata=metadata, receipts=[receipt]
    ) == []
    receipt["reactDecisions"] = [{"adaptiveTrigger": None}]
    assert "adaptive_trigger_not_observed:zero_result" in _scenario_safety(
        arm="react_v1", metadata=metadata, receipts=[receipt]
    )
    receipt["reactDecisions"] = [{
        "adaptiveTrigger": "zero_result",
        "status": "rejected",
        "optionId": None,
        "publishedOptionIds": ["answer.zero_result", "clarify.pending.0"],
    }]
    assert "adaptive_model_decision_not_accepted" in _scenario_safety(
        arm="react_v1", metadata=metadata, receipts=[receipt]
    )


def test_transaction_handoff_is_accepted_only_as_exact_pre_harness_denial() -> None:
    metadata = _read_jsonl(DEFAULT_PACKAGE / "runner_metadata.jsonl")[-1]
    assert metadata["scenarioId"] == "scenario-024"
    assert metadata["boundaryContract"] == "transaction_exact_confirmation_boundary"
    payload = {
        "answer": "需要交由独立的受信交易服务接管。",
        "trace": {
            "status": "ok",
            "transportStatus": "response_generated",
            "agentStatus": "not_run",
            "modelCallCounts": {},
        },
        "tool_trace": [{
            "tool": "transaction_handoff",
            "ok": False,
            "detail": {"status": "awaiting_trusted_transaction_agent"},
        }],
    }
    assert _turn_safety(
        arm="react_v1", metadata=metadata, payload=payload, debug=None
    ) == []


def test_known_empty_snapshot_accepts_exact_safe_recall_boundary() -> None:
    metadata = {
        "behaviorClass": "negative",
        "boundaryContract": "known_empty_439_snapshot_safe_response",
        "category": "laptop",
    }
    payload = {
        "answer": "当前冻结数据快照中没有可用于笔记本导购的商品记录。",
        "taskState": {
            "domainState": {"shoppingGuide": {"category": "laptop"}},
        },
        "trace": {
            "status": "ok",
            "transportStatus": "response_generated",
            "agentStatus": "ok",
            "modelCallCounts": {},
            "modelCallFailures": {},
        },
        "tool_trace": [{
            "tool": "search_products",
            "ok": False,
            "detail": {
                "code": "product_recall_unavailable",
                "requestedCategory": "laptop",
                "retrievalTrace": {
                    "channels": {
                        "elasticsearch": {"status": "active", "count": 0},
                        "bm25": {"status": "active", "count": 0},
                        "qdrant": {"status": "disabled", "count": 0},
                    }
                },
            },
        }],
    }
    debug = {
        "controlPolicy": "react_v1",
        "policyRevision": "react-v1-2026-08-27",
        "graphV2Events": [],
    }
    assert _turn_safety(
        arm="react_v1", metadata=metadata, payload=payload, debug=debug
    ) == []
    payload["tool_trace"][0]["detail"]["code"] = "transport_failed"
    assert "known_empty_snapshot_receipt_invalid" in _turn_safety(
        arm="react_v1", metadata=metadata, payload=payload, debug=debug
    )


def test_behavior_difference_requires_observable_paired_change() -> None:
    fixed = [{
        "scenarioId": "scenario-001",
        "turnIndex": 1,
        "answer": "没有候选。",
        "finalAction": "respond",
        "toolTrace": [],
    }]
    assert _behavior_difference_candidates(fixed, list(fixed)) == []
    react = [{**fixed[0], "answer": "没有候选，你愿意放宽预算吗？"}]
    result = _behavior_difference_candidates(fixed, react)
    assert result[0]["answerChanged"] is True


def test_partial_smoke_can_only_authorize_full_run() -> None:
    assert _paired_gate_decision(
        both_safe=True,
        has_behavior_differences=False,
        full_suite=False,
    ) == {
        "status": "ACCEPT",
        "blindPackEligible": False,
        "decision": "ACCEPT_FOR_FULL_RUN",
    }


def test_only_qualified_full_suite_can_authorize_blind_pack() -> None:
    assert _paired_gate_decision(
        both_safe=True,
        has_behavior_differences=True,
        full_suite=True,
    ) == {
        "status": "ACCEPT",
        "blindPackEligible": True,
        "decision": "ACCEPT_FOR_BLIND_PACK",
    }
