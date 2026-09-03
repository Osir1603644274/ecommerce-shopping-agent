from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path


sys.dont_write_bytecode = True
_PACKAGE = Path(__file__).resolve().parent


def _load(filename: str, name: str):
    path = _PACKAGE / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_package = _load("verify_package.py", "rumr_v3_verify_package_tests")
blind_builder = _load("blind_packet_builder.py", "rumr_v3_blind_builder_tests")


class RealUserMultiturnPackageV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conversations = verify_package.load_jsonl(verify_package.DATASET)
        cls.outputs = verify_package.dummy_outputs(cls.conversations)

    def _replace_answer_and_history(self, rows, target, value) -> None:
        semantic_turn = target["semanticTurn"]
        target["finalAnswer"] = value
        target["finalAnswerSha256"] = verify_package.sha_text(value)
        for row in rows:
            if row["conversationId"] == target["conversationId"] and row["arm"] == target["arm"] and row["semanticTurn"] >= semantic_turn:
                row["dialogue"][semantic_turn * 2 - 1]["content"] = value
                row["dialogueSha256"] = verify_package.sha_text(verify_package.canonical(row["dialogue"]))

    def _rebind_trace(self, row) -> None:
        row["traceSha256"] = verify_package.sha_text(verify_package.canonical(row["trace"]))
        binding = row["executionBinding"]
        payload = {
            "schemaVersion": "real-user-multiturn-trace-binding-v3",
            "packageId": verify_package.PACKAGE_ID,
            "conversationId": row["conversationId"],
            "turnId": row["turnId"],
            "semanticTurn": row["semanticTurn"],
            "arm": row["arm"],
            "executionOrdinal": binding["executionOrdinal"],
            "runId": binding["runId"],
            "taskId": binding["taskId"],
            "sessionId": binding["sessionId"],
            "executionBindingSha256": row["executionBindingSha256"],
            "traceSha256": row["traceSha256"],
            "status": row["status"],
        }
        row["traceBindingSha256"] = verify_package.sha_text(verify_package.canonical(payload))

    def test_complete_no_model_verification(self) -> None:
        result = verify_package.verify_all()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["sessionSourceReceiptsVerified"], 8)
        self.assertEqual(result["rawUserTurnCount"], 21)
        self.assertEqual(result["perTurnSourceLocatorsVerified"], 21)
        self.assertEqual(result["scheduledArmTurnsDryRun"], 42)
        self.assertEqual(result["adversarialFailClosedChecks"], 11)
        self.assertEqual(result["modelCalls"], 0)
        self.assertEqual(result["agentRuns"], 0)
        self.assertEqual(result["formalAbRuns"], 0)
        self.assertFalse(result["unseenOrSealedEligible"])

    def test_forged_same_arm_assistant_history_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        target = next(row for row in forged if row["conversationId"] == "rumr-v1-c002" and row["turnId"] == "rumr-v1-c002-t03" and row["arm"] == "RAW_FULL_CONTROL")
        opposite = next(row for row in forged if row["conversationId"] == "rumr-v1-c002" and row["turnId"] == "rumr-v1-c002-t01" and row["arm"] == "CONTEXT_TREATMENT")
        target["dialogue"][1]["content"] = opposite["finalAnswer"]
        target["dialogueSha256"] = verify_package.sha_text(verify_package.canonical(target["dialogue"]))
        with self.assertRaisesRegex(ValueError, "assistant history does not match"):
            blind_builder.build_packets(forged, self.conversations)

    def test_raw_full_control_label_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        target = forged[0]
        target["finalAnswer"] = "leaked RAW_FULL_CONTROL"
        target["finalAnswerSha256"] = verify_package.sha_text(target["finalAnswer"])
        target["dialogue"][-1]["content"] = target["finalAnswer"]
        target["dialogueSha256"] = verify_package.sha_text(verify_package.canonical(target["dialogue"]))
        with self.assertRaisesRegex(ValueError, "arm label leaked"):
            blind_builder.build_packets(forged, self.conversations)

    def test_context_treatment_label_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[1]["publicEvidence"] = [{"note": "CONTEXT_TREATMENT"}]
        with self.assertRaisesRegex(ValueError, "arm label leaked"):
            blind_builder.build_packets(forged, self.conversations)

    def test_arm_field_in_public_evidence_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[1]["publicEvidence"] = [{"arm": "CONTEXT_TREATMENT"}]
        with self.assertRaisesRegex(ValueError, "arm or execution label leaked"):
            blind_builder.build_packets(forged, self.conversations)

    def test_unbound_additional_property_fails_schema_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[0]["unboundTrace"] = {"accepted": True}
        with self.assertRaisesRegex(ValueError, "schema violation"):
            blind_builder.build_packets(forged, self.conversations)

    def test_cross_arm_run_identity_reuse_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        source = next(row for row in forged if row["conversationId"] == "rumr-v1-c001" and row["arm"] == "RAW_FULL_CONTROL")
        reused = source["executionBinding"]["runId"]
        for target in (row for row in forged if row["conversationId"] == "rumr-v1-c001" and row["arm"] == "CONTEXT_TREATMENT"):
            target["executionBinding"]["runId"] = reused
            payload = {
                "packageId": verify_package.PACKAGE_ID,
                "conversationId": target["conversationId"],
                "arm": target["arm"],
                "runId": target["executionBinding"]["runId"],
                "taskId": target["executionBinding"]["taskId"],
                "sessionId": target["executionBinding"]["sessionId"],
                "branchPointStateHash": target["executionBinding"]["branchPointStateHash"],
            }
            target["executionBinding"]["armStateBindingSha256"] = verify_package.sha_text(verify_package.canonical(payload))
            target["executionBindingSha256"] = verify_package.sha_text(verify_package.canonical(target["executionBinding"]))
            self._rebind_trace(target)
        with self.assertRaisesRegex(ValueError, "cross-arm or cross-session runId reuse"):
            blind_builder.build_packets(forged, self.conversations)

    def test_cross_arm_branch_point_mismatch_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        replacement = verify_package.sha_text("forged-branch-point")
        for target in (row for row in forged if row["conversationId"] == "rumr-v1-c001" and row["arm"] == "CONTEXT_TREATMENT"):
            target["executionBinding"]["branchPointStateHash"] = replacement
            payload = {
                "packageId": verify_package.PACKAGE_ID,
                "conversationId": target["conversationId"],
                "arm": target["arm"],
                "runId": target["executionBinding"]["runId"],
                "taskId": target["executionBinding"]["taskId"],
                "sessionId": target["executionBinding"]["sessionId"],
                "branchPointStateHash": replacement,
            }
            target["executionBinding"]["armStateBindingSha256"] = verify_package.sha_text(verify_package.canonical(payload))
            target["executionBindingSha256"] = verify_package.sha_text(verify_package.canonical(target["executionBinding"]))
            self._rebind_trace(target)
        with self.assertRaisesRegex(ValueError, "paired arms do not share one branchPointStateHash"):
            blind_builder.build_packets(forged, self.conversations)

    def test_configuration_drift_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[0]["trace"]["modelConfigurationHash"] = verify_package.sha_text("drifted-model-configuration")
        self._rebind_trace(forged[0])
        with self.assertRaisesRegex(ValueError, "does not share one frozen modelConfigurationHash"):
            blind_builder.build_packets(forged, self.conversations)

    def test_cross_arm_trace_swap_with_recomputed_trace_hash_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        target = next(row for row in forged if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == "RAW_FULL_CONTROL")
        source = next(row for row in forged if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == "CONTEXT_TREATMENT")
        target["trace"] = copy.deepcopy(source["trace"])
        target["traceSha256"] = verify_package.sha_text(verify_package.canonical(target["trace"]))
        with self.assertRaisesRegex(ValueError, "traceBindingSha256 mismatch"):
            blind_builder.build_packets(forged, self.conversations)

    def test_cross_session_trace_swap_with_recomputed_trace_hash_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        target = next(row for row in forged if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == "RAW_FULL_CONTROL")
        source = next(row for row in forged if row["conversationId"] == "rumr-v1-c003" and row["turnId"] == "rumr-v1-c003-t02" and row["arm"] == "RAW_FULL_CONTROL")
        target["trace"] = copy.deepcopy(source["trace"])
        target["traceSha256"] = verify_package.sha_text(verify_package.canonical(target["trace"]))
        with self.assertRaisesRegex(ValueError, "traceBindingSha256 mismatch"):
            blind_builder.build_packets(forged, self.conversations)

    def test_trace_sha_leak_in_answer_and_dialogue_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        target = next(row for row in forged if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == "RAW_FULL_CONTROL")
        self._replace_answer_and_history(forged, target, target["traceSha256"].upper())
        with self.assertRaisesRegex(ValueError, "execution or trace identifier leaked"):
            blind_builder.build_packets(forged, self.conversations)

    def test_context_binding_leak_in_public_evidence_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[0]["publicEvidence"] = [{"note": forged[0]["trace"]["contextBindingHash"]}]
        with self.assertRaisesRegex(ValueError, "execution or trace identifier leaked"):
            blind_builder.build_packets(forged, self.conversations)

    def test_camel_case_trace_label_in_public_evidence_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[0]["publicEvidence"] = [{"traceSha256": "redacted"}]
        with self.assertRaisesRegex(ValueError, "arm or execution label leaked"):
            blind_builder.build_packets(forged, self.conversations)

    def test_reference_binding_leak_in_dialogue_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        target = next(row for row in forged if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == "RAW_FULL_CONTROL")
        self._replace_answer_and_history(forged, target, target["trace"]["referenceContextBindingHash"])
        with self.assertRaisesRegex(ValueError, "execution or trace identifier leaked"):
            blind_builder.build_packets(forged, self.conversations)

    def test_candidate_scope_leak_in_public_evidence_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[0]["publicEvidence"] = [{"note": forged[0]["trace"]["candidateScopeHash"]}]
        with self.assertRaisesRegex(ValueError, "execution or trace identifier leaked"):
            blind_builder.build_packets(forged, self.conversations)

    def test_tool_call_additional_property_fails_schema_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[0]["trace"]["toolCalls"] = [{
            "callId": "tool-call-001", "callType": "tool", "toolName": "search_products",
            "status": "SUCCEEDED", "durationMs": 1.0,
            "requestSha256": verify_package.sha_text("tool-request"),
            "resultSha256": verify_package.sha_text("tool-result"),
            "unexpected": True,
        }]
        with self.assertRaisesRegex(ValueError, "schema violation"):
            blind_builder.build_packets(forged, self.conversations)

    def test_model_call_additional_property_fails_schema_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[0]["trace"]["modelCalls"] = [{
            "callId": "model-call-001", "callType": "model", "model": "offline-fixture",
            "status": "SUCCEEDED", "durationMs": 1.0,
            "usage": {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0},
            "requestSha256": verify_package.sha_text("model-request"),
            "resultSha256": verify_package.sha_text("model-result"),
            "unexpected": True,
        }]
        with self.assertRaisesRegex(ValueError, "schema violation"):
            blind_builder.build_packets(forged, self.conversations)

    def test_strict_tool_and_model_call_records_are_bound(self) -> None:
        bound = copy.deepcopy(self.outputs)
        target = bound[0]
        target["trace"]["toolCalls"] = [{
            "callId": "tool-call-001", "callType": "tool", "toolName": "search_products",
            "status": "SUCCEEDED", "durationMs": 1.0,
            "requestSha256": verify_package.sha_text("tool-request"),
            "resultSha256": verify_package.sha_text("tool-result"),
        }]
        target["trace"]["modelCalls"] = [{
            "callId": "model-call-001", "callType": "model", "model": "offline-fixture",
            "status": "SUCCEEDED", "durationMs": 2.0,
            "usage": {"promptTokens": 3, "completionTokens": 2, "totalTokens": 5},
            "requestSha256": verify_package.sha_text("model-request"),
            "resultSha256": verify_package.sha_text("model-result"),
        }]
        target["trace"]["promptTokens"] = 3
        target["trace"]["completionTokens"] = 2
        target["trace"]["totalTokens"] = 5
        self._rebind_trace(target)
        built = blind_builder.build_packets(bound, self.conversations)
        self.assertEqual(len(built["mapping"]), 13)


if __name__ == "__main__":
    unittest.main()
