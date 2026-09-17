"""Run existing exact-replay fixtures on owned real Redis (model/tools mocked).

Does not claim physical crash recovery or real model answer quality. It closes
the gap between in-memory checkpoint tests and persisted owned Redis receipts.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch
import uuid

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import ROOT, capture_sources, file_sha, now, verify_sources, write_new
from agent.evaluation.context_history_strategies_v1_20260905.private_redis import private_redis
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise

CASES = (
    "test_exact_answer_replay_is_idempotent_no_new_revision_tool_or_checkpoint",
    "test_duplicate_replays_are_identical_and_revision_stable",
    "test_replay_after_task_advanced_remains_idempotent",
    "test_different_answer_on_resolved_interrupt_fails_closed",
    "test_cross_session_resume_and_replay_reject_before_graph",
)


async def run(output):
    sys.path.insert(0, str(ROOT / "agent"))
    import redis.asyncio as redis
    from app import task_state, llm
    from app.settings import settings
    from tests import test_graph_v2_idempotent_resume as fixture
    output.mkdir(parents=True, exist_ok=False)
    hashes = capture_sources(output / "source_snapshot")
    for path in (Path(__file__), Path(fixture.__file__), ROOT / "agent/tests/test_graph_v2_interrupt_resume.py",
                 ROOT / "agent/tests/two_stage_ranking_fixtures.py"):
        hashes[path.relative_to(ROOT).as_posix()] = file_sha(path)
    write_new(output / "started.json", {"at": now(), "kind": "REAL_REDIS_CHECKPOINT_MOCK_MODEL_TOOL_PROBE",
        "cases": CASES, "sources": hashes, "models": "MOCKED_NO_NATIVE_CALLS", "tools": "FIXTURE_ONLY",
        "physicalCrashTested": False, "sourceIteration": "v7-shared-infrastructure"})
    rows = []
    for number, name in enumerate(CASES, 1):
        verify_sources(hashes)
        directory = output / f"case-{number:02d}"
        started = time.perf_counter()
        async with private_redis(directory / "redis") as store:
            with patch.object(fixture, "InFileRedis", return_value=store), \
                 patch.object(task_state, "_client", store), \
                 patch.object(settings, "context_history_v1_enabled", True), \
                 patch.object(redis, "from_url", side_effect=RuntimeError("unowned_redis_forbidden")), \
                 patch.object(llm, "get_client", side_effect=RuntimeError("model_fallback_forbidden")):
                case = fixture.GraphV2IdempotentResumeTests(name)
                case.setUp()
                try:
                    await getattr(case, name)()
                    keys = sorted([key async for key in store.scan_iter()])
                    info = await store.info("server")
                    row = {"name": name, "status": "PASS", "realRedisPid": info["process_id"],
                        "persistedKeyCount": len(keys), "persistedKeys": keys,
                        "nativeModelCalls": 0, "businessToolsMocked": True,
                        "durationSeconds": time.perf_counter() - started}
                except BaseException as exc:
                    row = {"name": name, "status": "FAIL", "type": type(exc).__name__,
                        "error": str(exc), "traceback": traceback.format_exc()}
                finally:
                    case.tearDown()
                rows.append(row)
                write_new(directory / "result.json", row)
                print(json.dumps({key: row[key] for key in ("name", "status")}), flush=True)
    verify_sources(hashes)
    accepted = all(row["status"] == "PASS" for row in rows)
    write_new(output / "result.json", {"status": "SCOPED_REAL_STORE_PASS" if accepted else "HOLD", "cases": rows,
        "sourceDrift": [], "nativeModelCalls": 0, "physicalCrashRecoveryVerified": False,
        "realModelOrProductQualityVerified": False,
        "note": "Real Redis persisted graph checkpoints and replay receipts; fixture assertions check no duplicate graph/tool/checkpoint or revision change and rejection of conflicts/foreign session. Separate from the real Codex subscription business probes."})
    return accepted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--worker-token")
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.exists():
        raise ValueError("new_absolute_output_required")
    if args.worker_token:
        if sys.stdin.readline().strip() != args.worker_token:
            raise RuntimeError("start_gate_not_released")
        raise SystemExit(0 if asyncio.run(run(args.output)) else 2)
    token = uuid.uuid4().hex
    command = [sys.executable, "-X", "utf8", "-B", "-m",
        "agent.evaluation.context_history_diagnostics_v1_20260905.real_store_resume_probe",
        str(args.output), "--worker-token", token]
    raise SystemExit(supervise(command, args.output.with_name(args.output.name + "_supervisor"), timeout=240, start_token=token))
