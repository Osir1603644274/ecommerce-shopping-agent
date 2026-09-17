import json
import unittest
import jsonschema
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from .subscription import decode_answer, parse_events, envelope_schema, SubscriptionClient


class SubscriptionContractTests(unittest.TestCase):
    def setUp(self):
        self.request = {"tools": [{"type": "function", "function": {"name": "save",
            "parameters": {"type": "object", "properties": {"n": {"type": "integer"}},
                           "required": ["n"], "additionalProperties": False}}}],
            "tool_choice": {"type": "function", "function": {"name": "save"}}}

    def answer(self, arguments='{"n":1}', name="save"):
        return json.dumps({"content": None, "toolCalls": [{"name": name, "arguments": arguments}]})

    def test_valid(self):
        self.assertEqual(decode_answer(self.answer(), self.request)["toolCalls"][0]["name"], "save")

    def test_invalid_arguments(self):
        for arguments in ('not json', '{"n":"one"}', '{"n":1,"arguments":{}}'):
            with self.subTest(arguments=arguments), self.assertRaises(Exception):
                decode_answer(self.answer(arguments), self.request)

    def test_unknown_tool(self):
        with self.assertRaises(Exception):
            decode_answer(self.answer(name="shell"), self.request)

    def test_required(self):
        with self.assertRaises((ValueError, jsonschema.ValidationError)):
            decode_answer('{"content":"done","toolCalls":[]}', self.request)

    def test_no_tools(self):
        with self.assertRaises((ValueError, jsonschema.ValidationError)):
            decode_answer(self.answer(), {})

    def test_schema_itself_prohibits_final_answer_tool_invention(self):
        self.assertEqual(envelope_schema({})["properties"]["toolCalls"]["maxItems"], 0)
        self.assertEqual(envelope_schema(self.request)["properties"]["toolCalls"]["items"]["properties"]["name"]["enum"], ["save"])

    def test_events(self):
        events = [{"type": "thread.started", "thread_id": "fresh"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3, "cached_input_tokens": 8}}]
        _, usage, _ = parse_events("\n".join(map(json.dumps, events)), 0)
        self.assertEqual(usage["input_tokens"] + usage["output_tokens"], 15)
        for bad in ([*events, {"type": "item.completed", "item": {"type": "command_execution"}}],
                    events[:-1], [*events[:-1], {"type": "turn.completed", "usage": {}}]):
            with self.assertRaises(ValueError):
                parse_events("\n".join(map(json.dumps, bad)), 0)

    def test_output_path_is_absolute_before_isolated_cwd(self):
        with tempfile.TemporaryDirectory(prefix="subscription-path-", dir=Path.cwd()) as directory:
            output = Path(directory) / "calls"
            module = "agent.evaluation.context_history_strategies_v1_20260905.subscription."
            with patch(module + "isolated_overrides", return_value={}), \
                 patch(module + "pilot.clean_env", return_value={}), \
                 patch(module + "tempfile.mkdtemp", return_value=directory), \
                 patch(module + "subprocess.run", return_value=SimpleNamespace(
                     returncode=0, stdout="Logged in using ChatGPT", stderr="")), \
                 patch(module + "subprocess.check_output", return_value="codex-test"):
                client = SubscriptionClient(Path(os.path.relpath(output)))
            self.assertEqual(client.output, output.resolve())


if __name__ == "__main__":
    unittest.main()
