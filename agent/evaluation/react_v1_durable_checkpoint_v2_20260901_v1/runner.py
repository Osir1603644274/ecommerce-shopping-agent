"""Real-Redis, real-process runner for the frozen durability matrix.

The deterministic layer explicitly selects ``control_policy='react_v1'`` and
uses only the production deterministic ReAct branch.  Any model call is logged
and rejected.  Faults are process-local and enabled only by this runner.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import redis
import redis.asyncio as aioredis
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from openai import AsyncOpenAI

from app import task_state
from app.agent_trace import TraceBuilder
from app.control.react_actions import react_action_anchor_key, react_plan_contract_sha256
from app.domains.ecommerce.models import CATEGORY_LABELS
from app.graph.checkpoint import GraphV2CheckpointSaver
from app.graph.nodes.react_policy import set_react_policy_fault_hook
from app.graph.nodes import executor as executor_module
from app.graph.nodes.validator import set_validator_fault_hook
from app.graph.pause_control import public_pause_receipt, request_graph_pause
from app.graph.resume import (
    prepare_terminal_response_receipt,
    read_terminal_response_receipt,
    run_graph_v2_durable,
    session_owner_hash,
)
from app.graph.tool_inbox_v2 import ToolInbox
from app.schemas import ToolTrace
from app.settings import settings
from app.task_state import (
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    get_task_state,
    update_task_state,
)
from app.tool_execution_v2 import ToolInboxCallerV2
from app.tools import TOOL_SCHEMAS
from tests.two_stage_ranking_fixtures import two_stage_search_detail

IDENTITY = "react-v1-durable-checkpoint-tool-inbox-v2-20260901-v1"
SESSION = "session-react-v1-durable-v2"
MESSAGE = "三千元以内推荐二手手机，优先续航。"
FAULT_EXIT = {
    "after_action_atomic_commit": 81,
    "after_in_flight_before_tool_effect": 82,
    "after_tool_effect_before_inbox_complete": 83,
    "after_inbox_success_before_taskstate_projection": 84,
    "after_validator_receipt": 85,
    "after_executor_receipt": 86,
}
SOURCE_FILES = (
    "app/graph/checkpoint.py",
    "app/graph/resume.py",
    "app/graph/tool_inbox_v2.py",
    "app/tool_execution_v2.py",
    "app/graph/nodes/__init__.py",
    "app/graph/nodes/clarification.py",
    "app/graph/nodes/executor.py",
    "app/graph/nodes/react_policy.py",
    "app/graph/nodes/validator.py",
    "app/graph/pause_control.py",
    "app/graph/runtime.py",
    "app/settings.py",
    "tests/test_graph_v2_checkpoint.py",
    "tests/test_graph_v2_process_restart.py",
    "evaluation/react_v1_durable_checkpoint_v2_20260901_v1/runner.py",
    "evaluation/react_v1_durable_checkpoint_v2_20260901_v1/scorer.py",
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_bytes(_canonical(value) + b"\n")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _schemas(_state: Any) -> list[dict[str, Any]]:
    return [
        item for item in TOOL_SCHEMAS
        if item["function"]["name"] == "search_products"
    ]


class _NoModelCompletions:
    def __init__(self, client: Any, key: str) -> None:
        self.client = client
        self.key = key

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        await self.client.rpush(
            self.key,
            json.dumps({"pid": os.getpid(), "unexpected": True}),
        )
        raise AssertionError("Layer A forbids model calls")


def _no_model_client(client: Any, key: str) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(completions=_NoModelCompletions(client, key))
    )


async def _bootstrap(
    port: int, *, clarification: bool = False,
    reason: str = "broad_catalog_discovery", category: str = "phone",
    message: str = MESSAGE,
) -> dict[str, str]:
    client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    task_state._client = client
    try:
        request = TaskStateCreateRequest(
            goal=message,
            task_type="ecommerce_guide",
            session_id=SESSION,
            status="collecting_information" if clarification else "ready",
            pending_questions=["请补充预算上限。"] if clarification else [],
            domain_state={
                "shoppingGuide": {
                    "mode": "recommend",
                    "category": category,
                    "useCases": ["续航"],
                    "requirements": [] if clarification else [{
                        "key": "price_minor",
                        "operator": "lte",
                        "value": 300000,
                        "unit": "CNY_MINOR",
                        "priority": "hard",
                        "source": "user",
                    }],
                    "candidateIds": [],
                    "comparedIds": [],
                    "evidenceStatus": "missing",
                },
                "taskStateExtraction": {"reason": reason},
            },
        )
        state = await create_task_state(request)
        if not clarification:
            state = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision,
                    actor="agent",
                    status="ready",
                ),
            )
        return {"taskId": state.task_id, "sessionId": SESSION}
    finally:
        await client.aclose()
        task_state._client = None


async def _external_drift(port: int, task_id: str, label: str) -> int:
    client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    task_state._client = client
    try:
        state = await get_task_state(task_id)
        assert state is not None
        updated = await update_task_state(
            task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="system",
                domainStatePatch={"durabilityEvaluationDrift": label},
            ),
        )
        return updated.revision
    finally:
        await client.aclose()
        task_state._client = None


async def _worker(args: argparse.Namespace) -> dict[str, Any]:
    client = aioredis.Redis(
        host="127.0.0.1", port=args.port, decode_responses=True
    )
    task_state._client = client
    ledger_key = f"eval:react-v1-durable:ledger:{args.task_id}"
    model_key = f"eval:react-v1-durable:model:{args.task_id}"
    original_execute = ToolInboxCallerV2.execute
    original_reconcile = executor_module._reconcile_revision
    original_contract = executor_module._react_execution_contract_is_authentic
    original_run_executor_step = executor_module.run_executor_step
    old_fault = settings.agent_graph_v2_fault_point
    model_events: list[dict[str, Any]] = []
    try:
        async def observed_reconcile(state: Any, deps: Any, live: Any) -> str:
            outcome = await original_reconcile(state, deps, live)
            await client.rpush(
                f"eval:react-v1-durable:debug:{args.task_id}",
                json.dumps({"check": "reconcile", "outcome": outcome}),
            )
            return outcome

        async def observed_contract(current: Any, deps: Any, action_id: str) -> bool:
            outcome = await original_contract(current, deps, action_id)
            await client.rpush(
                f"eval:react-v1-durable:debug:{args.task_id}",
                json.dumps({"check": "execution_contract", "outcome": outcome}),
            )
            return outcome

        async def observed_run_executor_step(*a: Any, **kw: Any) -> Any:
            try:
                return await original_run_executor_step(*a, **kw)
            except BaseException as exc:
                await client.rpush(
                    f"eval:react-v1-durable:debug:{args.task_id}",
                    json.dumps({
                        "check": "run_executor_step_exception",
                        "type": type(exc).__name__, "message": str(exc)[:500],
                    }),
                )
                raise

        executor_module._reconcile_revision = observed_reconcile
        executor_module._react_execution_contract_is_authentic = observed_contract
        executor_module.run_executor_step = observed_run_executor_step
        async def legacy(_name: str, _arguments: dict[str, Any]) -> ToolTrace:
            raise AssertionError("legacy caller forbidden")

        async def caller_v2(name: str, arguments: dict[str, Any], context: Any) -> ToolTrace:
            if args.fault == "after_in_flight_before_tool_effect":
                os._exit(FAULT_EXIT[args.fault])
            entry = {
                "pid": os.getpid(),
                "tool": name,
                "arguments": arguments,
                "taskId": context.task_id,
                "runId": context.run_id,
                "threadId": context.thread_id,
                "executionId": context.execution_id,
                "fence": context.fence,
            }
            await client.rpush(ledger_key, _canonical(entry).decode("ascii"))
            if args.fault == "after_tool_effect_before_inbox_complete":
                os._exit(FAULT_EXIT[args.fault])
            if args.request_pause:
                await request_graph_pause(
                    task_id=context.task_id,
                    session_id=args.session_id,
                    run_id=context.run_id,
                    thread_id=context.thread_id,
                    control_policy="react_v1",
                )
            return ToolTrace(
                tool=name,
                ok=True,
                durationMs=1.0,
                detail=two_stage_search_detail([101, 102]),
            )

        if args.fault == "after_action_atomic_commit":
            set_react_policy_fault_hook(
                lambda point: os._exit(FAULT_EXIT[args.fault])
                if point == "after_action_atomic_commit" else None
            )
        if args.fault == "after_validator_receipt":
            set_validator_fault_hook(lambda: os._exit(FAULT_EXIT[args.fault]))
        if args.fault == "after_inbox_success_before_taskstate_projection":
            async def faulting_execute(self: Any, *a: Any, **kw: Any) -> Any:
                result = await original_execute(self, *a, **kw)
                os._exit(FAULT_EXIT[args.fault])
            ToolInboxCallerV2.execute = faulting_execute
        settings.agent_graph_v2_fault_point = (
            "after_executor_receipt"
            if args.fault == "after_executor_receipt" else ""
        )

        resume = None
        pause_resume = None
        if args.payload_b64:
            payload = json.loads(base64.b64decode(args.payload_b64).decode("utf-8"))
            if args.mode == "resume":
                resume = payload
            else:
                pause_resume = payload
        trace = TraceBuilder(
            f"eval-worker-{os.getpid()}", mode="context_pack"
        )

        def on_model_call(
            phase: str, duration_ms: float, *, failed: bool,
        ) -> None:
            model_events.append({
                "phase": phase, "durationMs": round(float(duration_ms), 3),
                "failed": bool(failed),
            })

        def on_model_call_receipt(
            phase: str, duration_ms: float, *, failed: bool,
            response: Any, model_call_id: str, context_binding_hash: str,
        ) -> None:
            usage = getattr(response, "usage", None) if response is not None else None
            event = next(
                (item for item in reversed(model_events) if item["phase"] == phase),
                None,
            )
            if event is None:
                event = {
                    "phase": phase, "durationMs": round(float(duration_ms), 3),
                    "failed": bool(failed),
                }
                model_events.append(event)
            event.update({
                "modelCallId": model_call_id,
                "contextBindingHash": context_binding_hash,
                "promptTokens": getattr(usage, "prompt_tokens", None),
                "completionTokens": getattr(usage, "completion_tokens", None),
                "totalTokens": getattr(usage, "total_tokens", None),
            })

        model_client = (
            AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
            )
            if args.real_model else _no_model_client(client, model_key)
        )
        graph_started = time.perf_counter()
        worker_message = (
            base64.b64decode(args.user_message_b64).decode("utf-8")
            if args.user_message_b64 else MESSAGE
        )
        result = await run_graph_v2_durable(
            task_id=args.task_id,
            session_id=args.session_id,
            resume=resume,
            restart=args.mode == "restart",
            pause_resume=pause_resume,
            run_id=args.run_id if args.mode == "fresh" else None,
            user_message=worker_message,
            client=model_client,
            model=settings.deepseek_model if args.real_model else "deterministic-react-v1",
            resolve_tool_schemas=_schemas,
            tool_caller=legacy,
            tool_caller_v2=caller_v2,
            tool_inbox=ToolInbox(client, lease_ms=300, ttl_ms=60_000),
            trace_builder=trace,
            max_transitions=12,
            checkpointer=GraphV2CheckpointSaver(serde=JsonPlusSerializer()),
            control_policy="react_v1",
            react_decision_timeout_seconds=30.0 if args.real_model else 15.0,
            on_model_call=on_model_call,
            on_model_call_receipt=on_model_call_receipt,
        )
        graph_elapsed_ms = round((time.perf_counter() - graph_started) * 1000, 3)
        finished = trace.finish()
        live = await get_task_state(args.task_id)
        starts = [
            event.get("nodeName")
            for event in result.graph_state.get("node_events", [])
            if event.get("phase") == "start"
        ]
        end_events = [
            {
                "node": event.get("nodeName"),
                "route": event.get("routeDecision"),
                "error": event.get("errorCode"),
            }
            for event in result.graph_state.get("node_events", [])
            if event.get("phase") == "end"
        ]
        return {
            "pid": os.getpid(),
            "boundary": result.boundary,
            "mode": result.mode,
            "runId": result.run_id,
            "threadId": result.thread_id,
            "revision": result.revision,
            "checkpointHash": result.checkpoint_hash,
            "checkpointCount": result.checkpoint_count,
            "rejectedReason": result.rejected_reason,
            "proposalHash": result.proposal_hash,
            "interruptPayload": result.interrupt_payload,
            "pauseReceipt": public_pause_receipt(result.pause_receipt),
            "nodeStarts": starts,
            "nodeEnds": end_events,
            "terminalOutcome": result.terminal_outcome,
            "degradedReason": result.degraded_reason,
            "reactDecisionSources": [
                (
                    item.get("decisionSource")
                    if isinstance(item, dict)
                    else item.decision_source
                )
                for item in finished.react_decisions
            ],
            "reactOutcomeCount": len(finished.react_outcomes),
            "stateRevision": live.revision if live is not None else None,
            "graphElapsedMs": graph_elapsed_ms,
            "modelEvents": model_events,
        }
    finally:
        settings.agent_graph_v2_fault_point = old_fault
        set_react_policy_fault_hook(None)
        set_validator_fault_hook(None)
        ToolInboxCallerV2.execute = original_execute
        executor_module._reconcile_revision = original_reconcile
        executor_module._react_execution_contract_is_authentic = original_contract
        executor_module.run_executor_step = original_run_executor_step
        await client.aclose()
        task_state._client = None


def _payload_b64(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def _worker_command(
    *, port: int, identity: dict[str, str], run_id: str, mode: str,
    fault: str = "none", payload: dict[str, Any] | None = None,
    request_pause: bool = False,
    real_model: bool = False,
    user_message: str | None = None,
) -> list[str]:
    command = [
        sys.executable, "-u", "-m",
        "evaluation.react_v1_durable_checkpoint_v2_20260901_v1.runner",
        "--worker", "--port", str(port), "--task-id", identity["taskId"],
        "--session-id", identity["sessionId"], "--run-id", run_id,
        "--mode", mode, "--fault", fault,
    ]
    encoded = _payload_b64(payload)
    if encoded:
        command.extend(["--payload-b64", encoded])
    if request_pause:
        command.append("--request-pause")
    if real_model:
        command.append("--real-model")
    if user_message is not None:
        command.extend([
            "--user-message-b64",
            base64.b64encode(user_message.encode("utf-8")).decode("ascii"),
        ])
    return command


def _run_process(command: list[str], timeout: int = 45) -> dict[str, Any]:
    process = subprocess.Popen(
        command, cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    started = time.perf_counter()
    stdout, stderr = process.communicate(timeout=timeout)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    result = None
    if process.returncode == 0 and stdout.strip():
        result = json.loads(stdout.splitlines()[-1])
    return {
        "pid": process.pid,
        "returnCode": process.returncode,
        "elapsedMs": elapsed_ms,
        "result": result,
        "stdout": stdout,
        "stderr": stderr,
    }


def _redis_rows(sync: redis.Redis, key: str) -> list[dict[str, Any]]:
    return [json.loads(item) for item in sync.lrange(key, 0, -1)]


def _tamper_set_keep_ttl(sync: redis.Redis, key: str, value: str) -> None:
    """Rewrite an isolated-test key without manufacturing a TTL failure."""
    ttl_ms = sync.pttl(key)
    if ttl_ms > 0:
        sync.set(key, value, px=ttl_ms)
    else:
        sync.set(key, value)


def _state_summary(port: int, task_id: str) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        client = aioredis.Redis(
            host="127.0.0.1", port=port, decode_responses=True
        )
        task_state._client = client
        try:
            state = await get_task_state(task_id)
            if state is None:
                return {}
            domain = state.domain_state or {}
            action_receipt = domain.get("reactV1ActionReceipt") or {}
            action = action_receipt.get("action") or {}
            marker = domain.get("v2RunMarker") or {}
            anchor = None
            if action.get("actionId") and marker.get("runId") and marker.get("threadId"):
                raw_anchor = await client.get(react_action_anchor_key(
                    state.task_id, marker["runId"], marker["threadId"],
                    action["actionId"],
                ))
                try:
                    anchor = json.loads(raw_anchor) if raw_anchor else None
                except json.JSONDecodeError:
                    anchor = {"invalid": True}
            action_hash = hashlib.sha256(_canonical(action)).hexdigest() if action else None
            plan_hash = (
                react_plan_contract_sha256(state.active_plan)
                if state.active_plan is not None else None
            )
            contract_debug = {
                "receiptRun": action_receipt.get("runId") == marker.get("runId"),
                "receiptThread": action_receipt.get("threadId") == marker.get("threadId"),
                "receiptOwner": action_receipt.get("sessionOwnerHash") == marker.get("sessionOwnerHash"),
                "receiptPolicy": action_receipt.get("controlPolicy") == "react_v1",
                "receiptPolicyRevision": action_receipt.get("policyRevision") == "react-v1-2026-08-27",
                "planId": bool(state.active_plan) and state.active_plan.plan_id == f"react-{action.get('actionId')}"[:64],
                "oneStep": bool(state.active_plan) and len(state.active_plan.steps) == 1,
                "planHash": action_receipt.get("planSha256") == plan_hash,
                "actionHash": action_receipt.get("actionSha256") == action_hash,
                "revision": action.get("basedOnRevision", -2) + 1 == action_receipt.get("stateRevision"),
                "anchorExact": bool(anchor) and all(
                    anchor.get(key) == action_receipt.get(key)
                    for key in (
                        "runId", "threadId", "sessionOwnerHash", "controlPolicy",
                        "policyRevision", "action", "actionSha256", "planSha256",
                    )
                ),
            }
            return {
                "taskId": state.task_id,
                "sessionId": state.session_id,
                "revision": state.revision,
                "status": state.status,
                "planStatus": state.active_plan.status if state.active_plan else None,
                "planContractSha256": (
                    react_plan_contract_sha256(state.active_plan)
                    if state.active_plan is not None else None
                ),
                "stepExecutionResultCount": len(domain.get("stepExecutionResults") or []),
                "candidateScopeId": (domain.get("candidateScope") or {}).get("scopeId"),
                "v2ExecReceipt": domain.get("v2ExecReceipt"),
                "v2ExecReceiptProjection": domain.get("v2ExecReceiptProjection"),
                "reactV1ActionReceipt": domain.get("reactV1ActionReceipt"),
                "reactV1OutcomeReceipt": domain.get("reactV1OutcomeReceipt"),
                "v2PendingClarification": domain.get("v2PendingClarification"),
                "v2RunMarker": domain.get("v2RunMarker"),
                "reactActionAnchor": anchor,
                "reactContractDebug": contract_debug,
            }
        finally:
            await client.aclose()
            task_state._client = None
    return asyncio.run(read())


def _run_graph_bundle(
    sync: redis.Redis, port: int, *, family: str, index: int, fault: str,
    expected_fault_exit: int | None, post_restarts: int = 1,
) -> dict[str, Any]:
    identity = asyncio.run(_bootstrap(port))
    run_id = f"run-{family.lower()}-{index:03d}"
    first = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id,
        mode="fresh", fault=fault,
    ))
    restarts: list[dict[str, Any]] = []
    for _ in range(post_restarts if first["returnCode"] == 0 or expected_fault_exit else 0):
        restarts.append(_run_process(_worker_command(
            port=port, identity=identity, run_id=run_id,
            mode="restart",
        )))
    ledger_key = f"eval:react-v1-durable:ledger:{identity['taskId']}"
    model_key = f"eval:react-v1-durable:model:{identity['taskId']}"
    return {
        "family": family,
        "index": index,
        "identity": identity,
        "runId": run_id,
        "expectedFaultExit": expected_fault_exit,
        "first": first,
        "restarts": restarts,
        "ledger": _redis_rows(sync, ledger_key),
        "modelCalls": _redis_rows(sync, model_key),
        "debug": _redis_rows(
            sync, f"eval:react-v1-durable:debug:{identity['taskId']}"
        ),
        "finalState": _state_summary(port, identity["taskId"]),
    }


def _run_state_diverged(sync: redis.Redis, port: int, index: int) -> dict[str, Any]:
    identity = asyncio.run(_bootstrap(port))
    run_id = f"run-state-diverged-{index:03d}"
    first = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="fresh",
        fault="after_action_atomic_commit",
    ))
    drift_revision = asyncio.run(_external_drift(
        port, identity["taskId"], f"drift-{index}"
    ))
    restart = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="restart",
    ))
    return {
        "family": "state_diverged", "index": index, "identity": identity,
        "runId": run_id, "first": first, "driftRevision": drift_revision,
        "restarts": [restart],
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
        "modelCalls": _redis_rows(sync, f"eval:react-v1-durable:model:{identity['taskId']}"),
        "finalState": _state_summary(port, identity["taskId"]),
    }


def _run_clarification(sync: redis.Redis, port: int, index: int) -> dict[str, Any]:
    identity = asyncio.run(_bootstrap(port, clarification=True))
    run_id = f"run-clarification-{index:03d}"
    parked = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="fresh",
    ))
    interrupt = (parked.get("result") or {}).get("interruptPayload") or {}
    payload = {
        "answer": "预算上限 3000 元",
        "taskId": identity["taskId"],
        "runId": run_id,
        "threadId": (parked.get("result") or {}).get("threadId"),
        "revision": interrupt.get("revision"),
        "proposalHash": interrupt.get("proposalHash"),
    }
    resumed = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="resume", payload=payload,
    ))
    before_replay = _state_summary(port, identity["taskId"])
    replays = [_run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="resume", payload=payload,
    )) for _ in range(3)]
    return {
        "family": "clarification", "index": index, "identity": identity,
        "runId": run_id, "parked": parked, "resumed": resumed,
        "replays": replays, "beforeReplayState": before_replay,
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
        "modelCalls": _redis_rows(sync, f"eval:react-v1-durable:model:{identity['taskId']}"),
        "finalState": _state_summary(port, identity["taskId"]),
    }


def _run_pause(sync: redis.Redis, port: int, index: int) -> dict[str, Any]:
    identity = asyncio.run(_bootstrap(port))
    run_id = f"run-operator-pause-{index:03d}"
    paused = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="fresh",
        request_pause=True,
    ))
    receipt = (paused.get("result") or {}).get("pauseReceipt")
    resumed = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="restart",
        payload=receipt,
    ))
    return {
        "family": "operator_pause", "index": index, "identity": identity,
        "runId": run_id, "paused": paused, "resumed": resumed,
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
        "modelCalls": _redis_rows(sync, f"eval:react-v1-durable:model:{identity['taskId']}"),
        "finalState": _state_summary(port, identity["taskId"]),
    }


def _checkpoint_key(sync: redis.Redis, thread_id: str) -> str:
    latest_key = f"graph-v2:cp:{thread_id}::latest"
    checkpoint_id = sync.get(latest_key)
    if not checkpoint_id:
        raise RuntimeError("latest checkpoint missing")
    return f"graph-v2:cp:{thread_id}::cp:{checkpoint_id}"


def _rewrite_checkpoint_channel(
    sync: redis.Redis, thread_id: str, channel: str, value: Any,
) -> None:
    key = _checkpoint_key(sync, thread_id)
    envelope = json.loads(sync.get(key))
    payload = base64.b64decode(envelope["b"])
    serde = JsonPlusSerializer()
    loaded = serde.loads_typed((envelope["t"], payload))
    loaded["c"]["channel_values"][channel] = value
    type_name, rewritten = serde.dumps_typed(loaded)
    envelope.update({
        "t": type_name,
        "b": base64.b64encode(rewritten).decode("ascii"),
        "h": hashlib.sha256(rewritten).hexdigest()[:16],
        "n": len(rewritten),
    })
    _tamper_set_keep_ttl(
        sync, key, json.dumps(envelope, separators=(",", ":"))
    )


def _action_crash(sync: redis.Redis, port: int, label: str) -> tuple[dict[str, str], str, dict[str, Any]]:
    identity = asyncio.run(_bootstrap(port))
    run_id = f"run-tamper-{label}"
    first = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="fresh",
        fault="after_action_atomic_commit",
    ))
    if first.get("returnCode") != 81:
        raise RuntimeError(f"tamper seed {label} did not reach action fault")
    return identity, run_id, first


def _tamper_checkpoint_payload(sync: redis.Redis, port: int) -> dict[str, Any]:
    identity, run_id, first = _action_crash(sync, port, "checkpoint-payload")
    thread_id = f"v2-task:{identity['taskId']}:{run_id}"
    key = _checkpoint_key(sync, thread_id)
    envelope = json.loads(sync.get(key))
    blob = bytearray(base64.b64decode(envelope["b"]))
    blob[-1] ^= 1
    envelope["b"] = base64.b64encode(blob).decode("ascii")
    _tamper_set_keep_ttl(sync, key, json.dumps(envelope))
    restart = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="restart",
    ))
    return {
        "id": "T01", "target": "checkpoint_payload", "first": first,
        "restart": restart, "accepted": restart.get("returnCode") == 0
        and _result_boundary(restart) not in {"resume_rejected", "state_diverged"},
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
    }


def _result_boundary(process: dict[str, Any]) -> str | None:
    result = process.get("result")
    return result.get("boundary") if isinstance(result, dict) else None


def _tamper_checkpoint_identity(
    sync: redis.Redis, port: int, *, case_id: str, channel: str, value: Any,
) -> dict[str, Any]:
    identity, run_id, first = _action_crash(sync, port, case_id.lower())
    thread_id = f"v2-task:{identity['taskId']}:{run_id}"
    _rewrite_checkpoint_channel(sync, thread_id, channel, value)
    restart = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="restart",
    ))
    rejected = (
        restart.get("returnCode") != 0
        or _result_boundary(restart) in {"resume_rejected", "state_diverged"}
    )
    return {
        "id": case_id, "target": f"checkpoint_{channel}", "first": first,
        "restart": restart, "accepted": not rejected,
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
    }


def _tamper_pending_write(sync: redis.Redis, port: int) -> dict[str, Any]:
    identity = asyncio.run(_bootstrap(port, clarification=True))
    run_id = "run-tamper-pending-write"
    parked = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="fresh",
    ))
    result = parked.get("result") or {}
    thread_id = result.get("threadId")
    interrupt = result.get("interruptPayload") or {}
    tampered = 0
    for key in sync.scan_iter(match=f"graph-v2:cp:{thread_id}::writes:*"):
        fields = sync.hgetall(key)
        for field, encoded in fields.items():
            if field == "__ord":
                continue
            envelope = json.loads(encoded)
            blob = bytearray(base64.b64decode(envelope["b"]))
            blob[-1] ^= 1
            envelope["b"] = base64.b64encode(blob).decode("ascii")
            sync.hset(key, field, json.dumps(envelope))
            tampered += 1
    payload = {
        "answer": "预算 3000 元", "taskId": identity["taskId"],
        "runId": run_id, "threadId": thread_id,
        "revision": interrupt.get("revision"),
        "proposalHash": interrupt.get("proposalHash"),
    }
    resumed = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="resume", payload=payload,
    ))
    return {
        "id": "T02", "target": "checkpoint_pending_write",
        "parked": parked, "tamperedWrites": tampered, "restart": resumed,
        "accepted": resumed.get("returnCode") == 0
        and _result_boundary(resumed) not in {"resume_rejected", "state_diverged"},
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
    }


def _tamper_cursor(
    sync: redis.Redis, port: int, *, case_id: str, policy: bool,
) -> dict[str, Any]:
    identity, run_id, first = _action_crash(sync, port, case_id.lower())
    key = f"graph-v2:cursor:{identity['taskId']}"
    cursor = json.loads(sync.get(key))
    if policy:
        cursor.update({"controlPolicy": "fixed_v1", "policyRevision": "fixed-v1"})
    else:
        cursor.update({
            "runId": "run-foreign",
            "threadId": f"v2-task:{identity['taskId']}:run-foreign",
        })
    _tamper_set_keep_ttl(sync, key, json.dumps(cursor))
    restart = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="restart",
    ))
    return {
        "id": case_id, "target": "cursor_policy" if policy else "cursor_run_thread",
        "first": first, "restart": restart,
        "accepted": (
            restart.get("returnCode") == 0
            and _result_boundary(restart) not in {
                "resume_rejected", "state_diverged", "stop_turn"
            }
        ),
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
    }


def _tamper_marker(sync: redis.Redis, port: int) -> dict[str, Any]:
    identity, run_id, first = _action_crash(sync, port, "marker")
    async def mutate() -> int:
        client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
        task_state._client = client
        try:
            state = await get_task_state(identity["taskId"])
            marker = dict((state.domain_state or {})["v2RunMarker"])
            marker["runId"] = "run-foreign"
            updated = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision, actor="system",
                    domainStatePatch={"v2RunMarker": marker},
                ),
            )
            return updated.revision
        finally:
            await client.aclose()
            task_state._client = None
    revision = asyncio.run(mutate())
    restart = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="restart",
    ))
    return {
        "id": "T08", "target": "taskstate_run_marker", "first": first,
        "tamperedRevision": revision, "restart": restart,
        "accepted": (
            restart.get("returnCode") == 0
            and _result_boundary(restart) not in {
                "resume_rejected", "state_diverged", "stop_turn"
            }
        ),
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
    }


def _tamper_inbox_or_projection(
    sync: redis.Redis, port: int, *, case_id: str, projection: bool,
) -> dict[str, Any]:
    identity = asyncio.run(_bootstrap(port))
    run_id = f"run-tamper-{case_id.lower()}"
    first = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="fresh",
        fault="after_executor_receipt",
    ))
    if projection:
        async def mutate_projection() -> int:
            client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
            task_state._client = client
            try:
                state = await get_task_state(identity["taskId"])
                updated = await update_task_state(
                    state.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=state.revision, actor="system",
                        domainStatePatch={
                            "v2ExecReceiptProjection": {
                                "receiptHash": "0" * 64,
                                "projectionRevision": state.revision + 1,
                            }
                        },
                    ),
                )
                return updated.revision
            finally:
                await client.aclose()
                task_state._client = None
        mutate_detail: Any = asyncio.run(mutate_projection())
    else:
        state = _state_summary(port, identity["taskId"])
        receipt = state.get("v2ExecReceipt") or {}
        slot_key = receipt.get("logicalSlotKey")
        if not slot_key:
            raise RuntimeError("target ToolInbox logical slot is missing")
        record_key = f"agent-tool-inbox:v2:record:{slot_key}"
        record = json.loads(sync.get(record_key))
        record["resultHash"] = "0" * 64
        _tamper_set_keep_ttl(sync, record_key, json.dumps(record))
        mutate_detail = record_key
    restart = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="restart",
    ))
    return {
        "id": case_id,
        "target": "tool_receipt_projection" if projection else "tool_inbox_record",
        "first": first, "mutation": mutate_detail, "restart": restart,
        "accepted": (
            restart.get("returnCode") == 0
            and _result_boundary(restart) not in {
                "resume_rejected", "state_diverged", "stop_turn"
            }
        ),
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
    }


def _tamper_terminal_outbox(port: int) -> dict[str, Any]:
    async def case() -> dict[str, Any]:
        client = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
        task_state._client = client
        try:
            identity = await _bootstrap_on_client()
            state = await get_task_state(identity["taskId"])
            run_id = "run-tamper-terminal"
            thread_id = f"v2-task:{state.task_id}:{run_id}"
            proposal = "publication-tamper-001"
            answer = "已验证的最终回答"
            prepared = prepare_terminal_response_receipt(
                task_id=state.task_id, run_id=run_id, thread_id=thread_id,
                proposal_hash=proposal, session_id=SESSION, answer=answer,
                state_revision=state.revision + 1,
                base_task_revision=state.revision,
                control_policy="react_v1",
            )
            key, payload = prepared
            finalization = {
                "taskId": state.task_id, "runId": run_id,
                "threadId": thread_id, "publicationId": proposal,
                "sessionOwnerHash": session_owner_hash(SESSION),
                "baseTaskRevision": state.revision,
                "finalizationRevision": state.revision + 1,
                "controlPolicy": "react_v1",
                "policyRevision": "react-v1-2026-08-27",
                "answerSha256": payload["resultHash"],
            }
            updated = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision, actor="agent",
                    domainStatePatch={"v2FinalAnswerReceipt": finalization},
                ),
                immutable_side_record=(
                    key, json.dumps(payload, ensure_ascii=False, sort_keys=True)
                ),
            )
            before = await read_terminal_response_receipt(
                task_id=state.task_id, run_id=run_id, thread_id=thread_id,
                proposal_hash=proposal, session_id=SESSION, task_state=updated,
            )
            forged = dict(payload)
            forged["resultHash"] = "0" * 64
            ttl_ms = await client.pttl(key)
            await client.set(
                key, json.dumps(forged, ensure_ascii=False),
                px=ttl_ms if ttl_ms > 0 else None,
            )
            after = await read_terminal_response_receipt(
                task_id=state.task_id, run_id=run_id, thread_id=thread_id,
                proposal_hash=proposal, session_id=SESSION, task_state=updated,
            )
            return {
                "id": "T11", "target": "terminal_outbox",
                "beforeValid": before is not None, "afterValid": after is not None,
                "accepted": after is not None,
            }
        finally:
            await client.aclose()
            task_state._client = None

    async def _bootstrap_on_client() -> dict[str, str]:
        state = await create_task_state(TaskStateCreateRequest(
            goal=MESSAGE, task_type="ecommerce_guide", session_id=SESSION,
            domain_state={"shoppingGuide": {
                "mode": "recommend", "category": "phone", "useCases": [],
                "requirements": [], "candidateIds": [], "comparedIds": [],
                "evidenceStatus": "missing",
            }},
        ))
        return {"taskId": state.task_id, "sessionId": SESSION}
    return asyncio.run(case())


def _run_terminal_replay(port: int, index: int) -> dict[str, Any]:
    async def case() -> dict[str, Any]:
        client = aioredis.Redis(
            host="127.0.0.1", port=port, decode_responses=True
        )
        task_state._client = client
        try:
            identity = await _bootstrap_on_client()
            state = await get_task_state(identity["taskId"])
            assert state is not None
            run_id = f"run-terminal-replay-{index:03d}"
            thread_id = f"v2-task:{state.task_id}:{run_id}"
            publication_id = f"publication-terminal-{index:03d}"
            answer = f"最终回答-{index:03d}"
            key, payload = prepare_terminal_response_receipt(
                task_id=state.task_id, run_id=run_id, thread_id=thread_id,
                proposal_hash=publication_id, session_id=SESSION, answer=answer,
                state_revision=state.revision + 1,
                base_task_revision=state.revision,
                control_policy="react_v1",
            )
            finalization = {
                "taskId": state.task_id, "runId": run_id,
                "threadId": thread_id, "publicationId": publication_id,
                "sessionOwnerHash": session_owner_hash(SESSION),
                "baseTaskRevision": state.revision,
                "finalizationRevision": state.revision + 1,
                "controlPolicy": "react_v1",
                "policyRevision": "react-v1-2026-08-27",
                "answerSha256": payload["resultHash"],
            }
            published = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision, actor="agent",
                    domainStatePatch={"v2FinalAnswerReceipt": finalization},
                ),
                immutable_side_record=(
                    key, json.dumps(payload, ensure_ascii=False, sort_keys=True)
                ),
            )
            replay_hits: list[bool] = []
            for _ in range(3):
                receipt = await read_terminal_response_receipt(
                    task_id=state.task_id, run_id=run_id,
                    thread_id=thread_id, proposal_hash=publication_id,
                    session_id=SESSION, task_state=published,
                )
                replay_hits.append(receipt is not None)
            final_state = await get_task_state(state.task_id)
            publication_count = int(bool(await client.exists(key)))
            return {
                "family": "exact_replay", "index": index,
                "taskId": state.task_id, "runId": run_id,
                "publicationId": publication_id,
                "publishedRevision": published.revision,
                "finalRevision": final_state.revision if final_state else None,
                "replayHits": replay_hits,
                "publicationCount": publication_count,
            }
        finally:
            await client.aclose()
            task_state._client = None

    async def _bootstrap_on_client() -> dict[str, str]:
        state = await create_task_state(TaskStateCreateRequest(
            goal=MESSAGE, task_type="ecommerce_guide", session_id=SESSION,
            domain_state={"shoppingGuide": {
                "mode": "recommend", "category": "phone", "useCases": [],
                "requirements": [], "candidateIds": [], "comparedIds": [],
                "evidenceStatus": "missing",
            }},
        ))
        return {"taskId": state.task_id, "sessionId": SESSION}
    return asyncio.run(case())


def _tamper_pause(sync: redis.Redis, port: int) -> dict[str, Any]:
    identity = asyncio.run(_bootstrap(port))
    run_id = "run-tamper-pause"
    paused = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="fresh",
        request_pause=True,
    ))
    receipt = (paused.get("result") or {}).get("pauseReceipt")
    key = f"graph-v2:pause:{identity['taskId']}"
    stored = json.loads(sync.get(key))
    stored["checkpointHash"] = "0" * 16
    _tamper_set_keep_ttl(sync, key, json.dumps(stored))
    restart = _run_process(_worker_command(
        port=port, identity=identity, run_id=run_id, mode="restart",
        payload=receipt,
    ))
    return {
        "id": "T12", "target": "pause_checkpoint_binding",
        "paused": paused, "restart": restart,
        "accepted": (
            restart.get("returnCode") == 0
            and _result_boundary(restart) not in {
                "resume_rejected", "state_diverged", "stop_turn"
            }
        ),
        "ledger": _redis_rows(sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"),
    }


def _run_tamper_matrix(sync: redis.Redis, port: int) -> list[dict[str, Any]]:
    return [
        _tamper_checkpoint_payload(sync, port),
        _tamper_pending_write(sync, port),
        _tamper_checkpoint_identity(
            sync, port, case_id="T03", channel="task_id", value="task-foreign"
        ),
        _tamper_checkpoint_identity(
            sync, port, case_id="T04", channel="thread_id", value="v2-task:task-foreign:run-foreign"
        ),
        _tamper_checkpoint_identity(
            sync, port, case_id="T05", channel="policy_revision", value="fixed-v1"
        ),
        _tamper_cursor(sync, port, case_id="T06", policy=False),
        _tamper_cursor(sync, port, case_id="T07", policy=True),
        _tamper_marker(sync, port),
        _tamper_inbox_or_projection(sync, port, case_id="T09", projection=False),
        _tamper_inbox_or_projection(sync, port, case_id="T10", projection=True),
        _tamper_terminal_outbox(port),
        _tamper_pause(sync, port),
    ]


def _run_fence_race(port: int, index: int) -> dict[str, Any]:
    from app.graph.tool_inbox_v2 import InboxStatus, ToolInboxSlot, sha256
    async def finish() -> dict[str, Any]:
        client = aioredis.Redis(
            host="127.0.0.1", port=port, decode_responses=True
        )
        slot = ToolInboxSlot.create(
            task_id=f"task-fence-{index:03d}", plan_id="plan-fence",
            step_id="step-fence", state_revision=1,
            tool_name="search_products",
            canonical_args_sha256=sha256(
                {"query": "fence", "category": "手机"}
            ),
        )
        inbox = ToolInbox(client, lease_ms=100, ttl_ms=60_000)
        try:
            old = await inbox.claim(
                slot, run_id="run-fence", thread_id=f"thread-{index:03d}"
            )
            await asyncio.sleep(0.13)
            competitor = _run_process([
                sys.executable, "-u", "-m",
                "evaluation.react_v1_durable_checkpoint_v2_20260901_v1.runner",
                "--fence-worker", "--port", str(port),
                "--fence-index", str(index),
            ])
            new_result = competitor.get("result") or {}
            trace = ToolTrace(
                tool="search_products", ok=True, durationMs=1.0,
                detail=two_stage_search_detail([101]),
            )
            context_receipt = {
                "taskId": slot.task_id, "runId": "run-fence",
                "threadId": f"thread-{index:03d}", "sessionOwnerHash": "0" * 16,
                "planId": slot.plan_id, "stepId": slot.step_id,
                "toolName": slot.tool_name, "stateRevision": slot.state_revision,
                "inputHash": slot.canonical_args_sha256,
                "resultHash": __import__("app.graph.tool_inbox_v2", fromlist=["sha256"]).sha256(trace.model_dump(by_alias=True, mode="json")),
                "executionId": new_result.get("executionId"),
                "logicalSlotKey": slot.logical_slot_key(),
                "fence": new_result.get("fence"),
                "inboxStatus": "SUCCEEDED", "toolOutcome": "tool_succeeded",
            }
            stale = await inbox.complete(
                slot, execution_id=old.execution_id, fence=old.fence,
                trace=trace, receipt={**context_receipt, "fence": old.fence},
            )
            return {
                "index": index, "oldFence": old.fence,
                "newFence": new_result.get("fence"),
                "oldPid": os.getpid(), "newPid": competitor.get("pid"),
                "competitor": competitor,
                "inflight": new_result.get("inflight"),
                "newComplete": new_result.get("completed"),
                "oldLateComplete": stale.status.value,
                "pass": (
                    competitor.get("returnCode") == 0
                    and int(new_result.get("fence") or 0) > old.fence
                    and new_result.get("completed") == InboxStatus.SUCCEEDED.value
                    and stale.status is InboxStatus.FENCED_OUT
                ),
            }
        finally:
            await client.aclose()
    return asyncio.run(finish())


async def _fence_worker(args: argparse.Namespace) -> dict[str, Any]:
    from app.graph.tool_inbox_v2 import ToolInboxSlot, sha256
    index = int(args.fence_index)
    client = aioredis.Redis(
        host="127.0.0.1", port=args.port, decode_responses=True
    )
    slot = ToolInboxSlot.create(
        task_id=f"task-fence-{index:03d}", plan_id="plan-fence",
        step_id="step-fence", state_revision=1,
        tool_name="search_products",
        canonical_args_sha256=sha256({"query": "fence", "category": "手机"}),
    )
    inbox = ToolInbox(client, lease_ms=100, ttl_ms=60_000)
    try:
        claim = await inbox.claim(
            slot, run_id="run-fence", thread_id=f"thread-{index:03d}"
        )
        inflight = await inbox.enter_in_flight(
            slot, execution_id=claim.execution_id, fence=claim.fence
        )
        trace = ToolTrace(
            tool="search_products", ok=True, durationMs=1.0,
            detail=two_stage_search_detail([101]),
        )
        receipt = {
            "taskId": slot.task_id, "runId": "run-fence",
            "threadId": f"thread-{index:03d}", "sessionOwnerHash": "0" * 16,
            "planId": slot.plan_id, "stepId": slot.step_id,
            "toolName": slot.tool_name, "stateRevision": slot.state_revision,
            "inputHash": slot.canonical_args_sha256,
            "resultHash": __import__(
                "app.graph.tool_inbox_v2", fromlist=["sha256"]
            ).sha256(trace.model_dump(by_alias=True, mode="json")),
            "executionId": claim.execution_id,
            "logicalSlotKey": slot.logical_slot_key(), "fence": claim.fence,
            "inboxStatus": "SUCCEEDED", "toolOutcome": "tool_succeeded",
        }
        completed = await inbox.complete(
            slot, execution_id=claim.execution_id, fence=claim.fence,
            trace=trace, receipt=receipt,
        )
        return {
            "pid": os.getpid(), "executionId": claim.execution_id,
            "fence": claim.fence, "inflight": inflight.status.value,
            "completed": completed.status.value,
        }
    finally:
        await client.aclose()


def _scan_redis(sync: redis.Redis) -> dict[str, Any]:
    sensitive_terms = (
        "api_key", "password", "refresh_token",
        "begin private key", "api.deepseek.com",
    )
    sensitive: list[dict[str, Any]] = []
    missing_ttl: list[str] = []
    key_counts: dict[str, int] = {}
    for key in sorted(sync.scan_iter(match="*")):
        key_type = sync.type(key)
        key_counts[key_type] = key_counts.get(key_type, 0) + 1
        ttl = sync.ttl(key)
        if key.startswith((
            "graph-v2:cp:", "graph-v2:cursor:", "graph-v2:terminal:",
            "graph-v2:pause:", "agent-tool-inbox:v2:slot:",
            "agent-tool-inbox:v2:record:", "react-v1:", "task-state:",
        )) and ttl < 0:
            missing_ttl.append(key)
        chunks = [key]
        if key_type == "string":
            chunks.append(str(sync.get(key)))
        elif key_type == "hash":
            chunks.extend(f"{k}={v}" for k, v in sync.hgetall(key).items())
        elif key_type == "list":
            chunks.extend(sync.lrange(key, 0, -1))
        lower = "\n".join(chunks).lower()
        matched = [term for term in sensitive_terms if term in lower]
        if re.search(r"(?i)(?<![a-z0-9])(?:sk|key)-[a-z0-9_-]{12,}", lower):
            matched.append("credential_token_pattern")
        if matched:
            sensitive.append({"key": key, "terms": sorted(set(matched))})
    return {
        "keyTypeCounts": key_counts,
        "sensitiveHits": sensitive,
        "missingTtl": missing_ttl,
        "persistentFenceSequenceExists": bool(
            sync.exists("agent-tool-inbox:v2:fence-sequence")
        ),
    }


def _run_real_model_layer(sync: redis.Redis, port: int) -> list[dict[str, Any]]:
    specs = (
        (
            "B01", "unsupported_game_camera_evidence", "phone",
            "三千元内推荐二手手机；现有证据不足，请先核验续航和影像。",
        ),
        (
            "B02", "stale_candidate_reference", "phone",
            "三千元内推荐二手手机；候选引用已经过期，请安全处理。",
        ),
        (
            "B03", "unsupported_game_camera_evidence", "headphones",
            "推荐千元内耳机；现有证据互相冲突，请安全处理。",
        ),
    )
    rows: list[dict[str, Any]] = []
    for case_id, reason, category, message in specs:
        identity = asyncio.run(_bootstrap(
            port, reason=reason, category=category, message=message,
        ))
        run_id = f"run-real-model-{case_id.lower()}"
        process = _run_process(
            _worker_command(
                port=port, identity=identity, run_id=run_id,
                mode="fresh", real_model=True, user_message=message,
            ),
            timeout=60,
        )
        rows.append({
            "id": case_id,
            "reason": reason,
            "category": category,
            "identity": identity,
            "runId": run_id,
            "process": process,
            "ledger": _redis_rows(
                sync, f"eval:react-v1-durable:ledger:{identity['taskId']}"
            ),
            "finalState": _state_summary(port, identity["taskId"]),
        })
    return rows


def run_attempt(out_dir: Path) -> dict[str, Any]:
    out_dir = out_dir.resolve()
    if out_dir.exists():
        raise FileExistsError(f"new-only attempt already exists: {out_dir}")
    out_dir.mkdir(parents=True)
    redis_server = shutil.which("redis-server")
    if not redis_server:
        raise RuntimeError("redis-server is required")
    port = _free_port()
    server = subprocess.Popen(
        [redis_server, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sync = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    observations: dict[str, Any] = {
        "identity": IDENTITY,
        "attempt": out_dir.name,
        "startedAt": datetime.now(UTC).isoformat(),
        "redis": {"port": port, "pid": server.pid, "realProcess": True},
        "families": {},
    }
    try:
        for _ in range(100):
            try:
                if sync.ping():
                    break
            except redis.RedisError:
                time.sleep(0.05)
        else:
            raise RuntimeError("isolated Redis did not start")

        specs = (
            ("baseline", "none", None, 0),
            ("react_action_commit", "after_action_atomic_commit", 81, 1),
            ("inflight_before_effect", "after_in_flight_before_tool_effect", 82, 1),
            ("effect_before_inbox_complete", "after_tool_effect_before_inbox_complete", 83, 1),
            ("inbox_before_projection", "after_inbox_success_before_taskstate_projection", 84, 1),
            ("projection_before_checkpoint", "after_executor_receipt", 86, 1),
            ("validator_before_checkpoint", "after_validator_receipt", 85, 1),
        )
        for family, fault, exit_code, restarts in specs:
            observations["families"][family] = [
                _run_graph_bundle(
                    sync, port, family=family, index=index, fault=fault,
                    expected_fault_exit=exit_code,
                    post_restarts=(3 if family == "baseline" else restarts),
                )
                for index in range(1, 11)
            ]
        observations["families"]["state_diverged"] = [
            _run_state_diverged(sync, port, index) for index in range(1, 11)
        ]
        observations["families"]["exact_replay"] = [
            _run_terminal_replay(port, index) for index in range(1, 11)
        ]
        observations["families"]["clarification"] = [
            _run_clarification(sync, port, index) for index in range(1, 11)
        ]
        observations["families"]["operator_pause"] = [
            _run_pause(sync, port, index) for index in range(1, 11)
        ]
        observations["families"]["claim_takeover_fence"] = [
            _run_fence_race(port, index) for index in range(1, 21)
        ]
        observations["tamper"] = _run_tamper_matrix(sync, port)
        observations["realModel"] = _run_real_model_layer(sync, port)
        observations["redisAudit"] = _scan_redis(sync)
        observations["completedAt"] = datetime.now(UTC).isoformat()
        _write(out_dir / "observations.json", observations)
        source_hashes = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES
        }
        _write(out_dir / "source-hashes.json", source_hashes)
        manifest = {
            "schemaVersion": 1,
            "identity": IDENTITY,
            "attempt": out_dir.name,
            "createdAt": datetime.now(UTC).isoformat(),
            "controlPolicy": "react_v1",
            "policyRevision": "react-v1-2026-08-27",
            "observationsSha256": _sha(observations),
            "sourceHashesSha256": _sha(source_hashes),
            "preregistrationSha256": hashlib.sha256(
                (PACKAGE / "preregistration.json").read_bytes()
            ).hexdigest(),
            "scenarioMatrixSha256": hashlib.sha256(
                (PACKAGE / "scenario-matrix.json").read_bytes()
            ).hexdigest(),
            "implementationFreezeSha256": hashlib.sha256(
                (PACKAGE / "IMPLEMENTATION_FREEZE_RECEIPT.json").read_bytes()
            ).hexdigest(),
        }
        _write(out_dir / "manifest.json", manifest)
        from evaluation.react_v1_durable_checkpoint_v2_20260901_v1.scorer import score_attempt
        score = score_attempt(out_dir)
        _write(out_dir / "score.json", score)
        return score
    except BaseException as exc:
        observations["failedAt"] = datetime.now(UTC).isoformat()
        observations["runnerError"] = {
            "type": type(exc).__name__, "message": str(exc)[:1000],
        }
        _write(out_dir / "observations.partial.json", observations)
        raise
    finally:
        task_state._client = None
        sync.close()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def run_smoke() -> dict[str, Any]:
    """Non-formal one-sample plumbing check; never writes an attempt."""
    redis_server = shutil.which("redis-server")
    if not redis_server:
        raise RuntimeError("redis-server is required")
    port = _free_port()
    server = subprocess.Popen(
        [redis_server, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sync = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    try:
        for _ in range(100):
            try:
                if sync.ping():
                    break
            except redis.RedisError:
                time.sleep(0.05)
        baseline = _run_graph_bundle(
            sync, port, family="smoke_baseline", index=1, fault="none",
            expected_fault_exit=None, post_restarts=0,
        )
        return {
            "baseline": baseline,
            "fence": _run_fence_race(port, 1),
            "redisAudit": _scan_redis(sync),
        }
    finally:
        sync.close()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def run_tamper_smoke() -> dict[str, Any]:
    """Non-formal full tamper plumbing check; never writes an attempt."""
    redis_server = shutil.which("redis-server")
    if not redis_server:
        raise RuntimeError("redis-server is required")
    port = _free_port()
    server = subprocess.Popen(
        [redis_server, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sync = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    try:
        for _ in range(100):
            try:
                if sync.ping():
                    break
            except redis.RedisError:
                time.sleep(0.05)
        rows = _run_tamper_matrix(sync, port)
        replay = _run_terminal_replay(port, 1)
        return {
            "tamper": [{
                "id": row.get("id"), "target": row.get("target"),
                "accepted": row.get("accepted"),
                "returnCode": (row.get("restart") or {}).get("returnCode"),
                "boundary": _result_boundary(row.get("restart") or {}),
                "tamperedWrites": row.get("tamperedWrites"),
            } for row in rows],
            "exactReplay": replay,
            "redisAudit": _scan_redis(sync),
        }
    finally:
        sync.close()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _worker_main(args: argparse.Namespace) -> int:
    try:
        value = asyncio.run(_worker(args))
        print(json.dumps(value, ensure_ascii=True, sort_keys=True), flush=True)
        return 0
    except BaseException as exc:
        print(json.dumps({
            "workerError": type(exc).__name__, "message": str(exc)[:1000],
        }, sort_keys=True), flush=True)
        return 2


def _fence_worker_main(args: argparse.Namespace) -> int:
    try:
        value = asyncio.run(_fence_worker(args))
        print(json.dumps(value, ensure_ascii=True, sort_keys=True), flush=True)
        return 0
    except BaseException as exc:
        print(json.dumps({
            "workerError": type(exc).__name__, "message": str(exc)[:1000],
        }, sort_keys=True), flush=True)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--fence-worker", action="store_true")
    parser.add_argument("--fence-index", type=int)
    parser.add_argument("--port", type=int)
    parser.add_argument("--task-id")
    parser.add_argument("--session-id")
    parser.add_argument("--run-id")
    parser.add_argument("--mode", choices=("fresh", "restart", "resume"))
    parser.add_argument("--fault", default="none")
    parser.add_argument("--payload-b64")
    parser.add_argument("--request-pause", action="store_true")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--user-message-b64")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--tamper-smoke", action="store_true")
    args = parser.parse_args(argv)
    if args.worker:
        return _worker_main(args)
    if args.fence_worker:
        return _fence_worker_main(args)
    if args.smoke:
        print(json.dumps(run_smoke(), ensure_ascii=True, sort_keys=True))
        return 0
    if args.tamper_smoke:
        print(json.dumps(run_tamper_smoke(), ensure_ascii=True, sort_keys=True))
        return 0
    score = run_attempt(args.out_dir)
    print(json.dumps(score, sort_keys=True))
    return 0 if score.get("status") == "ACCEPT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
