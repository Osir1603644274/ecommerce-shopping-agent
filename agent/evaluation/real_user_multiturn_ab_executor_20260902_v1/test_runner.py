from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.evaluation.real_user_multiturn_ab_executor_20260902_v1 import runner


class FixtureAdapter:
    fixture_only = True

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.revisions: dict[tuple[str, str], int] = {}
        self.seen_prior: list[tuple[str, int, Any]] = []

    def begin_conversation(self, conversation: dict[str, Any], arms: tuple[str, str]):
        cid = conversation["conversationId"]
        branch = runner.sha_text(f"fixture-branch:{cid}")
        lanes = {}
        for arm in arms:
            key = (cid, arm)
            self.revisions[key] = 1
            label = "raw" if arm == runner.ARMS[0] else "ctx"
            lanes[arm] = runner.LaneBinding(
                f"fixture-run-{cid}-{label}", f"fixture-task-{cid}-{label}",
                f"fixture-session-{cid}-{label}", branch, 1,
            )
        return lanes

    def execute_turn(self, row, lane, prior_raw_same_arm_dialogue):
        self.assert_started_first()
        key = (row["conversationId"], row["arm"])
        pre = self.revisions[key]
        self.revisions[key] += 1
        prior_count = len(prior_raw_same_arm_dialogue or [])
        self.seen_prior.append((row["arm"], prior_count, prior_raw_same_arm_dialogue))
        ordinal = row["executionOrdinal"]
        usage = {"promptTokens": 10 + ordinal, "completionTokens": 2, "totalTokens": 12 + ordinal}
        model_call = {
            "callId": f"fixture-model-{ordinal}", "callType": "model", "model": "fixture-no-model",
            "status": "SUCCEEDED", "durationMs": 1.0, "usage": usage,
            "requestSha256": runner.sha_text(f"fixture-request:{ordinal}"),
            "resultSha256": runner.sha_text(f"fixture-result:{ordinal}"),
        }
        return {
            "runId": lane.run_id, "taskId": lane.task_id, "sessionId": lane.session_id,
            "preStateRevision": pre, "postStateRevision": pre + 1,
            "historyMode": row["historyMode"],
            "rawPriorMessageCount": prior_count if row["arm"] == runner.ARMS[0] else 0,
            "compiledContextUsed": row["arm"] == runner.ARMS[1],
            "contextBindingHash": runner.sha_text(f"fixture-context:{ordinal}"),
            "referenceContextBindingHash": runner.sha_text(f"fixture-reference:{ordinal}"),
            "candidateScopeHash": runner.sha_text(f"fixture-scope:{ordinal}"),
            "toolCalls": [], "modelCalls": [model_call], "durationMs": 2.0,
            "status": "SUCCEEDED", "finalAnswer": f"样例回答 {ordinal}",
            "publicEvidence": [], "failureCode": None,
        }

    def assert_started_first(self) -> None:
        if not (self.output_dir / "started.json").is_file():
            raise AssertionError("started.json was not durable before the first turn")


class ExecutorTests(unittest.TestCase):
    def test_production_wiring_is_hold(self) -> None:
        audit = runner.production_wiring_audit()
        self.assertEqual(audit["status"], "IN_PROGRESS_INTERNAL_ARM_WIRED_FORMAL_RUNTIME_HOLD")
        self.assertFalse(audit["formalExecutionAllowed"])
        self.assertEqual(len(audit["findings"]), 4)

    def test_dry_run_is_42_rows_and_zero_retry(self) -> None:
        result = runner.dry_run()
        self.assertEqual(result["rowCount"], 42)
        self.assertEqual(result["scoredArmTurnCount"], 26)
        self.assertTrue(result["zeroRetries"])

    def test_fixture_writes_strict_42_rows_with_arm_history_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "fixture-attempt"
            adapter = FixtureAdapter(output)
            receipt = runner.run_with_adapter(adapter, output, allow_fixture=True)
            self.assertEqual(receipt["pairedOutputRowCount"], 42)
            self.assertEqual(len((output / "paired_outputs.jsonl").read_text(encoding="utf-8").splitlines()), 42)
            treatment = [item for item in adapter.seen_prior if item[0] == runner.ARMS[1]]
            self.assertTrue(treatment)
            self.assertTrue(all(count == 0 and prior is None for _, count, prior in treatment))
            raw = [item for item in adapter.seen_prior if item[0] == runner.ARMS[0]]
            self.assertTrue(any(count > 0 and prior is not None for _, count, prior in raw))

    def test_fixture_cannot_be_used_formally(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "forbidden-attempt"
            with self.assertRaisesRegex(runner.ExecutionHold, "fixture adapter"):
                runner.run_with_adapter(FixtureAdapter(output), output)


if __name__ == "__main__":
    unittest.main()
