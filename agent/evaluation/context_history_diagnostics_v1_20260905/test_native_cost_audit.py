import json
from pathlib import Path
import tempfile
import unittest

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import sha, write_new
from .native_cost_audit import audit


class NativeReceiptTests(unittest.TestCase):
    def fixture(self, root, usage=None, status="STARTED", declared=None):
        call = root / "model_calls/call-001"
        call.mkdir(parents=True)
        write_new(call / "request.json", {})
        (call / "prompt.txt").write_text("prompt", encoding="utf-8")
        events = [{"type": "thread.started", "thread_id": "thread-fixture"}]
        if usage is not None:
            events.append({"type": "turn.completed", "usage": usage})
        (call / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
        row = {"ordinal": 1, "status": status, "requestSha256": sha({}), "promptSha256": sha("prompt"),
            "usage": usage if declared is None else declared}
        if usage is not None:
            row["threadId"] = "thread-fixture"
        (root / "model_calls/ledger.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        write_new(call / "result.json", row)

    def test_subsets_not_double_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 90, "reasoning_output_tokens": 15}, "COMPLETED")
            self.assertEqual(audit(root)["completeNativeTokenTotal"], 120)

    def test_explicit_flat_judge_layout_uses_same_native_math(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, {"input_tokens": 100, "output_tokens": 20}, "COMPLETED")
            result = audit(root / "model_calls", flat=True)
            self.assertEqual(result["completeNativeTokenTotal"], 120)
            self.assertIn(str(Path("call-001/events.jsonl")), result["calls"][0]["sourceHashes"])

    def test_known_failure_usage_remains_charged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, {"input_tokens": 100, "output_tokens": 20}, "FAILED")
            value = audit(root)
            self.assertEqual(value["completeNativeTokenTotal"], 120)
            self.assertEqual(value["failedOrUnclosedCalls"], [1])

    def test_no_terminal_stays_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            self.assertIsNone(audit(root)["completeNativeTokenTotal"])
            self.assertEqual(audit(root)["unknownUsageCalls"], [1])

    def test_tampered_ledger_usage_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, {"input_tokens": 100, "output_tokens": 20}, "COMPLETED", {"input_tokens": 50, "output_tokens": 20})
            with self.assertRaisesRegex(ValueError, "usage_disagree"):
                audit(root)


if __name__ == "__main__":
    unittest.main()
