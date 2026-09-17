import unittest

from .agent_smoke import _acceptance_record


class RetentionTests(unittest.TestCase):
    def test_large_audit_payloads_not_retained(self):
        row = {"turn": 1, "runId": "r", "expectedRunId": "r", "traceSummary": {"agentStatus": "ok", "phases": []},
            "modelCalls": [{"ordinal": 1}], "preState": {"large": "x" * 100000}, "toolTraces": ["x" * 100000]}
        compact = _acceptance_record(row)
        self.assertEqual(set(compact), {"turn", "runId", "expectedRunId", "traceSummary", "modelCalls"})
        self.assertEqual(compact["modelCalls"], 1)
        row["traceSummary"]["phases"].append({"outcome": "failed"})
        self.assertEqual(compact["traceSummary"]["phases"], [])

    def test_acceptance_and_failure_flags_equal(self):
        def passed(row):
            summary = row["traceSummary"]
            return bool(summary and row["runId"] == row["expectedRunId"] and row["modelCalls"]
                and summary.get("agentStatus") == "ok"
                and summary.get("finalAction") not in {"stop_turn", "context_only_answer_failed", "resume_rejected"}
                and not any(phase.get("outcome") == "failed" for phase in summary.get("phases", [])))
        for status, action, phases, identity, calls in [
            ("ok", "task_completed", [], "r", [1]), ("failed", "safe_stop", [], "r", []),
            ("ok", "resume_rejected", [], "r", [1]), ("ok", "task_completed", [{"outcome": "failed"}], "r", [1]),
            ("ok", "task_completed", [], "other", [1]), ("ok", "task_completed", [], "r", [])]:
            row = {"turn": 1, "runId": identity, "expectedRunId": "r", "modelCalls": calls,
                "traceSummary": {"agentStatus": status, "finalAction": action, "phases": phases}}
            self.assertEqual(passed(row), passed(_acceptance_record(row)))


if __name__ == "__main__":
    unittest.main()
