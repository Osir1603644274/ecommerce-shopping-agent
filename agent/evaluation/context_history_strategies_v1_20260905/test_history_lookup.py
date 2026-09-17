from pathlib import Path
import tempfile
from unittest import TestCase
import jsonschema

from agent.app.context_history import HistoryArchive
from .history_lookup import lookup


class LookupTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lookup-unit-")
        self.archive = HistoryArchive(Path(self.tmp.name) / "raw", session_id="s", task_id="t", create=True)
        self.archive.append("user", "最初预算为2000元", turn=1)
        self.row = self.archive.append("assistant", "第一款是101，第二款是102", turn=1,
            scope_id="expired", display={"batchId": "old-batch", "productIds": [101, 102]})
        self.archive.append("assistant", "最新第一款是201，第二款是202", turn=2,
            scope_id="current", display={"batchId": "new-batch", "productIds": [201, 202]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_original_turn_and_historical_batch(self):
        result = lookup(self.archive, {"operation": "turn", "selector": "1", "limit": 4}, token_budget=2000)
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["records"][0]["content"], "最初预算为2000元")
        old = lookup(self.archive, {"operation": "display", "selector": "old-batch", "limit": 1}, token_budget=2000)
        self.assertEqual(old["records"][0]["display"]["productIds"], [101, 102])
        self.assertFalse(old["actionAuthorized"])

    def test_unknown_id_and_no_cross_session_or_path_access(self):
        bad = lookup(self.archive, {"operation": "read", "selector": "../../other/identity.json", "limit": 1}, token_budget=1000)
        self.assertEqual(bad["error"], "source_not_in_current_archive")
        with self.assertRaises(jsonschema.ValidationError):
            lookup(self.archive, {"operation": "read", "selector": self.row["messageId"], "limit": 1, "sessionId": "other"}, token_budget=1000)

    def test_budget_never_clips_original(self):
        raw = "原始内容" * 1000
        row = self.archive.append("user", raw, turn=3)
        small = lookup(self.archive, {"operation": "read", "selector": row["messageId"], "limit": 1}, token_budget=500)
        self.assertEqual(small["records"], [])
        self.assertEqual(self.archive.read(row["messageId"])["content"], raw)
