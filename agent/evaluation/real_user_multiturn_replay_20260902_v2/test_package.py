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


verify_package = _load("verify_package.py", "rumr_v2_verify_package_tests")
blind_builder = _load("blind_packet_builder.py", "rumr_v2_blind_builder_tests")


class RealUserMultiturnPackageV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conversations = verify_package.load_jsonl(verify_package.DATASET)
        cls.outputs = verify_package.dummy_outputs(cls.conversations)

    def test_complete_no_model_verification(self) -> None:
        result = verify_package.verify_all()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["sessionSourceReceiptsVerified"], 8)
        self.assertEqual(result["scheduledArmTurnsDryRun"], 54)
        self.assertEqual(result["adversarialFailClosedChecks"], 5)
        self.assertEqual(result["modelCalls"], 0)
        self.assertEqual(result["agentRuns"], 0)
        self.assertEqual(result["formalAbRuns"], 0)
        self.assertFalse(result["unseenOrSealedEligible"])

    def test_forged_same_arm_assistant_history_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        target = next(row for row in forged if row["conversationId"] == "rumr-v1-c008" and row["turnId"] == "rumr-v1-c008-t03" and row["arm"] == "RAW_FULL_CONTROL")
        opposite = next(row for row in forged if row["conversationId"] == "rumr-v1-c008" and row["turnId"] == "rumr-v1-c008-t01" and row["arm"] == "CONTEXT_TREATMENT")
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
        with self.assertRaisesRegex(ValueError, "paired arms do not share one branchPointStateHash"):
            blind_builder.build_packets(forged, self.conversations)

    def test_configuration_drift_fails_closed(self) -> None:
        forged = copy.deepcopy(self.outputs)
        forged[0]["trace"]["modelConfigurationHash"] = verify_package.sha_text("drifted-model-configuration")
        forged[0]["traceSha256"] = verify_package.sha_text(verify_package.canonical(forged[0]["trace"]))
        with self.assertRaisesRegex(ValueError, "does not share one frozen modelConfigurationHash"):
            blind_builder.build_packets(forged, self.conversations)


if __name__ == "__main__":
    unittest.main()
