"""Real-Redis, subprocess crash smoke for the production V2 ToolInbox.

The only inbox state machine used here is ``app.graph.tool_inbox_v2.ToolInbox``
and every business crossing goes through ``ToolInboxCallerV2``.  The small task
and checkpoint sidecars are observations, not a second recovery protocol: full
GraphV2 resume is deliberately reported as CONTRACT_ONLY until it is exercised
through ``run_graph_v2_durable``.
"""
from __future__ import annotations

import argparse, asyncio, hashlib, json, os, shutil, socket, subprocess, sys, time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis
import redis.asyncio as aioredis

from agent.app.graph.tool_inbox_v2 import InboxStatus, ToolInbox, ToolInboxSlot, sha256
from agent.app.schemas import ToolTrace
from agent.app.tool_execution_v2 import ToolInboxCallerV2, ToolInboxExecutionRejected

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SCENARIOS = (
    "after_task_claim_before_inbox_claim_safe_retry", "after_inbox_claim_before_inflight_reclaim_new_fence",
    "after_inflight_before_business_unknown_safe_reject", "after_business_before_inbox_complete_unknown_safe_reject",
    "after_inbox_complete_before_task_projection_auto_exact_replay", "after_task_projection_before_checkpoint_auto_recovery",
    "clarification_parked", "exact_replay", "revision_drift", "checkpoint_tamper", "concurrent_duplicate_claim",
)
_UNKNOWN = frozenset(REQUIRED_SCENARIOS[2:4])
_BUSINESS_ALREADY_CROSSED = REQUIRED_SCENARIOS[3]
# These names originate in the old graph/checkpoint experiment.  This runner
# only proves the ToolInbox boundary, so it must never relabel those probes as
# graph recovery.
_CONTRACT_ONLY = frozenset({REQUIRED_SCENARIOS[5], REQUIRED_SCENARIOS[8], REQUIRED_SCENARIOS[9]})
_PARKED = "clarification_parked"
_EXACT_REPLAY = frozenset({REQUIRED_SCENARIOS[4], "exact_replay"})
_CONCURRENT = "concurrent_duplicate_claim"
_FAULT = {REQUIRED_SCENARIOS[0]: "task", REQUIRED_SCENARIOS[1]: "claim", REQUIRED_SCENARIOS[2]: "inflight", REQUIRED_SCENARIOS[3]: "business", REQUIRED_SCENARIOS[4]: "complete", REQUIRED_SCENARIOS[5]: "projection"}
_LEASE_MS = 120
# A fresh Windows Python worker can take longer than the deliberately short
# lease to import the project.  Retention must model production (days) rather
# than lease duration, otherwise a successful inbox record expires between the
# recovery and replay probes and manufactures a second business crossing.
_RETENTION_MS = 10 * 60 * 1000

def _canon(v: object) -> bytes: return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
def _sha(v: object) -> str: return hashlib.sha256(_canon(v)).hexdigest()
def _write(path: Path, v: object) -> None: path.write_bytes(_canon(v) + b"\n")
def _read(path: Path) -> Any: return json.loads(path.read_text(encoding="utf8"))

def _decode_observed_json(value: object) -> object:
    """Redis GET observations are stored as their raw string payloads."""
    if type(value) is not str:
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value

def _ids(scenario: str) -> dict[str, object]:
    h = _sha(scenario)
    args = {"query": "fixed replay", "scenario": scenario}
    slot = ToolInboxSlot.create(task_id=f"task-{h[:12]}", plan_id=f"plan-{h[12:24]}", step_id=f"step-{h[24:36]}", state_revision=1, tool_name="search_products", canonical_args_sha256=sha256(args))
    return {"slot": slot, "args": args, "run": f"run-{h[36:48]}", "thread": f"thread-{h[48:60]}", "owner": h[:16]}

