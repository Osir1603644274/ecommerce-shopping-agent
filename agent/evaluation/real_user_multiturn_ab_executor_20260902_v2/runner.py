"""Formal V2 entry for the real-user multi-turn Context A/B executor."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.evaluation.real_user_multiturn_ab_executor_20260902_v1.runner import (  # noqa: E402
    ARMS,
    DEFAULT_PACKAGE,
    LaneBinding,
    ReactV1TurnAdapter,
    _build_output,
    _load_module,
    canonical,
    load_json,
    load_jsonl,
    sha_file,
    sha_text,
)
from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2.lane_runtime import (  # noqa: E402
    IsolatedLaneRuntime,
)


class PersistentLoopReactV1TurnAdapter(ReactV1TurnAdapter):
    """Keep Redis, HTTPX and SDK async resources on one event loop."""

    def __init__(self, lane_runtime, run_agent_callable=None):  # noqa: ANN001
        super().__init__(lane_runtime, run_agent_callable)
        self._loop = asyncio.new_event_loop()

    def begin_conversation(self, conversation, arms):  # noqa: ANN001
        lanes = self._loop.run_until_complete(
            self._runtime.begin_conversation_async(conversation, arms)
        )
        if not isinstance(lanes, dict):
            raise RuntimeError("lane runtime did not return server bindings")
        return lanes

    def execute_turn(self, row, lane, prior_raw_same_arm_dialogue):  # noqa: ANN001
        return self._loop.run_until_complete(
            self._execute_turn(row, lane, prior_raw_same_arm_dialogue)
        )


def run_with_adapter_v2(
    adapter: PersistentLoopReactV1TurnAdapter,
    output_dir: Path,
    *,
    package: Path,
    run_set_id: str,
) -> dict:
    """Execute all rows with a new durable run identity for every user turn."""

    if output_dir.exists():
        raise RuntimeError("refusing to overwrite attempt directory")
    authority = load_json(package / "execution_authority.json")
    expected_hashes = dict(authority["expectedTraceHashes"])
    schema = load_json(package / "paired_output.schema.json")
    schema_version = schema["properties"]["schemaVersion"]["const"]
    package_id = authority["packageId"]
    conversations = load_jsonl(package / "conversations.jsonl")
    schedule_module = _load_module(package / "build_replay_schedule.py", "rumr_executor_v2_schedule")
    schedule = schedule_module.build_schedule(conversations, 20260902)
    by_conversation: dict[str, list[dict]] = {}
    for row in schedule:
        by_conversation.setdefault(row["conversationId"], []).append(row)

    output_dir.mkdir(parents=True, exist_ok=False)
    started = {
        "schemaVersion": "real-user-multiturn-executor-started-v2",
        "runSetId": run_set_id,
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "executionAuthoritySha256": sha_file(package / "execution_authority.json"),
        "executionConfigSnapshotSha256": sha_file(package / "execution_config_snapshot.json"),
        "runnerSha256": sha_file(Path(__file__)),
        "laneRuntimeSha256": sha_file(HERE / "lane_runtime.py"),
        "automaticRetries": 0,
        "fixtureOnly": False,
        "runIdentityPolicy": "unique_per_user_turn",
    }
    started_path = output_dir / "started.json"
    started_path.write_text(json.dumps(started, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    outputs: list[dict] = []
    private_receipts: list[dict] = []
    output_path = output_dir / "paired_outputs.jsonl"
    private_path = output_dir / "private_turn_receipts.jsonl"
    conversation_map = {item["conversationId"]: item for item in conversations}
    for conversation_id, rows in by_conversation.items():
        conversation = conversation_map[conversation_id]
        lanes = adapter.begin_conversation(conversation, ARMS)
        if set(lanes) != set(ARMS):
            raise RuntimeError("adapter did not create both isolated lanes")
        if len({lanes[arm].branch_point_state_hash for arm in ARMS}) != 1:
            raise RuntimeError("adapter lanes do not share the real branch point")
        dialogue_by_arm: dict[str, list[dict[str, str]]] = {arm: [] for arm in ARMS}
        for row in rows:
            arm = row["arm"]
            base_lane = lanes[arm]
            turn_lane = LaneBinding(
                adapter._runtime.turn_run_id(row, base_lane),
                base_lane.task_id,
                base_lane.session_id,
                base_lane.branch_point_state_hash,
                base_lane.pre_state_revision,
            )
            prior = [dict(item) for item in dialogue_by_arm[arm]] if arm == ARMS[0] else None
            result = adapter.execute_turn(row, turn_lane, prior)
            answer = result.get("finalAnswer", "")
            dialogue_by_arm[arm].extend([
                {"role": "user", "content": row["rawUserText"]},
                {"role": "assistant", "content": answer},
            ])
            output = _build_output(
                package_id=package_id,
                schema_version=schema_version,
                row=row,
                lane=turn_lane,
                result=result,
                dialogue=[dict(item) for item in dialogue_by_arm[arm]],
                expected_hashes=expected_hashes,
            )
            Draft202012Validator(schema).validate(output)
            outputs.append(output)
            private = {
                "executionOrdinal": row["executionOrdinal"],
                "conversationId": conversation_id,
                "turnId": row["turnId"],
                "arm": arm,
                "runId": turn_lane.run_id,
                "historyMode": result["historyMode"],
                "rawPriorMessageCount": result["rawPriorMessageCount"],
                "compiledContextUsed": result["compiledContextUsed"],
                "adapterResultSha256": sha_text(canonical(result)),
            }
            private_receipts.append(private)
            with output_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical(output) + "\n"); handle.flush(); os.fsync(handle.fileno())
            with private_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical(private) + "\n"); handle.flush(); os.fsync(handle.fileno())
            lanes[arm] = LaneBinding(
                turn_lane.run_id,
                turn_lane.task_id,
                turn_lane.session_id,
                turn_lane.branch_point_state_hash,
                int(result["postStateRevision"]),
            )
    if len(outputs) != 42:
        raise RuntimeError(f"incomplete attempt: {len(outputs)}/42 rows")

    canonical_outputs = "".join(canonical(row) + "\n" for row in outputs)
    receipt = {
        "schemaVersion": "real-user-multiturn-execution-receipt-v4",
        "packageId": package_id,
        "ownership": "RUNNER_OWNED",
        "producer": "formal_real_user_multiturn_replay_runner",
        "runSetId": run_set_id,
        "executionAuthoritySha256": sha_file(package / "execution_authority.json"),
        "executionConfigSnapshotSha256": sha_file(package / "execution_config_snapshot.json"),
        "pairedOutputsCanonicalSha256": sha_text(canonical_outputs),
        "pairedOutputRowCount": 42,
        "expectedTraceHashes": expected_hashes,
        "observedModelCallCount": sum(len(row["trace"]["modelCalls"]) for row in outputs),
        "observedToolCallCount": sum(len(row["trace"]["toolCalls"]) for row in outputs),
    }
    receipt["receiptBindingSha256"] = sha_text(canonical(receipt))
    receipt_schema = load_json(package / "execution_receipt.schema.json")
    Draft202012Validator(receipt_schema).validate(receipt)
    receipt_path = output_dir / "execution_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    blind = _load_module(package / "blind_packet_builder.py", "rumr_executor_v2_blind_validator")
    blind.validate_outputs(outputs, conversations, receipt, 20260902)
    witness_paths = (started_path, output_path, private_path, receipt_path)
    (output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha_file(path)}  {path.name}\n" for path in witness_paths),
        encoding="utf-8",
        newline="\n",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-set-id", default="context-ab-v2-attempt001")
    args = parser.parse_args()
    runtime = IsolatedLaneRuntime(namespace=args.run_set_id)
    adapter = PersistentLoopReactV1TurnAdapter(runtime)
    receipt = run_with_adapter_v2(
        adapter,
        args.output.resolve(),
        package=args.package.resolve(),
        run_set_id=args.run_set_id,
    )
    print(receipt["receiptBindingSha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
