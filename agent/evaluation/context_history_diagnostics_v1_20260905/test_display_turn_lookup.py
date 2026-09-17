from pathlib import Path
import tempfile
import unittest

from agent.app.context_history import HistoryArchive
from agent.evaluation.context_history_strategies_v1_20260905.history_lookup import lookup


class DisplayTurnLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="display-turn-")
        self.addCleanup(self.tmp.cleanup)
        self.archive = HistoryArchive(Path(self.tmp.name) / "archive", session_id="s", task_id="t", create=True)
        self.archive.append("user", "当时查询", turn=21)
        self.old = self.archive.append("assistant", "商品2625578无划痕，商品614290轻微划痕", turn=21,
            scope_id="expired", display={"batchId":"opaque-old-batch","productIds":[2625578,614290]})
        self.archive.append("assistant", "当前商品999", turn=22, scope_id="current",
            display={"batchId":"current-batch","productIds":[999]})

    def read(self, selector, budget=2000):
        return lookup(self.archive, {"operation":"display","selector":selector,"limit":1}, token_budget=budget)

    def test_explicit_turn_spelling_returns_exact_original_and_no_authority(self):
        for selector in ("turn:21", "第21轮实际展示批次", "第21轮"):
            with self.subTest(selector=selector):
                result = self.read(selector)
                self.assertEqual(result["records"][0]["messageId"], self.old["messageId"])
                self.assertEqual(result["records"][0]["content"], self.old["content"])
                self.assertEqual(result["records"][0]["display"]["productIds"], [2625578,614290])
                self.assertFalse(result["actionAuthorized"])

    def test_exact_opaque_batch_id_unchanged(self):
        self.assertEqual(self.read("opaque-old-batch")["records"][0]["recordHash"], self.old["recordHash"])

    def test_ambiguous_or_missing_turn_does_not_select_current_batch(self):
        for selector in ("第20轮", "第21轮和第22轮", "turn:0", "../../other"):
            self.assertEqual(self.read(selector)["records"], [])
        self.archive.append("assistant", "第二次展示", turn=21, display={"batchId":"another-old-batch","productIds":[777]})
        self.assertEqual(self.read("第21轮")["records"], [])

    def test_budget_does_not_clip_original_or_return_partial_mapping(self):
        row = self.archive.append("assistant", "冗长原文" * 2000, turn=23,
            display={"batchId":"long-old-batch","productIds":[111,222]})
        result = self.read("第23轮", budget=500)
        self.assertEqual(result["records"], [])
        self.assertEqual(self.archive.read(row["messageId"])["content"], row["content"])


if __name__ == "__main__":
    unittest.main()
