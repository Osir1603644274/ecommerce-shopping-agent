import json
from pathlib import Path
import tempfile
import unittest

from agent.evaluation.context_history_strategies_v1_20260905.audit import audit as legacy
from .stream_audit import audit


class StreamingAuditTests(unittest.TestCase):
    def test_same_results_for_success_failure_and_resume(self):
        for action, call, failure, resume in [
            ("task_completed", {"status":"COMPLETED","usage":{}}, False, None),
            ("stop_turn", {"status":"FAILED","usage":None}, True, None),
            ("task_completed", {"status":"COMPLETED","usage":{}}, False, {"answer":"different","runId":"r"})]:
            with self.subTest(action=action, failure=failure, resume=resume), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                row = {"turn":1,"query":"q","runId":"r","expectedRunId":"r",
                    "traceSummary":{"agentStatus":"ok","finalAction":action},
                    "modelCalls":[call],"resumeRequest":resume,
                    "postState":{"domainState":{"v2PendingClarification":{"status":"resolved","runId":"r"}}}}
                (directory / "turn-01.json").write_text(json.dumps(row), encoding="utf-8")
                (directory / "result.json").write_text(json.dumps({"plannedTurns":1}), encoding="utf-8")
                if failure:
                    (directory / "failure.json").write_text("{}", encoding="utf-8")
                self.assertEqual(audit(directory), legacy(directory))

    def test_empty_collection_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(audit(Path(tmp)), legacy(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
