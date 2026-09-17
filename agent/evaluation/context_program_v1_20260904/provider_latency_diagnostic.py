"""P3 diagnostic replay of the frozen V4 provider pair under current source."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.evaluation.context_compiler_provider_paired_v1_20260901_v4 import (
    runner as v4,
)


PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[2]
V4 = ROOT / "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4"
ARMS = tuple(v4.ARMS)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rows() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (V4 / "scenarios.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def percentile(values: list[float], q: float) -> float | None:
    return v4.base.percentile(values, q) if values else None


def aggregate(traces: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    rows = [row for row in traces if row["arm"] == arm]
    succeeded = [row for row in rows if row["status"] == "SUCCEEDED"]
    usage = [
        row for row in succeeded
        if all(row["usage"].get(key) is not None for key in (
            "promptTokens", "completionTokens", "totalTokens"
        ))
    ]
    durations = [float(row["durationMs"]) for row in succeeded]
    return {
        "callCount": len(rows),
        "succeeded": len(succeeded),
        "exactFidelity": sum(bool(row.get("exactFidelity")) for row in rows),
        "usageObserved": len(usage),
        "promptTokensSum": sum(int(row["usage"]["promptTokens"]) for row in usage),
        "completionTokensSum": sum(int(row["usage"]["completionTokens"]) for row in usage),
        "totalTokensSum": sum(int(row["usage"]["totalTokens"]) for row in usage),
        "cachedInputTokensSum": sum(
            int(row["usage"].get("cachedInputTokens") or 0) for row in usage
        ),
        "latencyMeanMs": sum(durations) / len(durations) if durations else None,
        "latencyP50Ms": percentile(durations, 0.5),
        "latencyP95Ms": percentile(durations, 0.95),
    }


def preflight(repetitions: int) -> dict[str, Any]:
    if repetitions < 1 or repetitions > 5:
        raise RuntimeError("repetitions must be between 1 and 5")
    rows = load_rows()
    valid = 0
    for case in rows:
        v4.validate_reference_state(case)
        for arm in ARMS:
            v4.compile_case(case, arm)
        valid += 1
    legacy = v4.deterministic_preflight(V4 / "manifest.json")
    return {
        "status": "PASS" if len(rows) == 69 and valid == 69 and legacy["status"] == "PASS" else "FAIL",
        "caseCount": len(rows),
        "repetitions": repetitions,
        "plannedProviderCalls": len(rows) * len(ARMS) * repetitions,
        "legacyDeterministicPreflight": legacy["status"],
        "deepseekKeyConfigured": bool(v4.base.settings.deepseek_api_key),
        "model": v4.base.settings.deepseek_model,
        "automaticRetries": 0,
    }


async def execute(output: Path, repetitions: int) -> int:
    check = preflight(repetitions)
    if check["status"] != "PASS" or not check["deepseekKeyConfigured"]:
        raise RuntimeError(f"preflight failed: {check}")
    if output.exists():
        raise RuntimeError(f"refusing to overwrite {output}")
    output.mkdir(parents=True, exist_ok=False)
    started = {
        "schemaVersion": "context-provider-latency-diagnostic-started-v1",
        "programId": PACKAGE.name,
        "kind": "NONFORMAL_DIAGNOSTIC_DOES_NOT_OVERWRITE_V4",
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repetitions": repetitions,
        "plannedProviderCalls": check["plannedProviderCalls"],
        "model": check["model"],
        "temperature": 0,
        "automaticRetries": 0,
        "sourceHashes": {
            "diagnosticRunner": sha_file(Path(__file__)),
            "v4Runner": sha_file(V4 / "runner.py"),
            "v4Scenarios": sha_file(V4 / "scenarios.jsonl"),
            "contextCompiler": sha_file(ROOT / "agent/app/context_compiler_v1.py"),
        },
    }
    started_path = output / "started.json"
    started_path.write_text(
        json.dumps(started, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    client = v4.DeepSeekToolCompatibleClient(
        api_key=v4.base.settings.deepseek_api_key,
        base_url=v4.base.settings.deepseek_base_url,
        timeout=45.0,
        max_retries=0,
    )
    rows = load_rows()
    traces: list[dict[str, Any]] = []
    trace_path = output / "traces.jsonl"
    ordinal = 0
    wall_started = time.perf_counter()
    try:
        for repetition in range(1, repetitions + 1):
            for case_index, case in enumerate(rows):
                order = ARMS if (repetition + case_index) % 2 == 0 else tuple(reversed(ARMS))
                for arm in order:
                    ordinal += 1
                    try:
                        trace = await v4._call_provider(
                            client,
                            model=check["model"],
                            max_tokens=900,
                            case=case,
                            arm=arm,
                            model_view=v4.compile_case(case, arm).model_view,
                        )
                    except Exception as exc:  # no retry; preserve the call failure
                        trace = {
                            "schemaVersion": "context-compiler-provider-trace-v4",
                            "caseId": case["caseId"],
                            "scenarioId": case["scenarioId"],
                            "turnId": case["turnId"],
                            "lineageKind": case["lineageKind"],
                            "arm": arm,
                            "status": "FAILED",
                            "durationMs": None,
                            "usage": {
                                "promptTokens": None,
                                "completionTokens": None,
                                "totalTokens": None,
                                "cachedInputTokens": None,
                            },
                            "exactFidelity": False,
                            "errorCode": type(exc).__name__,
                            "errorMessage": str(exc)[:500],
                        }
                    trace.update({
                        "diagnosticRepetition": repetition,
                        "providerCallOrdinal": ordinal,
                        "automaticRetryCount": 0,
                    })
                    traces.append(trace)
                    with trace_path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(canonical(trace) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
    finally:
        await client.close()
    aggregate_by_arm = {arm: aggregate(traces, arm) for arm in ARMS}
    control = aggregate_by_arm[ARMS[0]]
    treatment = aggregate_by_arm[ARMS[1]]
    latency_ratio = (
        treatment["latencyP95Ms"] / control["latencyP95Ms"]
        if control["latencyP95Ms"] and treatment["latencyP95Ms"] else None
    )
    prompt_reduction = (
        (control["promptTokensSum"] - treatment["promptTokensSum"])
        / control["promptTokensSum"]
        if control["promptTokensSum"] else None
    )
    total_reduction = (
        (control["totalTokensSum"] - treatment["totalTokensSum"])
        / control["totalTokensSum"]
        if control["totalTokensSum"] else None
    )
    expected = check["plannedProviderCalls"] // len(ARMS)
    gates = {
        "allCallsSucceeded": all(aggregate_by_arm[arm]["succeeded"] == expected for arm in ARMS),
        "allUsageObserved": all(aggregate_by_arm[arm]["usageObserved"] == expected for arm in ARMS),
        "allExactFidelity": all(aggregate_by_arm[arm]["exactFidelity"] == expected for arm in ARMS),
        "promptTokenReductionAtLeast3Pct": prompt_reduction is not None and prompt_reduction >= 0.03,
        "totalTokenReductionAtLeast2Pct": total_reduction is not None and total_reduction >= 0.02,
        "treatmentP95LatencyWithin20Pct": latency_ratio is not None and latency_ratio <= 1.20,
        "zeroRetries": True,
    }
    result = {
        "schemaVersion": "context-provider-latency-diagnostic-result-v1",
        "programId": PACKAGE.name,
        "status": "COMPLETE",
        "kind": "NONFORMAL_DIAGNOSTIC",
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "durationSeconds": time.perf_counter() - wall_started,
        "providerCalls": len(traces),
        "aggregate": aggregate_by_arm,
        "effects": {
            "promptTokenReductionFraction": prompt_reduction,
            "totalTokenReductionFraction": total_reduction,
            "treatmentToControlP95LatencyRatio": latency_ratio,
        },
        "gatesUsingHistoricalV4Thresholds": gates,
        "diagnosticVerdict": (
            "V4_LATENCY_HOLD_NOT_REPRODUCED_IN_DIAGNOSTIC"
            if gates["treatmentP95LatencyWithin20Pct"]
            else "V4_LATENCY_HOLD_REPRODUCED_IN_DIAGNOSTIC"
        ),
        "authorityBoundary": "diagnostic only; historical V4 verdict and production default remain unchanged",
    }
    result_path = output / "result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    receipt = {
        "schemaVersion": "context-provider-latency-diagnostic-receipt-v1",
        "programId": PACKAGE.name,
        "startedSha256": sha_file(started_path),
        "tracesSha256": sha_file(trace_path),
        "resultSha256": sha_file(result_path),
        "providerCalls": len(traces),
        "automaticRetries": 0,
    }
    receipt_path = output / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha_file(path)}  {path.name}\n" for path in (
            started_path, trace_path, result_path, receipt_path
        )),
        encoding="utf-8", newline="\n",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--output", type=Path, default=PACKAGE / "p3/diagnostic001")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if args.preflight:
        result = preflight(args.repetitions)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 2
    return asyncio.run(execute(args.output.resolve(), args.repetitions))


if __name__ == "__main__":
    raise SystemExit(main())

