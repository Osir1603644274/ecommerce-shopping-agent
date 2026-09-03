"""Execute the frozen raw-full-history versus compiled-context pair once."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.evaluation.context_compiler_provider_paired_v1_20260901_v4 import runner as v4


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PACKAGE / "manifest.json"
ARMS = ("RAW_FULL_CONTROL", "COMPILED_TREATMENT")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def raw_view(case: dict[str, Any]) -> dict[str, Any]:
    payload = case["contextPayload"]
    return {
        "taskId": payload["taskId"],
        "baseContextRevision": payload["baseContextRevision"],
        "goal": payload["goal"],
        "confirmedFacts": payload["confirmedFacts"],
        "hardConstraints": payload["hardConstraints"],
        "softPreferences": payload["softPreferences"],
        "unknowns": payload["unknowns"],
        "pendingQuestions": payload["pendingQuestions"],
        "shoppingGuideState": payload["shoppingGuideState"],
        "candidateScopeState": payload["candidateScopeState"],
        "allowedTools": payload["allowedTools"],
        "evidenceRefs": payload["evidenceRefs"],
        "referenceContextState": payload["referenceContextState"],
        "rawFullConversation": case["rawFullConversation"],
    }


def arm_view(case: dict[str, Any], arm: str) -> dict[str, Any]:
    if arm == "RAW_FULL_CONTROL":
        return raw_view(case)
    if arm == "COMPILED_TREATMENT":
        return v4.compile_case(case, "CTX1b").model_view
    raise ValueError(arm)


def preflight(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "FROZEN_BEFORE_EXECUTION":
        raise RuntimeError("manifest not frozen")
    for relative, expected in manifest["sourceFreeze"].items():
        actual = sha(ROOT / relative)
        if actual != expected:
            raise RuntimeError(f"source hash mismatch: {relative}")
    dataset = ROOT / manifest["dataset"]["path"]
    if sha(dataset) != manifest["dataset"]["sha256"]:
        raise RuntimeError("dataset hash mismatch")
    rows = load_rows(dataset)
    if len(rows) != manifest["dataset"]["scenarioCount"]:
        raise RuntimeError("scenario count mismatch")
    smaller = 0
    valid = 0
    for case in rows:
        v4.validate_reference_state(case)
        raw = json.dumps(arm_view(case, ARMS[0]), ensure_ascii=False, sort_keys=True)
        compiled = json.dumps(arm_view(case, ARMS[1]), ensure_ascii=False, sort_keys=True)
        smaller += len(compiled.encode("utf-8")) < len(raw.encode("utf-8"))
        valid += 1
    return {"status": "PASS" if valid == len(rows) and smaller == len(rows) else "FAIL", "scenarioCount": len(rows), "compiledSmallerCount": smaller, "referenceValidCount": valid}


def percentile(values: list[float], q: float) -> float:
    return v4.base.percentile(values, q)


def aggregate(traces: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    rows = [row for row in traces if row["arm"] == arm]
    succeeded = [row for row in rows if row["status"] == "SUCCEEDED"]
    observed = [row for row in succeeded if all(row["usage"].get(k) is not None for k in ("promptTokens", "completionTokens", "totalTokens"))]
    return {
        "caseCount": len(rows),
        "succeeded": len(succeeded),
        "exactFidelity": sum(bool(row.get("exactFidelity")) for row in rows),
        "usageObserved": len(observed),
        "promptTokensSum": sum(int(row["usage"]["promptTokens"]) for row in observed),
        "totalTokensSum": sum(int(row["usage"]["totalTokens"]) for row in observed),
        "latencyP50Ms": percentile([float(row["durationMs"]) for row in succeeded], .5),
        "latencyP95Ms": percentile([float(row["durationMs"]) for row in succeeded], .95),
    }


async def execute(manifest_path: Path, output: Path) -> int:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    check = preflight(manifest_path)
    if check["status"] != "PASS":
        raise RuntimeError("preflight failed")
    if output.exists():
        raise RuntimeError("attempt output exists")
    if not v4.base.settings.deepseek_api_key:
        raise RuntimeError("DeepSeek API key not configured")
    if v4.base.settings.deepseek_model != manifest["provider"]["model"]:
        raise RuntimeError("configured model differs from manifest")
    output.mkdir(parents=True, exist_ok=False)
    started = {
        "schemaVersion": "context-raw-full-vs-compiled-started-v1",
        "attemptId": manifest["attemptId"],
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "runnerSha256": sha(Path(__file__)),
        "automaticRetries": 0,
    }
    (output / "started.json").write_text(json.dumps(started, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    rows = load_rows(ROOT / manifest["dataset"]["path"])
    rng = random.Random(manifest["randomSeed"])
    client = v4.DeepSeekToolCompatibleClient(api_key=v4.base.settings.deepseek_api_key, base_url=v4.base.settings.deepseek_base_url, timeout=float(manifest["provider"]["timeoutSeconds"]), max_retries=0)
    traces: list[dict[str, Any]] = []
    trace_path = output / "traces.jsonl"
    ordinal = 0
    try:
        for case in rows:
            order = list(ARMS)
            rng.shuffle(order)
            for arm in order:
                ordinal += 1
                try:
                    trace = await v4._call_provider(client, model=manifest["provider"]["model"], max_tokens=int(manifest["provider"]["maxTokens"]), case=case, arm=arm, model_view=arm_view(case, arm))
                except Exception as exc:
                    trace = {"caseId": case["caseId"], "scenarioId": case["scenarioId"], "arm": arm, "status": "FAILED", "errorCode": type(exc).__name__, "errorMessage": str(exc)[:500], "usage": {"promptTokens": None, "completionTokens": None, "totalTokens": None}, "exactFidelity": False}
                trace["providerCallOrdinal"] = ordinal
                traces.append(trace)
                with trace_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(trace, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        await client.close()
    stats = {arm: aggregate(traces, arm) for arm in ARMS}
    control, treatment = stats[ARMS[0]], stats[ARMS[1]]
    prompt_reduction = (control["promptTokensSum"] - treatment["promptTokensSum"]) / control["promptTokensSum"] if control["promptTokensSum"] else 0.0
    total_reduction = (control["totalTokensSum"] - treatment["totalTokensSum"]) / control["totalTokensSum"] if control["totalTokensSum"] else 0.0
    latency_ratio = treatment["latencyP95Ms"] / control["latencyP95Ms"] if control["latencyP95Ms"] else None
    count = len(rows)
    gates = {
        "allCallsSucceeded": all(stats[a]["succeeded"] == count for a in ARMS),
        "allUsageObserved": all(stats[a]["usageObserved"] == count for a in ARMS),
        "bothArmsExactFidelity": all(stats[a]["exactFidelity"] == count for a in ARMS),
        "promptTokenReductionAtLeast15Pct": prompt_reduction >= .15,
        "totalTokenReductionAtLeast10Pct": total_reduction >= .10,
        "p95LatencyRatioAtMost1_30": latency_ratio is not None and latency_ratio <= 1.30,
        "zeroRetries": True,
        "preflight": True,
    }
    accepted = all(gates.values())
    result = {
        "schemaVersion": "context-raw-full-vs-compiled-result-v1",
        "attemptId": manifest["attemptId"],
        "status": "COMPLETE",
        "verdict": "BOUNDED_CONTEXT_RAW_PAIR_ACCEPT" if accepted else "HOLD_CONTEXT_RAW_PAIR",
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "synthetic_pressure_context_fidelity_not_full_task_success_not_human_transcript",
        "scenarioCount": count,
        "actualProviderCalls": len(traces),
        "aggregate": stats,
        "pairedEffects": {"promptTokenReductionFraction": prompt_reduction, "totalTokenReductionFraction": total_reduction, "treatmentToControlP95LatencyRatio": latency_ratio},
        "gates": gates,
        "productionDefaultsChanged": False,
        "taskSuccessMeasured": False,
    }
    result_path = output / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    receipt = {"manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(), "startedSha256": sha(output / "started.json"), "tracesSha256": sha(trace_path), "resultSha256": sha(result_path), "providerCalls": len(traces)}
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if accepted else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=PACKAGE / "attempt001")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if args.preflight:
        print(json.dumps(preflight(args.manifest), ensure_ascii=False, indent=2))
        return 0
    return asyncio.run(execute(args.manifest, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
