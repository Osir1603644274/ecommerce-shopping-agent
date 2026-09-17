import json
from pathlib import Path
import tempfile
from unittest import TestCase

from .artifacts import write_new
from .audit import audit


class AuditTests(TestCase):
    def test_failure_artifact_overrides_stale_pass_result(self):
        with tempfile.TemporaryDirectory(prefix="context-audit-") as directory:
            root = Path(directory)
            write_new(root / "result.json", {"plannedTurns": 1, "status": "INTEGRATION_PASS"})
            write_new(root / "turn-01.json", {"turn": 1, "runId": "r", "expectedRunId": "r",
                "traceSummary": {"agentStatus": "ok", "finalAction": "answer"}})
            write_new(root / "failure.json", {"error": "source_changed_during_attempt"})
            result = audit(root)
            self.assertEqual(result["status"], "EXECUTION_AUDIT_HOLD")
            self.assertIn("attempt_failure_artifact", [item["code"] for item in result["problems"]])
