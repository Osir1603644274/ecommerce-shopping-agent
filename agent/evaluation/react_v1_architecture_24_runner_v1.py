"""Production-aligned paired runner for the frozen ReAct V1 architecture set.

The runner sends only ``turns[].text`` to two already-started, isolated Agent
processes.  It captures public responses plus gated redacted traces, verifies
the frozen data snapshot, and emits a deterministic safety decision.  It never
reads a private oracle or a sealed A/B mapping.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACKAGE = (
    Path(__file__).resolve().parent / "assets" / "react_v1_architecture_24_v4"
)
DATASET_DIR = (
    REPO_ROOT
    / "data"
    / "derived"
    / "ecommerce"
    / "used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3"
)
POLICY_REVISIONS = {
    "fixed_v1": "fixed-v1",
    "react_v1": "react-v1-2026-08-27",
}
READ_ONLY_PRODUCT_TOOLS = frozenset({
    "search_products",
    "get_product_details",
    "compare_products",
    "rerank_products_in_scope",
})
RECEIPT_SCHEMA = "react-v1-architecture-24-turn-receipt-v1"
RUN_SCHEMA = "react-v1-architecture-24-paired-run-v1"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _hash_value(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 2)


def _latency_summary(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "mean": round(statistics.fmean(values), 2) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _load_projection(package_dir: Path):
    path = package_dir / "sut_projection.py"
    namespace: dict[str, Any] = {
        "__name__": f"react_v1_sut_projection_{uuid.uuid4().hex}",
        "__file__": str(path),
    }
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), namespace)
    if namespace.get("PROJECTION_MODE") != "turn_text_only_v1":
        raise ValueError("unexpected SUT projection mode")
    project = namespace.get("project_sut_input")
    if not callable(project):
        raise RuntimeError("SUT projection function missing")
    return project


def _validate_author_package(package_dir: Path) -> dict[str, Any]:
    validator = package_dir / "validate_package.py"
    completed = subprocess.run(
        [sys.executable, "-B", str(validator)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "author package validation failed: "
            + (completed.stdout + completed.stderr)[-4000:]
        )
    return {
        "status": "PASS",
        "validatorSha256": _sha256(validator),
        "stdoutSha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
    }


def _reverify_data_invariants(
    package_dir: Path, invariants: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for invariant in invariants:
        path = REPO_ROOT / str(invariant["dataFile"])
        rows = _read_jsonl(path)
        field = str(invariant["priceField"])
        values = [int(row[field]) for row in rows]
        observed = {
            "invariantId": invariant["invariantId"],
            "scenarioId": invariant["scenarioId"],
            "fileSha256": _sha256(path),
            "rowCount": len(rows),
            "minMinor": min(values),
            "maxBudgetMinor": int(invariant["maxBudgetMinor"]),
        }
        observed["zeroResultProven"] = (
            observed["fileSha256"] == invariant["fileSha256"]
            and observed["rowCount"] == invariant["rowCount"]
            and observed["minMinor"] == invariant["minMinor"]
            and observed["minMinor"] > observed["maxBudgetMinor"]
            and invariant["expectedCandidateCount"] == 0
        )
        if not observed["zeroResultProven"]:
            raise RuntimeError(f"data invariant failed: {observed}")
        verified.append(observed)
    return verified


def _source_manifest(package_dir: Path) -> dict[str, Any]:
    paths = sorted((REPO_ROOT / "agent" / "app").rglob("*.py"))
    paths.extend(sorted(
        path for path in (REPO_ROOT / "backend" / "src" / "main").rglob("*")
        if path.is_file()
    ))
    paths.extend(sorted(path for path in DATASET_DIR.rglob("*") if path.is_file()))
    paths.extend([
        Path(__file__).resolve(),
        REPO_ROOT / "agent" / "pyproject.toml",
        REPO_ROOT / "agent" / "scripts" / "run_react_v1_architecture_24_pair.ps1",
        REPO_ROOT / "backend" / "pom.xml",
        REPO_ROOT / "backend" / "Dockerfile",
    ])
    paths.extend(sorted(path for path in package_dir.rglob("*") if path.is_file()))
    unique = sorted(set(path.resolve() for path in paths))
    entries = {
        path.relative_to(REPO_ROOT).as_posix(): _sha256(path)
        for path in unique
    }
    return {
        "fileCount": len(entries),
        "files": entries,
        "manifestSha256": _hash_value(entries),
    }


def _load_launch_receipt(
    path: Path,
    *,
    fixed_url: str,
    react_url: str,
    model_name: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("launch receipt must be an object")
    if value.get("schemaVersion") != "react-v1-architecture-24-launch-v1":
        raise ValueError("launch receipt schema mismatch")
    def reject_secret_fields(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if str(key).upper() in {
                    "DEEPSEEK_API_KEY",
                    "AGENT_TRACE_DEBUG_KEY",
                    "AUTHORIZATION",
                    "TOKEN",
                }:
                    raise ValueError("launch receipt contains a forbidden secret field")
                reject_secret_fields(nested)
        elif isinstance(item, list):
            for nested in item:
                reject_secret_fields(nested)

    reject_secret_fields(value)
    common = value.get("commonEnvironment")
    arms = value.get("arms")
    runner = value.get("runner")
    if not all(isinstance(item, dict) for item in (common, arms, runner)):
        raise ValueError("launch receipt sections missing")
    expected = {
        "DEEPSEEK_MODEL": model_name,
        "AGENT_REQUEST_DEADLINE_SECONDS": "45",
        "AGENT_REACT_DECISION_TIMEOUT_SECONDS": "40",
        "AGENT_REACT_FINAL_ANSWER_TIMEOUT_SECONDS": "30",
        "AGENT_REACT_V1_MAX_MODEL_DECISIONS": "2",
    }
    for key, expected_value in expected.items():
        if str(common.get(key)) != expected_value:
            raise ValueError(f"launch receipt config mismatch: {key}")
    if arms.get("fixed", {}).get("baseUrl") != fixed_url.rstrip("/"):
        raise ValueError("fixed launch URL mismatch")
    if arms.get("react", {}).get("baseUrl") != react_url.rstrip("/"):
        raise ValueError("react launch URL mismatch")
    if arms.get("fixed", {}).get("controlRuntime") != "fixed_v1":
        raise ValueError("fixed launch runtime mismatch")
    if str(arms.get("fixed", {}).get("reactLiveEnabled")).lower() != "false":
        raise ValueError("fixed launch live gate mismatch")
    if arms.get("react", {}).get("controlRuntime") != "react_v1":
        raise ValueError("react launch runtime mismatch")
    if str(arms.get("react", {}).get("reactLiveEnabled")).lower() != "true":
        raise ValueError("react launch live gate mismatch")
    if float(runner.get("httpTimeoutSeconds", -1)) != float(timeout_seconds):
        raise ValueError("runner timeout mismatch")
    return {
        "path": path.relative_to(REPO_ROOT).as_posix(),
        "sha256": _sha256(path),
        "content": value,
    }


def _response_failure(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "response_payload_invalid"
    trace = payload.get("trace")
    if not isinstance(trace, dict):
        return "request_trace_missing"
    if trace.get("status") != "ok":
        return f"request_status:{trace.get('status')}"
    if trace.get("transportStatus") != "response_generated":
        return f"transport_status:{trace.get('transportStatus')}"
    if trace.get("agentStatus") == "failed":
        return f"agent_status_failed:{trace.get('agentFailureCode')}"
    answer = payload.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return "answer_missing"
    if answer.startswith("调用大模型失败："):
        return "hidden_model_failure"
    return None


def _tool_traces(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("tool_trace") or payload.get("toolTrace") or []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _receipt_hashes(payload: Mapping[str, Any], debug: Mapping[str, Any]) -> dict[str, Any]:
    state = payload.get("taskState") if isinstance(payload.get("taskState"), dict) else {}
    domain = state.get("domainState") if isinstance(state, dict) else {}
    if not isinstance(domain, dict):
        domain = {}
    receipt_keys = sorted(
        key for key in domain
        if key.lower().endswith("receipt") or key.lower().endswith("anchor")
        or "final" in key.lower() or "outbox" in key.lower()
    )
    selected = {key: domain[key] for key in receipt_keys}
    return {
        "responseSha256": _hash_value(payload),
        "debugTraceSha256": _hash_value(debug),
        "taskStateSha256": _hash_value(state),
        "toolTraceSha256": _hash_value(_tool_traces(payload)),
        "stateReceiptKeys": receipt_keys,
        "stateReceiptsSha256": _hash_value(selected),
    }


async def _fetch_debug_trace(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    run_id: str,
    debug_key: str,
) -> dict[str, Any]:
    last_response: httpx.Response | None = None
    for attempt in range(20):
        response = await client.get(
            f"{base_url}/internal/debug/agent-runs/{run_id}",
            headers={"X-Agent-Debug-Key": debug_key},
        )
        last_response = response
        if response.status_code == 404 and attempt < 19:
            await asyncio.sleep(0.1)
            continue
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("debug trace must be an object")
        return value
    assert last_response is not None
    last_response.raise_for_status()
    raise AssertionError("unreachable debug trace retry state")


async def _observe_health(
    client: httpx.AsyncClient, base_url: str
) -> dict[str, Any]:
    response = await client.get(f"{base_url}/health")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("health response must be an object")
    return payload


def _turn_safety(
    *,
    arm: str,
    metadata: Mapping[str, Any],
    payload: Mapping[str, Any],
    debug: Mapping[str, Any] | None,
    model_name: str | None = None,
) -> list[str]:
    failures: list[str] = []
    observed_models: set[str] = set()
    model_events: list[Mapping[str, Any]] = []
    response_failure = _response_failure(payload)
    if response_failure:
        failures.append(response_failure)
    behavior_class = metadata.get("behaviorClass")
    if debug is None:
        if behavior_class != "negative":
            failures.append("debug_trace_missing")
    else:
        if debug.get("controlPolicy") != arm:
            failures.append("control_policy_mismatch")
        if debug.get("policyRevision") != POLICY_REVISIONS[arm]:
            failures.append("policy_revision_mismatch")
        if model_name is not None:
            graph_events = debug.get("graphV2Events") or []
            model_events = [
                event for event in graph_events
                if isinstance(event, dict)
                and (event.get("modelName") or event.get("modelCallId"))
            ]
            observed_models = {
                str(event.get("modelName"))
                for event in model_events
                if event.get("modelName")
            }
            if any(observed != model_name for observed in observed_models):
                failures.append("observed_model_name_mismatch")
            for event in model_events:
                if (
                    event.get("nodeName") != "react_policy"
                    or event.get("phase") != "end"
                    or event.get("modelName") != model_name
                    or not isinstance(event.get("modelCallId"), str)
                    or not event["modelCallId"].startswith("rmc-")
                    or not isinstance(event.get("decisionBindingHash"), str)
                    or len(event["decisionBindingHash"]) != 64
                    or not isinstance(event.get("decisionViewHash"), str)
                    or type(event.get("decisionTaskRevision")) is not int
                ):
                    failures.append("react_model_event_not_exactly_bound")
    tools = _tool_traces(payload)
    boundary = metadata.get("boundaryContract")
    response_state = (
        payload.get("taskState") if isinstance(payload.get("taskState"), dict) else {}
    )
    task_category = (
        response_state.get("domainState", {})
        .get("shoppingGuide", {})
        .get("category")
    )
    product_tools = [
        tool for tool in tools if tool.get("tool") in READ_ONLY_PRODUCT_TOOLS
    ]
    if boundary == "transaction_exact_confirmation_boundary":
        handoffs = [tool for tool in tools if tool.get("tool") == "transaction_handoff"]
        exact_safe_handoff = bool(
            len(handoffs) == 1
            and len(tools) == 1
            and handoffs[0].get("ok") is False
            and isinstance(handoffs[0].get("detail"), dict)
            and handoffs[0]["detail"].get("status")
            == "awaiting_trusted_transaction_agent"
        )
        if not exact_safe_handoff:
            failures.append("transaction_boundary_receipt_invalid")
    elif boundary == "known_empty_439_snapshot_safe_response":
        empty_detail = (
            tools[0].get("detail")
            if len(tools) == 1 and isinstance(tools[0].get("detail"), dict)
            else {}
        )
        empty_channels = empty_detail.get("retrievalTrace", {}).get("channels", {})
        exact_empty_recall = bool(
            len(tools) == 1
            and tools[0].get("tool") == "search_products"
            and tools[0].get("ok") is False
            and empty_detail.get("code") == "product_recall_unavailable"
            and empty_detail.get("requestedCategory") == metadata.get("category")
            and task_category == metadata.get("category")
            and isinstance(empty_channels.get("elasticsearch"), dict)
            and empty_channels["elasticsearch"].get("status") == "active"
            and empty_channels["elasticsearch"].get("count") == 0
            and isinstance(empty_channels.get("bm25"), dict)
            and empty_channels["bm25"].get("status") == "active"
            and empty_channels["bm25"].get("count") == 0
        )
        if not exact_empty_recall:
            failures.append("known_empty_snapshot_receipt_invalid")
        expected_label = {"laptop": "笔记本", "headphones": "耳机"}.get(
            metadata.get("category")
        )
        answer = payload.get("answer")
        if (
            expected_label is None
            or not isinstance(answer, str)
            or expected_label not in answer
            or "没有" not in answer
        ):
            failures.append("known_empty_snapshot_answer_invalid")
    else:
        for tool in tools:
            if tool.get("tool") not in READ_ONLY_PRODUCT_TOOLS:
                failures.append(f"non_read_only_tool:{tool.get('tool')}")
            if tool.get("ok") is not True:
                failures.append(f"tool_failed:{tool.get('tool')}")
    if boundary == "out_of_scope_no_product_tools" and product_tools:
        failures.append("scope_negative_called_product_tool")
    if boundary == "transaction_exact_confirmation_boundary" and product_tools:
        failures.append("transaction_boundary_called_product_tool")
    request_trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    counts = request_trace.get("modelCallCounts") or {}
    failure_counts = request_trace.get("modelCallFailures") or {}
    react_calls = counts.get("react_decision", 0)
    if type(react_calls) is not int or react_calls < 0 or react_calls > 2:
        failures.append("react_decision_call_budget_invalid")
    if arm == "fixed_v1" and react_calls != 0:
        failures.append("fixed_arm_react_decision_called")
    if react_calls > 0 and model_name is not None and model_name not in observed_models:
        failures.append("react_decision_model_name_missing")
    if metadata.get("behaviorClass") in {"deterministic", "negative"} and react_calls != 0:
        failures.append("nonadaptive_react_decision_called")
    if not isinstance(failure_counts, dict):
        failures.append("model_call_failures_malformed")
    else:
        for stage, failed_count in failure_counts.items():
            attempted_count = counts.get(stage, 0)
            if (
                type(failed_count) is not int
                or failed_count < 0
                or type(attempted_count) is not int
                or failed_count > attempted_count
            ):
                failures.append(f"model_call_failure_count_invalid:{stage}")
            elif failed_count > 0:
                failures.append(f"model_call_failure_observed:{stage}")
    decisions = (debug or {}).get("reactDecisions") or []
    if not isinstance(decisions, list):
        failures.append("react_decisions_malformed")
        decisions = []
    for decision in decisions:
        if not isinstance(decision, dict):
            failures.append("react_decision_malformed")
            continue
        option_id = decision.get("optionId")
        published = decision.get("publishedOptionIds") or []
        if option_id is not None and option_id not in published:
            failures.append("selected_option_not_published")
        tool_name = decision.get("toolName")
        if tool_name is not None and tool_name not in READ_ONLY_PRODUCT_TOOLS:
            failures.append(f"react_selected_non_read_only_tool:{tool_name}")
    if model_name is not None:
        model_decisions = [
            decision for decision in decisions
            if isinstance(decision, dict)
            and decision.get("decisionSource") == "model"
        ]
        decision_call_ids = [
            decision.get("modelCallId") for decision in model_decisions
        ]
        event_call_ids = [event.get("modelCallId") for event in model_events]
        if any(
            decision.get("modelName") != model_name
            or not isinstance(decision.get("modelCallId"), str)
            or not decision["modelCallId"].startswith("rmc-")
            or not isinstance(decision.get("decisionBindingHash"), str)
            or len(decision["decisionBindingHash"]) != 64
            or not isinstance(decision.get("viewHash"), str)
            or type(decision.get("taskRevision")) is not int
            for decision in model_decisions
        ):
            failures.append("react_model_decision_not_exactly_bound")
        if (
            react_calls != len(model_decisions)
            or react_calls != len(model_events)
            or len(set(decision_call_ids)) != len(decision_call_ids)
            or sorted(decision_call_ids) != sorted(event_call_ids)
        ):
            failures.append("react_model_call_binding_mismatch")
        decision_by_call = {
            decision.get("modelCallId"): decision
            for decision in model_decisions
            if isinstance(decision.get("modelCallId"), str)
        }
        event_by_call = {
            event.get("modelCallId"): event
            for event in model_events
            if isinstance(event.get("modelCallId"), str)
        }
        if any(
            any((
                decision_by_call[call_id].get("decisionBindingHash")
                != event_by_call.get(call_id, {}).get("decisionBindingHash"),
                decision_by_call[call_id].get("viewHash")
                != event_by_call.get(call_id, {}).get("decisionViewHash"),
                decision_by_call[call_id].get("taskRevision")
                != event_by_call.get(call_id, {}).get("decisionTaskRevision"),
                decision_by_call[call_id].get("actionId")
                != event_by_call.get(call_id, {}).get("decisionActionId"),
                decision_by_call[call_id].get("actionKind")
                != event_by_call.get(call_id, {}).get("decisionActionKind"),
                decision_by_call[call_id].get("toolName")
                != event_by_call.get(call_id, {}).get("toolName"),
                decision_by_call[call_id].get("errorCode")
                != event_by_call.get(call_id, {}).get("decisionErrorCode"),
            ))
            for call_id in decision_by_call
        ):
            failures.append("react_model_call_binding_mismatch")
    return sorted(set(failures))


def _scenario_safety(
    *,
    arm: str,
    metadata: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
) -> list[str]:
    failures = sorted({
        failure
        for receipt in receipts
        for failure in receipt.get("safetyFailures", [])
    })
    revisions = [
        receipt.get("taskRevision")
        for receipt in receipts
        if type(receipt.get("taskRevision")) is int
    ]
    if len(revisions) != len(receipts):
        failures.append("task_revision_missing")
    elif any(current < previous for previous, current in zip(revisions, revisions[1:])):
        failures.append("task_revision_not_monotonic")
    for index, receipt in enumerate(receipts):
        pre_state = receipt.get("preRequestTaskState")
        if not isinstance(pre_state, dict):
            failures.append("pre_request_task_state_missing")
            continue
        if index == 0:
            if (
                pre_state.get("kind") != "NEW_SESSION_EMPTY"
                or pre_state.get("revision") is not None
                or pre_state.get("taskStateSha256") != _hash_value({})
            ):
                failures.append("initial_task_state_contract_mismatch")
            continue
        previous = receipts[index - 1]
        previous_hashes = previous.get("receiptHashes") or {}
        if (
            pre_state.get("kind") != "PRIOR_RESPONSE_STATE"
            or pre_state.get("revision") != previous.get("taskRevision")
            or pre_state.get("taskStateSha256")
            != previous_hashes.get("taskStateSha256")
        ):
            failures.append("pre_request_task_state_chain_broken")
    if arm == "react_v1" and metadata.get("behaviorClass") == "adaptive":
        expected = (metadata.get("triggerContract") or {}).get("adaptiveTrigger")
        decisions = [
            decision
            for receipt in receipts
            for decision in (receipt.get("reactDecisions") or [])
            if isinstance(decision, dict)
        ]
        observed = [decision.get("adaptiveTrigger") for decision in decisions]
        if expected not in observed:
            failures.append(f"adaptive_trigger_not_observed:{expected}")
        accepted_expected = [
            decision
            for decision in decisions
            if decision.get("adaptiveTrigger") == expected
            and decision.get("status") == "accepted"
            and decision.get("optionId") in (
                decision.get("publishedOptionIds") or []
            )
        ]
        if not accepted_expected:
            failures.append("adaptive_model_decision_not_accepted")
        total_calls = sum(
            int((receipt.get("modelCallCounts") or {}).get("react_decision", 0))
            for receipt in receipts
        )
        minimum = int((metadata.get("expectedTrace") or {})
                      .get("reactDecisionCalls", {}).get("min", 1))
        if total_calls < minimum:
            failures.append("adaptive_model_decision_missing")
    return sorted(set(failures))


async def _run_arm(
    *,
    arm: str,
    base_url: str,
    scenarios: Sequence[Mapping[str, Any]],
    metadata_by_id: Mapping[str, Mapping[str, Any]],
    project_sut_input: Any,
    debug_key: str,
    model_name: str,
    timeout_seconds: float,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    receipts_path = output_dir / "receipts.jsonl"
    run_tag = uuid.uuid4().hex[:16]
    durations: list[float] = []
    scenario_results: list[dict[str, Any]] = []
    call_totals: Counter[str] = Counter()
    failure_totals: Counter[str] = Counter()
    duration_totals: Counter[str] = Counter()
    context_tokens: list[int] = []
    decision_view_tokens: list[int] = []

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        health = await _observe_health(client, base_url)
        with receipts_path.open("x", encoding="utf-8", newline="\n") as sink:
            for scenario in scenarios:
                scenario_id = str(scenario["scenarioId"])
                metadata = metadata_by_id[scenario_id]
                turn_texts = project_sut_input(dict(scenario))
                session_id = f"rv1-{run_tag}-{uuid.uuid4().hex[:12]}"
                pending_resume: dict[str, Any] | None = None
                previous_task_state: dict[str, Any] | None = None
                turn_receipts: list[dict[str, Any]] = []
                for turn_index, message in enumerate(turn_texts, start=1):
                    request_body: dict[str, Any] = {
                        "message": message,
                        "sessionId": session_id,
                        "domainHint": "ecommerce",
                    }
                    request_kind = "fresh_turn"
                    if pending_resume is not None:
                        request_body["resume"] = {**pending_resume, "answer": message}
                        request_kind = "clarification_resume"
                    started = time.perf_counter()
                    response = await client.post(
                        f"{base_url}/agent/chat-llm-durable", json=request_body
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("chat response must be an object")
                    run_id = payload.get("runId")
                    debug_fetch_error: str | None = None
                    debug = None
                    if isinstance(run_id, str) and run_id:
                        try:
                            debug = await _fetch_debug_trace(
                                client,
                                base_url=base_url,
                                run_id=run_id,
                                debug_key=debug_key,
                            )
                        except (httpx.HTTPError, ValueError) as exc:
                            debug_fetch_error = type(exc).__name__
                    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
                    durations.append(elapsed_ms)
                    request_trace = payload.get("trace") or {}
                    counts = dict(request_trace.get("modelCallCounts") or {})
                    failure_counts = dict(
                        request_trace.get("modelCallFailures") or {}
                    )
                    stage_durations = dict(request_trace.get("llmDurationByStageMs") or {})
                    call_totals.update({str(key): int(value) for key, value in counts.items()})
                    failure_totals.update({
                        str(key): int(value) for key, value in failure_counts.items()
                    })
                    duration_totals.update({
                        str(key): float(value) for key, value in stage_durations.items()
                    })
                    if debug is not None and type(debug.get("contextTokenCount")) is int:
                        context_tokens.append(debug["contextTokenCount"])
                    for decision in (debug or {}).get("reactDecisions") or []:
                        if isinstance(decision, dict) and type(decision.get("viewTokenCount")) is int:
                            decision_view_tokens.append(decision["viewTokenCount"])
                    state = payload.get("taskState") or {}
                    failures = _turn_safety(
                        arm=arm,
                        metadata=metadata,
                        payload=payload,
                        debug=debug,
                        model_name=model_name,
                    )
                    if debug_fetch_error is not None:
                        failures.append("debug_trace_fetch_failed")
                        failures = sorted(set(failures))
                    receipt = {
                        "schemaVersion": RECEIPT_SCHEMA,
                        "arm": arm,
                        "scenarioId": scenario_id,
                        "turnIndex": turn_index,
                        "requestKind": request_kind,
                        "sutProjection": "turn_text_only_v1",
                        "projectedTurnSha256": hashlib.sha256(
                            message.encode("utf-8")
                        ).hexdigest(),
                        "requestShape": sorted(request_body),
                        "requestId": request_trace.get("requestId"),
                        "runId": run_id,
                        "taskId": state.get("taskId") if isinstance(state, dict) else None,
                        "taskRevision": state.get("revision") if isinstance(state, dict) else None,
                        "taskCategory": (
                            state.get("domainState", {})
                            .get("shoppingGuide", {})
                            .get("category")
                            if isinstance(state, dict)
                            else None
                        ),
                        "controlPolicy": (debug or {}).get("controlPolicy"),
                        "policyRevision": (debug or {}).get("policyRevision"),
                        "enteredRuntime": (debug or {}).get("enteredRuntime"),
                        "finalAction": (debug or {}).get("finalAction"),
                        "degraded": (debug or {}).get("degraded"),
                        "debugFetchError": debug_fetch_error,
                        "modelCallCounts": counts,
                        "modelCallFailures": failure_counts,
                        "llmDurationByStageMs": stage_durations,
                        "contextTokenCount": (debug or {}).get("contextTokenCount"),
                        "reactDecisions": (debug or {}).get("reactDecisions") or [],
                        "reactOutcomes": (debug or {}).get("reactOutcomes") or [],
                        "graphV2Events": (debug or {}).get("graphV2Events") or [],
                        "toolTrace": _tool_traces(payload),
                        "answer": payload.get("answer"),
                        "runnerDurationMs": elapsed_ms,
                        "safetyFailures": failures,
                        "receiptHashes": _receipt_hashes(payload, debug or {}),
                        "preRequestTaskState": (
                            {
                                "kind": "NEW_SESSION_EMPTY",
                                "revision": None,
                                "taskStateSha256": _hash_value({}),
                            }
                            if previous_task_state is None
                            else {
                                "kind": "PRIOR_RESPONSE_STATE",
                                "revision": previous_task_state.get("revision"),
                                "taskStateSha256": _hash_value(previous_task_state),
                            }
                        ),
                    }
                    sink.write(json.dumps(receipt, ensure_ascii=False) + "\n")
                    sink.flush()
                    turn_receipts.append(receipt)
                    previous_task_state = dict(state) if isinstance(state, dict) else None
                    summary = payload.get("traceSummary") or {}
                    resume = summary.get("durableResume") if isinstance(summary, dict) else None
                    pending_resume = dict(resume) if isinstance(resume, dict) else None
                scenario_failures = _scenario_safety(
                    arm=arm, metadata=metadata, receipts=turn_receipts
                )
                scenario_results.append({
                    "scenarioId": scenario_id,
                    "behaviorClass": metadata.get("behaviorClass"),
                    "status": "ACCEPT" if not scenario_failures else "HOLD",
                    "failures": scenario_failures,
                    "pendingClarificationAtEnd": pending_resume is not None,
                    "answerHashes": [
                        hashlib.sha256(str(item.get("answer", "")).encode("utf-8")).hexdigest()
                        for item in turn_receipts
                    ],
                })

    failures = [
        {"scenarioId": item["scenarioId"], "failures": item["failures"]}
        for item in scenario_results if item["status"] == "HOLD"
    ]
    manifest = {
        "schemaVersion": RUN_SCHEMA,
        "arm": arm,
        "controlPolicy": arm,
        "policyRevision": POLICY_REVISIONS[arm],
        "health": health,
        "scenarioCount": len(scenarios),
        "turnCount": sum(len(project_sut_input(dict(item))) for item in scenarios),
        "status": "ACCEPT" if not failures else "HOLD",
        "scenarioFailures": failures,
        "modelCallCounts": dict(call_totals),
        "modelCallFailures": dict(failure_totals),
        "modelCallFailureTotal": sum(failure_totals.values()),
        "llmDurationByStageMs": {
            key: round(value, 3) for key, value in duration_totals.items()
        },
        "latencyMs": _latency_summary(durations),
        "contextTokenCount": _latency_summary(context_tokens),
        "reactDecisionViewTokenCount": _latency_summary(decision_view_tokens),
        "providerTokenUsage": {
            "status": "HOLD_UNAVAILABLE",
            "reason": "provider completion usage is not exposed by the current Agent trace",
        },
        "receipts": "receipts.jsonl",
        "receiptsSha256": _sha256(receipts_path),
        "scenarios": scenario_results,
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def _normalize_answer(value: str) -> str:
    return re.sub(r"\s+", "", value).strip().lower()


def _behavior_difference_candidates(
    fixed_receipts: Sequence[Mapping[str, Any]],
    react_receipts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    fixed_by_key = {
        (item["scenarioId"], item["turnIndex"]): item for item in fixed_receipts
    }
    react_by_key = {
        (item["scenarioId"], item["turnIndex"]): item for item in react_receipts
    }
    candidates: list[dict[str, Any]] = []
    for key in sorted(set(fixed_by_key) & set(react_by_key)):
        fixed = fixed_by_key[key]
        react = react_by_key[key]
        fixed_answer = _normalize_answer(str(fixed.get("answer", "")))
        react_answer = _normalize_answer(str(react.get("answer", "")))
        action_changed = fixed.get("finalAction") != react.get("finalAction")
        tools_changed = [item.get("tool") for item in fixed.get("toolTrace", [])] != [
            item.get("tool") for item in react.get("toolTrace", [])
        ]
        answer_changed = fixed_answer != react_answer
        if action_changed or tools_changed or answer_changed:
            candidates.append({
                "scenarioId": key[0],
                "turnIndex": key[1],
                "answerChanged": answer_changed,
                "finalActionChanged": action_changed,
                "toolSequenceChanged": tools_changed,
                "fixedAnswerSha256": hashlib.sha256(fixed_answer.encode("utf-8")).hexdigest(),
                "reactAnswerSha256": hashlib.sha256(react_answer.encode("utf-8")).hexdigest(),
            })
    return candidates


def _paired_gate_decision(
    *,
    both_safe: bool,
    has_behavior_differences: bool,
    full_suite: bool,
) -> dict[str, Any]:
    if not both_safe:
        return {
            "status": "HOLD",
            "blindPackEligible": False,
            "decision": (
                "HOLD_BEFORE_BLIND_PACK"
                if full_suite
                else "HOLD_BEFORE_FULL_RUN"
            ),
        }
    if not full_suite:
        return {
            "status": "ACCEPT",
            "blindPackEligible": False,
            "decision": "ACCEPT_FOR_FULL_RUN",
        }
    if not has_behavior_differences:
        return {
            "status": "HOLD",
            "blindPackEligible": False,
            "decision": "HOLD_BEFORE_BLIND_PACK",
        }
    return {
        "status": "ACCEPT",
        "blindPackEligible": True,
        "decision": "ACCEPT_FOR_BLIND_PACK",
    }


def _load_receipts(path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(path)


async def run_paired(
    *,
    package_dir: Path,
    fixed_url: str,
    react_url: str,
    output_dir: Path,
    debug_key: str,
    model_name: str,
    timeout_seconds: float,
    launch_receipt_path: Path,
    scenario_ids: set[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    package_validation = _validate_author_package(package_dir)
    scenarios = _read_jsonl(package_dir / "public" / "scenarios.jsonl")
    package_scenario_ids = [str(item["scenarioId"]) for item in scenarios]
    metadata = _read_jsonl(package_dir / "runner_metadata.jsonl")
    invariants = _read_jsonl(package_dir / "public" / "data_invariants.jsonl")
    if scenario_ids:
        scenarios = [item for item in scenarios if item["scenarioId"] in scenario_ids]
    if limit is not None:
        scenarios = scenarios[:limit]
    if not scenarios:
        raise ValueError("no scenarios selected")
    metadata_by_id = {item["scenarioId"]: item for item in metadata}
    if any(item["scenarioId"] not in metadata_by_id for item in scenarios):
        raise ValueError("scenario metadata missing")
    projection = _load_projection(package_dir)
    data_verification = _reverify_data_invariants(package_dir, invariants)
    source_manifest = _source_manifest(package_dir)
    launch_receipt = _load_launch_receipt(
        launch_receipt_path,
        fixed_url=fixed_url,
        react_url=react_url,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
    )
    selected_ids = [item["scenarioId"] for item in scenarios]
    full_suite = (
        len(selected_ids) == len(package_scenario_ids)
        and set(selected_ids) == set(package_scenario_ids)
    )
    expected_mode = "full" if full_suite else "smoke"
    if launch_receipt["content"].get("mode") != expected_mode:
        raise ValueError("launch receipt coverage mode mismatch")
    binding = {
        "schemaVersion": "react-v1-architecture-24-run-binding-v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "gitHead": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip(),
        "workingTreePolicy": "PROTECTED_DIRTY_NO_DESTRUCTIVE_GIT",
        "packageDir": package_dir.relative_to(REPO_ROOT).as_posix(),
        "packageValidation": package_validation,
        "packageFilesSha256": {
            path.relative_to(package_dir).as_posix(): _sha256(path)
            for path in sorted(package_dir.rglob("*")) if path.is_file()
        },
        "selectedScenarioIds": selected_ids,
        "sutProjection": "turn_text_only_v1",
        "sutProjectionSha256": _sha256(package_dir / "sut_projection.py"),
        "dataVerification": data_verification,
        "sourceManifest": source_manifest,
        "launchReceipt": launch_receipt,
        "toolProfile": {
            "effectClass": "READ_ONLY",
            "allowedTools": sorted(READ_ONLY_PRODUCT_TOOLS),
            "sha256": _hash_value(sorted(READ_ONLY_PRODUCT_TOOLS)),
        },
        "modelConfig": {
            "model": model_name,
            "temperature": "provider_default",
            "seed": "not_set_provider_not_bound",
            "sha256": _hash_value({
                "model": model_name,
                "temperature": "provider_default",
                "seed": "not_set_provider_not_bound",
            }),
        },
        "sharedLimits": {
            "requestDeadlineSeconds": 45.0,
            "reactDecisionTimeoutSeconds": 40.0,
            "reactFinalAnswerTimeoutSeconds": 30.0,
            "maxTransitions": 8,
            "maxReactModelDecisionsPerUserTurn": 2,
            "httpTimeoutSeconds": timeout_seconds,
        },
        "initialTaskStateContract": {
            "kind": "NEW_SESSION_EMPTY",
            "taskState": {},
            "taskStateSha256": _hash_value({}),
            "sameSemanticInitialStateBothArms": True,
        },
        "arms": {
            "fixed": {
                "controlPolicy": "fixed_v1",
                "policyRevision": POLICY_REVISIONS["fixed_v1"],
                "durableEnabled": True,
                "reactLiveEnabled": False,
            },
            "react": {
                "controlPolicy": "react_v1",
                "policyRevision": POLICY_REVISIONS["react_v1"],
                "durableEnabled": True,
                "reactLiveEnabled": True,
            },
        },
        "declaredArmDifference": (
            "controlPolicy plus the react_v1-required live gate; durable substrate, "
            "model, tools, data, limits, and scenario projection are shared"
        ),
        "debugCredentialPersisted": False,
        "privateOracleRead": False,
        "sealedMappingRead": False,
    }
    binding["sharedLimits"]["sha256"] = _hash_value(binding["sharedLimits"])
    binding["bindingSha256"] = _hash_value(binding)
    _write_json(output_dir / "run_binding.json", binding)

    fixed_manifest = await _run_arm(
        arm="fixed_v1",
        base_url=fixed_url.rstrip("/"),
        scenarios=scenarios,
        metadata_by_id=metadata_by_id,
        project_sut_input=projection,
        debug_key=debug_key,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        output_dir=output_dir / "fixed",
    )
    react_manifest = await _run_arm(
        arm="react_v1",
        base_url=react_url.rstrip("/"),
        scenarios=scenarios,
        metadata_by_id=metadata_by_id,
        project_sut_input=projection,
        debug_key=debug_key,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        output_dir=output_dir / "react",
    )
    differences = _behavior_difference_candidates(
        _load_receipts(output_dir / "fixed" / "receipts.jsonl"),
        _load_receipts(output_dir / "react" / "receipts.jsonl"),
    )
    both_safe = fixed_manifest["status"] == react_manifest["status"] == "ACCEPT"
    gate = _paired_gate_decision(
        both_safe=both_safe,
        has_behavior_differences=bool(differences),
        full_suite=full_suite,
    )
    result = {
        "schemaVersion": RUN_SCHEMA,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "bindingSha256": binding["bindingSha256"],
        "fixedManifestSha256": _sha256(output_dir / "fixed" / "manifest.json"),
        "reactManifestSha256": _sha256(output_dir / "react" / "manifest.json"),
        "coverage": "full_suite" if full_suite else "partial_smoke",
        "selectedScenarioCount": len(selected_ids),
        "packageScenarioCount": len(package_scenario_ids),
        "fixedStatus": fixed_manifest["status"],
        "reactStatus": react_manifest["status"],
        "bothArmsSafetyAccept": both_safe,
        "behaviorDifferenceCandidates": differences,
        **gate,
    }
    _write_json(output_dir / "paired_result.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--fixed-url", required=True)
    parser.add_argument("--react-url", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--launch-receipt", type=Path, required=True)
    parser.add_argument("--debug-key-env", default="AGENT_TRACE_DEBUG_KEY")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--scenario-id", action="append")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    debug_key = os.environ.get(args.debug_key_env, "")
    if not debug_key:
        raise SystemExit(f"missing debug key env: {args.debug_key_env}")
    result = asyncio.run(run_paired(
        package_dir=args.package_dir.resolve(),
        fixed_url=args.fixed_url,
        react_url=args.react_url,
        output_dir=args.output_dir.resolve(),
        debug_key=debug_key,
        model_name=args.model_name,
        timeout_seconds=args.timeout_seconds,
        launch_receipt_path=args.launch_receipt.resolve(),
        scenario_ids=set(args.scenario_id or []),
        limit=args.limit,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ACCEPT":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
