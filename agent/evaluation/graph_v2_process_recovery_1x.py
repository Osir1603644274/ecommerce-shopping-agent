"""One-shot, cross-process GraphV2 dangerous-window evidence.

This is intentionally separate from the Inbox-only smoke: PID1 executes the
production durable graph and exits at its production fault point; PID2 starts
in a fresh interpreter and resumes the same Redis task/thread/checkpoint.
"""
from __future__ import annotations

import argparse, asyncio, hashlib, json, os, shutil, socket, subprocess, sys, time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import redis
import redis.asyncio as aioredis
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app import task_state
from app.agent_trace import TraceBuilder
from app.control.planning import PlanArgumentSource, PlanStep, TaskPlan
from app.graph.checkpoint import GraphV2CheckpointSaver
from app.graph.resume import run_graph_v2_durable
from app.graph.tool_inbox_v2 import ToolInbox
from app.schemas import ToolTrace
from app.settings import settings
from app.task_state import TaskStateCreateRequest, TaskStatePatchRequest, create_task_state, get_task_state, update_task_state
from app.tools import TOOL_SCHEMAS
from tests.two_stage_ranking_fixtures import two_stage_search_detail

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "graph-v2-process-recovery-1x"
SOURCE_FILES = (
    "evaluation/graph_v2_process_recovery_1x.py", "app/executor.py",
    "app/graph/resume.py", "app/graph/nodes/__init__.py",
    "app/graph/nodes/executor.py", "app/graph/checkpoint.py",
    "app/graph/tool_inbox_v2.py", "app/tool_execution_v2.py",
)

def _canon(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()

def _sha(value: object) -> str: return hashlib.sha256(_canon(value)).hexdigest()
def _write(path: Path, value: object) -> None: path.write_bytes(_canon(value) + b"\n")
def _read(path: Path) -> Any: return json.loads(path.read_text(encoding="utf8"))
def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); return int(probe.getsockname()[1])
def _schemas(_state: Any) -> list[dict[str, Any]]:
    return [item for item in TOOL_SCHEMAS if item["function"]["name"] == "search_products"]
def _client() -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=None)))

async def _bootstrap(port: int) -> dict[str, str]:
    client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    task_state._client = client
    try:
        created = await create_task_state(TaskStateCreateRequest(
            goal="想找 iOS 二手机。", task_type="ecommerce_guide", session_id="session-process-1x",
            domain_state={"shoppingGuide": {"mode":"recommend", "category":"phone", "useCases":[], "requirements":[{"key":"os","operator":"eq","value":"ios","unit":"enum","priority":"hard","source":"user"}], "candidateIds":[], "comparedIds":[], "evidenceStatus":"missing"}},
        ))
        ready = await update_task_state(created.task_id, TaskStatePatchRequest(expectedRevision=created.revision, actor="agent", status="ready"))
        plan = TaskPlan(planId="plan-process-1x", basedOnRevision=ready.revision, steps=[PlanStep(
            stepId="step-process-1x", description="检索二手 iOS 手机", toolName="search_products",
            arguments={"query": ready.goal, "category":"手机"}, argumentSources={"query":PlanArgumentSource(kind="task_goal"), "category":PlanArgumentSource(kind="shopping_guide", reference="category")},
            expectedOutput={"requiresProductCandidates":True},
        )])
        planned = await update_task_state(ready.task_id, TaskStatePatchRequest(expectedRevision=ready.revision, actor="agent", activePlan=plan))
        return {"taskId":planned.task_id, "sessionId":"session-process-1x"}
    finally:
        await client.aclose(); task_state._client = None

async def _worker(port: int, task_id: str, session_id: str, mode: str) -> dict[str, Any]:
    client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    task_state._client = client
    ledger_key = f"graph-v2-1x:ledger:{task_id}"
    try:
        async def legacy(_name: str, _args: dict[str, Any]) -> ToolTrace: raise AssertionError("legacy caller forbidden")
        async def v2(name: str, arguments: dict[str, Any], context: Any) -> ToolTrace:
            await client.rpush(ledger_key, _canon({"pid":os.getpid(), "tool":name, "taskId":context.task_id, "runId":context.run_id, "threadId":context.thread_id, "executionId":context.execution_id, "fence":context.fence, "arguments":arguments}).decode())
            return ToolTrace(tool=name, ok=True, durationMs=1, detail=two_stage_search_detail([101,102]))
        old = settings.agent_graph_v2_fault_point
        settings.agent_graph_v2_fault_point = "after_executor_receipt" if mode == "initial" else ""
        try:
            result = await run_graph_v2_durable(task_id=task_id, session_id=session_id, restart=(mode=="restart"), user_message="想找 iOS 二手机。", client=_client(), model="process-1x", resolve_tool_schemas=_schemas, tool_caller=legacy, tool_caller_v2=v2, tool_inbox=ToolInbox(client, lease_ms=1_000, ttl_ms=60_000), trace_builder=TraceBuilder(f"worker-{os.getpid()}", mode="context_pack"), max_transitions=8, checkpointer=GraphV2CheckpointSaver(serde=JsonPlusSerializer()))
        finally:
            settings.agent_graph_v2_fault_point = old
        return {"pid":os.getpid(), "boundary":result.boundary, "runId":result.run_id, "threadId":result.thread_id, "revision":result.revision, "checkpointHash":result.checkpoint_hash}
    finally:
        await client.aclose(); task_state._client = None

