"""Score executable ReAct V0 sequence invariants from public run receipts.

This scorer does not judge answer quality or relevance.  It checks only that a
live ReAct turn emitted a complete, fail-closed action/outcome sequence.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sequence_result(
    sequence: object,
) -> tuple[bool, bool, bool, str, str]:
    if not isinstance(sequence, dict) or sequence.get("status") != "observed":
        return False, False, False, "sequence_not_observed", "missing"
    actions = sequence.get("actions")
    outcomes = sequence.get("outcomes")
    if not isinstance(actions, list) or not isinstance(outcomes, list):
        return False, False, False, "sequence_malformed", "malformed"
    if not actions:
        return False, False, False, "action_outcome_count_mismatch", "malformed"
    if any(not isinstance(item, dict) for item in [*actions, *outcomes]):
        return False, False, False, "sequence_malformed", "malformed"
    if len(actions) == len(outcomes) + 1:
        failed_decision = actions[-1]
        preceding_actions = actions[:-1]
        if (
            failed_decision.get("status") in {"failed", "rejected"}
            and failed_decision.get("kind") is None
            and isinstance(failed_decision.get("errorCode"), str)
            and len(preceding_actions) == len(outcomes)
            and all(item.get("status") == "accepted" for item in preceding_actions)
        ):
            signature = "->".join([
                *(str(item.get("kind") or "UNKNOWN") for item in preceding_actions),
                "DECISION_FAILED",
            ])
            return True, False, True, "decision_failed_closed", signature
    if len(actions) != len(outcomes):
        return False, False, False, "action_outcome_count_mismatch", "malformed"
    if any(item.get("status") != "accepted" for item in actions):
        return False, False, False, "decision_not_accepted", "rejected"

    kinds = [str(item.get("kind") or "UNKNOWN") for item in actions]
    signature = "->".join(kinds)
    first = kinds[0]
    if first == "CALL_TOOL":
        if len(actions) == 1:
            outcome = outcomes[0]
            failed_closed = (
                outcome.get("status") in {"FAILED", "REJECTED"}
                and isinstance(outcome.get("errorCode"), str)
            )
            if failed_closed:
                return True, False, True, "tool_failed_closed", signature
            return False, False, False, "tool_sequence_incomplete", signature
        if kinds == ["CALL_TOOL", "ASK_CLARIFICATION"]:
            tool_outcome, clarification_outcome = outcomes
            if not (
                tool_outcome.get("status") == "REJECTED"
                and tool_outcome.get("validatorOutcome") == "REJECTED"
                and tool_outcome.get("errorCode") == "product_candidates_missing"
            ):
                return False, False, False, "adaptive_trigger_invalid", signature
            if (
                clarification_outcome.get("status"),
                clarification_outcome.get("validatorOutcome"),
            ) != ("INTERRUPTED", "NOT_RUN"):
                return False, False, False, "clarification_outcome_incomplete", signature
            return True, True, False, "passed", signature
        if kinds != ["CALL_TOOL", "ANSWER"]:
            return False, False, False, "tool_sequence_not_terminal_answer", signature
        tool_outcome, answer_outcome = outcomes
        if (
            tool_outcome.get("status") != "SUCCEEDED"
            or tool_outcome.get("validatorOutcome") != "PASSED"
        ):
            return False, False, False, "tool_not_validated", signature
        if answer_outcome.get("status") != "SUCCEEDED":
            failed_closed = (
                answer_outcome.get("status") in {"FAILED", "REJECTED"}
                and isinstance(answer_outcome.get("errorCode"), str)
            )
            if failed_closed:
                return True, False, True, "answer_failed_closed", signature
            return False, False, False, "answer_outcome_incomplete", signature
        return True, True, False, "passed", signature
    if len(actions) != 1:
        return False, False, False, "terminal_action_not_single", signature
    expected = {
        "ANSWER": ("SUCCEEDED", "PASSED"),
        "ASK_CLARIFICATION": ("INTERRUPTED", "NOT_RUN"),
        "NEEDS_REVIEW": ("REJECTED", "NOT_RUN"),
    }.get(first)
    if expected is None:
        return False, False, False, "unsupported_action", signature
    if (
        outcomes[0].get("status"), outcomes[0].get("validatorOutcome")
    ) != expected:
        return False, False, False, "terminal_outcome_mismatch", signature
    return True, True, False, "passed", signature


def score_receipts(receipts_path: Path) -> dict[str, Any]:
    receipts = _read_jsonl(receipts_path)
    excluded: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    signatures: Counter[str] = Counter()
    evaluated = 0
    complete = 0
    successful = 0
    failed_closed = 0
    tool_actions = 0
    validated_tool_actions = 0
    model_decisions = 0
    model_after_observation_turns = 0
    published_option_violations = 0
    model_call_counts: Counter[str] = Counter()
    llm_duration_by_stage_ms: Counter[str] = Counter()
    adaptive_gate_receipts: list[dict[str, Any]] = []
    three_scenario_gate: dict[str, list[dict[str, Any]]] = {
        "UPHB-V1-015:T1": [],
        "UPHB-V1-022:T4": [],
        "UPHB-V1-024:T2": [],
        "UPHB-V1-024:T3": [],
    }

    for receipt in receipts:
        if receipt.get("executionTier") != "live_439":
            excluded["non_live_439_tier"] += 1
            continue
        if receipt.get("expectedRuntime") != "react_v0":
            excluded["non_react_v0_runtime"] += 1
            continue
        if receipt.get("status") != "ok":
            excluded["request_error"] += 1
            continue
        if (
            receipt.get("runtimeMatches") is not True
            or receipt.get("enteredRuntime") != "react_v0"
        ):
            excluded["runtime_not_entered"] += 1
            continue

        evaluated += 1
        sequence = receipt.get("reactSequence")
        is_complete, is_successful, is_failed_closed, reason, signature = (
            _sequence_result(sequence)
        )
        signatures[signature] += 1
        complete += int(is_complete)
        successful += int(is_successful)
        failed_closed += int(is_failed_closed)
        if reason != "passed":
            failures[reason] += 1
        actions = sequence.get("actions", []) if isinstance(sequence, dict) else []
        outcomes = sequence.get("outcomes", []) if isinstance(sequence, dict) else []
        model_action_indexes = [
            index for index, action in enumerate(actions)
            if isinstance(action, dict) and action.get("decisionSource") == "model"
        ]
        model_decisions += len(model_action_indexes)
        model_after_observation_turns += int(any(index > 0 for index in model_action_indexes))
        for index in model_action_indexes:
            action = actions[index]
            if action.get("status") != "accepted":
                continue
            option_id = action.get("optionId")
            published = action.get("publishedOptionIds")
            if not isinstance(option_id, str) or not isinstance(published, list) or option_id not in published:
                published_option_violations += 1

        attribution = receipt.get("modelAttribution")
        if not isinstance(attribution, dict):
            attribution = receipt.get("requestTrace")
        normalized_calls: dict[str, int] = {}
        if isinstance(attribution, dict):
            calls = attribution.get("modelCallCounts")
            durations = attribution.get("llmDurationByStageMs")
            if isinstance(calls, dict):
                for stage, count in calls.items():
                    if isinstance(stage, str) and isinstance(count, int):
                        model_call_counts[stage] += count
                        normalized_calls[stage] = count
            if isinstance(durations, dict):
                for stage, duration in durations.items():
                    if isinstance(stage, str) and isinstance(duration, (int, float)):
                        llm_duration_by_stage_ms[stage] += float(duration)

        if receipt.get("scenarioId") == "UPHB-V1-015" and receipt.get("turnId") == "T1":
            adaptive_gate_receipts.append({
                "requestId": receipt.get("requestId"),
                "enteredRuntime": receipt.get("enteredRuntime"),
                "signature": signature,
                "complete": is_complete,
                "modelAfterObservation": any(index > 0 for index in model_action_indexes),
                "publishedOptionBound": all(
                    isinstance(actions[index].get("optionId"), str)
                    and actions[index].get("optionId") in actions[index].get("publishedOptionIds", [])
                    for index in model_action_indexes
                ) if model_action_indexes else False,
                "zeroResultObserved": bool(
                    outcomes
                    and isinstance(outcomes[0], dict)
                    and outcomes[0].get("errorCode") == "product_candidates_missing"
                ),
            })
        target_key = f"{receipt.get('scenarioId')}:{receipt.get('turnId')}"
        if target_key in three_scenario_gate:
            first_action = actions[0] if actions and isinstance(actions[0], dict) else {}
            first_outcome = outcomes[0] if outcomes and isinstance(outcomes[0], dict) else {}
            three_scenario_gate[target_key].append({
                "requestId": receipt.get("requestId"),
                "signature": signature,
                "complete": is_complete,
                "firstDecisionSource": first_action.get("decisionSource"),
                "firstKind": first_action.get("kind"),
                "optionId": first_action.get("optionId"),
                "publishedOptionCount": len(first_action.get("publishedOptionIds", [])),
                "publishedOptionBound": bool(
                    isinstance(first_action.get("optionId"), str)
                    and first_action.get("optionId")
                    in first_action.get("publishedOptionIds", [])
                ),
                "firstOutcomeStatus": first_outcome.get("status"),
                "firstErrorCode": first_outcome.get("errorCode"),
                "modelCallCounts": normalized_calls,
            })
        for index, action in enumerate(actions):
            if isinstance(action, dict) and action.get("kind") == "CALL_TOOL":
                tool_actions += 1
                if index < len(outcomes) and isinstance(outcomes[index], dict):
                    validated_tool_actions += int(
                        outcomes[index].get("status") == "SUCCEEDED"
                        and outcomes[index].get("validatorOutcome") == "PASSED"
                    )

    return {
        "schemaVersion": "used-phone-react-sequence-score-v1",
        "scope": "live_react_v0_execution_contract_only",
        "evaluatedTurns": evaluated,
        "completeSequenceTurns": complete,
        "sequenceReceiptCompletenessRate": (
            round(complete / evaluated, 4) if evaluated else None
        ),
        "successfulTurns": successful,
        "executionSuccessRate": (
            round(successful / evaluated, 4) if evaluated else None
        ),
        "failedClosedTurns": failed_closed,
        "toolActionCount": tool_actions,
        "validatedToolActionCount": validated_tool_actions,
        "toolValidationPassRate": (
            round(validated_tool_actions / tool_actions, 4)
            if tool_actions else None
        ),
        "sequenceCounts": dict(signatures),
        "modelDecisionCount": model_decisions,
        "modelAfterObservationTurns": model_after_observation_turns,
        "publishedOptionViolationCount": published_option_violations,
        "modelCallCounts": dict(model_call_counts),
        "llmDurationByStageMs": {
            stage: round(duration, 2)
            for stage, duration in llm_duration_by_stage_ms.items()
        },
        "uphbV1015T1AdaptiveGate": {
            "status": (
                "ACCEPT"
                if adaptive_gate_receipts
                and all(
                    item["enteredRuntime"] == "react_v0"
                    and item["signature"] in {
                        "CALL_TOOL->ASK_CLARIFICATION",
                        "CALL_TOOL->ANSWER",
                    }
                    and item["complete"]
                    and item["modelAfterObservation"]
                    and item["publishedOptionBound"]
                    and item["zeroResultObserved"]
                    for item in adaptive_gate_receipts
                )
                else "HOLD"
            ),
            "receipts": adaptive_gate_receipts,
        },
        "threeScenarioAdaptiveGate": {
            "status": (
                "ACCEPT"
                if all(len(items) == 1 for items in three_scenario_gate.values())
                and all(
                    item["complete"] and item["publishedOptionBound"]
                    for items in three_scenario_gate.values() for item in items
                )
                and three_scenario_gate["UPHB-V1-015:T1"][0]["signature"]
                    in {"CALL_TOOL->ASK_CLARIFICATION", "CALL_TOOL->ANSWER"}
                and three_scenario_gate["UPHB-V1-015:T1"][0]["firstErrorCode"]
                    == "product_candidates_missing"
                and len(adaptive_gate_receipts) == 1
                and adaptive_gate_receipts[0]["modelAfterObservation"]
                and adaptive_gate_receipts[0]["publishedOptionBound"]
                and three_scenario_gate["UPHB-V1-022:T4"][0]["firstDecisionSource"]
                    == "deterministic_policy"
                and three_scenario_gate["UPHB-V1-022:T4"][0]["publishedOptionCount"]
                    == 1
                and three_scenario_gate["UPHB-V1-022:T4"][0]["firstKind"]
                    == "ASK_CLARIFICATION"
                and three_scenario_gate["UPHB-V1-024:T2"][0]["firstDecisionSource"]
                    == "deterministic_policy"
                and three_scenario_gate["UPHB-V1-024:T2"][0]["publishedOptionCount"]
                    == 1
                and three_scenario_gate["UPHB-V1-024:T2"][0]["firstKind"]
                    == "ANSWER"
                and three_scenario_gate["UPHB-V1-024:T2"][0]["modelCallCounts"].get(
                    "final_answer", 0
                ) >= 1
                and three_scenario_gate["UPHB-V1-024:T3"][0]["firstKind"]
                    == "ANSWER"
                and three_scenario_gate["UPHB-V1-024:T3"][0]["modelCallCounts"].get(
                    "task_state", 0
                ) == 0
                else "HOLD"
            ),
            "targets": three_scenario_gate,
        },
        "failureCounts": dict(failures),
        "excludedCounts": dict(excluded),
        "claimBoundary": {
            "scoresActionOutcomeCompleteness": True,
            "scoresValidatorGate": True,
            "scoresAnswerQuality": False,
            "scoresRecommendationQuality": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = score_receipts(args.receipts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
