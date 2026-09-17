import json
import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE
from agent.evaluation.context_history_strategies_v1_20260905.subscription import parse_events, SubscriptionClient
from .transport_recovery import candidate_parse_events, terminal_usage


class TransportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.events = (HERE / "core72_v4_A001/model_calls/call-165/events.jsonl").read_text(encoding="utf-8")

    def test_recorded_recovery_candidate(self):
        answer, usage, thread = candidate_parse_events(self.events, 0)
        self.assertTrue(answer)
        self.assertEqual(usage["input_tokens"] + usage["output_tokens"], 43182)
        self.assertTrue(thread)

    def test_missing_terminal_still_fails(self):
        with self.assertRaises(ValueError):
            candidate_parse_events("\n".join(self.events.splitlines()[:-1]), 0)

    def test_nonzero_process_still_fails(self):
        with self.assertRaises(ValueError):
            candidate_parse_events(self.events, 1)

    def test_true_terminal_failure_still_fails(self):
        with self.assertRaises(ValueError):
            candidate_parse_events(self.events + '\n{"type":"turn.failed"}', 0)

    def test_host_tool_still_fails(self):
        with self.assertRaises(ValueError):
            candidate_parse_events(self.events + '\n{"type":"item.completed","item":{"type":"command_execution"}}', 0)

    def test_unrecognized_error_still_fails(self):
        with self.assertRaises(ValueError):
            candidate_parse_events(self.events + '\n{"type":"error","message":"unknown failure"}', 0)

    def test_duplicate_terminal_rejected(self):
        with self.assertRaises(ValueError):
            candidate_parse_events(self.events + "\n" + self.events.splitlines()[-1], 0)

    def test_cost_available_without_accepting_adapter(self):
        usage, _ = terminal_usage(self.events)
        self.assertEqual(usage["output_tokens"], 1177)
        value = self.events + '\n{"type":"turn.failed"}'
        with self.assertRaises(ValueError):
            terminal_usage(value)


class LiveTransportImplementationTests(unittest.TestCase):
    def test_actual_parser_accepts_recorded_recovery_with_successful_exit(self):
        events = (HERE / "core72_v4_A001/model_calls/call-165/events.jsonl").read_text(encoding="utf-8")
        _, usage, _ = parse_events(events, 0)
        self.assertEqual(usage["input_tokens"] + usage["output_tokens"], 43182)


class FailedCostPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonzero_exit_retains_terminal_usage_without_accepting_answer(self):
        events = [{"type": "thread.started", "thread_id": "isolated-fixture"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"content":"ok","toolCalls":[]}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3, "cached_input_tokens": 8}}]
        class FakeProcess:
            pid = 999999
            returncode = None
            def __init__(self, *args, stdout, **kwargs):
                self.stdout = stdout
            def communicate(self, prompt, timeout):
                self.stdout.write("\n".join(json.dumps(event) for event in events) + "\n")
                self.stdout.flush()
                self.returncode = 1
            def poll(self):
                return self.returncode
        module = "agent.evaluation.context_history_strategies_v1_20260905.subscription."
        with tempfile.TemporaryDirectory(prefix="transport-cost-fixture-") as folder:
            output = Path(folder) / "calls"
            with patch(module + "isolated_overrides", return_value={}), \
                 patch(module + "pilot.clean_env", return_value={}), \
                 patch(module + "tempfile.mkdtemp", return_value=folder), \
                 patch(module + "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")), \
                 patch(module + "subprocess.check_output", return_value="codex-fixture"), \
                 patch(module + "subprocess.Popen", FakeProcess):
                client = SubscriptionClient(output)
                with self.assertRaises(ValueError):
                    await client.create(model="fixture", messages=[{"role": "user", "content": "fixture"}])
            result = json.loads((output / "call-001/result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["usage"], events[-1]["usage"])
            self.assertEqual(result["processExitCode"], 1)


if __name__ == "__main__":
    unittest.main()
