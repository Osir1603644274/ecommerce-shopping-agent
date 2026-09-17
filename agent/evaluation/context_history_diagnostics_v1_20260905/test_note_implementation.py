"""Live implementation regressions: red on v5, no runtime candidate applied."""
import json
import unittest
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.app.task_state import TaskState
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE


class LiveNoteImplementationTests(unittest.TestCase):
    def test_recorded_nonproduct_note_does_not_reuse_comparison_pair(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            for arm in "ABC":
                with self.subTest(arm=arm):
                    row = json.loads((HERE / f"core48_v5_vivo_{arm}001/turn-32.json").read_text(encoding="utf-8"))
                    payload, observation = llm._deterministic_used_phone_task_state_decision(
                        TaskState.model_validate(row["preState"]), row["query"])
                    self.assertIsNone(payload)
                    self.assertEqual(observation["route"], "model_fallback")
                    self.assertEqual(observation["reason"], "context_note_or_recall_requires_semantics")

    def test_record_and_recall_mixed_requests_require_semantic_extraction(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            for message in ("把天气和交通方案分别记录", "回忆之前的验机安排", "先回查运输清单，再比较最近第一款和第二款"):
                self.assertTrue(llm._requires_context_semantic_change(message))

    def test_plain_comparison_keeps_existing_deterministic_eligibility(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            self.assertFalse(llm._requires_context_semantic_change("比较最近实际展示的第一款和第二款"))

    def test_default_off_stays_off(self):
        with patch.object(settings, "context_history_v1_enabled", False):
            self.assertFalse(llm._requires_context_semantic_change("把天气和交通方案分别记录"))


if __name__ == "__main__":
    unittest.main()