def _identity(scenario: str, fence: int = 1) -> dict[str, object]:
    ids = _ids(scenario); slot = ids["slot"]
    assert isinstance(slot, ToolInboxSlot)
    trace = ToolTrace(tool="search_products", ok=True, durationMs=1, detail={"items": []})
    return {"taskId": slot.task_id, "runId": ids["run"], "threadId": ids["thread"], "sessionOwnerHash": ids["owner"], "planId": slot.plan_id, "stepId": slot.step_id, "toolName": slot.tool_name, "stateRevision": slot.state_revision, "inputHash": slot.canonical_args_sha256, "resultHash": sha256(trace.model_dump(by_alias=True, mode="json")), "toolOutcome": "tool_succeeded", "executionId": slot.execution_id(run_id=str(ids["run"]), thread_id=str(ids["thread"])), "logicalSlotKey": slot.logical_slot_key(), "fence": fence}

def _succeeded_receipt(scenario: str, fence: int = 1) -> dict[str, object]:
    """The receipt is the identity plus the authoritative inbox terminal state."""
    return _succeeded_receipt_from_identity(_identity(scenario, fence))

def _succeeded_receipt_from_identity(identity: dict[str, object]) -> dict[str, object]:
    return {**identity, "inboxStatus": InboxStatus.SUCCEEDED.value}

async def _worker_async(port: int, namespace: str, scenario: str, phase: str, barrier_key: str | None = None, barrier_count: int = 0) -> dict[str, object]:
    client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    try:
        ids = _ids(scenario); slot = ids["slot"]; args = ids["args"]
        assert isinstance(slot, ToolInboxSlot) and isinstance(args, dict)
        # Namespace isolation is supplied by an adapter that prefixes actual production key calls.
        class PrefixClient:
            async def eval(self, script: str, numkeys: int, *values: object) -> object:
                keys = [f"{namespace}:{value}" for value in values[:numkeys]]
                return await client.eval(script, numkeys, *keys, *values[numkeys:])
        inbox = ToolInbox(PrefixClient(), lease_ms=_LEASE_MS, ttl_ms=_RETENTION_MS)
        ledger_key = f"{namespace}:runner-ledger:{scenario}"
        async def caller(name: str, call_args: dict[str, Any], context: Any) -> ToolTrace:
            start = _identity(scenario, context.fence)
            await client.rpush(ledger_key, _canon({"kind": "business_started", "identity": start, "tool": name, "arguments": call_args, "toolOutcome": start["toolOutcome"]}).decode())
            if phase == "business":
                # It crossed the real ToolInbox IN_FLIGHT boundary, then is killed before complete.
                trace = ToolTrace(tool=name, ok=True, durationMs=1, detail={"items": []})
                finish = _identity(scenario, context.fence)
                await client.rpush(ledger_key, _canon({"kind": "business_finished", "identity": finish, "resultHash": finish["resultHash"], "toolOutcome": finish["toolOutcome"]}).decode())
                print(json.dumps({"readyToKill": True, "stage": "after_business"}), flush=True); print("READY_TO_KILL", flush=True)
                while True: await asyncio.sleep(1)
            trace = ToolTrace(tool=name, ok=True, durationMs=1, detail={"items": []})
            finish = _identity(scenario, context.fence)
            await client.rpush(ledger_key, _canon({"kind": "business_finished", "identity": finish, "resultHash": finish["resultHash"], "toolOutcome": finish["toolOutcome"]}).decode())
            return trace
        boundary = ToolInboxCallerV2(inbox=inbox, caller=caller)
        if phase == "task":
            await client.set(f"{namespace}:task:{scenario}", _canon({"taskClaimed": True, "identity": _identity(scenario)}))
            print(json.dumps({"readyToKill": True, "stage": "after_task"}), flush=True); print("READY_TO_KILL", flush=True)
            while True: await asyncio.sleep(1)
        if phase == "claim":
            await inbox.claim(slot, run_id=str(ids["run"]), thread_id=str(ids["thread"]))
            print(json.dumps({"readyToKill": True, "stage": "after_claim"}), flush=True); print("READY_TO_KILL", flush=True)
            while True: await asyncio.sleep(1)
        if phase == "inflight":
            claimed = await inbox.claim(slot, run_id=str(ids["run"]), thread_id=str(ids["thread"]))
            entered = await inbox.enter_in_flight(slot, execution_id=str(claimed.execution_id), fence=int(claimed.fence))
            print(json.dumps({"readyToKill": True, "stage": "after_inflight", "status": entered.status.value}), flush=True); print("READY_TO_KILL", flush=True)
            while True: await asyncio.sleep(1)
        if phase == "parked":
            await client.set(f"{namespace}:task:{scenario}", _canon({"clarification": "PARKED", "identity": _identity(scenario)}))
            return {"status": "PARKED"}
        if barrier_key is not None:
            if barrier_count != 2:
                raise ValueError("concurrent smoke requires exactly two fresh workers")
            await client.rpush(barrier_key, "ready")
            deadline = time.monotonic() + 10
            while await client.llen(barrier_key) < barrier_count:
                if time.monotonic() >= deadline:
                    raise TimeoutError("concurrent worker rendezvous timed out")
                await asyncio.sleep(.005)
        try:
            result = await boundary.execute(slot=slot, run_id=str(ids["run"]), thread_id=str(ids["thread"]), session_owner_hash=str(ids["owner"]), tool_name="search_products", arguments=args)
            if phase == "complete":
                print(json.dumps({"readyToKill": True, "stage": "after_complete"}), flush=True); print("READY_TO_KILL", flush=True)
                while True: await asyncio.sleep(1)
            if phase == "projection":
                await client.set(f"{namespace}:task:{scenario}", _canon({"projection": result.receipt, "identity": _identity(scenario, result.context.fence)}))
                print(json.dumps({"readyToKill": True, "stage": "after_projection"}), flush=True); print("READY_TO_KILL", flush=True)
                while True: await asyncio.sleep(1)
            return {"status": "REPLAYED" if result.replayed else "RECOVERED", "fence": result.context.fence, "receipt": result.receipt}
        except ToolInboxExecutionRejected as exc:
            return {"status": "UNKNOWN_SAFE_REJECT" if exc.response.status is InboxStatus.UNKNOWN else f"REJECTED_{exc.response.status.value}"}
    finally: await client.aclose()