async def _read_final(port: int, task_id: str, thread_id: str) -> tuple[dict[str, Any], str | None]:
    client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    task_state._client = client
    try:
        state = await get_task_state(task_id)
        checkpoint = await GraphV2CheckpointSaver(serde=JsonPlusSerializer()).alatest_checkpoint_hash(thread_id)
        return (state.model_dump(by_alias=True, mode="json") if state is not None else {}), checkpoint
    finally:
        await client.aclose(); task_state._client = None

def _worker_main(args: argparse.Namespace) -> int:
    value = asyncio.run(_worker(args.port, args.task_id, args.session_id, args.mode))
    value.update({"mode": args.mode, "restart": args.mode == "restart"})
    print(json.dumps(value, sort_keys=True), flush=True); return 0

def _cmd(port: int, identity: dict[str,str], mode: str) -> list[str]:
    return [sys.executable, "-u", "-m", "evaluation.graph_v2_process_recovery_1x", "--worker", "--port", str(port), "--task-id", identity["taskId"], "--session-id", identity["sessionId"], "--mode", mode]

def _run_worker(command: list[str]) -> dict[str, Any]:
    process = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate(timeout=45)
    return {"pid": process.pid, "returnCode": process.returncode, "stdout": stdout, "stderr": stderr}

def score_attempt_dir(out_dir: Path) -> dict[str, Any]:
    try:
        manifest, obs, sources = _read(out_dir/"manifest.json"), _read(out_dir/"observations.json"), _read(out_dir/"source-hashes.json")
        if manifest.get("identity") != IDENTITY or manifest.get("observationsSha256") != _sha(obs) or manifest.get("sourceHashesSha256") != _sha(sources): return {"status":"FAIL","errors":["manifest"]}
        if set(sources) != set(SOURCE_FILES): return {"status":"FAIL","errors":["source_set"]}
        if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest for name,digest in sources.items()): return {"status":"FAIL","errors":["source_drift"]}
        ledger, before, after = obs["ledger"], obs["pid1"], obs["pid2"]
        final = obs["finalState"]
        receipt = final.get("domainState",{}).get("v2ExecReceipt", {})
        projection = final.get("domainState",{}).get("v2ExecReceiptProjection", {})
        result = after.get("result", {})
        expected_arguments = obs.get("expectedArguments")
        step_results = final.get("domainState", {}).get("stepExecutionResults", [])
        matching_step = next((item for item in step_results if isinstance(item, dict) and item.get("planId") == receipt.get("planId") and item.get("stepId") == receipt.get("stepId")), None)
        same_identity = all(receipt.get(key) == result.get(key) for key in ("runId", "threadId")) and receipt.get("taskId") == obs["identity"]["taskId"]
        ledger_matches = len(ledger) == 1 and isinstance(ledger[0], dict) and ledger[0] == {
            "arguments": expected_arguments, "executionId": receipt.get("executionId"), "fence": receipt.get("fence"),
            "pid": ledger[0].get("pid"), "runId": receipt.get("runId"), "taskId": receipt.get("taskId"),
            "threadId": receipt.get("threadId"), "tool": receipt.get("toolName"),
        } and isinstance(ledger[0].get("pid"), int) and ledger[0]["pid"] > 0
        receipt_matches = (
            receipt.get("toolName") == "search_products" and receipt.get("toolOutcome") == "tool_succeeded"
            and receipt.get("inboxStatus") == "SUCCEEDED" and isinstance(receipt.get("executionId"), str)
            and receipt.get("fence") == 1 and projection.get("receiptHash") == _sha(receipt)
            and projection.get("projectionRevision") == receipt.get("stateRevision", -5) + 5
            and isinstance(final.get("revision"), int) and projection.get("projectionRevision") <= final["revision"]
            and matching_step is not None and matching_step.get("outcome") == "tool_succeeded"
            and matching_step.get("resolvedArguments") == expected_arguments
        )
        ok = (
            isinstance(before.get("pid"), int) and isinstance(after.get("pid"), int) and before["pid"] > 0 and after["pid"] > 0 and before["pid"] != after["pid"]
            and before.get("returnCode") == 86 and "result" not in before and not before.get("stdout") and not before.get("stderr")
            and after.get("returnCode") == 0 and not after.get("stderr") and result.get("mode") == "restart" and result.get("restart") is True
            and result.get("boundary") == "task_completed" and final.get("taskId") == obs["identity"]["taskId"] and final.get("sessionId") == obs["identity"]["sessionId"]
            and final.get("activePlan", {}).get("status") == "completed" and final.get("revision") == result.get("revision")
            and result.get("checkpointHash") == obs.get("checkpointHash") and isinstance(obs.get("checkpointHash"), str) and bool(obs["checkpointHash"])
            and isinstance(obs.get("rtoMs"), int) and obs["rtoMs"] >= 0 and same_identity and ledger_matches and receipt_matches
        )
        return {
            "status": "HOLD" if ok else "FAIL",
            "dangerousWindowStatus": "PASS" if ok else "FAIL",
            "errors": [] if ok else ["dangerous_window"],
            "tamperRevisionStatus": "HOLD_NOT_EXERCISED",
            "revisionConflictStatus": "HOLD_NOT_EXERCISED",
        }
    except Exception as exc: return {"status":"FAIL","errors":[type(exc).__name__]}

