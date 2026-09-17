"""Recompute native cost from original terminal events, not summary counters."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha, sha, write_new
from agent.evaluation.context_history_strategies_v1_20260905.subscription import native_terminal_usage


def audit(directory, *, flat=False):
    calls_root = directory if flat else directory / "model_calls"
    ledger_path = calls_root / "ledger.jsonl"
    latest = {}
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        latest[row["ordinal"]] = row
    if set(latest) != set(range(1, len(latest) + 1)):
        raise ValueError("native_ledger_ordinal_gap")
    calls, inputs, outputs, unknown = [], 0, 0, []
    for ordinal, row in sorted(latest.items()):
        directory_call = calls_root / f"call-{ordinal:03d}"
        request = json.loads((directory_call / "request.json").read_text(encoding="utf-8"))
        prompt = (directory_call / "prompt.txt").read_text(encoding="utf-8")
        if row.get("requestSha256") != sha(request) or row.get("promptSha256") != sha(prompt):
            raise ValueError("native_request_or_prompt_binding_mismatch")
        terminal_path = directory_call / "events.jsonl"
        usage, thread = None, None
        if terminal_path.exists():
            try:
                usage, thread = native_terminal_usage(terminal_path.read_text(encoding="utf-8"))
            except (ValueError, KeyError, TypeError):
                pass
        if usage != row.get("usage"):
            raise ValueError("native_terminal_and_ledger_usage_disagree:" + str(ordinal))
        if thread is not None and row.get("threadId") != thread:
            raise ValueError("native_thread_binding_mismatch")
        result_path = directory_call / "result.json"
        if result_path.exists() and json.loads(result_path.read_text(encoding="utf-8")) != row:
            raise ValueError("native_result_and_ledger_disagree")
        if usage is None:
            unknown.append(ordinal)
        else:
            inputs += usage["input_tokens"]
            outputs += usage["output_tokens"]
        files = (directory_call / "request.json", directory_call / "prompt.txt", terminal_path, result_path)
        calls.append({"ordinal": ordinal, "status": row["status"], "threadId": thread,
            "usage": usage, "sourceHashes": {str(path.relative_to(directory)): file_sha(path) for path in files if path.exists()}})
    return {"status": "NATIVE_COST_RECONCILED" if not unknown else "NATIVE_COST_PARTIALLY_UNKNOWN",
        "directory": str(directory), "nativeCalls": len(calls), "calls": calls,
        "nativeInput": inputs, "nativeOutput": outputs, "knownNativeTokenSubtotal": inputs + outputs,
        "completeNativeTokenTotal": None if unknown else inputs + outputs, "unknownUsageCalls": unknown,
        "failedOrUnclosedCalls": [row["ordinal"] for row in calls if row["status"] != "COMPLETED"],
        "ledgerSha256": file_sha(ledger_path), "auditSourceSha256": file_sha(__file__),
        "qualityAcceptance": False, "note": "Input+output once; cached/reasoning subsets never added twice. Known completed native usage remains charged even if application or process failed. Unreconciled counters fail closed."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = audit(args.directory)
    write_new(args.output, value)
    print(json.dumps({key: value[key] for key in ("status", "nativeCalls", "completeNativeTokenTotal", "unknownUsageCalls")}))
