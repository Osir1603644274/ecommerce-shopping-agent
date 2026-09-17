"""Every shared View cap respects explicit experimental scope, never global settings."""
import unittest

from pydantic import BaseModel

from agent.app.context_input import experimental_context_input, experimental_pack_budget
from agent.app.context_view import _enforce_view_budget, ContextViewBudgetExceeded
from agent.app.context_pack import PHASE_TOKEN_BUDGETS, _estimate_dict_tokens


class LargeView(BaseModel):
    protected_facts: str


class CapacityTests(unittest.TestCase):
    def test_all_phases_use_explicit_shared_experimental_capacity(self):
        view = LargeView(protected_facts="原文事实不可裁剪" * 3000)
        original = view.model_dump_json()
        self.assertGreater(_estimate_dict_tokens(view.model_dump()), 10000)
        for phase in PHASE_TOKEN_BUDGETS:
            with self.subTest(phase=phase):
                with self.assertRaises(ContextViewBudgetExceeded):
                    _enforce_view_budget(view, phase)
                with experimental_context_input(pack_budget_tokens=96000):
                    _enforce_view_budget(view, phase)
                self.assertEqual(original, view.model_dump_json())

    def test_scope_without_explicit_capacity_keeps_legacy_limits(self):
        view = LargeView(protected_facts="原文事实" * 10000)
        with experimental_context_input():
            self.assertIsNone(experimental_pack_budget())
            with self.assertRaises(ContextViewBudgetExceeded):
                _enforce_view_budget(view, "final_answer")

    def test_explicit_limit_still_fail_closed_and_scope_restores(self):
        view = LargeView(protected_facts="原文事实" * 10000)
        with experimental_context_input(pack_budget_tokens=96000):
            with experimental_context_input(pack_budget_tokens=1000):
                with self.assertRaises(ContextViewBudgetExceeded):
                    _enforce_view_budget(view, "final_answer")
            _enforce_view_budget(view, "final_answer")
        with self.assertRaises(ContextViewBudgetExceeded):
            _enforce_view_budget(view, "final_answer")


if __name__ == "__main__":
    unittest.main()