def run_1x(out_dir: Path) -> dict[str, Any]:
    out_dir = out_dir.resolve()
    if out_dir.exists(): raise FileExistsError(f"new-only evidence directory already exists: {out_dir}")
    exe=shutil.which("redis-server")
    if not exe: raise RuntimeError("redis-server required")
    out_dir.mkdir(parents=True); port=_port(); server=subprocess.Popen([exe,"--port",str(port),"--save","","--appendonly","no"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    sync=redis.Redis(host="127.0.0.1",port=port,decode_responses=True)
    try:
        for _ in range(50):
            try:
                if sync.ping(): break
            except redis.RedisError: time.sleep(.05)
        identity=asyncio.run(_bootstrap(port)); first=_run_worker(_cmd(port,identity,"initial")); started=time.monotonic(); second=_run_worker(_cmd(port,identity,"restart")); rto=int((time.monotonic()-started)*1000)
        result=json.loads(second["stdout"].splitlines()[-1]) if second["returnCode"]==0 else None; ledger=[json.loads(x) for x in sync.lrange(f"graph-v2-1x:ledger:{identity['taskId']}",0,-1)]
        thread=result.get("threadId") if isinstance(result,dict) else ""; final, checkpoint=asyncio.run(_read_final(port, identity["taskId"], thread)) if thread else ({},None)
        obs={"identity":identity,"expectedArguments":{"category":"手机","query":"想找 iOS 二手机。"},"pid1":first,"pid2":{**second,"result":result},"rtoMs":rto,"ledger":ledger,"finalState":final,"checkpointHash":checkpoint,"tamperRevisionStatus":"HOLD_NOT_EXERCISED"}; sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCE_FILES}; _write(out_dir/"observations.json",obs); _write(out_dir/"source-hashes.json",sources); manifest={"identity":IDENTITY,"attempt":out_dir.name,"createdAt":datetime.now(UTC).isoformat(),"observationsSha256":_sha(obs),"sourceHashesSha256":_sha(sources)}; _write(out_dir/"manifest.json",manifest); score=score_attempt_dir(out_dir); _write(out_dir/"score.json",score); return score
    finally:
        task_state._client=None; sync.close(); server.terminate()
        try: server.wait(timeout=5)
        except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=5)

def main(argv: list[str]|None=None)->int:
    p=argparse.ArgumentParser(); p.add_argument("--worker",action="store_true"); p.add_argument("--port",type=int); p.add_argument("--task-id"); p.add_argument("--session-id"); p.add_argument("--mode",choices=("initial","restart")); p.add_argument("--out-dir",type=Path); a=p.parse_args(argv)
    if a.worker: return _worker_main(a)
    score=run_1x(a.out_dir); print(json.dumps(score,sort_keys=True)); return 0 if score["dangerousWindowStatus"]=="PASS" else 1
if __name__=="__main__": raise SystemExit(main())
