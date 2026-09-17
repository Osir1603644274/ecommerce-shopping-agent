"""Do not answer a contextual question with a canned product listing."""
import unittest
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings


class AnswerGateTests(unittest.TestCase):
    def test_contextual_composition_requires_model_but_plain_listing_does_not(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            for text in ("请复述当前预算和已经撤销的电池条件。", "现在预算是多少？只回答当前值。",
                         "回忆之前的验机时间，标明已失效。", "请整理目前有效的任务备注和待核实清单。",
                         "解释这两批候选的条件差异。", "帮我总结当前要求并展示商品。",
                         "请只回答现在的验机时间、已经取消的以前时间和当前预算，不要重搜，也不要列商品。",
                         "记录本次任务验机安排：周六下午两点。",
                         "记录本次任务的验机安排：周六下午两点去商场验机。这只是执行备注，预算和商品筛选条件不变。"):
                self.assertTrue(llm._requires_contextual_final_answer(text), text)
            for text in ("请按当前条件重新搜索并展示前三款。", "预算2000元，Android，请找二手手机。"):
                self.assertFalse(llm._requires_contextual_final_answer(text), text)
        with patch.object(settings, "context_history_v1_enabled", False):
            self.assertFalse(llm._requires_contextual_final_answer("请复述当前预算。"))


if __name__ == "__main__":
    unittest.main()
