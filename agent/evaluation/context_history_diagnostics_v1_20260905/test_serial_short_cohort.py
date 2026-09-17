from pathlib import Path
import unittest

from agent.evaluation.context_history_diagnostics_v1_20260905.serial_short_cohort import SCRIPT, SCRIPT_SHA, schedule
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha


class SerialScheduleTests(unittest.TestCase):
    def test_approved_development_script_bytes(self):
        self.assertEqual(file_sha(SCRIPT), SCRIPT_SHA)

    def test_arm_independence_and_shared_controls(self):
        jobs = schedule(Path("F:/agent/agent/evaluation/context_history_strategies_v1_20260905/never_run_fixture"))
        self.assertEqual([row["label"] for row in jobs], ["A", "B", "C"])
        self.assertEqual(len({row["output"] for row in jobs}), 3)
        for row in jobs:
            arguments = row["arguments"]
            self.assertEqual(arguments[arguments.index("--script") + 1], str(SCRIPT))
            self.assertEqual(arguments[arguments.index("--input-budget") + 1], "96000")
            self.assertEqual(arguments[arguments.index("--attempt-timeout") + 1], "7200")
        self.assertNotIn("--working-budget", jobs[0]["arguments"])
        self.assertIn("--fill-history-budget", jobs[1]["arguments"])
        self.assertNotIn("--adaptive-summary-items", jobs[1]["arguments"])
        self.assertIn("--adaptive-summary-items", jobs[2]["arguments"])
        self.assertNotIn("--fill-history-budget", jobs[2]["arguments"])


if __name__ == "__main__":
    unittest.main()
