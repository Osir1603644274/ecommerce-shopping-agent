"""Read-only replay of the recorded C26 failed history lookup against v6."""
import argparse
import json
from pathlib import Path

from agent.app.context_history import HistoryArchive
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new
from agent.evaluation.context_history_strategies_v1_20260905.history_lookup import lookup
from agent.evaluation.context_history_strategies_v1_20260905.history_strategies import tokens


def run(output):
    attempt = HERE / "core72_v4_C001"
    identity = json.loads((attempt / "archive/identity.json").read_text(encoding="utf-8"))
    archive = HistoryArchive(attempt / "archive", session_id=identity["sessionId"], task_id=identity["taskId"])
    original = json.loads((attempt / "turn-21.json").read_text(encoding="utf-8"))
    failed = json.loads((attempt / "history_lookups.jsonl").read_text(encoding="utf-8").splitlines()[0])
    result = lookup(archive, failed["arguments"], token_budget=2000)
    exact = len(result["records"]) == 1 and result["records"][0]["turn"] == 21 and result["records"][0]["content"] == original["answer"]
    passed = exact and result["actionAuthorized"] is False and tokens(result) <= 2000
    value = {"status": "RECORDED_DISPLAY_LOOKUP_PASS" if passed else "HOLD",
        "oldArguments": failed["arguments"], "oldResult": failed["result"], "newResult": result,
        "exactOriginalAnswer": exact, "responseTokens": tokens(result),
        "noFutureTurnReturned": all(r["turn"] == 21 for r in result["records"]),
        "modelCalls": 0, "businessWrites": 0, "semanticAnswerRepairNotProven": True,
        "sources": {name: file_sha(attempt / name) for name in ("archive/messages.jsonl", "archive/identity.json", "turn-21.json", "turn-26.json", "history_lookups.jsonl", "model_calls/call-053/result.json")},
        "lookupSourceSha256": file_sha(HERE / "history_lookup.py"), "diagnosticSha256": file_sha(__file__),
        "qualityFinding": "Recorded C26 reverses the visible scratch attributes of IDs 2625578 and 614290 and incorrectly calls already recorded original-battery evidence unknown; corrected lookup now returns the exact original IDs and statements, but does not retroactively repair that answer."}
    write_new(output, value)
    print(json.dumps({k: value[k] for k in ("status", "exactOriginalAnswer", "responseTokens", "noFutureTurnReturned")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
