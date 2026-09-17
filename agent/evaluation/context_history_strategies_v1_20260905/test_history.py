from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase

from agent.app.context_history import HistoryArchive, HistoryArchiveError
from .history_strategies import HistoryPolicy, HistoryStrategies


class HistoryTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="history-unit-")
        self.archive = HistoryArchive(Path(self.tmp.name) / "archive", session_id="s", task_id="t", create=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_preservation_resume_and_identity(self):
        text = "第一行\n  保留空格\t" + "完整内容" * 300 + "末尾备注"
        row = self.archive.append("user", text, turn=1)
        resumed = HistoryArchive(self.archive.directory, session_id="s", task_id="t")
        self.assertEqual(resumed.read(row["messageId"])["content"], text)
        with self.assertRaises(HistoryArchiveError):
            HistoryArchive(self.archive.directory, session_id="other", task_id="t")
        with self.assertRaises(HistoryArchiveError):
            resumed.read("../../identity.json")

    def test_historical_display_is_not_authority(self):
        self.archive.append("assistant", "展示商品 1 和 2", turn=1,
                            scope_id="expired", display={"batchId": "old", "productIds": [2, 1]})
        self.assertFalse(self.archive.historical_display("old")["actionAuthorized"])
        self.assertEqual(self.archive.historical_display("old")["record"]["display"]["productIds"], [2, 1])

    async def test_a_c_equal_before_threshold(self):
        self.archive.append("user", "预算2000", turn=1)
        strategy = HistoryStrategies(self.archive, HistoryPolicy())
        async def forbidden(*args):
            self.fail("summary called before threshold")
        self.assertEqual(await strategy.llm_history(forbidden), strategy.full())

    async def test_repeated_summary_reads_originals(self):
        policy = HistoryPolicy(input_budget=1000, recent_messages=1)
        strategy = HistoryStrategies(self.archive, policy)
        calls = []
        async def summarize(sources, target):
            calls.append(sources)
            row = sources[0]
            return {"items": [{"text": "先预算2000", "kind": "request", "sourceId": row["messageId"], "quote": "预算2000"}]}
        self.archive.append("user", "预算2000。" + "长文本" * 600, turn=1)
        self.archive.append("assistant", "已记录", turn=1)
        await strategy.llm_history(summarize)
        self.archive.append("user", "预算改为1500。" + "后续文本" * 600, turn=2)
        self.archive.append("assistant", "已更新", turn=2)
        await strategy.llm_history(summarize)
        self.assertEqual(len(calls), 2)
        self.assertIn("长文本" * 600, calls[1][0]["content"])
        self.assertEqual(len(strategy.receipts), 2)

    async def test_invalid_summary_does_not_commit(self):
        self.archive.append("user", "内容" * 1500, turn=1)
        self.archive.append("assistant", "收到", turn=1)
        strategy = HistoryStrategies(self.archive, HistoryPolicy(input_budget=1000, recent_messages=1))
        async def invalid(*args):
            return {"items": [{"text": "伪造", "kind": "request", "sourceId": "other", "quote": "不在原文"}]}
        with self.assertRaises(HistoryArchiveError):
            await strategy.llm_history(invalid)
        self.assertIsNone(strategy.summary)
        self.assertEqual(strategy.covered_ids, [])

    def test_b_does_not_clip_short_history(self):
        self.archive.append("user", "很长但仍未超预算。" * 100 + "尾部明确条件", turn=1)
        strategy = HistoryStrategies(self.archive, HistoryPolicy())
        self.assertEqual(strategy.pack_history("条件"), strategy.full())

    def test_working_window_must_fit_common_hard_limit(self):
        for working in (999, 17000):
            with self.assertRaises(ValueError):
                HistoryPolicy(input_budget=16000, working_budget=working)

    def test_b_selects_at_explicit_working_window_not_hard_limit(self):
        self.archive.append("user", "旧安排" * 900, turn=1)
        self.archive.append("assistant", "收到", turn=1)
        strategy = HistoryStrategies(self.archive, HistoryPolicy(
            input_budget=16000, working_budget=1000, recent_messages=1, lookup_messages=0))
        selected = strategy.pack_history("新问题")
        self.assertNotEqual(selected, strategy.full())
        self.assertEqual(len(selected["messages"]), 1)
        self.assertEqual(strategy.receipts[-1]["workingBudget"], 1000)

    async def test_c_uses_working_trigger_but_fallback_can_use_hard_limit(self):
        self.archive.append("user", "当前安排" * 900, turn=1)
        self.archive.append("assistant", "收到", turn=1)
        strategy = HistoryStrategies(self.archive, HistoryPolicy(
            input_budget=16000, working_budget=1000, recent_messages=1))
        calls = []
        async def invalid(*args):
            calls.append(1)
            return {"bad": "not a summary"}
        self.assertEqual(await strategy.llm_history(invalid), strategy.full())
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(row["kind"] == "C_FULL_FALLBACK" for row in strategy.receipts))

    async def test_c_equal_to_a_below_working_trigger(self):
        self.archive.append("user", "预算2000", turn=1)
        strategy = HistoryStrategies(self.archive, HistoryPolicy(input_budget=16000, working_budget=4000))
        async def forbidden(*args):
            self.fail("summary before working trigger")
        self.assertEqual(await strategy.llm_history(forbidden), strategy.full())

    def test_fill_variant_uses_spare_budget_without_dropping_recent(self):
        from .history_strategies import tokens
        for turn in range(1, 31):
            self.archive.append("user", f"安排{turn}：" + "有用的信息" * 10, turn=turn)
        policy = HistoryPolicy(input_budget=16000, working_budget=1500, recent_messages=1,
                               lookup_messages=0, fill_history_budget=True)
        strategy = HistoryStrategies(self.archive, policy)
        value = strategy.pack_history("完全不同的问题")
        self.assertGreater(len(value["messages"]), 5)
        self.assertLessEqual(tokens(value), 1500)
        self.assertEqual(value["messages"][-1]["turn"], 30)

    def test_adaptive_summary_does_not_force_six_current_filters(self):
        from .history_strategies import CodexSummarizer
        instruction = CodexSummarizer(None, HistoryPolicy(adaptive_summary_items=True)).instruction([], 3000)
        self.assertIn("最多33项", instruction)
        self.assertIn("执行备注", instruction)
        self.assertNotIn("最多6项", instruction)

    async def test_invalid_summary_falls_back_only_within_full_budget(self):
        from .history_strategies import tokens
        self.archive.append("user", "预算2000，喜欢拍照。" * 180, turn=1)
        self.archive.append("assistant", "收到", turn=1)
        probe = HistoryStrategies(self.archive, HistoryPolicy())
        strategy = HistoryStrategies(self.archive, HistoryPolicy(input_budget=max(1000, int(tokens(probe.full()) * 1.15)), recent_messages=1))
        calls = []
        async def invalid(*args):
            calls.append(1)
            return {"bad": "not a source-linked summary"}
        self.assertEqual(await strategy.llm_history(invalid), strategy.full())
        self.assertEqual(await strategy.llm_history(invalid), strategy.full())
        self.assertEqual(len(calls), 1)
        self.assertIsNone(strategy.summary)
        self.assertTrue(any(row["kind"] == "C_FULL_FALLBACK" for row in strategy.receipts))
