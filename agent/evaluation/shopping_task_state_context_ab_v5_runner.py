"""Leakage-safe react_v1 runner for the V5 TaskState semantic-source pair."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from evaluation.used_phone_harness_behavior_runner_v1 import (
    derive_authoritative_action,
    extract_react_sequence,
)


MODULE_PATH = Path(__file__).resolve()
ASSET_ROOT = MODULE_PATH.parent / "assets" / "shopping_task_state_context_ab_v5_20260829"
SOURCE_ROOT = MODULE_PATH.parent / "assets" / "used_phone_harness_behavior_v1_20260825"
PUBLIC_SCENARIOS = SOURCE_ROOT / "public" / "scenarios.jsonl"
PREREGISTRATION = ASSET_ROOT / "manifest.json"
SELECTION = ASSET_ROOT / "selection.json"
READ_ONLY_TOOLS = {
    "search_products",
    "get_product_details",
    "compare_products",
    "rerank_products_in_scope",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_value(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 2)


async def _fetch_debug_trace(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    run_id: str,
    debug_key: str,
) -> dict[str, Any]:
    last: httpx.Response | None = None
    for attempt in range(20):
        response = await client.get(
            f"{base_url}/internal/debug/agent-runs/{run_id}",
            headers={"X-Agent-Debug-Key": debug_key},
        )
        last = response
        if response.status_code == 404 and attempt < 19:
            await asyncio.sleep(0.1)
            continue
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("debug trace must be an object")
        return value
    assert last is not None
    last.raise_for_status()
    raise AssertionError("unreachable")


def _response_failure(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "response_payload_invalid"
    trace = payload.get("trace")
    if not isinstance(trace, dict):
        return "request_trace_missing"
    if trace.get("status") != "ok":
        return f"request_status:{trace.get('status')}"
    if trace.get("agentStatus") == "failed":
        return f"agent_failed:{trace.get('agentFailureCode')}"
    if not isinstance(payload.get("answer"), str) or not payload["answer"].strip():
        return "answer_missing"
    return None


def _turn_failures(
    payload: dict[str, Any],
    debug: dict[str, Any] | None,
    runtime_status: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    response_failure = _response_failure(payload)
    if response_failure:
        failures.append(response_failure)
    if (
        runtime_status.get("enteredRuntimeDefault") != "react_v1"
        or runtime_status.get("controlPolicy") != "react_v1"
        or runtime_status.get("policyRevision") != "react-v1-2026-08-27"
        or runtime_status.get("reactLive") is not True
        or runtime_status.get("durableCheckpoint") is not True
    ):
        failures.append("service_runtime_attestation_mismatch")
    if debug is not None:
        if debug.get("enteredRuntime") != "react_v1":
            failures.append("entered_runtime_mismatch")
        if debug.get("controlPolicy") != "react_v1":
            failures.append("control_policy_mismatch")
        if debug.get("policyRevision") != "react-v1-2026-08-27":
            failures.append("policy_revision_mismatch")
    request_trace = payload.get("trace") or {}
    counts = request_trace.get("modelCallCounts") or {}
    failed = request_trace.get("modelCallFailures") or {}
    if not isinstance(counts, dict) or not isinstance(failed, dict):
        failures.append("model_attribution_malformed")
    elif any(type(value) is not int or value != 0 for value in failed.values()):
        failures.append("model_call_failure")
    react_calls = counts.get("react_decision", 0) if isinstance(counts, dict) else None
    if type(react_calls) is not int or not 0 <= react_calls <= 2:
        failures.append("react_decision_budget_invalid")
    for item in payload.get("tool_trace") or payload.get("toolTrace") or []:
        if isinstance(item, dict) and item.get("tool") not in READ_ONLY_TOOLS:
            failures.append(f"non_read_only_tool:{item.get('tool')}")
    if debug is None:
        tool_trace = payload.get("tool_trace") or payload.get("toolTrace") or []
        if tool_trace or react_calls != 0 or request_trace.get("agentStatus") != "not_run":
            failures.append("missing_run_id_not_direct_server_response")
    return sorted(set(failures))


async def run_public_scenarios(
    *,
    arm: str,
    base_url: str,
    output_dir: Path,
    debug_key: str,
    timeout_seconds: float = 120.0,
    scenario_ids: set[str] | None = None,
) -> dict[str, Any]:
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    selected_ids = selection["selectedScenarioIds"]
    scenarios_by_id = {row["scenarioId"]: row for row in _read_jsonl(PUBLIC_SCENARIOS)}
    if set(selected_ids) != set(scenarios_by_id) or len(selected_ids) != 24:
        raise ValueError("V5 selection must equal the complete frozen 24-scenario corpus")
    if scenario_ids:
        unknown = scenario_ids - set(selected_ids)
        if unknown:
            raise ValueError(f"unknown scenario IDs: {sorted(unknown)}")
        selected_ids = [item for item in selected_ids if item in scenario_ids]
    scenarios = [scenarios_by_id[item] for item in selected_ids]
    expected_turns = sum(len(row["turns"]) for row in scenarios)
    full_run = len(selected_ids) == 24 and expected_turns == 65
    if not scenarios or (not scenario_ids and not full_run):
        raise ValueError("V5 corpus must contain exactly 65 turns")
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    receipts_path = output_dir / "receipts.jsonl"
    started_at = datetime.now(timezone.utc)
    run_tag = uuid.uuid4().hex[:12]
    durations: list[float] = []
    failures: list[dict[str, Any]] = []
    stage_calls: Counter[str] = Counter()
    stage_failures: Counter[str] = Counter()

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        health_response = await client.get(f"{base_url}/health")
        health_response.raise_for_status()
        runtime_response = await client.get(f"{base_url}/agent/runtime-status")
        runtime_response.raise_for_status()
        runtime_status = runtime_response.json()
        if not isinstance(runtime_status, dict):
            raise ValueError("runtime status must be an object")
        if _turn_failures(
            {
                "answer": "runtime probe",
                "trace": {
                    "status": "ok",
                    "agentStatus": "not_run",
                    "modelCallCounts": {"react_decision": 0},
                    "modelCallFailures": {},
                },
                "toolTrace": [],
            },
            None,
            runtime_status,
        ):
            raise ValueError("service runtime attestation failed")
        with receipts_path.open("x", encoding="utf-8", newline="\n") as sink:
            for scenario in scenarios:
                session_id = f"sts-v5-{arm}-{run_tag}-{scenario['scenarioId'].lower()}"
                pending_resume: dict[str, Any] | None = None
                for turn in scenario["turns"]:
                    body: dict[str, Any] = {
                        "message": turn["text"],
                        "sessionId": session_id,
                        "domainHint": "ecommerce",
                    }
                    request_kind = "fresh_turn"
                    if pending_resume is not None:
                        body["resume"] = {**pending_resume, "answer": turn["text"]}
                        request_kind = "clarification_resume"
                    started = time.perf_counter()
                    receipt: dict[str, Any] = {
                        "schemaVersion": "shopping-task-state-context-live-receipt-v5",
                        "arm": arm,
                        "scenarioId": scenario["scenarioId"],
                        "turnId": turn["turnId"],
                        "executionTier": scenario["executionTier"],
                        "provenanceKind": scenario["provenanceKind"],
                        "sessionId": session_id,
                        "expectedRuntime": "react_v1",
                        "requestKind": request_kind,
                        "projectedTurnSha256": hashlib.sha256(turn["text"].encode("utf-8")).hexdigest(),
                    }
                    try:
                        response = await client.post(
                            f"{base_url}/agent/chat-llm-durable",
                            json=body,
                        )
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise ValueError("response must be an object")
                        run_id = payload.get("runId")
                        debug = (
                            await _fetch_debug_trace(
                                client,
                                base_url=base_url,
                                run_id=run_id,
                                debug_key=debug_key,
                            )
                            if isinstance(run_id, str) and run_id
                            else None
                        )
                        turn_failures = _turn_failures(payload, debug, runtime_status)
                        request_trace = payload.get("trace") or {}
                        stage_calls.update(request_trace.get("modelCallCounts") or {})
                        stage_failures.update(request_trace.get("modelCallFailures") or {})
                        authoritative = derive_authoritative_action(payload)
                        receipt.update({
                            "status": "ok" if not turn_failures else "safety_error",
                            "safetyFailures": turn_failures,
                            "requestId": request_trace.get("requestId"),
                            "runId": run_id,
                            "requestTrace": request_trace,
                            "enteredRuntime": (
                                debug.get("enteredRuntime")
                                if debug is not None
                                else runtime_status.get("enteredRuntimeDefault")
                            ),
                            "controlPolicy": (
                                debug.get("controlPolicy")
                                if debug is not None
                                else runtime_status.get("controlPolicy")
                            ),
                            "policyRevision": (
                                debug.get("policyRevision")
                                if debug is not None
                                else runtime_status.get("policyRevision")
                            ),
                            "runtimeMatches": (
                                debug.get("enteredRuntime") == "react_v1"
                                if debug is not None
                                else runtime_status.get("enteredRuntimeDefault") == "react_v1"
                            ),
                            "traceEvidenceStatus": (
                                "observed"
                                if debug is not None
                                else "not_applicable_direct_server_response"
                            ),
                            "selectedAction": {"status": "derived", **authoritative},
                            "authoritativeAction": authoritative,
                            "reactSequence": extract_react_sequence(debug),
                            "taskRelation": payload.get("taskRelation"),
                            "taskState": payload.get("taskState"),
                            "toolTrace": payload.get("tool_trace") or payload.get("toolTrace") or [],
                            "answer": payload.get("answer"),
                            "guideResult": payload.get("guideResult"),
                            "modelAttribution": {
                                "modelCallCounts": request_trace.get("modelCallCounts") or {},
                                "modelCallFailures": request_trace.get("modelCallFailures") or {},
                                "llmDurationByStageMs": request_trace.get("llmDurationByStageMs") or {},
                            },
                            "debugMetrics": {
                                "contextPackHash": debug.get("contextPackHash") if debug else None,
                                "contextTokenCount": debug.get("contextTokenCount") if debug else None,
                                "contextViews": (debug.get("contextViews") or []) if debug else [],
                                "degraded": debug.get("degraded") if debug else None,
                                "degradedReasons": (debug.get("degradedReasons") or []) if debug else [],
                            },
                            "payloadSha256": _hash_value(payload),
                            "debugSha256": _hash_value(debug) if debug is not None else None,
                        })
                        if turn_failures:
                            failures.append({
                                "scenarioId": scenario["scenarioId"],
                                "turnId": turn["turnId"],
                                "failures": turn_failures,
                            })
                        summary = payload.get("traceSummary") or {}
                        resume = summary.get("durableResume") if isinstance(summary, dict) else None
                        pending_resume = dict(resume) if isinstance(resume, dict) else None
                    except Exception as exc:
                        receipt.update({
                            "status": "error",
                            "runtimeMatches": False,
                            "errorType": type(exc).__name__,
                            "error": str(exc)[:1000],
                        })
                        failures.append({
                            "scenarioId": scenario["scenarioId"],
                            "turnId": turn["turnId"],
                            "failures": [f"runner_error:{type(exc).__name__}"],
                        })
                        pending_resume = None
                    elapsed = round((time.perf_counter() - started) * 1000.0, 2)
                    receipt["runnerDurationMs"] = elapsed
                    durations.append(elapsed)
                    sink.write(json.dumps(receipt, ensure_ascii=False) + "\n")
                    sink.flush()

    manifest = {
        "schemaVersion": "shopping-task-state-context-live-run-manifest-v5",
        "experimentId": "shopping-task-state-context-ab-v5-20260829",
        "arm": arm,
        "status": "ACCEPT" if not failures else "HOLD",
        "runtime": "react_v1",
        "coverage": "full_corpus" if full_run else "targeted_smoke",
        "scenarioCount": len(scenarios),
        "turnCount": expected_turns,
        "startedAt": started_at.isoformat(),
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "baseUrl": base_url,
        "runtimeStatus": runtime_status,
        "publicDatasetSha256": _sha256(PUBLIC_SCENARIOS),
        "preregistrationSha256": _sha256(PREREGISTRATION),
        "selectionSha256": _sha256(SELECTION),
        "runnerSha256": _sha256(MODULE_PATH),
        "receiptsSha256": _sha256(receipts_path),
        "privateOracleReadByRunner": False,
        "sealedMappingReadByRunner": False,
        "failures": failures,
        "modelCallCounts": dict(sorted(stage_calls.items())),
        "modelCallFailures": dict(sorted(stage_failures.items())),
        "latencyMs": {
            "mean": round(statistics.fmean(durations), 2) if durations else None,
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
        },
        "callAttributionBoundary": {
            "taskStateDecisionStages": ["task_state"],
            "reactDecisionStages": ["react_decision"],
            "excludedFromTaskStateOrReactDecision": ["task_manager", "final_answer"],
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("control", "treatment"), required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--debug-key", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--scenario-id", action="append")
    args = parser.parse_args()
    result = asyncio.run(run_public_scenarios(
        arm=args.arm,
        base_url=args.base_url.rstrip("/"),
        output_dir=args.output_dir,
        debug_key=args.debug_key,
        timeout_seconds=args.timeout_seconds,
        scenario_ids=set(args.scenario_id or []),
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ACCEPT":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
