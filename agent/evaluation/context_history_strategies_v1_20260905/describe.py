"""Complete-cost census of immutable development attempts, not effect claims."""
import argparse
import json
import math
from pathlib import Path

from .artifacts import file_sha, write_new


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def describe(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "started.json").read_text(encoding="utf-8"))
    rows, sources = [], {}
    for path in sorted(directory.glob("turn-*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        summary = value.get("traceSummary") or {}
        trace = value.get("agentRunTrace") or {}
        rows.append({"turn": value["turn"], "durationMs": value["durationMs"],
            "modelCalls": len(value.get("modelCalls", [])), "action": summary.get("finalAction"),
            "status": summary.get("agentStatus"), "identityValid": value.get("runId") == value.get("expectedRunId"),
            "decisionErrors": [item["errorCode"] for item in trace.get("reactDecisions", []) if item.get("errorCode")]})
        sources[path.name] = file_sha(path)
    calls = {}
    ledger = directory / "model_calls/ledger.jsonl"
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            calls[row["ordinal"]] = row  # Last terminal overwrites STARTED, never removes a failure.
        sources["model_calls/ledger.jsonl"] = file_sha(ledger)
    totals = {"input": 0, "output": 0, "cachedInputSubset": 0}
    unknown_usage, unknown_cache, failed_calls = [], [], []
    for ordinal, call in sorted(calls.items()):
        usage = call.get("usage")
        if not usage or any(type(usage.get(key)) is not int for key in ("input_tokens", "output_tokens")):
            unknown_usage.append(ordinal)
        else:
            totals["input"] += usage["input_tokens"]
            totals["output"] += usage["output_tokens"]
            if usage.get("cached_input_tokens") is None:
                unknown_cache.append(ordinal)
            else:
                totals["cachedInputSubset"] += usage["cached_input_tokens"]
        if call.get("status") != "COMPLETED":
            failed_calls.append(ordinal)
    total = totals["input"] + totals["output"]
    result_path = directory / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    planned = result.get("plannedTurns")
    if planned is None and manifest.get("script"):
        script = Path(manifest["script"])
        if file_sha(script) != manifest["scriptSha256"]:
            raise ValueError("script_drift")
        planned = len(json.loads(script.read_text(encoding="utf-8"))["turns"])
    complete = bool(result) and len(rows) == planned
    timings = [row["durationMs"] for row in rows]
    inputs_path = directory / "context_inputs.jsonl"
    inputs = [json.loads(line)["applicationRequestTokens"] for line in inputs_path.read_text(encoding="utf-8").splitlines()] if inputs_path.exists() else []
    hist_path = directory / "history_receipts.jsonl"
    history = [json.loads(line) for line in hist_path.read_text(encoding="utf-8").splitlines()] if hist_path.exists() else result.get("historyReceipts", [])
    lookup_path = directory / "history_lookups.jsonl"
    lookup_count = len(lookup_path.read_text(encoding="utf-8").splitlines()) if lookup_path.exists() else 0
    return {"kind": "DEVELOPMENT_DESCRIPTIVE_CENSUS_NOT_COMPARATIVE_ACCEPTANCE", "directory": str(directory.resolve()),
        "arm": manifest.get("arm"), "configuration": manifest["configuration"],
        "historyPolicy": manifest.get("historyPolicy", result.get("historyPolicy")),
        "plannedTurns": planned, "observedTurns": len(rows), "dataCollectionComplete": complete,
        "turnsWithModelCalls": sum(row["modelCalls"] > 0 for row in rows),
        "deterministicOnlyObservedTurns": sum(row["modelCalls"] == 0 for row in rows),
        "unsafeTerminalTurns": [row["turn"] for row in rows if row["status"] != "ok" or not row["identityValid"]
            or row["action"] in {"stop_turn", "resume_rejected", "context_only_answer_failed"}],
        "nativeModelCalls": len(calls), "failedOrUnclosedModelCalls": failed_calls,
        "knownNativeTokenSubtotal": total, "completeNativeTokenTotal": None if unknown_usage else total,
        "nativeBreakdown": totals, "unknownUsageCalls": unknown_usage, "unknownCachedSubsetCalls": unknown_cache,
        "completeConversationMs": sum(timings) if complete else None, "observedTurnMsSubtotal": sum(timings),
        "observedTurnP50Ms": percentile(timings, 0.5), "observedTurnP95Ms": percentile(timings, 0.95),
        "worstObservedTurnMs": max(timings) if timings else None, "percentileMethod": "empirical_nearest_rank",
        "applicationInputTokenMin": min(inputs) if inputs else None, "applicationInputTokenMax": max(inputs) if inputs else None,
        "summaryCommits": sum(row["kind"] == "C_LLM_SUMMARY" for row in history),
        "summaryFallbacks": sum(row["kind"] == "C_FULL_FALLBACK" for row in history),
        "historyLookups": lookup_count, "perTurn": rows, "sourceSha256": sources,
        "qualityAcceptance": False, "latencyComparability": "DEVELOPMENT_ONLY; instrumentation and concurrent resource contention not removed",
        "usageScope": "Native Codex turn usage includes host overhead. Input+output only; cached/reasoning subsets not added twice."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = describe(args.directory)
    write_new(args.output, value)
    print(json.dumps({key: value[key] for key in ("arm", "observedTurns", "dataCollectionComplete", "nativeModelCalls", "completeNativeTokenTotal", "unsafeTerminalTurns")}, ensure_ascii=False))
