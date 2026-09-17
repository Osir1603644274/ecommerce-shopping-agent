"""Charge actual native summary calls to their closed Agent turns, not free work."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha, write_new


def inspect(directory, through):
    bindings = {}
    binding_path = directory / "native_call_bindings.jsonl"
    for line in binding_path.read_bytes().splitlines(keepends=True):
        if line.endswith(b"\n"):
            binding = json.loads(line)
            bindings[binding["nativeOrdinal"]] = binding
    turns, total = [], 0
    for turn in range(1, through + 1):
        path = directory / f"turn-{turn:02}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        summaries = []
        for call in row["modelCalls"]:
            ordinal = call["ordinal"]
            if bindings.get(ordinal, {}).get("purpose") != "history_summary":
                continue
            result_path = directory / f"model_calls/call-{ordinal:03}/result.json"
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else call
            usage = result.get("usage") or {}
            known = all(type(usage.get(k)) is int for k in ("input_tokens", "output_tokens"))
            summaries.append({"ordinal": ordinal, "phase": bindings[ordinal]["phase"],
                "status": result["status"], "nativeTokens": usage["input_tokens"] + usage["output_tokens"] if known else None,
                "durationMs": result.get("durationMs"),
                "sourceSha256": file_sha(result_path) if result_path.exists() else file_sha(path)})
        if summaries:
            total += len(summaries)
            turns.append({"turn": turn, "query": row["query"], "agentTurnDurationMs": row["durationMs"],
                "turnSha256": file_sha(path), "summaryCalls": summaries,
                "knownSummaryNativeTokenSubtotal": sum(s["nativeTokens"] or 0 for s in summaries),
                "unknownSummaryUsageCalls": [s["ordinal"] for s in summaries if s["nativeTokens"] is None]})
    return {"status": "DEVELOPMENT_SUMMARY_COST_DIAGNOSTIC", "source": str(directory.resolve()),
        "throughTurn": through, "summaryNativeCalls": total, "turnsWithSummaryCalls": turns,
        "modelCallsIssuedByDiagnostic": 0, "formalLatencyAcceptance": False,
        "note": "Summary calls, including repairs/failures, are included in Agent turn time and native token totals. Their own duration is not a causal A-vs-C added wait estimate; development processes run concurrently. No presumed zero usage for failures."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--through", type=int, required=True)
    args = parser.parse_args()
    value = inspect(args.directory, args.through)
    write_new(args.output, value)
    print(json.dumps({"summaryNativeCalls": value["summaryNativeCalls"],
        "turns": [r["turn"] for r in value["turnsWithSummaryCalls"]]}))
