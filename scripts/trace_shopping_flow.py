"""Capture synthetic, read-only 5173 shopping turns without session secrets.

Each turn starts in the browser BFF's step mode. The trace records the
already-committed interpretation and every subsequent durable checkpoint.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.app.api.commerce_demo import _redis
from agent.app.catalog_service import fingerprint
from agent.app.task_state import get_task_state


BASE = "http://127.0.0.1:5173"
SCENARIOS = {
    "preference": ["我喜欢苹果手机", "预算3000元以内", "不限苹果，安卓也行"],
    "explicit": ["来一个苹果手机", "预算3000元以内", "撤销上次修改"],
    "reference": ["找一个玻璃杯", "比较第1项和第2项", "第1项多少钱"],
}


def scope_view(scope):
    if not isinstance(scope, dict):
        return None
    return {"scopeId": scope.get("scopeId"), "sha256": fingerprint(scope),
            "query": scope.get("query"), "groupCount": len(scope.get("groups") or []),
            "firstGroups": [{"number": group.get("number"), "title": group.get("title")}
                            for group in (scope.get("groups") or [])[:5]]}


def state_view(task):
    if task is None:
        return None
    domain = task.domain_state
    return {"revision": task.revision, "status": task.status,
            "shopping": domain.get("shopping"),
            "shoppingMigration": domain.get("shoppingMigration"),
            "evidenceRef": domain.get("guideEvidenceRef"),
            "legacyGuideV1Present": "guideV1" in domain}


def run_view(run, workspace):
    plan = run.get("catalogPlan") or {}
    receipt = run.get("catalogRouteCall") or {}
    next_state = run.get("catalogNext") or {}
    return {"status": run.get("status"), "phaseIndex": run.get("catalogPhase"),
            "nextStage": run.get("nextStage"), "notice": run.get("notice"),
            "intent": run.get("intent"), "workflow": run.get("workflow"),
            "modelPlan": receipt.get("modelPlan"), "effectivePlan": plan,
            "parseAttempts": receipt.get("parseAttempts"),
            "parseUsage": receipt.get("usage"),
            "literalQueryPreserved": receipt.get("literalQueryPreserved", False),
            "queryRenderedFromRequirements": receipt.get("queryRenderedFromRequirements", False),
            "nextDemandProjection": {key: next_state.get(key) for key in
                ("query", "retrievalQuery", "requirements", "revision")},
            "retrievedScope": scope_view(next_state.get("scope")),
            "publishedScope": scope_view(workspace.get("catalogScope")),
            "legacyCatalogSearchPresent": "catalogSearch" in workspace,
            "cardsCount": len(workspace.get("cards") or []),
            "answer": run.get("catalogAnswer"),
            "answerUsage": (run.get("catalogAnswerCall") or {}).get("usage"),
            "nodes": [{"label": node.get("label"), "outcome": node.get("outcome"),
                       "durationMs": node.get("durationMs"),
                       "detail": node.get("detail")} for node in run.get("nodes", [])]}


async def find_run(client, request_id):
    async for key in client.scan_iter(match="commerce:workspace:*:run", count=100):
        raw = await client.get(key)
        if raw is None:
            continue
        run = json.loads(raw)
        if run.get("requestId") == request_id:
            workspace = json.loads(await client.get(key.removesuffix(":run")) or "{}")
            return run, workspace
    raise RuntimeError("submitted run was not found")


async def capture(client, redis, scenario, message, csrf):
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    response = await client.post("/api/commerce-demo/workspace/run",
        headers={"X-CSRF-Token": csrf},
        json={"message": message, "requestId": request_id, "mode": "step"})
    response.raise_for_status()
    run, workspace = await find_run(redis, request_id)
    task = await get_task_state(run["guideTaskId"]) if run.get("guideTaskId") else None
    turn = {"scenario": scenario, "input": message, "requestId": request_id,
            "submittedHttpStatus": response.status_code,
            "afterSubmission": {"run": run_view(run, workspace), "task": state_view(task)},
            "checkpoints": []}
    for _ in range(12):
        if run["status"] not in {"waiting", "paused"}:
            break
        public = (await client.get("/api/commerce-demo/workspace")).json().get("run") or {}
        step = await client.post("/api/commerce-demo/workspace/control/step",
            headers={"X-CSRF-Token": csrf},
            json={"runId": public["id"], "revision": public["revision"]})
        step.raise_for_status()
        deadline = time.monotonic() + 180
        while True:
            public = (await client.get("/api/commerce-demo/workspace")).json().get("run") or {}
            if public.get("status") not in {"running", "pausing"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("step did not reach a checkpoint")
            await asyncio.sleep(.5)
        run, workspace = await find_run(redis, request_id)
        task = await get_task_state(run["guideTaskId"]) if run.get("guideTaskId") else None
        turn["checkpoints"].append({"run": run_view(run, workspace), "task": state_view(task)})
        if run["status"] in {"completed", "interrupted", "failed", "ended", "clarification"}:
            break
    final_message = next((item for item in reversed(workspace.get("messages") or [])
        if item.get("role") == "assistant" and item.get("requestId") == request_id), None)
    turn["final"] = {"status": run.get("status"),
                     "answer": final_message.get("content") if final_message else None,
                     "cardsCount": len(final_message.get("cards") or []) if final_message else 0,
                     "elapsedSeconds": round(time.perf_counter() - started, 2)}
    return turn


async def main():
    output = Path(sys.argv[1])
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"capturedAtUtc": datetime.now(timezone.utc).isoformat(),
                "baseUrl": BASE, "mode": "step", "syntheticInputs": True,
                "scenarios": [], "limitations": [
                    "Model tool-call arguments are not retained verbatim; modelPlan is the validated, default-filled decision before deterministic query rewriting.",
                    "Cookies, CSRF tokens, customer identity and raw workspace keys are excluded.",
                    "All requests are synthetic and read-only; no transaction or after-sales command is submitted."]}
    redis = _redis()
    for name, messages in SCENARIOS.items():
        async with httpx.AsyncClient(base_url=BASE,
            headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin",
                     "X-Conversation-Source": "automated_test"}, timeout=30) as client:
            bootstrap = await client.get("/api/commerce-demo/workspace")
            bootstrap.raise_for_status()
            csrf = bootstrap.json()["csrfToken"]
            scenario = {"name": name, "turns": []}
            evidence["scenarios"].append(scenario)
            for message in messages:
                turn = await capture(client, redis, name, message, csrf)
                scenario["turns"].append(turn)
                output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"scenario": name, "input": message,
                    "status": turn["final"]["status"],
                    "intent": turn["afterSubmission"]["run"]["intent"],
                    "checkpoints": len(turn["checkpoints"])}, ensure_ascii=False), flush=True)
                if turn["final"]["status"] != "completed":
                    return


if __name__ == "__main__":
    asyncio.run(main())
