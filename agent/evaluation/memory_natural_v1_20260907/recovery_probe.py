"""Zero-model, real Redis/Java and distinct-process interrupt recovery probe."""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import redis.asyncio as redis

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import write_new
from agent.evaluation.context_history_strategies_v1_20260905.private_redis import private_redis

OUT = Path("D:/agent-experiments/memory-natural-v1-20260907")
ORIGIN = "http://memory-recovery.test"


async def child(data):
    from agent.app import task_state
    from agent.app.api import memory_bff
    from agent.app.settings import settings
    from agent.app.memory.durable_snapshot import prepare_memory_guard, MemorySnapshotRejected
    from agent.app.agent_trace import TraceBuilder
    from agent.app.graph.resume import run_graph_v2_durable
    store = redis.Redis(host="127.0.0.1", port=data["port"], decode_responses=True)
    model_calls = tool_calls = 0
    async def no_model(**kwargs):
        nonlocal model_calls
        model_calls += 1
        raise RuntimeError("probe_model_forbidden")
    async def no_tool(*args, **kwargs):
        nonlocal tool_calls
        tool_calls += 1
        raise RuntimeError("probe_tool_forbidden")
    with patch.object(task_state, "_client", store), patch.object(memory_bff, "_client", store):
        for key, value in data["settings"].items():
            setattr(settings, key, value)
        resolution = await memory_bff.resolve_memory_run_for_browser_session(data["cookie"],
            category_id="phone", recipient_scope="self", catalog_revision=settings.memory_active_catalog_revision,
            task_id=data["taskId"])
        try:
            guard = await prepare_memory_guard(redis=store, binding=resolution.binding,
                task_id=data["taskId"], run_id=data["runId"], session_id=data["sessionId"],
                resuming=data["restart"])
            result = await run_graph_v2_durable(task_id=data["taskId"], session_id=data["sessionId"],
                run_id=data["runId"], restart=data["restart"], user_message="我想选二手手机",
                client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=no_model))),
                model="no-model", resolve_tool_schemas=lambda state: [],
                tool_caller=no_tool, tool_caller_v2=no_tool,
                trace_builder=TraceBuilder(data["runId"], mode="context_pack"), max_transitions=12,
                control_policy="react_v1", memory_run_binding=resolution.binding, memory_guard=guard)
            response = {"boundary": result.boundary, "runId": result.run_id,
                "threadId": result.thread_id, "checkpointCount": result.checkpoint_count,
                "proposalHash": result.proposal_hash, "retained": len(resolution.binding.preferences)}
        except MemorySnapshotRejected as exc:
            response = {"boundary": "MEMORY_REJECTED", "reason": str(exc)}
    await store.aclose()
    return {**response, "pid": os.getpid(), "modelCalls": model_calls, "toolCalls": tool_calls}


async def main(name):
    from agent.app import task_state
    from agent.app.api import memory_bff
    from agent.app.main import app
    from agent.app.settings import settings
    output = OUT / name
    output.mkdir(exist_ok=False)
    backend = json.loads((OUT / "infrastructure001/ready.json").read_text(encoding="utf-8"))["backendUrl"]
    username, password = "memory-probe-" + secrets.token_hex(8), "Mp1!" + secrets.token_urlsafe(24)
    configuration = {"memory_bff_enabled": True, "memory_projection_client_enabled": True,
        "memory_durable_snapshot_enabled": True, "memory_bff_canary_usernames": username,
        "memory_bff_epoch": name, "backend_base_url": backend}
    responses = []
    async with AsyncExitStack() as stack:
        store = await stack.enter_async_context(private_redis(output / "redis"))
        stack.enter_context(patch.object(task_state, "_client", store))
        stack.enter_context(patch.object(memory_bff, "_client", store))
        for key, value in configuration.items():
            stack.enter_context(patch.object(settings, key, value))
        async with httpx.AsyncClient(base_url=backend) as java:
            registered = await java.post("/api/auth/register", json={"username": username, "password": password})
            if registered.status_code != 201: raise RuntimeError("registration failed")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as browser:
            login = await browser.post("/api/web-memory/login", json={"username": username, "password": password},
                headers={"Origin": ORIGIN})
            if login.status_code != 200: raise RuntimeError("login failed")
            cookie = browser.cookies.get(memory_bff.COOKIE_NAME)
            csrf = login.json()["csrfToken"]
            session = await memory_bff._session(cookie)
            preference = dict(categoryId="phone", recipientScope="self", preferenceKind="prefer",
                attributeKey="os", normalizedValue="android", catalogRevision=settings.memory_active_catalog_revision,
                source="user_confirmed")
            valid = await memory_bff._java("POST", "/api/memory/catalog/validate/v3",
                access_token=session["accessToken"], body=preference)
            assert valid["valid"] is True
            candidate = await memory_bff.store_validated_candidate_for_binding(
                session_binding=session["sessionBinding"], preference=preference, display_text="测试夹具：记住安卓偏好？")
            headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
            confirmed = await browser.post(f"/api/web-memory/candidates/{candidate}/decision",
                json={"action": "confirm"}, headers=headers)
            assert confirmed.status_code == 200
            state = await task_state.create_task_state(task_state.TaskStateCreateRequest(
                task_type="ecommerce_guide", session_id="memory-recovery-session",
                status="collecting_information", goal="帮我选二手手机", pending_questions=["预算是多少？"],
                domain_state={"shoppingGuide": {"category": "phone", "mode": "recommend",
                    "requirements": [], "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing"}}))
            data = {"settings": configuration, "port": store.connection_pool.connection_kwargs["port"],
                "cookie": cookie, "taskId": state.task_id, "runId": "memory-recovery-run",
                "sessionId": state.session_id, "restart": False}
            for index in range(3):
                if index == 2:
                    entries = (await browser.get("/api/web-memory/entries")).json()["entries"]
                    assert len(entries) == 1
                    revoked = await browser.delete("/api/web-memory/entries/" + entries[0]["memoryHandle"], headers=headers)
                    assert revoked.status_code == 200
                data["restart"] = index != 0
                result = await asyncio.to_thread(subprocess.run,
                    [sys.executable, "-m", "agent.evaluation.memory_natural_v1_20260907.recovery_probe", "--child"],
                    input=json.dumps(data), capture_output=True, text=True, encoding="utf-8", timeout=90,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode:
                    write_new(output / f"child-{index}-failure.json", {"exitCode": result.returncode,
                        "stderr": result.stderr[-5000:]})
                    raise RuntimeError("recovery child failed")
                response = json.loads(result.stdout.strip().splitlines()[-1])
                responses.append(response)
                write_new(output / f"child-{index}.json", response)
                print(json.dumps(response), flush=True)
    passed = (len(responses) == 3 and len({row["pid"] for row in responses}) == 3
        and all(row["modelCalls"] == row["toolCalls"] == 0 for row in responses)
        and all(row["boundary"] == "clarification" for row in responses[:2])
        and responses[0]["runId"] == responses[1]["runId"]
        and responses[0]["threadId"] == responses[1]["threadId"]
        and responses[2]["boundary"] == "MEMORY_REJECTED")
    write_new(output / "report.json", {"status": "PASS" if passed else "FAIL", "responses": responses,
        "scope": "REAL_INTERRUPT_PROCESS_RESTART_AND_REVOKED_SNAPSHOT_REJECTION",
        "fixtureConsent": "SCRIPTED", "modelCalls": 0, "toolCalls": 0})


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        print(json.dumps(asyncio.run(child(json.loads(sys.stdin.read())))))
    else:
        asyncio.run(main(sys.argv[1]))
