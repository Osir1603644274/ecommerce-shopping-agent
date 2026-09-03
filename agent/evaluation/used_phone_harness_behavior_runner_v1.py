"""Leakage-safe HTTP runner for the used-phone Harness behavior dataset.

The runner reads only the public scenario file. Private expectations are read
later by the separate scorer, never placed in requests or model context.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ASSET_ROOT = (
    Path(__file__).resolve().parent
    / "assets"
    / "used_phone_harness_behavior_v1_20260825"
)
PUBLIC_SCENARIOS = ASSET_ROOT / "public" / "scenarios.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1),
    )
    return round(ordered[index], 2)


def _response_failure_reason(payload: object) -> str | None:
    """Detect an application failure hidden inside an HTTP 200 response."""

    if not isinstance(payload, dict):
        return "response_payload_invalid"
    trace = payload.get("trace")
    if not isinstance(trace, dict):
        return None
    if trace.get("status") == "error":
        failure_class = trace.get("failureClass")
        agent_status = trace.get("agentStatus")
        return f"request_trace_error:{failure_class or 'unknown'}:{agent_status or 'unknown'}"
    return None


def derive_authoritative_action(response: dict[str, Any]) -> dict[str, Any]:
    """Derive the fixed_v1 boundary from public response fields only."""

    traces = response.get("tool_trace") or response.get("toolTrace") or []
    successful = [
        item for item in traces
        if isinstance(item, dict) and item.get("ok") is True
    ]
    for tool_name in (
        "compare_products",
        "rerank_products_in_scope",
        "search_products",
    ):
        if any(item.get("tool") == tool_name for item in successful):
            return {"kind": "CALL_TOOL", "toolName": tool_name}
    summary = response.get("traceSummary") or response.get("trace_summary") or {}
    final_action = summary.get("finalAction") if isinstance(summary, dict) else None
    task_state = response.get("taskState") or response.get("task_state") or {}
    pending = task_state.get("pendingQuestions") if isinstance(task_state, dict) else []
    if final_action == "ask_user" or pending:
        return {"kind": "ASK_CLARIFICATION", "toolName": None}
    if final_action in {"safe_stop", "stop_turn"}:
        return {"kind": "NEEDS_REVIEW", "toolName": None}
    return {"kind": "ANSWER", "toolName": None}


def extract_shadow_action(trace: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(trace, dict):
        return {"status": "not_observed", "kind": None, "toolName": None}
    decisions = trace.get("reactDecisions")
    if not isinstance(decisions, list) or len(decisions) != 1:
        return {
            "status": "not_observed" if not decisions else "invalid_count",
            "kind": None,
            "toolName": None,
        }
    item = decisions[0]
    if not isinstance(item, dict):
        return {"status": "malformed", "kind": None, "toolName": None}
    return {
        "status": item.get("status"),
        "decisionSource": item.get("decisionSource"),
        "kind": item.get("actionKind"),
        "toolName": item.get("toolName"),
        "reasonCode": item.get("reasonCode"),
        "errorCode": item.get("errorCode"),
        "durationMs": item.get("durationMs"),
        "viewTokenCount": item.get("viewTokenCount"),
        "viewHash": item.get("viewHash"),
    }


def extract_react_sequence(trace: dict[str, Any] | None) -> dict[str, Any]:
    """Extract only the redacted action/outcome sequence from debug trace."""

    if not isinstance(trace, dict):
        return {"status": "not_observed", "actions": [], "outcomes": []}
    decisions = trace.get("reactDecisions")
    outcomes = trace.get("reactOutcomes")
    if not isinstance(decisions, list) or not isinstance(outcomes, list):
        return {"status": "not_observed", "actions": [], "outcomes": []}
    actions = []
    for item in decisions:
        if not isinstance(item, dict):
            return {"status": "malformed", "actions": [], "outcomes": []}
        actions.append({
            "status": item.get("status"),
            "decisionSource": item.get("decisionSource"),
            "kind": item.get("actionKind"),
            "optionId": item.get("optionId"),
            "publishedOptionIds": item.get("publishedOptionIds") or [],
            "toolName": item.get("toolName"),
            "reasonCode": item.get("reasonCode"),
            "errorCode": item.get("errorCode"),
            "durationMs": item.get("durationMs"),
            "viewTokenCount": item.get("viewTokenCount"),
            "viewHash": item.get("viewHash"),
        })
    redacted_outcomes = []
    for item in outcomes:
        if not isinstance(item, dict):
            return {"status": "malformed", "actions": [], "outcomes": []}
        redacted_outcomes.append({
            "status": item.get("status"),
            "validatorOutcome": item.get("validatorOutcome"),
            "stateRevisionAfter": item.get("stateRevisionAfter"),
            "retryable": item.get("retryable"),
            "errorCode": item.get("errorCode"),
            "observationRefHash": item.get("observationRefHash"),
        })
    return {
        "status": "observed" if actions else "not_observed",
        "actions": actions,
        "outcomes": redacted_outcomes,
        "terminalKind": actions[-1]["kind"] if actions else None,
    }


async def _fetch_debug_trace(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    run_id: str | None,
    debug_key: str | None,
) -> dict[str, Any] | None:
    if not run_id or not debug_key:
        return None
    response = await client.get(
        f"{base_url}/internal/debug/agent-runs/{run_id}",
        headers={"X-Agent-Debug-Key": debug_key},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("debug trace response must be an object")
    return payload


async def run_public_scenarios(
    *,
    base_url: str,
    expected_runtime: str,
    output_dir: Path,
    debug_key: str | None,
    dataset_path: Path = PUBLIC_SCENARIOS,
    limit: int | None = None,
    scenario_ids: set[str] | None = None,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    scenarios = _read_jsonl(dataset_path)
    if scenario_ids:
        scenarios = [row for row in scenarios if row["scenarioId"] in scenario_ids]
    if limit is not None:
        scenarios = scenarios[:limit]
    if not scenarios:
        raise ValueError("no public scenarios selected")
    if not debug_key:
        raise ValueError("paired runtime evaluation requires a gated debug key")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    receipts_path = output_dir / "receipts.jsonl"
    run_tag = uuid.uuid4().hex[:10]
    started_at = datetime.now(timezone.utc)
    durations: list[float] = []
    receipt_count = 0
    runtime_mismatch_count = 0
    error_count = 0

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        with receipts_path.open("w", encoding="utf-8", newline="\n") as sink:
            for scenario in scenarios:
                session_id = f"uphb-{expected_runtime}-{run_tag}-{scenario['scenarioId'].lower()}"
                for turn in scenario["turns"]:
                    started = time.perf_counter()
                    receipt: dict[str, Any] = {
                        "schemaVersion": "used-phone-harness-behavior-receipt-v1",
                        "scenarioId": scenario["scenarioId"],
                        "turnId": turn["turnId"],
                        "executionTier": scenario["executionTier"],
                        "provenanceKind": scenario["provenanceKind"],
                        "sessionId": session_id,
                        "expectedRuntime": expected_runtime,
                    }
                    try:
                        response = await client.post(
                            f"{base_url}/agent/chat-llm",
                            json={
                                "message": turn["text"],
                                "sessionId": session_id,
                                "domainHint": "ecommerce",
                            },
                        )
                        response.raise_for_status()
                        payload = response.json()
                        run_id = payload.get("runId")
                        request_trace = payload.get("trace")
                        receipt.update({
                            "requestId": (
                                request_trace.get("requestId")
                                if isinstance(request_trace, dict) else None
                            ),
                            "runId": run_id,
                            "requestTrace": request_trace,
                        })
                        response_failure = _response_failure_reason(payload)
                        if response_failure is not None:
                            raise RuntimeError(response_failure)
                        debug_trace = await _fetch_debug_trace(
                            client,
                            base_url=base_url,
                            run_id=run_id,
                            debug_key=debug_key,
                        )
                        entered_runtime = (
                            debug_trace.get("enteredRuntime")
                            if isinstance(debug_trace, dict)
                            else None
                        )
                        runtime_matches = (
                            entered_runtime == expected_runtime
                            or (
                                entered_runtime is None
                                and expected_runtime != "react_v0"
                            )
                        )
                        runtime_mismatch_count += int(not runtime_matches)
                        authoritative = derive_authoritative_action(payload)
                        shadow = extract_shadow_action(debug_trace)
                        react_sequence = extract_react_sequence(debug_trace)
                        if expected_runtime == "react_v0_shadow":
                            selected = shadow
                        elif expected_runtime == "react_v0":
                            selected = (
                                react_sequence["actions"][0]
                                if react_sequence["actions"]
                                else {"status": "not_observed", "kind": None,
                                      "toolName": None}
                            )
                        else:
                            selected = {"status": "derived", **authoritative}
                        receipt.update({
                            "status": "ok",
                            "enteredRuntime": entered_runtime,
                            "runtimeMatches": runtime_matches,
                            "selectedAction": selected,
                            "reactSequence": react_sequence,
                            "authoritativeAction": authoritative,
                            "taskRelation": payload.get("taskRelation"),
                            "taskState": payload.get("taskState"),
                            "toolTrace": payload.get("tool_trace") or payload.get("toolTrace") or [],
                            "answer": payload.get("answer"),
                            "guideResult": payload.get("guideResult"),
                            "modelAttribution": {
                                "modelCallCounts": (payload.get("trace") or {}).get(
                                    "modelCallCounts", {}
                                ),
                                "llmDurationByStageMs": (payload.get("trace") or {}).get(
                                    "llmDurationByStageMs", {}
                                ),
                            },
                        })
                    except Exception as exc:
                        error_count += 1
                        receipt.update({
                            "status": "error",
                            "errorType": type(exc).__name__,
                            "error": str(exc)[:1000],
                        })
                    duration_ms = (time.perf_counter() - started) * 1000.0
                    durations.append(duration_ms)
                    receipt["runnerDurationMs"] = round(duration_ms, 2)
                    sink.write(json.dumps(receipt, ensure_ascii=False) + "\n")
                    sink.flush()
                    receipt_count += 1

    finished_at = datetime.now(timezone.utc)
    manifest = {
        "schemaVersion": "used-phone-harness-behavior-run-manifest-v1",
        "status": "COMPLETE" if error_count == 0 else "COMPLETED_WITH_ERRORS",
        "evidenceScope": (
            "live_react_action_outcome_sequence_and_e2e_capture"
            if expected_runtime == "react_v0"
            else "shadow_action_selection_and_authoritative_e2e_capture"
        ),
        "expectedRuntime": expected_runtime,
        "baseUrl": base_url,
        "dataset": str(dataset_path),
        "datasetSha256": _sha256(dataset_path),
        "privateOracleReadByRunner": False,
        "scenarioCount": len(scenarios),
        "turnCount": receipt_count,
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "errorCount": error_count,
        "runtimeMismatchCount": runtime_mismatch_count,
        "latencyMs": {
            "mean": round(statistics.fmean(durations), 2) if durations else None,
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
        },
        "receipts": "receipts.jsonl",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument(
        "--expected-runtime",
        required=True,
        choices=("fixed_v1", "react_v0_shadow", "react_v0"),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--debug-key")
    parser.add_argument("--dataset", type=Path, default=PUBLIC_SCENARIOS)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--scenario-id", action="append")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    args = parser.parse_args()
    result = asyncio.run(run_public_scenarios(
        base_url=args.base_url.rstrip("/"),
        expected_runtime=args.expected_runtime,
        output_dir=args.output_dir,
        debug_key=args.debug_key,
        dataset_path=args.dataset,
        limit=args.limit,
        scenario_ids=set(args.scenario_id or []),
        timeout_seconds=args.timeout_seconds,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errorCount"] or result["runtimeMismatchCount"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
