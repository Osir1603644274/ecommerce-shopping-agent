"""Explicit current-turn refresh must not be satisfied by retained evidence."""
import json
import unittest
from unittest.mock import patch

from agent.app.control.react_context import build_decision_context_view
from agent.app.control.react_decision import deterministic_next_action
from agent.app.control.react_actions import ActionOutcome
from agent.app.settings import settings
from agent.app import llm
from agent.app.task_state import TaskState
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE


class RefreshTests(unittest.TestCase):
    def state(self):
        row = json.loads((HERE / "note_v6_B002/turn-06.json").read_text(encoding="utf-8"))
        return TaskState.model_validate(row["postState"])

    def view(self, text, outcome=None, tools=None):
        return build_decision_context_view(self.state(), user_message=text,
            allowed_tool_names=tools if tools is not None else ["search_products", "compare_products"],
            last_outcome=outcome)

    def test_refresh_precedes_old_pair_or_answer(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            for text in ("请按最终生效条件重新检索，仅展示前三款。",
                         "请先按新条件重新检索，再仅比较这次实际展示的第一款和第二款。"):
                view = self.view(text)
                self.assertEqual([x.option_id for x in view.allowed_action_options], ["tool.search_products"])
                self.assertEqual(deterministic_next_action(view).tool_name, "search_products")

    def test_negated_historical_and_conditional_text_not_refresh(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            for text in ("不要重新检索，只回忆原文", "请不要重新检索", "如果没货请重新检索",
                         "上次我说请重新检索", "请解释重新检索的意思", "请记录：重新检索是备选",
                         "请先按原条件不要重新检索", "比较最近实际展示的第一款和第二款"):
                self.assertFalse(self.view(text).server_signals.get("freshSearchRequired", False), text)

    def test_successful_current_action_does_not_repeat_search(self):
        outcome = ActionOutcome(actionId="test-action", status="SUCCEEDED", observationRef="validated:test",
            validatorOutcome="PASSED", stateRevisionAfter=self.state().revision, retryable=False, errorCode=None)
        with patch.object(settings, "context_history_v1_enabled", True):
            view = self.view("请重新检索", outcome)
            self.assertFalse(view.server_signals.get("freshSearchRequired", False))
            self.assertEqual(deterministic_next_action(view).kind, "ANSWER")

    def test_missing_search_capability_does_not_publish_old_answer(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            view = self.view("请重新检索", tools=["compare_products"])
            self.assertTrue(all(x.kind == "NEEDS_REVIEW" for x in view.allowed_action_options))

    def test_default_off_preserved(self):
        with patch.object(settings, "context_history_v1_enabled", False):
            self.assertFalse(self.view("请重新检索").server_signals.get("freshSearchRequired", False))

    def test_fresh_search_plus_comparison_cannot_end_as_listing_template(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            self.assertTrue(llm._requires_contextual_final_answer(
                "请先按当前条件重新检索，再仅比较这次实际展示的第一款和第二款"))
            self.assertFalse(llm._requires_contextual_final_answer("请重新检索并展示前三款"))

    def test_new_search_comparison_does_not_bind_previous_display(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            message = "请先按当前条件重新检索，再仅比较这次实际展示的第一款和第二款"
            self.assertTrue(llm._requests_compound_first_two_comparison(message))
            payload, observation = llm._deterministic_used_phone_task_state_decision(self.state(), message)
            self.assertIsNotNone(payload)
            self.assertNotIn("_boundComparedIds", observation)
            self.assertTrue(observation.get("_compoundComparison"))

    def test_validated_compound_second_action_uses_new_pair(self):
        state = self.state()
        scope = state.domain_state["candidateScope"]
        ids = state.domain_state["shoppingGuide"]["comparedIds"]
        state.domain_state["compoundComparison"] = {"status": "ready", "kind": "compare_first_two",
            "taskId": state.task_id, "sourcePlanId": scope["sourcePlanId"], "productIds": ids}
        state.domain_state["taskStateExtraction"] = {"reason": "no_deterministic_signal", "executionKind": "model"}
        state.domain_state["validationResult"] = None
        outcome = ActionOutcome(actionId="test-search", status="SUCCEEDED", observationRef="validated:search",
            validatorOutcome="PASSED", stateRevisionAfter=state.revision, retryable=False, errorCode=None)
        with patch.object(settings, "context_history_v1_enabled", True):
            view = build_decision_context_view(state, user_message="请重新检索，再比较前两款",
                allowed_tool_names=["search_products", "compare_products"], last_outcome=outcome)
            self.assertTrue(view.server_signals["boundComparison"])
            self.assertEqual(deterministic_next_action(view).tool_name, "compare_products")


if __name__ == "__main__":
    unittest.main()
