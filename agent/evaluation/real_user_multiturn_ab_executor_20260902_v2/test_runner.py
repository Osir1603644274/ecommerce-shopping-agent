from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2 import runner


PACKAGE = Path("agent/evaluation/real_user_multiturn_replay_20260903_v7").resolve()


class FixtureRuntime:
    @staticmethod
    def turn_run_id(row: dict[str, Any], lane: runner.LaneBinding) -> str:
        return f"{lane.task_id}-turn-{row['turnId']}-{row['arm'].lower()}"


class FixtureAdapter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self._runtime = FixtureRuntime()
        self.revisions: dict[tuple[str, str], int] = {}
        self.run_ids: list[str] = []

    def begin_conversation(self, conversation: dict[str, Any], arms: tuple[str, str]):
        conversation_id = conversation["conversationId"]
        branch = runner.sha_text(f"fixture-branch:{conversation_id}")
        lanes: dict[str, runner.LaneBinding] = {}
        for arm in arms:
            key = (conversation_id, arm)
            self.revisions[key] = 1
            lane_label = "raw" if arm == runner.ARMS[0] else "context"
            lanes[arm] = runner.LaneBinding(
                f"unused-initial-run-{conversation_id}-{lane_label}",
                f"fixture-task-{conversation_id}-{lane_label}",
                f"fixture-session-{conversation_id}-{lane_label}",
                branch,
                1,
            )
        return lanes

    def execute_turn(
        self,
        row: dict[str, Any],
        lane: runner.LaneBinding,
        prior_raw_same_arm_dialogue: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        if not (self.output_dir / "started.json").is_file():
            raise AssertionError("started.json must be durable before execution")
        key = (row["conversationId"], row["arm"])
        pre_revision = self.revisions[key]
        self.revisions[key] += 1
        self.run_ids.append(lane.run_id)
        ordinal = int(row["executionOrdinal"])
        usage = {
            "promptTokens": 10 + ordinal,
            "completionTokens": 2,
            "totalTokens": 12 + ordinal,
        }
        model_call = {
            "callId": f"fixture-model-{ordinal}",
            "callType": "model",
            "model": "fixture-no-model",
            "status": "SUCCEEDED",
            "durationMs": 1.0,
            "usage": usage,
            "requestSha256": runner.sha_text(f"fixture-request:{ordinal}"),
            "resultSha256": runner.sha_text(f"fixture-result:{ordinal}"),
        }
        prior_count = len(prior_raw_same_arm_dialogue or [])
        return {
            "runId": lane.run_id,
            "taskId": lane.task_id,
            "sessionId": lane.session_id,
            "preStateRevision": pre_revision,
            "postStateRevision": pre_revision + 1,
            "historyMode": row["historyMode"],
            "rawPriorMessageCount": prior_count if row["arm"] == runner.ARMS[0] else 0,
            "compiledContextUsed": row["arm"] == runner.ARMS[1],
            "contextBindingHash": runner.sha_text(f"fixture-context:{ordinal}"),
            "referenceContextBindingHash": runner.sha_text(f"fixture-reference:{ordinal}"),
            "candidateScopeHash": runner.sha_text(f"fixture-scope:{ordinal}"),
            "toolCalls": [],
            "modelCalls": [model_call],
            "durationMs": 2.0,
            "status": "SUCCEEDED",
            "finalAnswer": f"样例回答 {ordinal}",
            "publicEvidence": [],
            "failureCode": None,
        }


class ExecutorV2Tests(unittest.TestCase):
    def test_fixture_writes_and_validates_all_42_unique_turn_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "fixture-attempt"
            adapter = FixtureAdapter(output)
            receipt = runner.run_with_adapter_v2(
                adapter,
                output,
                package=PACKAGE,
                run_set_id="fixture-v5",
            )
            self.assertEqual(receipt["pairedOutputRowCount"], 42)
            self.assertEqual(len(adapter.run_ids), 42)
            self.assertEqual(len(set(adapter.run_ids)), 42)
            self.assertEqual(
                len((output / "paired_outputs.jsonl").read_text(encoding="utf-8").splitlines()),
                42,
            )


if __name__ == "__main__":
    unittest.main()
