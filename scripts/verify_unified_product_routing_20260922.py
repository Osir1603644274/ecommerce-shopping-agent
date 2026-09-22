"""Live 5173 acceptance for category-neutral product routing.

Uses fresh anonymous owners and read-only product searches. It reads only each
client's server-owned Redis workspace in order to verify the private route; no
order, payment, refund, or catalog mutation is performed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid

import httpx
import redis.asyncio as redis


BASE = "http://127.0.0.1:5173"
PREFIX = "/api/commerce-demo/workspace"
CASES = (
    ("phone_search", "找一台3000元以内的苹果手机", "catalog", True),
    ("storage_search", "找适合宿舍的小型透明收纳盒", "catalog", True),
    ("order_business", "查询我的订单", "business", False),
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def own_workspace_key(store: redis.Redis, cookie: str) -> str:
    digest = hashlib.sha256(cookie.encode()).hexdigest()
    matches = [key async for key in store.scan_iter(match=f"commerce:workspace:*:{digest}:guest")]
    if len(matches) != 1:
        raise AssertionError(f"expected one owner workspace, got {len(matches)}")
    return matches[0]


async def wait_for_checkpoint(client: httpx.AsyncClient, previous_revision: int) -> dict:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = await client.get(PREFIX)
        response.raise_for_status()
        value = response.json()
        run = value["run"]
        if run["revision"] > previous_revision and run["status"] in {
            "waiting", "completed", "clarification", "interrupted", "failed"
        }:
            return value
        await asyncio.sleep(0.5)
    raise TimeoutError("workspace checkpoint timeout")


async def execute_catalog(client: httpx.AsyncClient, value: dict) -> dict:
    for _ in range(10):
        run = value["run"]
        if run["status"] == "completed":
            return value
        if run["status"] != "waiting":
            raise AssertionError((run["status"], run.get("notice")))
        response = await client.post(PREFIX + "/control/step", json={
            "runId": run["id"], "revision": run["revision"]
        })
        response.raise_for_status()
        value = await wait_for_checkpoint(client, run["revision"])
    raise AssertionError("catalog workflow did not complete within ten checkpoints")


async def run_case(store: redis.Redis, case_id: str, message: str,
                   expected_route: str, execute: bool) -> dict:
    headers = {"Origin": BASE, "Sec-Fetch-Site": "same-origin",
               "X-Conversation-Source": "automated_test"}
    began = time.perf_counter()
    async with httpx.AsyncClient(base_url=BASE, timeout=210, headers=headers) as client:
        bootstrap = await client.get(PREFIX)
        bootstrap.raise_for_status()
        client.headers["X-CSRF-Token"] = bootstrap.json()["csrfToken"]
        cookie = client.cookies.get("commerce_browser")
        if not cookie:
            raise AssertionError("owner cookie missing")
        key = await own_workspace_key(store, cookie)
        response = await client.post(PREFIX + "/run", json={
            "message": message, "requestId": str(uuid.uuid4()), "mode": "step"
        })
        response.raise_for_status()
        value = response.json()
        private_run = json.loads(await store.get(key + ":run"))
        plan = private_run.get("catalogPlan") or private_run.get("catalogRouteCall", {}).get("modelPlan") or {}
        route = plan.get("route")
        workflow = private_run.get("workflow")
        if route != expected_route:
            raise AssertionError(f"{case_id}: expected route {expected_route}, got {route}")
        if (expected_route == "catalog") != (workflow == "catalog_workspace_v1"):
            raise AssertionError(f"{case_id}: workflow mismatch: {workflow}")
        if execute:
            value = await execute_catalog(client, value)
            private_state = json.loads(await store.get(key))
            scope = (private_state.get("catalogSearch") or {}).get("scope") or {}
            if not scope.get("scopeId"):
                raise AssertionError(f"{case_id}: completed without a catalog scope")
        else:
            run = value["run"]
            response = await client.post(PREFIX + "/control/end", json={
                "runId": run["id"], "revision": run["revision"]
            })
            response.raise_for_status()
        run = value["run"]
        return {
            "case": case_id,
            "message": message,
            "route": route,
            "workflow": workflow,
            "action": plan.get("action"),
            "status": run["status"],
            "seconds": round(time.perf_counter() - began, 3),
            "nodeWorkflows": [node.get("detail", {}).get("workflow") for node in run.get("nodes", [])],
            "answerPublished": any(item.get("role") == "assistant" and item.get("requestId") == run["requestId"]
                                   for item in value.get("messages", [])),
        }


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    sources = [
        "agent/app/catalog_conversation.py",
        "agent/app/api/commerce_controls.py",
        "agent/app/api/catalog_workspace.py",
        "agent/app/product_followup.py",
        "agent/app/main.py",
        "frontend/src/Shop.tsx",
        "frontend/src/CatalogExecutionGraph.tsx",
    ]
    contract = {
        "status": "FROZEN_BEFORE_CALLS",
        "baseUrl": BASE,
        "cases": [{"id": c, "message": m, "expectedRoute": r, "execute": e} for c, m, r, e in CASES],
        "sourceSha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources},
        "commerceWrites": False,
        "qualityClaim": "routing_and_workflow_acceptance_only",
    }
    write_json(output / "CONTRACT.json", contract)
    store = redis.from_url("redis://[::1]:6379/0", decode_responses=True)
    results = []
    try:
        for case in CASES:
            result = await run_case(store, *case)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        await store.aclose()
    same_workflow = results[0]["workflow"] == results[1]["workflow"] == "catalog_workspace_v1"
    status = "PASSED" if same_workflow and all(row["route"] == expected for row, (_, _, expected, _) in zip(results, CASES)) else "FAILED"
    write_json(output / "RESULTS.json", {
        "status": status,
        "sameProductWorkflow": same_workflow,
        "cases": results,
        "limitations": ["does not judge relevance quality", "does not exercise commerce writes"],
    })
    if status != "PASSED":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args().output.resolve()))
