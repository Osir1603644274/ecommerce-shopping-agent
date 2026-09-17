import json
import unittest
from unittest.mock import patch

from agent.app import context_input, llm
from agent.app.context_pack import build_context_pack, ContextPackBudgetExceeded
from agent.app.settings import settings
from agent.app.task_state import TaskState
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha


class NextRepairTests(unittest.IsolatedAsyncioTestCase):
    def state(self):
        path = HERE / "core48_v3_A001/turn-43.json"
        self.assertEqual(file_sha(path), "2c493cdd17b0d9420b892250ef0fe364efefd1118c439bd7d230ebda92ba3d6a")
        return TaskState.model_validate(json.loads(path.read_text(encoding="utf-8"))["postState"])

    async def test_declared_common_budget_reaches_internal_pack(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            with context_input.experimental_context_input(pack_budget_tokens=64000):
                pack = await build_context_pack(self.state(), history=[])
        self.assertEqual(len(pack.confirmed_facts), len([fact for fact in self.state().facts if fact.certainty == "confirmed"]))

    async def test_explicit_small_budget_still_rejects(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            with context_input.experimental_context_input(pack_budget_tokens=64000):
                with self.assertRaises(ContextPackBudgetExceeded):
                    await build_context_pack(self.state(), budget_tokens=6000, history=[])

    async def test_old_scope_retains_default_budget(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            with context_input.experimental_context_input():
                with self.assertRaises(ContextPackBudgetExceeded):
                    await build_context_pack(self.state(), history=[])

    def test_scope_resets_and_nesting_is_local(self):
        self.assertIsNone(context_input.experimental_pack_budget())
        with context_input.experimental_context_input(pack_budget_tokens=64000):
            self.assertEqual(context_input.experimental_pack_budget(), 64000)
            with context_input.experimental_context_input():
                self.assertIsNone(context_input.experimental_pack_budget())
            self.assertEqual(context_input.experimental_pack_budget(), 64000)
        self.assertIsNone(context_input.experimental_pack_budget())

    def test_accessory_note_is_not_a_listing_request(self):
        text = "修改第16轮的配件约定：卖家不再需要附数据线，原盒也不再加分，我会自带可靠线材；充电头和耳机仍不要求。防撞填充、封箱连续视频、可查物流及到货连续录像继续有效，配件不得成为硬筛选。"
        with patch.object(settings, "context_history_v1_enabled", True):
            self.assertTrue(llm._requires_contextual_final_answer(text))


if __name__ == "__main__":
    unittest.main()
