"""One-conversation live smoke for the V2 Context A/B lane runtime."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.evaluation.real_user_multiturn_ab_executor_20260902_v1.runner import (  # noqa: E402
    ARMS,
    DEFAULT_PACKAGE,
    _load_module,
    load_jsonl,
)
from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2.runner import (  # noqa: E402
    PersistentLoopReactV1TurnAdapter,
)
from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2.lane_runtime import (  # noqa: E402
    IsolatedLaneRuntime,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError("refusing to overwrite smoke output")
    output.mkdir(parents=True)
    started = {
        "schemaVersion": "real-user-multiturn-ab-v2-live-smoke-v1",
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "formalAttempt": False,
        "automaticRetries": 0,
        "conversationId": "rumr-v1-c001",
    }
    (output / "started.json").write_text(
        json.dumps(started, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        conversations = load_jsonl(DEFAULT_PACKAGE / "conversations.jsonl")
        conversation = next(row for row in conversations if row["conversationId"] == "rumr-v1-c001")
        schedule_builder = _load_module(DEFAULT_PACKAGE / "build_replay_schedule.py", "context_ab_v2_smoke_schedule")
        rows = [
            row for row in schedule_builder.build_schedule(conversations, 20260902)
            if row["conversationId"] == conversation["conversationId"]
        ]
        by_arm = {arm: sorted((row for row in rows if row["arm"] == arm), key=lambda row: row["semanticTurn"]) for arm in ARMS}
        runtime = IsolatedLaneRuntime(namespace="smoke008")
        adapter = PersistentLoopReactV1TurnAdapter(runtime)
        lanes = adapter.begin_conversation(conversation, ARMS)
        results = []
        for arm in ARMS:
            dialogue: list[dict[str, str]] = []
            lane = lanes[arm]
            for row in by_arm[arm]:
                turn_lane = type(lane)(
                    runtime.turn_run_id(row, lane), lane.task_id, lane.session_id,
                    lane.branch_point_state_hash, lane.pre_state_revision,
                )
                prior = [dict(item) for item in dialogue] if arm == ARMS[0] else None
                result = adapter.execute_turn(row, turn_lane, prior)
                results.append({
                    "arm": arm,
                    "turnId": row["turnId"],
                    "status": result["status"],
                    "preStateRevision": result["preStateRevision"],
                    "postStateRevision": result["postStateRevision"],
                    "modelCallCount": len(result["modelCalls"]),
                    "toolCallCount": len(result["toolCalls"]),
                    "promptTokens": sum(item["usage"]["promptTokens"] for item in result["modelCalls"]),
                    "completionTokens": sum(item["usage"]["completionTokens"] for item in result["modelCalls"]),
                    "durationMs": result["durationMs"],
                    "contextBindingHash": result["contextBindingHash"],
                    "finalAnswer": result["finalAnswer"],
                    "failureCode": result["failureCode"],
                })
                dialogue.extend([
                    {"role": "user", "content": row["rawUserText"]},
                    {"role": "assistant", "content": result["finalAnswer"]},
                ])
                lane = type(lane)(
                    turn_lane.run_id, lane.task_id, lane.session_id, lane.branch_point_state_hash,
                    int(result["postStateRevision"]),
                )
        payload = {
            "status": "PASS" if len(results) == 4 and all(row["status"] == "SUCCEEDED" for row in results) else "HOLD",
            "resultCount": len(results),
            "results": results,
        }
        (output / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"status": payload["status"], "resultCount": len(results)}, ensure_ascii=False))
        return 0 if payload["status"] == "PASS" else 2
    except Exception as exc:
        failed = {
            "status": "FAILED",
            "errorType": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        (output / "failure.json").write_text(
            json.dumps(failed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"status": "FAILED", "errorType": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