def _worker(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("--worker", action="store_true"); p.add_argument("--port", type=int, required=True); p.add_argument("--namespace", required=True); p.add_argument("--scenario", choices=REQUIRED_SCENARIOS, required=True); p.add_argument("--phase", required=True); p.add_argument("--barrier-key"); p.add_argument("--barrier-count", type=int, default=0)
    a = p.parse_args(argv); value = asyncio.run(_worker_async(a.port, a.namespace, a.scenario, a.phase, a.barrier_key, a.barrier_count)); print(json.dumps(value, sort_keys=True), flush=True); return 0

def _cmd(port: int, ns: str, scenario: str, phase: str, *, barrier_key: str | None = None) -> list[str]:
    command = [sys.executable, "-u", "-m", "agent.evaluation.checkpoint_recovery_inbox_v2", "--worker", "--port", str(port), "--namespace", ns, "--scenario", scenario, "--phase", phase]
    if barrier_key is not None:
        command.extend(["--barrier-key", barrier_key, "--barrier-count", "2"])
    return command
def _call(port: int, ns: str, s: str, phase: str, kill: bool=False) -> dict[str, object]:
    proc = subprocess.Popen(_cmd(port, ns, s, phase), cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if kill:
        assert proc.stdout
        lines=[]
        for _ in range(30):
            line=proc.stdout.readline(); lines.append(line)
            if line.strip()=="READY_TO_KILL": proc.kill(); out, err=proc.communicate(timeout=5); return {"returnCode":proc.returncode,"killed":True,"stdout":"".join(lines)+out,"stderr":err}
        proc.kill(); out,err=proc.communicate(timeout=5); return {"returnCode":proc.returncode,"killed":True,"stdout":"".join(lines)+out,"stderr":err,"error":"not_ready"}
    out,err=proc.communicate(timeout=20); return {"returnCode":proc.returncode,"stdout":out,"stderr":err}
def _status(t: dict[str, object]) -> str | None:
    try: return json.loads(str(t["stdout"]).splitlines()[-1])["status"]
    except Exception: return None

def _kill_stage(t: object) -> str | None:
    if not isinstance(t, dict) or t.get("killed") is not True or type(t.get("returnCode")) is not int or t["returnCode"] == 0:
        return None
    lines = str(t.get("stdout", "")).splitlines()
    if len(lines) < 2 or lines[-1] != "READY_TO_KILL":
        return None
    try:
        ready = json.loads(lines[0])
        return ready.get("stage") if ready.get("readyToKill") is True and type(ready.get("stage")) is str else None
    except json.JSONDecodeError:
        return None
def _port() -> int:
    with socket.socket() as s: s.bind(("127.0.0.1",0)); return int(s.getsockname()[1])

IDENTITY_FIELDS = tuple(_identity(REQUIRED_SCENARIOS[0]).keys())

def _expected_business_crossings(scenario: str) -> int:
    # A crash after IN_FLIGHT but before the caller has entered the tool is
    # safely rejected with no crossing.  A crash after the caller returns is
    # also UNKNOWN, but must retain its single runner-owned ledger crossing.
    return 0 if scenario == REQUIRED_SCENARIOS[2] or scenario == _PARKED else 1

def _expected_classification(scenario: str) -> str:
    if scenario in _UNKNOWN:
        return "SAFE_REJECT_PASS"
    if scenario == _PARKED:
        return "PARKED_CONTRACT_ONLY"
    if scenario in _CONTRACT_ONLY:
        return "CONTRACT_ONLY"
    if scenario == _CONCURRENT:
        return "CONCURRENT_SINGLE_CROSSING"
    if scenario in _EXACT_REPLAY:
        return "EXACT_REPLAY_PASS"
    return "RECOVERED"

def _identity_matches_slot(identity: object, scenario: str) -> bool:
    if not isinstance(identity, dict) or set(identity) != set(IDENTITY_FIELDS):
        return False
    expected = _identity(scenario, int(identity.get("fence", 0)) if type(identity.get("fence")) is int else 0)
    return identity == expected

def score_checkpoint_recovery_inbox_v2(report: dict[str, object]) -> dict[str, object]:
    errors=[]; cases=report.get("scenarios")
    if report.get("identity")!="checkpoint-recovery-inbox-v2" or not isinstance(cases,list): return {"status":"FAIL","errors":["report"]}
    names=[x.get("scenario") for x in cases if isinstance(x,dict)]
    if len(cases)!=11 or set(names)!=set(REQUIRED_SCENARIOS) or len(names)!=len(set(names)): errors.append("scenario_closure")
    for c in cases:
        if not isinstance(c,dict): errors.append("case"); continue
        n=c.get("scenario"); identity=c.get("identity",{}); ledger=c.get("ledger",[]); status=c.get("recoveryStatus")
        if c.get("status") != "PASS" or c.get("runnerOwned") is not True:
            errors.append(f"{n}:runner_ownership")
        if type(n) is not str or not _identity_matches_slot(identity, n): errors.append(f"{n}:identity")
        expected_crossings = _expected_business_crossings(n) if isinstance(n, str) else -1
        if not isinstance(ledger,list) or len(ledger) != expected_crossings * 2 or len(ledger) % 2 or len(ledger) // 2 > 1: errors.append(f"{n}:business_count")
        for i in range(0,len(ledger),2):
            a,b=ledger[i:i+2]
            args = _ids(str(n))["args"] if isinstance(n, str) else None
            if (not isinstance(a,dict) or not isinstance(b,dict)
                    or a.get("kind") != "business_started" or b.get("kind") != "business_finished"
                    or a.get("tool") != "search_products" or a.get("arguments") != args
                    or a.get("identity") != b.get("identity") or b.get("identity") != identity
                    or b.get("resultHash") != identity.get("resultHash")
                    or a.get("toolOutcome") != identity.get("toolOutcome")
                    or b.get("toolOutcome") != identity.get("toolOutcome")):
                errors.append(f"{n}:ledger_closure")
        if c.get("classification") != _expected_classification(n) if isinstance(n, str) else True:
            errors.append(f"{n}:classification")
        if n in _FAULT:
            expected_stage={"task":"after_task", "claim":"after_claim", "inflight":"after_inflight", "business":"after_business", "complete":"after_complete", "projection":"after_projection"}[_FAULT[n]]
            transcripts=c.get("transcripts")
            if not isinstance(transcripts,dict) or _kill_stage(transcripts.get("initial")) != expected_stage:
                errors.append(f"{n}:kill_transcript")
        if n in _UNKNOWN:
            if status!="UNKNOWN_SAFE_REJECT" or c.get("classification")!="SAFE_REJECT_PASS": errors.append(f"{n}:unknown")
        elif status=="UNKNOWN_SAFE_REJECT": errors.append(f"{n}:unknown_unregistered")
        elif n == _PARKED:
            if status != "PARKED": errors.append(f"{n}:parked")
        elif n in _CONTRACT_ONLY:
            if status != "CONTRACT_ONLY_NOT_EXERCISED": errors.append(f"{n}:contract_only")
        elif n == _CONCURRENT:
            workers=c.get("concurrentWorkers")
            if status != "CONCURRENT_SINGLE_CROSSING" or not isinstance(workers,list) or len(workers)!=2 or sorted(_status(x) for x in workers if isinstance(x,dict)) != ["RECOVERED","REPLAYED"] or any(not isinstance(x,dict) or x.get("returnCode") != 0 or x.get("stderr") != "" for x in workers): errors.append(f"{n}:concurrency")
        elif n in _EXACT_REPLAY:
            if status != "REPLAYED": errors.append(f"{n}:exact_replay")
        elif status != "RECOVERED": errors.append(f"{n}:recovered")
        replay_required = n not in _CONTRACT_ONLY if isinstance(n, str) else False
        r=c.get("exactReplay")
        if replay_required:
            if not isinstance(r,dict) or r.get("applicable") is not True or any(r.get(k)!=0 for k in ("taskStateDelta","checkpointDelta","inboxDelta","ledgerDelta")): errors.append(f"{n}:replay_delta")
        elif r != {"applicable": False}:
            errors.append(f"{n}:replay_not_applicable")
    # A production inbox smoke can pass while the graph/checkpoint integration
    # remains unproven.  Never collapse that distinction into a 11/11 claim.
    inbox_status = "PASS" if not errors else "FAIL"
    return {"status": "HOLD" if inbox_status == "PASS" else "FAIL", "toolInboxSmokeStatus":inbox_status,"fullGraphStatus":"HOLD","errors":errors}

def score_attempt_dir(out_dir: Path) -> dict[str, object]:
    try:
        manifest, report, source = _read(out_dir/"manifest.json"), _read(out_dir/"observations.json"), _read(out_dir/"source-hashes.json")
        if manifest.get("observationsSha256")!=_sha(report) or manifest.get("sourceIdentitySha256")!=_sha(source): return {"status":"FAIL","errors":["manifest_hash"]}
        for name,digest in source.items():
            if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest: return {"status":"FAIL","errors":["source_drift"]}
        for case in report["scenarios"]:
            if not isinstance(case, dict) or type(case.get("scenario")) is not str:
                return {"status":"FAIL","errors":["case"]}
            d=out_dir/case["scenario"]
            side={k:_read(d/f"{k}.json") for k in ("task","inbox","checkpoint","ledger")}
            if case.get("sidecars")!={k:_sha(v) for k,v in side.items()} or side["ledger"]!=case.get("ledger"):
                return {"status":"FAIL","errors":[f"{case['scenario']}:sidecar"]}
            identity=case.get("identity")
            inbox=side["inbox"]
            scenario=case["scenario"]
            expected_task = (
                {"taskClaimed": True, "identity": identity} if scenario == REQUIRED_SCENARIOS[0]
                else {"clarification": "PARKED", "identity": identity} if scenario == _PARKED
                else {"projection": _succeeded_receipt_from_identity(identity), "identity": identity} if scenario == REQUIRED_SCENARIOS[5]
                else None
            )
            if type(side["task"]) is str or type(side["checkpoint"]) is str:
                return {"status":"FAIL","errors":[f"{scenario}:raw_task_checkpoint_sidecar"]}
            if side["task"] != expected_task or side["checkpoint"] is not None:
                return {"status":"FAIL","errors":[f"{scenario}:task_checkpoint_sidecar"]}
            if not isinstance(identity,dict):
                return {"status":"FAIL","errors":[f"{scenario}:identity_sidecar"]}
            if inbox is None and scenario == _PARKED:
                continue
            if not isinstance(inbox, dict) or inbox.get("base") != _ids(scenario)["slot"].base() or inbox.get("inputHash") != identity.get("inputHash"):
                return {"status":"FAIL","errors":[f"{case['scenario']}:inbox_sidecar"]}
            if scenario in _UNKNOWN:
                if (
                    inbox.get("status") != InboxStatus.UNKNOWN.value
                    or inbox.get("fence") != identity.get("fence")
                    or inbox.get("executionId") != identity.get("executionId")
                    or any(inbox.get(k) is not None for k in ("trace", "receipt", "resultHash", "receiptHash"))
                ):
                    return {"status":"FAIL","errors":[f"{scenario}:unknown_sidecar"]}
            elif inbox.get("status") == InboxStatus.SUCCEEDED.value:
                try:
                    receipt=json.loads(inbox["receipt"]); trace=ToolTrace.model_validate_json(inbox["trace"])
                    if (
                        receipt != _succeeded_receipt(scenario, int(identity["fence"]))
                        or inbox.get("executionId") != identity.get("executionId")
                        or inbox.get("fence") != identity.get("fence")
                        or inbox.get("resultHash") != identity.get("resultHash")
                        or inbox.get("receiptHash") != sha256(receipt)
                        or receipt.get("toolOutcome") != identity.get("toolOutcome")
                        or trace.ok is not True
                        or sha256(trace.model_dump(by_alias=True, mode="json")) != identity.get("resultHash")
                    ):
                        return {"status":"FAIL","errors":[f"{case['scenario']}:result_sidecar"]}
                except Exception:
                    return {"status":"FAIL","errors":[f"{case['scenario']}:result_sidecar"]}
            else:
                return {"status":"FAIL","errors":[f"{scenario}:terminal_inbox"]}
        return score_checkpoint_recovery_inbox_v2(report)
    except Exception as exc: return {"status":"FAIL","errors":[f"evidence:{type(exc).__name__}"]}

def run_checkpoint_recovery_inbox_v2(out_dir: Path) -> dict[str, object]:
    out_dir=out_dir.resolve()
    if out_dir.exists(): raise FileExistsError(f"new-only evidence directory already exists: {out_dir}")
    exe=shutil.which("redis-server")
    if not exe: raise RuntimeError("redis-server required")
    out_dir.mkdir(parents=True); port=_port(); ns=f"checkpoint-v2:{_sha(str(out_dir))[:16]}"; server=subprocess.Popen([exe,"--port",str(port),"--save","","--appendonly","no"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); sync=redis.Redis(host="127.0.0.1",port=port,decode_responses=True)
    try:
        for _ in range(50):
            try:
                if sync.ping(): break
            except redis.RedisError: time.sleep(.05)
        cases=[]
        for s in REQUIRED_SCENARIOS:
            phase="parked" if s=="clarification_parked" else _FAULT.get(s,"normal")
            classification=_expected_classification(s)
            if s == _CONCURRENT:
                barrier_key=f"{ns}:concurrent-barrier:{s}"
                workers=[subprocess.Popen(_cmd(port,ns,s,"normal",barrier_key=barrier_key),cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
                concurrent=[]
                for worker in workers:
                    out, err=worker.communicate(timeout=45)
                    concurrent.append({"returnCode":worker.returncode,"stdout":out,"stderr":err})
                t={f"initial{index}":value for index,value in enumerate(concurrent)}
            else:
                t={"initial":_call(port,ns,s,phase,s in _FAULT)}
            if phase in {"claim","inflight","business"}: time.sleep(.22)
            if s == _PARKED:
                t["recovery"]=_call(port,ns,s,"parked")
            elif s in _CONTRACT_ONLY:
                t["recovery"]={"returnCode":0,"stdout":json.dumps({"status":"CONTRACT_ONLY_NOT_EXERCISED"}),"stderr":""}
            elif s == _CONCURRENT:
                t["recovery"]={"returnCode":0,"stdout":json.dumps({"status":"CONCURRENT_SINGLE_CROSSING"}),"stderr":""}
            else:
                t["recovery"]=_call(port,ns,s,"normal")
            before={k:sync.get(f"{ns}:{k}:{s}") for k in ("task","checkpoint")}; before["inbox"]=sync.get(f"{ns}:agent-tool-inbox:v2:record:{_ids(s)['slot'].logical_slot_key()}"); before["ledger"]=sync.lrange(f"{ns}:runner-ledger:{s}",0,-1)
            if s == _PARKED:
                t["replay"]=_call(port,ns,s,"parked")
            elif s in _CONTRACT_ONLY:
                t["replay"]={"returnCode":0,"stdout":json.dumps({"status":"NOT_APPLICABLE"}),"stderr":""}
            else:
                t["replay"]=_call(port,ns,s,"normal")
            after={k:sync.get(f"{ns}:{k}:{s}") for k in ("task","checkpoint")}; after["inbox"]=sync.get(f"{ns}:agent-tool-inbox:v2:record:{_ids(s)['slot'].logical_slot_key()}"); after["ledger"]=sync.lrange(f"{ns}:runner-ledger:{s}",0,-1)
            ledger=[json.loads(x) for x in after["ledger"]]; record=json.loads(after["inbox"]) if after["inbox"] else None; fence=int(record["fence"]) if record else 1
            d=out_dir/s; d.mkdir(); side={"task":_decode_observed_json(before["task"]),"inbox":record,"checkpoint":_decode_observed_json(before["checkpoint"]),"ledger":ledger}
            for k,v in side.items(): _write(d/f"{k}.json",v)
            replay={"applicable": False} if s in _CONTRACT_ONLY else {"applicable":True,"taskStateDelta":int(before["task"]!=after["task"]),"checkpointDelta":int(before["checkpoint"]!=after["checkpoint"]),"inboxDelta":int(before["inbox"]!=after["inbox"]),"ledgerDelta":int(before["ledger"]!=after["ledger"])}
            cases.append({"scenario":s,"status":"PASS","classification":classification,"recoveryStatus":_status(t["recovery"]),"identity":_identity(s,fence),"ledger":ledger,"exactReplay":replay,"sidecars":{k:_sha(v) for k,v in side.items()},"transcripts":t,"concurrentWorkers":concurrent if s == _CONCURRENT else None,"runnerOwned":True})
        report={"identity":"checkpoint-recovery-inbox-v2","createdAt":datetime.now(UTC).isoformat(),"scenarios":cases,"redis":{"port":port,"namespace":ns},"scope":"ToolInboxCallerV2 real Redis; GraphV2 resume CONTRACT_ONLY"}; _write(out_dir/"observations.json",report)
        sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ("agent/evaluation/checkpoint_recovery_inbox_v2.py","agent/scripts/run_checkpoint_recovery_inbox_v2.py","agent/tests/test_checkpoint_recovery_inbox_v2.py","agent/app/graph/tool_inbox_v2.py","agent/app/tool_execution_v2.py")}; _write(out_dir/"source-hashes.json",sources)
        manifest={"identity":"checkpoint-recovery-inbox-v2","attempt":out_dir.name,"observationsSha256":_sha(report),"sourceIdentitySha256":_sha(sources),"scenarioCount":11}; _write(out_dir/"manifest.json",manifest); score=score_attempt_dir(out_dir); _write(out_dir/"score.json",score); return {"outDir":str(out_dir),"score":score,"manifest":manifest}
    finally:
        sync.close(); server.terminate()
        try: server.wait(timeout=4)
        except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=4)

def main(argv: list[str]|None=None)->int:
    if "--worker" in (argv or sys.argv[1:]): return _worker(argv)
    p=argparse.ArgumentParser(); p.add_argument("--out-dir",type=Path,required=True); a=p.parse_args(argv); r=run_checkpoint_recovery_inbox_v2(a.out_dir); print(json.dumps(r,sort_keys=True)); return 0 if r["score"].get("toolInboxSmokeStatus")=="PASS" else 1
if __name__=="__main__": raise SystemExit(main())
