import tempfile
from pathlib import Path
import unittest

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import append, write_new
from .prefix_census import census


class PrefixTests(unittest.TestCase):
    def fixture(self, directory, usage):
        write_new(directory / "started.json", {"arm": "A_FULL_HISTORY", "historyPolicy": {"input_budget": 96000}})
        write_new(directory / "turn-01.json", {"turn": 1, "durationMs": 500, "runId": "r", "expectedRunId": "r",
            "modelCalls": [{"ordinal": 1}], "traceSummary": {"agentStatus": "ok", "finalAction": "task_completed"}})
        (directory / "model_calls").mkdir()
        append(directory / "model_calls/ledger.jsonl", {"ordinal": 1, "status": "COMPLETED", "usage": usage})
        append(directory / "model_calls/ledger.jsonl", {"ordinal": 2, "status": "STARTED", "usage": None})

    def test_future_call_excluded(self):
        with tempfile.TemporaryDirectory(prefix="context-prefix-test-") as temp:
            directory = Path(temp)
            self.fixture(directory, {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 80})
            value = census(directory, 1)
            self.assertEqual(value["completeNativeTokenTotal"], 120)
            self.assertEqual(value["nativeCalls"], 1)
            self.assertEqual(value["unknownUsageCalls"], [])

    def test_unknown_usage_is_not_zero(self):
        with tempfile.TemporaryDirectory(prefix="context-prefix-test-") as temp:
            directory = Path(temp)
            self.fixture(directory, None)
            value = census(directory, 1)
            self.assertIsNone(value["completeNativeTokenTotal"])
            self.assertEqual(value["unknownUsageCalls"], [1])


if __name__ == "__main__":
    unittest.main()
