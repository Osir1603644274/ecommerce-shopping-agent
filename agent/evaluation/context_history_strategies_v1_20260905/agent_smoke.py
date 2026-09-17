"""Real business-flow subscription integration smoke; not a comparative study."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import traceback
from unittest.mock import patch
import uuid

from .artifacts import append, canonical, file_sha, now, write_new, capture_sources, verify_sources
from .subscription import SubscriptionClient
from .private_redis import private_redis
from .catalog_transport import ContextCatalogTransport


def _acceptance_record(row):
    """Only retain fields used by final acceptance; full row is on disk."""
    return {"turn": row["turn"], "runId": row["runId"], "expectedRunId": row["expectedRunId"],
        "traceSummary": deepcopy(row["traceSummary"]), "modelCalls": len(row["modelCalls"])}


async def run(output: Path, runtime="react_v1", arm=None, script=None, turn_limit=None, input_budget=128000,
              trigger_fraction=0.7, target_fraction=0.45, working_budget=None,
              fill_history_budget=False, adaptive_summary_items=False, attempt_timeout=1800):
    from agent.app import agent_trace, llm, task_state, model_call_observability, reference_context
    from agent.app.schemas import ReferenceContextHint
    from agent.app.evaluation_context_arm import issue_evaluation_context_arm
    from agent.app.settings import settings
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2 import lane_runtime as catalog
    from agent.app.context_history import HistoryArchive
    from agent.app.context_input import experimental_context_input
    from .context_client import ContextClient
    from .history_strategies import HistoryPolicy, HistoryStrategies

    output.mkdir(parents=True, exist_ok=False)
    append(output / "runner_events.jsonl", {"event": "RUNNER_STARTED", "at": now(),
        "pid": os.getpid(), "parentPid": os.getppid(), "output": str(output.resolve())})
    source_hashes = capture_sources(output / "source_snapshot")
    configuration = {"agent_context_mode": "context_pack", "agent_orchestrator_mode": "unified",
        "agent_control_runtime": runtime, "agent_graph_v2_durable_enabled": runtime == "react_v1",
        "agent_graph_v2_enabled": False, "agent_graph_v2_shadow_enabled": False,
        "agent_request_deadline_seconds": 900.0, "multi_agent_v2_enabled": False,
        "context_history_v1_enabled": True,
        "agent_react_decision_timeout_seconds": 300.0,
        "agent_react_final_answer_timeout_seconds": 300.0,
        "context_compiler_shadow_enabled": False, "deepseek_model": "gpt-5.6-sol"}
    configuration.update({"used_phone_synthetic_price_policy": "budget_and_ranking",
                          "used_phone_synthetic_price_dir": str(catalog.CATALOG_DIR)})
    if arm == "A_FULL_HISTORY" and working_budget is not None:
        raise ValueError("full_history_control_has_no_working_compaction_budget")
    if fill_history_budget and arm != "B_PACK_VIEW" or adaptive_summary_items and arm != "C_LLM_THRESHOLD":
        raise ValueError("strategy_option_wrong_arm")
    policy = HistoryPolicy(input_budget=input_budget, trigger_fraction=trigger_fraction,
                           target_fraction=target_fraction, working_budget=working_budget,
                           fill_history_budget=fill_history_budget, adaptive_summary_items=adaptive_summary_items)
    write_new(output / "started.json", {"at": now(), "kind": "STRATEGY_INTEGRATION_ONLY" if arm else "INTEGRATION_ONLY_NOT_A_B_C",
        "arm": arm,
        "contextBoundaryVersion": "source-index-v7",
        "internalPackBudgetTokens": input_budget if arm else None,
        "internalSharedViewBudgetTokens": input_budget if arm else None,
        "harnessRetention": "compact-acceptance-metadata-v1",
        "strategyVariant": "SOURCE_INDEX_V3" if fill_history_budget or adaptive_summary_items else "EXPLICIT_WORKING_WINDOW_V2" if working_budget is not None else "HARD_WINDOW_V1",
        "historyPolicy": vars(policy), "attemptHardTimeoutSeconds": attempt_timeout,
        "script": str(script) if script else None, "scriptSha256": file_sha(script) if script else None,
        "configuration": configuration, "runnerSha256": file_sha(__file__),
        "stateStore": "OWNED_LOOPBACK_REDIS_AOF", "durableRecoveryTested": False})
    client = SubscriptionClient(output / "model_calls", max_calls=864, application_input_budget=input_budget)
    if file_sha(catalog.CATALOG_PATH) != catalog.EXPECTED_CATALOG_SHA256:
        raise ValueError("catalog_drift")
    rows = [json.loads(line) for line in catalog.CATALOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    frozen = ContextCatalogTransport(tuple(catalog._product_from_catalog(row) for row in rows))
    observed_tool_count = 0

    async def transport(name, arguments, **kwargs):
        nonlocal observed_tool_count
        result = await frozen(name, arguments, **kwargs)
        receipt = {"name": name, "arguments": arguments, "result": result.model_dump(mode="json", by_alias=True)}
        append(output / "tools.jsonl", receipt)
        observed_tool_count += 1
        return result

    async with AsyncExitStack() as stack:
        store = await stack.enter_async_context(private_redis(output / "redis"))
        for key, value in configuration.items():
            if not hasattr(settings, key):
                raise ValueError("unknown_setting:" + key)
            stack.enter_context(patch.object(settings, key, value))
        stack.enter_context(patch.object(task_state, "_client", store))
        stack.enter_context(patch.object(reference_context, "_client", store))
        stack.enter_context(patch.object(agent_trace, "_trace_store", agent_trace.TraceStore(store)))
        stack.enter_context(patch.object(model_call_observability, "_store",
                                         model_call_observability.ModelCallReceiptStore(client=store,
                                             spool_path=output / "model_receipt_spool.jsonl")))
        stack.enter_context(patch("redis.asyncio.from_url", side_effect=RuntimeError("unbound_live_redis_forbidden")))
        stack.enter_context(patch.object(llm, "get_client", side_effect=RuntimeError("deepseek_fallback_forbidden")))
        session = "strategy-smoke-" + uuid.uuid4().hex[:12]
        state = await task_state.create_task_state(task_state.TaskStateCreateRequest(
            taskType="ecommerce_guide", sessionId=session, goal="二手手机多轮导购接入验真",
            domainState={"shoppingGuide": {"category": "phone", "mode": "recommend", "requirements": [],
                                          "useCases": [], "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing"}}))
        archive = HistoryArchive(output / "archive", session_id=session, task_id=state.task_id, create=True)
        history_strategy = HistoryStrategies(archive, policy)
        history = []
        reference_hint = None
        resume_hint = None
        results = []
        queries = ["给父亲买二手手机，预算1500元左右，倾向华为，平时微信视频和看新闻，不玩游戏。请结合取舍给建议，不要把品牌偏好当硬条件。",
                   "预算改到1800元以内，其他条件不变。请重新给出购买建议，并解释比刚才的方案改善了什么、哪些仍然未知。",
                   "第一款和第二款哪个好？"]
        if script:
            queries = [item["userText"] for item in json.loads(Path(script).read_text(encoding="utf-8"))["turns"]]
        if turn_limit:
            queries = queries[:turn_limit]
        if not 1 <= len(queries) <= 72:
            raise ValueError("development_turn_count_ceiling")
        for ordinal, query in enumerate(queries, 1):
            verify_sources(source_hashes)
            client.begin_turn(max_calls=12)
            model_client = ContextClient(client, arm=arm, history=history_strategy, task_id=state.task_id,
                session_id=session, query=query, output=output / "context_inputs.jsonl") if arm else client
            expected_run_id = resume_hint["runId"] if resume_hint else f"{session}-t{ordinal}"
            append(output / "runner_events.jsonl", {"event": "TURN_STARTED", "at": now(),
                "turn": ordinal, "expectedRunId": expected_run_id, "taskId": state.task_id,
                "sessionId": session, "nativeCallsBefore": len(client.calls)})
            resume_request = {**resume_hint, "answer": query} if resume_hint else None
            cap = issue_evaluation_context_arm(arm="RAW_FULL_CONTROL", run_id=expected_run_id,
                task_id=state.task_id, session_id=session, model="gpt-5.6-sol", model_client=model_client,
                tool_transport=transport, provider_max_retries=0)
            before = state

            async def capture(current, phase):
                nonlocal state
                state = current
                append(output / "state_transitions.jsonl", {"turn": ordinal, "phase": phase,
                    "state": current.model_dump(mode="json", by_alias=True)})

            calls_before = len(client.calls)
            started = time.perf_counter()
            resolved_reference = await reference_context.resolve_reference_context(session_id=session,
                state=state, hint=ReferenceContextHint(handle=reference_hint["handle"],
                    presentationMode=reference_hint["presentationMode"])) if reference_hint else None
            with experimental_context_input(pack_budget_tokens=input_budget) if arm else nullcontext():
                answer, traces, messages, run_id, summary = await llm.run_agent(query, history=history,
                    task_state=state, session_id=session, domain_hint="ecommerce", on_task_state=capture,
                    evaluation_context_arm=cap, reference_context=resolved_reference, resume=resume_request)
            latest = await task_state.get_task_state(state.task_id)
            if latest is not None:
                state = latest
            guide_result = llm.build_validated_guide_result(state)
            reference_hint = await reference_context.publish_reference_context(session_id=session,
                state=state, guide_result=guide_result) if guide_result else None
            # Functional context persistence belongs inside complete Agent wait,
            # unlike the experiment's post-turn JSON snapshot serialization.
            archive.append("user", query, turn=ordinal)
            product_rows = guide_result.get("products", []) if guide_result else []
            ids = reference_context._guide_product_ids(product_rows) if product_rows else []
            archive.append("assistant", answer, turn=ordinal,
                scope_id=reference_hint["scopeId"] if reference_hint else None,
                display={"batchId": f"{cap.identity.run_id}:publication:{ordinal}", "productIds": ids} if ids else None)
            elapsed = (time.perf_counter() - started) * 1000
            row = {"turn": ordinal, "query": query, "answer": answer, "runId": run_id,
                "expectedRunId": cap.identity.run_id, "durationMs": elapsed,
                "preState": before.model_dump(mode="json", by_alias=True),
                "postState": state.model_dump(mode="json", by_alias=True),
                "traceSummary": summary.model_dump(mode="json", by_alias=True) if summary else None,
                "modelCalls": client.calls[calls_before:], "toolTraces": [trace.model_dump(mode="json", by_alias=True) for trace in traces],
                "ledger": cap.ledger.snapshot(), "agentTurnMessages": messages}
            row["publishedGuideResult"] = guide_result
            row["contextPolicyEvidence"] = deepcopy(model_client.receipts) if arm else []
            row["referenceReceipt"] = reference_hint
            row["resumeRequest"] = resume_request
            raw_trace = await agent_trace._trace_store.get(run_id)
            row["agentRunTrace"] = raw_trace.model_dump(mode="json", by_alias=True) if raw_trace else None
            resume_hint = summary.durable_resume if summary else None
            if resume_hint is not None and hasattr(resume_hint, "model_dump"):
                resume_hint = resume_hint.model_dump(mode="json", by_alias=True)
            write_new(output / f"turn-{ordinal:02d}.json", row)
            append(output / "runner_events.jsonl", {"event": "TURN_COMMITTED", "at": now(),
                "turn": ordinal, "runId": run_id, "expectedRunId": cap.identity.run_id,
                "nativeCallsAfter": len(client.calls), "turnSha256": file_sha(output / f"turn-{ordinal:02d}.json")})
            results.append(_acceptance_record(row))
            history.extend([{"role": "user", "content": query}, {"role": "assistant", "content": answer}])
            # Full transport traces are archived separately in tools.jsonl.
            # They were not raw dialogue shown to the model or user, and must
            # not be multiplied into A as fake conversational history.
            print(canonical({"turn": ordinal, "modelCalls": len(client.calls)-calls_before, "toolCalls": len(traces),
                             "durationMs": elapsed, "status": summary.agent_status if summary else "NO_TRACE"}), flush=True)
            # A business safe stop terminates this reply, not all future user
            # turns in a predeclared conversation. Keep its failure and cost;
            # only missing trace/identity breaks make further attribution unsafe.
            if summary is None or run_id != cap.identity.run_id:
                break
        accepted = len(results) == len(queries) and all(row["traceSummary"] and row["runId"] == row["expectedRunId"] for row in results)
        accepted = accepted and any(row["modelCalls"] for row in results) and bool(observed_tool_count)
        accepted = accepted and all(row["traceSummary"].get("agentStatus") == "ok"
            and row["traceSummary"].get("finalAction") not in {"stop_turn", "context_only_answer_failed", "resume_rejected"}
            and not any(phase.get("outcome") == "failed" for phase in row["traceSummary"].get("phases", []))
            for row in results)
        verify_sources(source_hashes)
        write_new(output / "result.json", {"status": "INTEGRATION_PASS" if accepted else "INTEGRATION_HOLD",
            "arm": arm, "historyPolicy": vars(history_strategy.policy), "historyReceipts": history_strategy.receipts,
            "turns": len(results), "plannedTurns": len(queries), "dataCollectionComplete": len(results) == len(queries),
            "unsafeTerminalTurns": [row["turn"] for row in results if not row["traceSummary"]
                or row["traceSummary"].get("agentStatus") != "ok"
                or row["traceSummary"].get("finalAction") in {"stop_turn", "resume_rejected", "context_only_answer_failed"}],
            "modelCalls": len(client.calls), "toolCalls": observed_tool_count,
            "comparativeReady": False, "A_FULL_HISTORY_Verified": accepted and arm == "A_FULL_HISTORY",
            "notes": "Actual run_agent/state/planner/validator/final flow on owned Redis with explicit strategy boundary. Development integration only; crash recovery has not yet been tested."})
        return accepted


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--runtime", choices=["fixed_v1", "react_v1"], default="react_v1")
    parser.add_argument("--arm", choices=["A_FULL_HISTORY", "B_PACK_VIEW", "C_LLM_THRESHOLD"])
    parser.add_argument("--script", type=Path)
    parser.add_argument("--turn-limit", type=int)
    parser.add_argument("--input-budget", type=int, default=128000)
    parser.add_argument("--trigger-fraction", type=float, default=0.7)
    parser.add_argument("--target-fraction", type=float, default=0.45)
    parser.add_argument("--working-budget", type=int)
    parser.add_argument("--fill-history-budget", action="store_true")
    parser.add_argument("--adaptive-summary-items", action="store_true")
    parser.add_argument("--attempt-timeout", type=int, default=1800)
    parser.add_argument("--supervised-start-token")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("existing_attempt_is_immutable:" + str(args.output))
    if not args.output.is_absolute() or args.script is not None and not args.script.is_absolute():
        raise ValueError("absolute_attempt_and_script_paths_required")
    if args.supervised_start_token:
        import sys
        if sys.stdin.readline().strip() != args.supervised_start_token:
            raise RuntimeError("supervisor_start_gate_not_released")
    if not 60 <= args.attempt_timeout <= 7200:
        raise ValueError("attempt_timeout_outside_development_safety_ceiling")
    try:
        success = asyncio.run(asyncio.wait_for(run(args.output, args.runtime, args.arm, args.script,
            args.turn_limit, args.input_budget, args.trigger_fraction, args.target_fraction, args.working_budget,
            args.fill_history_budget, args.adaptive_summary_items, args.attempt_timeout), timeout=args.attempt_timeout))
    except BaseException as exc:
        if args.output.exists():
            write_new(args.output / "failure.json", {"at": now(), "type": type(exc).__name__, "error": str(exc),
                                                    "traceback": traceback.format_exc()})
            append(args.output / "runner_events.jsonl", {"event": "RUNNER_FAILED", "at": now(),
                "pid": os.getpid(), "type": type(exc).__name__, "error": str(exc)[:1000]})
        raise
    append(args.output / "runner_events.jsonl", {"event": "RUNNER_FINISHED", "at": now(),
        "pid": os.getpid(), "exitCode": 0 if success else 2})
    raise SystemExit(0 if success else 2)
