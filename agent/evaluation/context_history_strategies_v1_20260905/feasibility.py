"""Read-only application-window diagnostics; no benefit or optimality claim."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from .artifacts import canonical, file_sha, sha, write_new
from .history_strategies import tokens


def inspect(directory):
    inputs_path = directory / "context_inputs.jsonl"
    receipts = [json.loads(line) for line in inputs_path.read_text(encoding="utf-8").splitlines()]
    by_hash = {row["renderedRequestHash"]: row for row in receipts}
    measured = []
    for path in sorted((directory / "model_calls").glob("call-*/request.json")):
        request = json.loads(path.read_text(encoding="utf-8"))
        receipt = by_hash.get(sha(request))
        if receipt is None:
            continue  # Lookup follow-ups/summaries are not cold initial inputs.
        original = tokens(request)
        if original != receipt["applicationRequestTokens"]:
            raise ValueError("recorded_request_token_mismatch")
        bounded = deepcopy(request)
        matching = []
        for index, message in enumerate(bounded["messages"]):
            try:
                payload = json.loads(message.get("content") or "")
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("history"), dict):
                matching.append((index, payload))
        if len(matching) != 1:
            raise ValueError("unrecognized_history_boundary")
        index, payload = matching[0]
        history = payload["history"].get("messages", [])
        payload["history"] = {"messages": history[-4:], "summary": {"items": []}}
        bounded["messages"][index]["content"] = canonical(payload)
        measured.append({"phase": receipt["phase"], "fullInputTokens": original,
            "protectedWithFourRecentTokens": tokens(bounded), "requestSha256": file_sha(path)})
    cells = []
    # Earlier development failures may have only an exception string. Never
    # treat their absent request artifact as proof that A fit the candidate.
    rejected = []
    for path in sorted(directory.glob("preflight-rejected-*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if sha(value["request"]) != value["requestSha256"] or tokens(value["request"]) != value["applicationRequestTokens"]:
            raise ValueError("rejected_request_hash_or_tokens_mismatch")
        rejected.append({"tokens": value["applicationRequestTokens"], "source": path.name, "exactRequestPreserved": True})
    failure_path = directory / "failure.json"
    if failure_path.exists() and not rejected:
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        error = failure.get("error", "")
        if error.startswith("application_input_budget_exceeded:"):
            rejected.append({"tokens": int(error.rsplit(":", 1)[1]), "source": failure_path.name,
                             "exactRequestPreserved": False})
    for budget in (8000, 16000, 32000):
        for trigger in (0.6, 0.7, 0.8):
            for target in (0.35, 0.45, 0.55):
                eligible = [row for row in measured if row["fullInputTokens"] > budget * trigger]
                cells.append({"inputBudget": budget, "trigger": trigger, "target": target,
                    "observedAAboveWindowInputs": sum(row["fullInputTokens"] > budget for row in measured)
                        + sum(row["tokens"] > budget for row in rejected),
                    "coldTriggerCandidateInputs": len(eligible),
                    "infeasibleProtectedTargets": sum(row["protectedWithFourRecentTokens"] + 128 > budget * target for row in eligible)})
    return {"status": "DEVELOPMENT_WINDOW_FEASIBILITY_ONLY", "source": str(directory.resolve()),
        "measuredInitialRequests": len(measured), "requests": measured, "rejectedInputs": rejected, "cells": cells,
        "selectedConfiguration": None, "formalAcceptance": False,
        "note": "Uses recorded A states only. Cold-trigger counts are not actual compaction counts. No C state evolution, summary quality, latency or caching is simulated; rerun after shared Agent repairs."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = inspect(args.directory)
    write_new(args.output, result)
    print(json.dumps({"measured": result["measuredInitialRequests"], "cells": len(result["cells"]), "selected": None}))
