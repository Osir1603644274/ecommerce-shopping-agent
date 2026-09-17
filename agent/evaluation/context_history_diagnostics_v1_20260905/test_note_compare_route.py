import json
import unittest
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.app.task_state import TaskState
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE
from .note_compare_route import candidate_scope, requires_note_semantics, recorded_v5_semantic_scope


class NoteRoutingCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.row = json.loads((HERE / "core48_v5_vivo_A001/turn-32.json").read_text(encoding="utf-8"))
        cls.state = TaskState.model_validate(cls.row["preState"])

    def setUp(self):
        self.enterContext(recorded_v5_semantic_scope())

    def test_recorded_false_comparison_reproduces(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            payload, observation = llm._deterministic_used_phone_task_state_decision(self.state, self.row["query"])
        self.assertIsNotNone(payload)
        self.assertEqual(observation["reason"], "bound_comparison")

    def test_candidate_requests_schema_validated_model_extraction(self):
        with patch.object(settings, "context_history_v1_enabled", True), candidate_scope():
            payload, observation = llm._deterministic_used_phone_task_state_decision(self.state, self.row["query"])
        self.assertIsNone(payload)
        self.assertEqual(observation["route"], "model_fallback")

    def test_plain_comparison_not_forced_into_model_extraction(self):
        message = "比较最近实际展示的第一款和第二款"
        self.assertFalse(requires_note_semantics(message))
        with patch.object(settings, "context_history_v1_enabled", True):
            before = llm._deterministic_used_phone_task_state_decision(self.state, message)
            with candidate_scope():
                after = llm._deterministic_used_phone_task_state_decision(self.state, message)
        self.assertEqual(before, after)
        self.assertIsNotNone(after[0])

    def test_default_off_behavior_unchanged(self):
        with patch.object(settings, "context_history_v1_enabled", False):
            before = llm._deterministic_used_phone_task_state_decision(self.state, self.row["query"])
            with candidate_scope():
                after = llm._deterministic_used_phone_task_state_decision(self.state, self.row["query"])
        self.assertEqual(before, after)

    def test_mixed_note_and_product_action_needs_semantics_not_silent_elision(self):
        message = "先回忆邮寄安排，然后比较最近的第一款和第二款"
        with patch.object(settings, "context_history_v1_enabled", True), candidate_scope():
            payload, observation = llm._deterministic_used_phone_task_state_decision(self.state, message)
        self.assertIsNone(payload)
        self.assertEqual(observation["route"], "model_fallback")


if __name__ == "__main__":
    unittest.main()
