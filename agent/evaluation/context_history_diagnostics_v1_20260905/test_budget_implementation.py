"""Red on unchanged v4; must turn green on actual SUT implementation, no candidate patch."""
import unittest
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.app.task_state import TaskState
from .budget_after_compare import load_case, price


class LiveBudgetImplementationTests(unittest.TestCase):
    def test_actual_sut_keeps_correct_recorded_budget_update(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            for arm in "ABC":
                with self.subTest(arm=arm):
                    row, arguments, _ = load_case(arm)
                    payload, _ = llm._build_validated_task_state_payload(TaskState.model_validate(row["preState"]),
                        arguments, message=row["query"], require_status=True)
                    self.assertEqual(price(payload["domainStatePatch"]["shoppingGuide"]["requirements"]), 140000)

    def test_actual_sut_budget_change_uses_semantic_lane(self):
        row, _, _ = load_case("A")
        with patch.object(settings, "context_history_v1_enabled", True):
            self.assertTrue(llm._requires_context_semantic_change(row["query"]))

    def test_actual_sut_default_off_remains_off(self):
        row, _, _ = load_case("A")
        with patch.object(settings, "context_history_v1_enabled", False):
            self.assertFalse(llm._requires_context_semantic_change(row["query"]))


if __name__ == "__main__":
    unittest.main()
