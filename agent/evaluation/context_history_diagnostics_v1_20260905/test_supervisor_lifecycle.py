import json
from pathlib import Path
import sys
import tempfile
import unittest

from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise


class SupervisorTests(unittest.TestCase):
    def run_child(self, script, *, timeout=15, token=None):
        with tempfile.TemporaryDirectory(prefix="context-supervisor-test-") as temporary:
            output = Path(temporary) / "observation"
            code = supervise([sys.executable, "-X", "utf8", "-B", "-c", script], output,
                timeout=timeout, interval=.1, start_token=token)
            return code, json.loads((output / "result.json").read_text(encoding="utf-8")), (output / "stdout.log").read_text(encoding="utf-8")

    def test_gate_then_exit_zero_is_not_quality_acceptance(self):
        code, result, stdout = self.run_child("import sys; assert sys.stdin.readline().strip() == 'bound'; print('released')", token="bound")
        self.assertEqual(code, 0)
        self.assertEqual(result["reason"], "EXITED_ZERO")
        self.assertIn("released", stdout)
        self.assertFalse(result["allExpectedOutputsVerified"])

    def test_nonzero_exit_preserves_exact_exit(self):
        code, result, _ = self.run_child("raise SystemExit(7)")
        self.assertEqual(code, 7)
        self.assertEqual(result["childExitCode"], 7)
        self.assertEqual(result["reason"], "EXITED_NONZERO")

    def test_hard_timeout_is_terminal_failure(self):
        code, result, _ = self.run_child("import time; time.sleep(60)", timeout=.5)
        self.assertEqual(code, 124)
        self.assertEqual(result["reason"], "HARD_TIMEOUT")
        self.assertIsNotNone(result["childExitCode"])

    def test_existing_output_cannot_be_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileExistsError):
                supervise([sys.executable, "-c", "print('not run')"], Path(temporary), timeout=1)
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
