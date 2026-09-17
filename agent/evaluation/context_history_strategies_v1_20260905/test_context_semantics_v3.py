from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from agent.app import llm
from agent.app.domains.ecommerce.models import ShoppingGuideState
from agent.app.task_state import TaskState
from .history_strategies import message


class SemanticRegressionTests(TestCase):
    def state(self):
        now = datetime.now(timezone.utc)
        return TaskState(taskId="semantic-v3", sessionId="session-v3", taskType="ecommerce_guide",
            status="ready", revision=1, goal="买手机", createdAt=now, updatedAt=now,
            domainState={"shoppingGuide": {"category": "phone", "mode": "compare",
                "candidateIds": [11, 12], "comparedIds": [11, 12], "requirements": [
                    {"key": "os", "operator": "eq", "value": "android", "priority": "soft", "source": "user", "unit": "enum"},
                    {"key": "motherboard_repair", "operator": "eq", "value": "not_repaired", "priority": "hard", "source": "user", "unit": "enum"}]}})

    def test_typed_condition_count_is_not_comparison_continuation(self):
        with patch.object(llm.settings, "context_history_v1_enabled", True):
            _, observation = llm._deterministic_used_phone_task_state_decision(
                self.state(), "保留两个条件，主板无维修不变。")
            self.assertNotEqual(observation["reason"], "bound_comparison")

    def test_named_scratch_exclusion_does_not_reject_another_grade(self):
        with patch.object(llm.settings, "context_history_v1_enabled", True):
            self.assertEqual(llm._explicit_used_phone_exclusions("明显划痕排除"), {"scratch_level": ["obvious"]})
            self.assertEqual(llm._explicit_used_phone_exclusions("轻微划痕排除"), {"scratch_level": ["light"]})

    def test_soft_system_replacement_uses_semantic_extraction(self):
        text = "把系统软偏好从Android替换为iOS，因为家人更方便协助设置；这只是同价同况时的排序偏好，系统和品牌仍都不是硬条件。"
        proposed = ShoppingGuideState(category="phone", mode="recommend", requirements=[
            {"key": "os", "operator": "eq", "value": "ios", "priority": "soft", "source": "user", "unit": "enum"}])
        with patch.object(llm.settings, "context_history_v1_enabled", True):
            args, _ = llm._deterministic_used_phone_task_state_decision(self.state(), text)
            self.assertIsNone(args)
            normalized = llm._canonicalize_explicit_used_phone_semantics(self.state(), text, proposed)
            self.assertEqual(normalized.requirements[0].priority, "soft")

    def test_multi_enum_alternatives_require_semantic_reading(self):
        with patch.object(llm.settings, "context_history_v1_enabled", True):
            self.assertTrue(llm._requires_context_semantic_change("划痕只接受无划痕或轻微划痕两个档，明显划痕排除。"))
            self.assertTrue(llm._requires_context_semantic_change("电池健康只接受80-90%或90%以上两个档。"))

    def test_negative_large_game_is_not_positive_title_retrieval(self):
        with patch.object(llm.settings, "context_history_v1_enabled", True):
            self.assertNotEqual(llm._used_phone_text_claim_discovery("备用手机，不玩大型游戏"), "gaming_title_claim")

    def test_selected_message_keeps_original_turn(self):
        row = {"messageId": "msg-original", "role": "user", "content": "原文", "turn": 14}
        self.assertEqual(message(row).get("turn"), 14)
