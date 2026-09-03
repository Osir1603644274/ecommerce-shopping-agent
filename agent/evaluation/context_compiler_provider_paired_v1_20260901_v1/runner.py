"""Provider-observed CTX1a/CTX1b paired evaluation.

The public 24-scenario/65-turn corpus is expanded into frozen turn cases before
execution.  Each arm sends the same deterministic fidelity task to the same
provider; only the compiled context history policy differs.  There are no
retries and an existing attempt directory is never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.context_compiler_v1 import (
    RunContextV1,
    canonical_json,
    compile_context_v1,
    context_items_from_pack,
    sha256_json,
)
from app.settings import settings


ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PACKAGE_DIR / "manifest.json"
DEFAULT_OUTPUT = PACKAGE_DIR / "attempt001"
HASH_A = "a" * 64
HASH_B = "b" * 64
ARMS = ("CTX1a", "CTX1b")
PROTECTED_FIELDS = (
    "goal",
    "confirmedFacts",
    "hardConstraints",
    "candidateScopeState",
    "evidenceRefs",
)


class Pack:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.run_id = payload["runId"]
        self.candidate_scope_state = payload.get("candidateScopeState")

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self.payload)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(payload) + "\n")
        handle.flush()


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def _run_context(case: dict[str, Any], arm: str) -> RunContextV1:
    payload = case["contextPayload"]
    scope = payload["candidateScopeState"]
    return RunContextV1(
        runId=f"{payload['runId']}-{arm.casefold()}",
        parentRunId=None,
        handoffId=None,
        tenantId="tenant-context-provider-public",
        ownerId="owner-context-provider-public",
        sessionId=f"session-{case['scenarioId']}",
        recipientType="SELF",
        recipientId="owner-context-provider-public",
        taskId=payload["taskId"],
        taskRevision=payload["baseContextRevision"],
        agentRole="SHOPPING_AGENT",
        phase="SHOPPING_FINAL_ANSWER",
        modelCallOrdinal=0 if arm == "CTX1a" else 1,
        candidateScopeId=scope["scopeId"],
        candidateScopeSourceRevision=scope["sourceRevision"],
        candidateScopeHash=sha256_json(scope),
        deadlineAt=datetime.now(timezone.utc) + timedelta(hours=1),
        compilerVersion="context-compiler-v1",
        policyVersion=(
            "context-policy-semantic-preserve-v1"
            if arm == "CTX1a"
            else "context-policy-query-focused-v1"
        ),
        capabilityGrantId="shopping-agent-read-v1",
        capabilityGrantHash=HASH_A,
        sensitivity="SERVER_ONLY",
    )


def compile_case(case: dict[str, Any], arm: str) -> Any:
    if arm not in ARMS:
        raise ValueError(f"unsupported arm: {arm}")
    payload = json.loads(canonical_json(case["contextPayload"]))
    pack = Pack(payload)
    run = _run_context(case, arm)
    return compile_context_v1(
        run,
        context_items_from_pack(pack, run),
        budget_tokens=20_000,
        tool_schema_hash=HASH_A,
        model_config_hash=HASH_B,
        history_policy="preserve" if arm == "CTX1a" else "query_focused",
        query=case["query"],
    )


def deterministic_preflight(
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "FROZEN_BEFORE_EXECUTION":
        raise RuntimeError("manifest is not frozen before execution")
    for relative, expected in manifest["sourceFreeze"].items():
        actual = file_hash(ROOT / relative)
        if actual != expected:
            raise RuntimeError(
                f"source hash mismatch for {relative}: expected {expected}, got {actual}"
            )
    scenarios_path = ROOT / manifest["dataset"]["path"]
    if file_hash(scenarios_path) != manifest["dataset"]["sha256"]:
        raise RuntimeError("scenario hash mismatch")
    cases = load_jsonl(scenarios_path)
    if len(cases) != manifest["dataset"]["turnCaseCount"]:
        raise RuntimeError("scenario count mismatch")

    protected_equal = 0
    recovery_equal = 0
    reduced_cases = 0
    estimates: dict[str, list[int]] = {arm: [] for arm in ARMS}
    for case in cases:
        compiled = {arm: compile_case(case, arm) for arm in ARMS}
        if all(
            compiled["CTX1a"].model_view.get(field)
            == compiled["CTX1b"].model_view.get(field)
            for field in PROTECTED_FIELDS
        ):
            protected_equal += 1
        if (
            compiled["CTX1b"].receipt.estimated_tokens
            < compiled["CTX1a"].receipt.estimated_tokens
        ):
            reduced_cases += 1
        for arm in ARMS:
            estimates[arm].append(compiled[arm].receipt.estimated_tokens)
            replay = compile_case(json.loads(canonical_json(case)), arm)
            if (
                replay.model_view == compiled[arm].model_view
                and replay.receipt.semantic_hash == compiled[arm].receipt.semantic_hash
            ):
                recovery_equal += 1
    return {
        "status": "PASS" if (
            protected_equal == len(cases)
            and recovery_equal == len(cases) * len(ARMS)
            and reduced_cases > 0
        ) else "FAIL",
        "turnCaseCount": len(cases),
        "protectedFieldEqualityCount": protected_equal,
        "compileReplayRecoveryCount": recovery_equal,
        "compileReplayRecoveryExpected": len(cases) * len(ARMS),
        "treatmentReductionCaseCount": reduced_cases,
        "estimatedTokensP50": {
            arm: percentile([float(value) for value in estimates[arm]], 0.5)
            for arm in ARMS
        },
    }


FIDELITY_TOOL = {
    "type": "function",
    "function": {
        "name": "record_context_fidelity",
        "description": "Copy the requested fields exactly from the supplied context.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "currentGoal",
                "candidateIds",
                "evidenceRefs",
                "recentReference",
            ],
            "properties": {
                "currentGoal": {"type": "string"},
                "candidateIds": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "evidenceRefs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "recentReference": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                },
            },
        },
    },
}

SYSTEM_PROMPT = """You are a deterministic context-fidelity checker.
Call record_context_fidelity exactly once. Copy currentGoal, candidateIds and
evidenceRefs exactly from goal, candidateScopeState.rankedItemIds and
evidenceRefs. If referenceQuery is true and historySummaries is non-empty,
copy the most recent user summary into recentReference; otherwise return null.
Do not infer, translate, reorder, add or omit values."""


def _parse_tool_arguments(response: Any) -> dict[str, Any]:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("provider returned no choices")
    calls = getattr(choices[0].message, "tool_calls", None) or []
    matching = [
        call
        for call in calls
        if call.function.name == "record_context_fidelity"
    ]
    if len(matching) != 1:
        raise RuntimeError("provider did not call record_context_fidelity exactly once")
    return json.loads(matching[0].function.arguments)


def _usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    prompt = getattr(usage, "prompt_tokens", None) if usage is not None else None
    completion = (
        getattr(usage, "completion_tokens", None) if usage is not None else None
    )
    total = getattr(usage, "total_tokens", None) if usage is not None else None
    cached = getattr(usage, "prompt_cache_hit_tokens", None) if usage is not None else None
    if cached is None and usage is not None:
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details is not None else None
    return {
        "promptTokens": int(prompt) if isinstance(prompt, int) else None,
        "completionTokens": int(completion) if isinstance(completion, int) else None,
        "totalTokens": int(total) if isinstance(total, int) else None,
        "cachedInputTokens": int(cached) if isinstance(cached, int) else None,
    }


def expected_output(case: dict[str, Any]) -> dict[str, Any]:
    payload = case["contextPayload"]
    return {
        "currentGoal": payload["goal"],
        "candidateIds": payload["candidateScopeState"]["rankedItemIds"],
        "evidenceRefs": payload["evidenceRefs"],
        "recentReference": case["expectedRecentReference"],
    }


def exact_fidelity(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return actual == expected


async def _call_provider(
    client: AsyncOpenAI,
    *,
    model: str,
    max_tokens: int,
    case: dict[str, Any],
    arm: str,
    model_view: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": canonical_json({
                    "caseId": case["caseId"],
                    "referenceQuery": case["referenceQuery"],
                    "context": model_view,
                }),
            },
        ],
        tools=[FIDELITY_TOOL],
        tool_choice={
            "type": "function",
            "function": {"name": "record_context_fidelity"},
        },
        temperature=0,
        max_tokens=max_tokens,
    )
    duration_ms = (time.perf_counter() - started) * 1000.0
    actual = _parse_tool_arguments(response)
    expected = expected_output(case)
    return {
        "schemaVersion": "context-compiler-provider-trace-v1",
        "caseId": case["caseId"],
        "scenarioId": case["scenarioId"],
        "turnId": case["turnId"],
        "arm": arm,
        "status": "SUCCEEDED",
        "durationMs": duration_ms,
        "usage": _usage(response),
        "expected": expected,
        "actual": actual,
        "exactFidelity": exact_fidelity(actual, expected),
    }


def _aggregate(traces: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    rows = [row for row in traces if row["arm"] == arm]
    succeeded = [row for row in rows if row["status"] == "SUCCEEDED"]
    observed = [
        row for row in succeeded
        if row["usage"]["promptTokens"] is not None
        and row["usage"]["completionTokens"] is not None
    ]
    latencies = [float(row["durationMs"]) for row in succeeded]
    prompts = [float(row["usage"]["promptTokens"]) for row in observed]
    completions = [float(row["usage"]["completionTokens"]) for row in observed]
    return {
        "caseCount": len(rows),
        "succeeded": len(succeeded),
        "exactFidelity": sum(bool(row.get("exactFidelity")) for row in rows),
        "usageObserved": len(observed),
        "promptTokensSum": int(sum(prompts)),
        "promptTokensP50": percentile(prompts, 0.5),
        "promptTokensP95": percentile(prompts, 0.95),
        "completionTokensSum": int(sum(completions)),
        "latencyP50Ms": percentile(latencies, 0.5),
        "latencyP95Ms": percentile(latencies, 0.95),
    }


async def execute(
    manifest_path: Path,
    output_dir: Path,
    attempt_id: str,
) -> int:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    preflight = deterministic_preflight(manifest_path)
    if preflight["status"] != "PASS":
        raise RuntimeError("deterministic preflight failed")
    if attempt_id != manifest["attemptId"]:
        raise RuntimeError("attempt id differs from frozen manifest")
    if output_dir.exists():
        raise RuntimeError("attempt output already exists; refusing overwrite")
    if not settings.deepseek_api_key:
        raise RuntimeError("DeepSeek API key is not configured")
    if settings.deepseek_model != manifest["provider"]["model"]:
        raise RuntimeError("configured model differs from frozen manifest")

    output_dir.mkdir(parents=True, exist_ok=False)
    attempt_path = output_dir / "attempt.json"
    started = {
        "schemaVersion": "context-compiler-provider-attempt-v1",
        "attemptId": attempt_id,
        "status": "RUNNING",
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "runnerSha256": file_hash(Path(__file__)),
        "compilerSha256": file_hash(ROOT / "agent/app/context_compiler_v1.py"),
    }
    attempt_path.write_text(
        json.dumps(started, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    cases = load_jsonl(ROOT / manifest["dataset"]["path"])
    trace_path = output_dir / "traces.jsonl"
    rng = random.Random(manifest["randomSeed"])
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=float(manifest["provider"]["timeoutSeconds"]),
        max_retries=0,
    )
    traces: list[dict[str, Any]] = []
    for case in cases:
        order = list(ARMS)
        rng.shuffle(order)
        for arm in order:
            compiled = compile_case(case, arm)
            try:
                trace = await _call_provider(
                    client,
                    model=manifest["provider"]["model"],
                    max_tokens=int(manifest["provider"]["maxTokens"]),
                    case=case,
                    arm=arm,
                    model_view=compiled.model_view,
                )
            except Exception as exc:
                trace = {
                    "schemaVersion": "context-compiler-provider-trace-v1",
                    "caseId": case["caseId"],
                    "scenarioId": case["scenarioId"],
                    "turnId": case["turnId"],
                    "arm": arm,
                    "status": "FAILED",
                    "errorCode": type(exc).__name__,
                    "usage": {
                        "promptTokens": None,
                        "completionTokens": None,
                        "totalTokens": None,
                        "cachedInputTokens": None,
                    },
                    "exactFidelity": False,
                }
            traces.append(trace)
            append_jsonl(trace_path, trace)

    aggregate = {arm: _aggregate(traces, arm) for arm in ARMS}
    expected_calls = len(cases) * len(ARMS)
    all_calls_succeeded = sum(
        row["status"] == "SUCCEEDED" for row in traces
    ) == expected_calls
    all_usage_observed = all(
        aggregate[arm]["usageObserved"] == len(cases) for arm in ARMS
    )
    all_fidelity_exact = all(
        aggregate[arm]["exactFidelity"] == len(cases) for arm in ARMS
    )
    provider_reduction = (
        all_usage_observed
        and aggregate["CTX1b"]["promptTokensSum"]
        < aggregate["CTX1a"]["promptTokensSum"]
    )
    accepted = (
        preflight["status"] == "PASS"
        and all_calls_succeeded
        and all_usage_observed
        and all_fidelity_exact
        and provider_reduction
    )
    summary = {
        "schemaVersion": "context-compiler-provider-summary-v1",
        "attemptId": attempt_id,
        "status": "COMPLETE",
        "verdict": (
            "BOUNDED_CONTEXT_PROVIDER_ACCEPT"
            if accepted
            else "HOLD_CONTEXT_PROVIDER_PAIR"
        ),
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "public_context_fidelity_only_not_ecommerce_task_success",
        "scenarioCount": manifest["dataset"]["scenarioCount"],
        "turnCaseCount": len(cases),
        "expectedProviderCalls": expected_calls,
        "actualProviderCalls": len(traces),
        "noRetries": True,
        "preflight": preflight,
        "aggregate": aggregate,
        "gates": {
            "allCallsSucceeded": all_calls_succeeded,
            "allUsageObserved": all_usage_observed,
            "allFidelityExact": all_fidelity_exact,
            "treatmentProviderPromptTokensLower": provider_reduction,
            "compileReplayRecoveryExact": (
                preflight["compileReplayRecoveryCount"]
                == preflight["compileReplayRecoveryExpected"]
            ),
        },
        "latencyRole": "descriptive_not_a_superiority_gate",
        "productionDefaultsChanged": False,
        "taskSuccessMeasured": False,
        "resumeClaimAllowed": "bounded_context_fidelity_and_compile_replay_only",
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    completed = dict(started)
    completed.update({
        "status": "COMPLETE",
        "finishedAt": summary["completedAt"],
        "verdict": summary["verdict"],
        "tracesSha256": file_hash(trace_path),
        "summarySha256": file_hash(summary_path),
    })
    attempt_path.write_text(
        json.dumps(completed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "schemaVersion": "context-compiler-provider-receipt-v1",
        "attemptId": attempt_id,
        "verdict": summary["verdict"],
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "attemptSha256": file_hash(attempt_path),
        "tracesSha256": file_hash(trace_path),
        "summarySha256": file_hash(summary_path),
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checksum_rows = [
        f"{file_hash(path)}  {path.name}"
        for path in (attempt_path, trace_path, summary_path, receipt_path)
    ]
    (output_dir / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_rows) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if accepted else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--attempt-id",
        default="context-compiler-provider-paired-v1-20260901-attempt001",
    )
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    manifest = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    if args.preflight:
        print(
            json.dumps(
                deterministic_preflight(manifest),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return asyncio.run(execute(manifest, output, args.attempt_id))


if __name__ == "__main__":
    raise SystemExit(main())

