"""Fail-closed scorer tests for the real ToolInbox smoke evidence."""
from __future__ import annotations

import json
import unittest

from agent.evaluation.checkpoint_recovery_inbox_v2 import (
    REQUIRED_SCENARIOS,
    _CONCURRENT,
    _CONTRACT_ONLY,
    _PARKED,
    _UNKNOWN,
    _expected_business_crossings,
    _expected_classification,
    _decode_observed_json,
    _identity,
    _ids,
    _succeeded_receipt,
    _succeeded_receipt_from_identity,
    score_checkpoint_recovery_inbox_v2,
)


class CheckpointRecoveryInboxV2Tests(unittest.TestCase):
    def report(self) -> dict[str, object]:
        cases = []
        for name in REQUIRED_SCENARIOS:
            identity = _identity(name)
            ledger: list[dict[str, object]] = []
            if _expected_business_crossings(name):
                ledger = [
                    {"kind": "business_started", "identity": identity, "tool": "search_products", "arguments": _ids(name)["args"], "toolOutcome": identity["toolOutcome"]},
                    {"kind": "business_finished", "identity": identity, "resultHash": identity["resultHash"], "toolOutcome": identity["toolOutcome"]},
                ]
            if name in _UNKNOWN:
                recovery = "UNKNOWN_SAFE_REJECT"
            elif name == _PARKED:
                recovery = "PARKED"
            elif name in _CONTRACT_ONLY:
                recovery = "CONTRACT_ONLY_NOT_EXERCISED"
            elif name == _CONCURRENT:
                recovery = "CONCURRENT_SINGLE_CROSSING"
            elif _expected_classification(name) == "EXACT_REPLAY_PASS":
                recovery = "REPLAYED"
            else:
                recovery = "RECOVERED"
            cases.append(
                {
                    "scenario": name,
                    "status": "PASS",
                    "classification": _expected_classification(name),
                    "recoveryStatus": recovery,
                    "identity": identity,
                    "ledger": ledger,
                    "exactReplay": {"applicable": False} if name in _CONTRACT_ONLY else {
                        "applicable": True, "taskStateDelta": 0, "checkpointDelta": 0, "inboxDelta": 0, "ledgerDelta": 0,
                    },
                    "concurrentWorkers": [
                        {"returnCode": 0, "stderr": "", "stdout": "{\"status\":\"RECOVERED\"}"},
                        {"returnCode": 0, "stderr": "", "stdout": "{\"status\":\"REPLAYED\"}"},
                    ] if name == _CONCURRENT else None,
                    "runnerOwned": True,
                }
            )
            if name in REQUIRED_SCENARIOS[:6]:
                stage = ("after_task", "after_claim", "after_inflight", "after_business", "after_complete", "after_projection")[REQUIRED_SCENARIOS.index(name)]
                cases[-1]["transcripts"] = {"initial": {"killed": True, "returnCode": 1, "stdout": json.dumps({"readyToKill": True, "stage": stage}) + "\nREADY_TO_KILL", "stderr": ""}}
        return {"identity": "checkpoint-recovery-inbox-v2", "scenarios": cases}

    def test_complete_shape_scores_hold_with_inbox_pass(self):
        score = score_checkpoint_recovery_inbox_v2(self.report())
        self.assertEqual(score["status"], "HOLD")
        self.assertEqual(score["toolInboxSmokeStatus"], "PASS")
        self.assertEqual(score["fullGraphStatus"], "HOLD")

    def test_missing_duplicate_unknown_and_crossing_count_fail(self):
        report = self.report(); report["scenarios"] = report["scenarios"][:-1]
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
        report = self.report(); report["scenarios"][1]["scenario"] = report["scenarios"][0]["scenario"]
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
        report = self.report(); report["scenarios"][0]["recoveryStatus"] = "UNKNOWN_SAFE_REJECT"
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
        report = self.report(); report["scenarios"][0]["ledger"] *= 2
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")

    def test_pseudo_ledger_input_result_identity_and_replay_fail(self):
        report = self.report(); report["scenarios"][0]["ledger"][0]["arguments"] = {"forged": True}
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
        report = self.report(); report["scenarios"][0]["ledger"][1]["resultHash"] = "0" * 64
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
        report = self.report(); report["scenarios"][0]["identity"]["taskId"] = "forged-task"
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
        report = self.report(); report["scenarios"][0]["exactReplay"]["ledgerDelta"] = 1
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")

    def test_contract_only_and_unknown_cannot_claim_recovery(self):
        report = self.report()
        case = next(item for item in report["scenarios"] if item["scenario"] in _CONTRACT_ONLY)
        case["recoveryStatus"] = "RECOVERED"
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
        report = self.report()
        case = next(item for item in report["scenarios"] if item["scenario"] in _UNKNOWN)
        case["classification"] = "RECOVERED"
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")

    def test_replay_is_not_required_for_contract_only_probe(self):
        report = self.report()
        case = next(item for item in report["scenarios"] if item["scenario"] in _CONTRACT_ONLY)
        case["exactReplay"] = {"applicable": True, "taskStateDelta": 0, "checkpointDelta": 0, "inboxDelta": 0, "ledgerDelta": 0}
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")

    def test_unknown_after_business_retains_one_runner_owned_crossing(self):
        report = self.report()
        case = next(item for item in report["scenarios"] if item["scenario"] == REQUIRED_SCENARIOS[3])
        self.assertEqual(len(case["ledger"]), 2)
        case["ledger"] = []
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")

    def test_runner_owned_marker_is_required(self):
        report = self.report()
        report["scenarios"][0]["runnerOwned"] = False
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")

    def test_succeeded_receipt_keeps_the_authoritative_inbox_terminal_state(self):
        receipt = _succeeded_receipt(REQUIRED_SCENARIOS[0])
        self.assertEqual(receipt["inboxStatus"], "SUCCEEDED")
        self.assertEqual(receipt["resultHash"], _identity(REQUIRED_SCENARIOS[0])["resultHash"])
        self.assertEqual(_succeeded_receipt_from_identity(_identity(REQUIRED_SCENARIOS[0], 7))["fence"], 7)

    def test_raw_redis_json_sidecar_is_decoded_before_contract_comparison(self):
        self.assertEqual(_decode_observed_json('{"clarification":"PARKED"}'), {"clarification": "PARKED"})
        self.assertIsNone(_decode_observed_json(None))

    def test_tool_outcome_is_bound_in_the_runner_ledger(self):
        report = self.report(); report["scenarios"][0]["ledger"][1].pop("toolOutcome")
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")

    def test_kill_and_concurrent_worker_transcripts_are_bound(self):
        report = self.report(); report["scenarios"][0]["transcripts"]["initial"]["killed"] = False
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
        report = self.report(); case = next(c for c in report["scenarios"] if c["scenario"] == _CONCURRENT); case["concurrentWorkers"][0]["returnCode"] = 1
        self.assertEqual(score_checkpoint_recovery_inbox_v2(report)["status"], "FAIL")
