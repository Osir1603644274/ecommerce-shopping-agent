"""Owned real-Redis process death/restart at the actual executor receipt hook.

Reuse the established graph recovery fixture, explicitly switch to react_v1
and the experimental history flag. Product transport is a recorded fixture;
no native model is requested. This is a scoped crash-window test, not an
end-to-end model publication replay claim.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
import uuid

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import ROOT, capture_sources, file_sha, now, sha, verify_sources, write_new
from agent.evaluation.context_history_strategies_v1_20260905.private_redis import private_redis
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise


def fixture_module():
    sys.path.insert(0, str(ROOT / "agent"))
    from evaluation import graph_v2_process_recovery_1x as fixture
    return fixture


async def worker(port, task, session, mode):
    fixture = fixture_module()
    original = fixture.run_graph_v2_durable

    async def react_graph(**kwargs):
        return await original(**kwargs, control_policy="react_v1")

    with patch.object(fixture.settings, "context_history_v1_enabled", True), \
         patch.object(fixture, "run_graph_v2_durable", side_effect=react_graph):
        result = await fixture._worker(port, task, session, mode)
        return {**result, "controlPolicy": "react_v1", "contextHistoryEnabled": True, "mode": mode}


async def run(output):
    fixture = fixture_module()
    output.mkdir(parents=True, exist_ok=False)
    sources = capture_sources(output / "source_snapshot")
    for path in (Path(__file__), Path(fixture.__file__), ROOT / "agent/tests/two_stage_ranking_fixtures.py"):
        sources[path.relative_to(ROOT).as_posix()] = file_sha(path)
    write_new(output / "started.json", {"at": now(), "kind": "PHYSICAL_PROCESS_DEATH_AT_REAL_GRAPH_HOOK",
        "faultPoint": "after_executor_receipt", "expectedDeathExit": 86,
        "controlPolicy": "react_v1", "contextHistoryEnabled": True, "sourceHashes": sources,
        "nativeModelCalls": 0, "productTransport": "FIXTURE_WITH_PERSISTED_EXECUTION_LEDGER"})
    async with private_redis(output / "redis") as store:
        info = await store.info("server")
        port = int(info["tcp_port"])
        identity = await fixture._bootstrap(port)
        write_new(output / "identity.json", identity)
        records = []
        for mode in ("initial", "restart"):
            verify_sources(sources)
            command = [sys.executable, "-X", "utf8", "-B", "-m",
                "agent.evaluation.context_history_diagnostics_v1_20260905.physical_checkpoint_probe",
                str(output), "--mode", mode, "--port", str(port),
                "--task", identity["taskId"], "--session", identity["sessionId"]]
            started = time.perf_counter()
            # Each new child remains inside the outer supervisor's Windows Job.
            process = await asyncio.to_thread(subprocess.run, command, cwd=ROOT,
                capture_output=True, text=True, encoding="utf-8", timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            row = {"mode": mode, "command": command, "exitCode": process.returncode,
                "stdout": process.stdout, "stderr": process.stderr,
                "durationSeconds": time.perf_counter() - started}
            if process.returncode == 0:
                row["result"] = json.loads(process.stdout.strip().splitlines()[-1])
            records.append(row)
            write_new(output / (mode + ".json"), row)
            print(json.dumps({"mode": mode, "exitCode": process.returncode}), flush=True)
        ledger = [json.loads(line) for line in await store.lrange("graph-v2-1x:ledger:" + identity["taskId"], 0, -1)]
        second = records[1].get("result") or {}
        final, checkpoint = await fixture._read_final(port, identity["taskId"], second["threadId"]) if second.get("threadId") else ({}, None)
        receipt = final.get("domainState", {}).get("v2ExecReceipt") or {}
        projection = final.get("domainState", {}).get("v2ExecReceiptProjection") or {}
        marker = final.get("domainState", {}).get("v2RunMarker") or {}
        checks = {
            "physicalFaultExitObserved": records[0]["exitCode"] == 86 and not records[0]["stdout"],
            "restartCompleted": records[1]["exitCode"] == 0 and second.get("boundary") == "task_completed",
            "singlePersistedToolExecution": len(ledger) == 1,
            "differentActualWorkerProcesses": len(ledger) == 1 and ledger[0].get("pid") != second.get("pid") and bool(second.get("pid")),
            "sameTaskRunThread": len(ledger) == 1 and all(ledger[0].get(key) == receipt.get(key) for key in ("taskId", "runId", "threadId"))
                and all(receipt.get(key) == second.get(key) for key in ("runId", "threadId"))
                and receipt.get("taskId") == identity["taskId"],
            "sameExecutionAndFence": len(ledger) == 1 and all(ledger[0].get(key) == receipt.get(key) for key in ("executionId", "fence")),
            "toolReceiptSuccessful": receipt.get("toolOutcome") == "tool_succeeded" and receipt.get("inboxStatus") == "SUCCEEDED",
            "projectionBindsReceipt": projection.get("receiptHash") == sha(receipt),
            "checkpointMatchesRestart": bool(checkpoint) and checkpoint == second.get("checkpointHash"),
            "sameReactControlPolicy": marker.get("controlPolicy") == "react_v1" and second.get("controlPolicy") == "react_v1",
            "finalRevisionMatches": final.get("revision") == second.get("revision"),
        }
        verify_sources(sources)
        value = {"status": "SCOPED_PHYSICAL_RECOVERY_PASS" if all(checks.values()) else "HOLD",
            "checks": checks, "workers": records, "ledger": ledger, "finalState": final,
            "checkpointHash": checkpoint, "realRedisPid": info["process_id"], "nativeModelCalls": 0,
            "sourceDrift": [], "fullAgentModelPublicationReplayVerified": False,
            "note": "Actual os._exit(86) production executor fault hook then fresh interpreter on the same owned Redis checkpoint/inbox. Validates this tool-receipt window only; model/answer publication and host restart remain distinct."}
        write_new(output / "result.json", value)
        print(json.dumps({"status": value["status"], "checks": checks}), flush=True)
        return all(checks.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--worker-token")
    parser.add_argument("--mode", choices=("initial", "restart"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--task")
    parser.add_argument("--session")
    args = parser.parse_args()
    if args.mode:
        print(json.dumps(asyncio.run(worker(args.port, args.task, args.session, args.mode))), flush=True)
        raise SystemExit(0)
    if not args.output.is_absolute() or args.output.exists():
        raise ValueError("new_absolute_output_required")
    if args.worker_token:
        if sys.stdin.readline().strip() != args.worker_token:
            raise RuntimeError("start_gate_not_released")
        raise SystemExit(0 if asyncio.run(run(args.output)) else 2)
    token = uuid.uuid4().hex
    command = [sys.executable, "-X", "utf8", "-B", "-m",
        "agent.evaluation.context_history_diagnostics_v1_20260905.physical_checkpoint_probe",
        str(args.output), "--worker-token", token]
    raise SystemExit(supervise(command, args.output.with_name(args.output.name + "_supervisor"), timeout=180, start_token=token))
