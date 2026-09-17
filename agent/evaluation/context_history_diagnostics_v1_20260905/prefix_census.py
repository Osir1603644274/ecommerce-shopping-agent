"""Predeclared prefix accounting without reading future user turns into judges."""
import argparse
import hashlib
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha, write_new
from agent.evaluation.context_history_strategies_v1_20260905.describe import percentile


def census(directory, through):
    manifest = json.loads((directory / "started.json").read_text(encoding="utf-8"))
    metrics, hashes, declared_calls = [], {}, set()
    for number in range(1, through + 1):
        path = directory / f"turn-{number:02d}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["turn"] != number:
            raise ValueError("noncontiguous_prefix_turns")
        summary = row.get("traceSummary") or {}
        calls = [item["ordinal"] for item in row["modelCalls"]]
        declared_calls.update(calls)
        metrics.append({"turn": number, "durationMs": row["durationMs"], "nativeOrdinals": calls,
            "identityValid": row["runId"] == row["expectedRunId"], "status": summary.get("agentStatus"),
            "action": summary.get("finalAction"), "failedPhase": any(item.get("outcome") == "failed" for item in summary.get("phases", []))})
        hashes[path.name] = file_sha(path)
    last_ordinal = max(declared_calls, default=0)
    if declared_calls != set(range(1, last_ordinal + 1)):
        raise ValueError("prefix_native_ordinal_gap")
    ledger_path = directory / "model_calls/ledger.jsonl"
    ledger_bytes = ledger_path.read_bytes() if ledger_path.exists() else b""
    calls = {}
    # A future call may currently be appending its row. All prefix terminals
    # must already exist before the closed prefix turn artifact was written.
    lines = ledger_bytes.splitlines(keepends=True)
    for line in lines:
        if not line.endswith(b"\n"):
            continue
        value = json.loads(line.decode("utf-8"))
        if value["ordinal"] <= last_ordinal:
            calls[value["ordinal"]] = value
    if set(calls) != declared_calls:
        raise ValueError("prefix_call_missing_from_native_ledger")
    unknown, failed, input_tokens, output_tokens, cache = [], [], 0, 0, 0
    for ordinal, call in sorted(calls.items()):
        usage = call.get("usage") or {}
        if not all(type(usage.get(key)) is int for key in ("input_tokens", "output_tokens")):
            unknown.append(ordinal)
        else:
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
            if type(usage.get("cached_input_tokens")) is int:
                cache += usage["cached_input_tokens"]
        if call.get("status") != "COMPLETED":
            failed.append(ordinal)
    timings = [row["durationMs"] for row in metrics]
    unsafe = [row["turn"] for row in metrics if not row["identityValid"] or row["status"] != "ok" or row["failedPhase"]
        or row["action"] in {"stop_turn", "resume_rejected", "context_only_answer_failed"}]
    return {"kind": "PREDECLARED_DEVELOPMENT_PREFIX_CENSUS_NOT_INDEPENDENT_SAMPLE", "parent": str(directory.resolve()),
        "prefixTurns": through, "closedPrefixCollected": True, "parentCompletionNotImplied": True,
        "arm": manifest["arm"], "historyPolicy": manifest["historyPolicy"], "startedSha256": file_sha(directory / "started.json"),
        "nativeCalls": len(calls), "lastIncludedNativeOrdinal": last_ordinal, "futureCallsIncluded": False,
        "knownNativeTokenSubtotal": input_tokens + output_tokens,
        "completeNativeTokenTotal": None if unknown else input_tokens + output_tokens,
        "nativeInput": input_tokens, "nativeOutput": output_tokens, "knownCachedInputSubset": cache,
        "unknownUsageCalls": unknown, "failedOrUnclosedCalls": failed, "unsafeTerminalTurns": unsafe,
        "completePrefixMs": sum(timings), "turnP50Ms": percentile(timings, .5), "turnP95Ms": percentile(timings, .95),
        "worstTurnMs": max(timings), "perTurn": metrics, "turnSourceSha256": hashes,
        "ledgerSnapshotSha256": hashlib.sha256(ledger_bytes).hexdigest(), "ledgerSnapshotBytes": len(ledger_bytes),
        "qualityAcceptance": False, "formalAcceptance": False,
        "note": "All native ordinals through the closed prefix boundary, including failures and summary/lookup/repair calls. Same-parent endpoints are dependent; timings remain development-only."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--through", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.through <= 72:
        raise ValueError("prefix_size_out_of_bounds")
    value = census(args.directory, args.through)
    write_new(args.output, value)
    print(json.dumps({key: value[key] for key in ("arm", "prefixTurns", "nativeCalls", "completeNativeTokenTotal", "unsafeTerminalTurns")}))
