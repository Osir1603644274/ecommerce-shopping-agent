"""Real Agent + owned Redis + Java/MySQL confirmation cross-session pilot.

Product tools use the frozen 439-item fixture, not a live marketplace. Memory
commands are real same-origin BFF HTTP requests against a fresh Java database.
Scripted confirmation is not evidence of actual user willingness.
"""
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time
from unittest.mock import patch

import httpx

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import write_new, append
from .budget import BudgetClient as SubscriptionClient
from agent.evaluation.context_history_strategies_v1_20260905.private_redis import private_redis
from agent.evaluation.context_history_strategies_v1_20260905.catalog_transport import ContextCatalogTransport
from .trajectories import DEV, VALIDATION

ROOT = Path(__file__).resolve().parents[3]
OUT = Path("D:/agent-experiments/memory-natural-v1-20260907")
ORIGIN = "http://memory-first.test"


async def main(name, trajectory_id, arm):
    if arm not in {"M0", "M1", "M2"}:
        raise ValueError("invalid arm")
    trajectory = next(row for row in DEV + VALIDATION if row["id"] == trajectory_id)
    backend = json.loads((OUT / "infrastructure001/ready.json").read_text(encoding="utf-8"))["backendUrl"]
    from agent.app import agent_trace, llm, task_state, model_call_observability, reference_context
    from agent.app import memory_candidate_worker as worker
    from agent.app.api import memory_bff
    from agent.app.main import app
    from agent.app.evaluation_context_arm import issue_evaluation_context_arm
    from agent.app.settings import settings
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2 import lane_runtime as catalog

    output = OUT / name
    output.mkdir(exist_ok=False)
    sources = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (ROOT / "agent/app").rglob("*.py")}
    for path in [ROOT / "agent/app/static/index.html", *Path(__file__).parent.glob("*.py")]:
        sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    username = "memory-first-" + secrets.token_hex(8)
    password = "Mf1!" + secrets.token_urlsafe(24)
    configuration = {"agent_context_mode": "context_pack", "agent_orchestrator_mode": "unified",
        "agent_control_runtime": "react_v1", "agent_graph_v2_durable_enabled": True,
        "agent_graph_v2_enabled": False, "agent_graph_v2_shadow_enabled": False,
        "agent_request_deadline_seconds": 600.0, "multi_agent_v2_enabled": False,
        "context_history_v1_enabled": True, "agent_react_decision_timeout_seconds": 180.0,
        "agent_react_final_answer_timeout_seconds": 180.0, "context_compiler_shadow_enabled": False,
        "deepseek_model": "gpt-5.6-sol", "backend_base_url": backend,
        "memory_bff_enabled": True, "memory_projection_client_enabled": True,
        "memory_durable_snapshot_enabled": True, "memory_natural_candidates_enabled": arm == "M2",
        "memory_bff_canary_usernames": username, "memory_bff_epoch": name,
        "memory_candidate_stream_key": "memory:first:" + name,
        "memory_catalog_values_path": str(ROOT / "agent" / settings.memory_catalog_values_path),
        "used_phone_synthetic_price_policy": "budget_and_ranking",
        "used_phone_synthetic_price_dir": str(catalog.CATALOG_DIR)}
    write_new(output / "started.json", {"kind": "CROSS_SESSION_AGENT_FLOW",
        "arm": arm, "trajectory": trajectory, "sourceSha256": sources,
        "configuration": configuration, "maxCalls": 24, "timeoutSeconds": 1800,
        "confirmation": "SCRIPTED_REAL_BFF_HTTP", "catalog": "FROZEN_439_NOT_LIVE_SEARCH",
        "usageScope": "CODEX_TURN_INCLUDING_HOST_OVERHEAD", "globalDefaultChanged": False})
    native = SubscriptionClient(output / "model_calls", max_calls=24,
        timeout_seconds=180, application_input_budget=32000)
    if hashlib.sha256(catalog.CATALOG_PATH.read_bytes()).hexdigest() != catalog.EXPECTED_CATALOG_SHA256:
        raise ValueError("catalog drift")
    products = [catalog._product_from_catalog(json.loads(line)) for line in
                catalog.CATALOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    frozen = ContextCatalogTransport(tuple(products))
    original_extract = worker._extract_observed

    async def extraction(*args, **kwargs):
        before = len(native.calls)
        try:
            return await original_extract(*args, **kwargs, client=native, model="gpt-5.6-sol")
        finally:
            append(output / "purpose.jsonl", {"purpose": "memory_candidate",
                "nativeOrdinals": [row["ordinal"] for row in native.calls[before:]]})

    async def transport(name, arguments, **kwargs):
        result = await frozen(name, arguments, **kwargs)
        append(output / "tools.jsonl", {"tool": name, "arguments": arguments,
            "result": result.model_dump(mode="json", by_alias=True)})
        return result

    observations = []
    async with AsyncExitStack() as stack:
        store = await stack.enter_async_context(private_redis(output / "redis"))
        for key, value in configuration.items():
            stack.enter_context(patch.object(settings, key, value))
        for module in (task_state, reference_context, memory_bff):
            stack.enter_context(patch.object(module, "_client", store))
        stack.enter_context(patch.object(agent_trace, "_trace_store", agent_trace.TraceStore(store)))
        stack.enter_context(patch.object(model_call_observability, "_store",
            model_call_observability.ModelCallReceiptStore(client=store,
                spool_path=output / "model-receipts.jsonl")))
        stack.enter_context(patch("redis.asyncio.from_url", side_effect=RuntimeError("unbound_redis_forbidden")))
        stack.enter_context(patch.object(llm, "get_client", side_effect=RuntimeError("api_fallback_forbidden")))
        stack.enter_context(patch.object(worker, "_extract_observed", extraction))
        worker._catalog.cache_clear()
        async with httpx.AsyncClient(base_url=backend, timeout=15) as java:
            registration = await java.post("/api/auth/register", json={"username": username, "password": password})
            if registration.status_code != 201:
                raise RuntimeError("fresh_registration_failed:" + str(registration.status_code))
        for episode, query in enumerate(trajectory["turns"], 1):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as browser:
                login = await browser.post("/api/web-memory/login", json={"username": username, "password": password},
                    headers={"Origin": ORIGIN})
                if login.status_code != 200:
                    raise RuntimeError("bff_login_failed:" + str(login.status_code))
                csrf = login.json()["csrfToken"]
                cookie = browser.cookies.get(memory_bff.COOKIE_NAME)
                headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
                interaction = []
                if episode == 3 and trajectory.get("revokeBeforeThird"):
                    entries = (await browser.get("/api/web-memory/entries")).json()["entries"]
                    for entry in entries:
                        result = await browser.delete("/api/web-memory/entries/" + entry["memoryHandle"], headers=headers)
                        if result.status_code != 200:
                            raise RuntimeError("scripted_revocation_failed")
                    interaction.append({"action": "revoke", "count": len(entries)})
                scope = trajectory.get("scopes", ["self"] * 3)[episode-1]
                session = name + "-s" + str(episode)
                state = await task_state.create_task_state(task_state.TaskStateCreateRequest(
                    taskType="ecommerce_guide", sessionId=session, goal="跨会话购物记忆首版验证",
                    domainState={"shoppingGuide": {"category": "phone", "mode": "recommend", "requirements": [],
                        "useCases": [], "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing"}}))
                async def capture(current, phase):
                    nonlocal state
                    state = current
                calls_before = len(native.calls)
                started = time.perf_counter()
                resolution = await memory_bff.resolve_memory_run_for_browser_session(
                    cookie if arm != "M0" else None, category_id="phone", recipient_scope=scope,
                    catalog_revision=settings.memory_active_catalog_revision, task_id=state.task_id)
                expected_run = name + "-run" + str(episode)
                cap = issue_evaluation_context_arm(arm="RAW_FULL_CONTROL", run_id=expected_run,
                    task_id=state.task_id, session_id=session, model="gpt-5.6-sol", model_client=native,
                    tool_transport=transport, provider_max_retries=0)
                answer, traces, messages, run_id, summary = await llm.run_agent(query, history=[],
                    task_state=state, session_id=session, domain_hint="ecommerce", on_task_state=capture,
                    evaluation_context_arm=cap, memory_run_binding=resolution.binding)
                foreground_ms = (time.perf_counter()-started)*1000
                answer_calls = len(native.calls)
                candidate_started = time.perf_counter()
                if arm != "M0":
                    job = await worker.enqueue_memory_extraction(browser_session_id=cookie,
                        user_message=query, category_id="phone", recipient_scope=scope, message_id=expected_run)
                    if job:
                        stream_rows = await store.xrange(settings.memory_candidate_stream_key)
                        fields = next(value for _, value in stream_rows if value["jobId"] == job)
                        await worker._process(fields)
                candidate_ms = (time.perf_counter()-candidate_started)*1000
                cards = (await browser.get("/api/web-memory/candidates")).json()["candidates"]
                pending = [card for card in cards if card["status"] == "pending"]
                if episode == 1:
                    for card in pending:
                        action = trajectory["decision"]
                        confirm_started = time.perf_counter()
                        result = await browser.post("/api/web-memory/candidates/" + card["candidateId"] + "/decision",
                            json={"action": action}, headers=headers)
                        if result.status_code != 200:
                            raise RuntimeError("scripted_candidate_decision_failed")
                        decision_ms = (time.perf_counter()-confirm_started)*1000
                        available_ms = None
                        if action == "confirm":
                            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                         base_url=ORIGIN) as fresh_browser:
                                fresh_login = await fresh_browser.post("/api/web-memory/login",
                                    json={"username": username, "password": password}, headers={"Origin": ORIGIN})
                                if fresh_login.status_code != 200:
                                    raise RuntimeError("confirmation_availability_login_failed")
                                visible = await fresh_browser.get("/api/web-memory/entries")
                                if visible.status_code != 200 or not visible.json().get("entries"):
                                    raise RuntimeError("confirmed_memory_unavailable_in_fresh_session")
                                available_ms = (time.perf_counter()-confirm_started)*1000
                        interaction.append({"action": action, "response": result.json(),
                            "displayText": card["displayText"], "evidenceQuote": card.get("evidenceQuote"),
                            "decisionMs": decision_ms, "confirmToNewSessionAvailableMs": available_ms})
                guide = llm.build_validated_guide_result(state)
                row = {"episode": episode, "query": query, "recipientScope": scope, "answer": answer,
                    "runId": run_id, "expectedRunId": expected_run,
                    "foregroundMs": foreground_ms, "candidateReadyAfterAnswerMs": candidate_ms,
                    "memoryLoad": resolution.summary, "retainedMemory": resolution.binding.payload_for_phase("planner"),
                    "candidateCount": len(pending), "interactions": interaction,
                    "traceSummary": summary.model_dump(by_alias=True, mode="json") if summary else None,
                    "state": state.model_dump(by_alias=True, mode="json"), "publishedGuideResult": guide,
                    "toolTraces": [trace.model_dump(by_alias=True, mode="json") for trace in traces],
                    "agentLedger": cap.ledger.snapshot(), "agentNativeOrdinals": list(range(calls_before+1, answer_calls+1)),
                    "candidateNativeOrdinals": list(range(answer_calls+1, len(native.calls)+1))}
                write_new(output / f"episode-{episode}.json", row)
                observations.append(row)
                print(json.dumps({"episode": episode, "arm": arm, "foregroundMs": foreground_ms,
                    "memoryRetained": resolution.summary["retainedCount"], "candidates": len(pending),
                    "modelCalls": len(native.calls), "status": row["traceSummary"].get("agentStatus") if summary else None}), flush=True)
                if run_id != expected_run or summary is None:
                    break
        changed = [path for path, expected in sources.items()
                   if hashlib.sha256((ROOT/path).read_bytes()).hexdigest() != expected]
        write_new(output / "report.json", {"arm": arm, "trajectoryId": trajectory_id,
            "episodes": len(observations), "plannedEpisodes": 3, "nativeCalls": len(native.calls),
            "sourceChanged": changed, "complete": len(observations) == 3 and not changed
                and all(row["runId"] == row["expectedRunId"] and row["traceSummary"]
                        and row["traceSummary"].get("agentStatus") == "ok"
                        and row["agentNativeOrdinals"] for row in observations),
            "quality": "PENDING_INDEPENDENT_REVIEW", "calls": native.calls})


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(*sys.argv[1:]), timeout=1800))
    except BaseException as exc:
        destination = OUT / sys.argv[1]
        if destination.exists():
            write_new(destination / "failure.json", {"type": type(exc).__name__, "message": str(exc)[:500]})
        raise
