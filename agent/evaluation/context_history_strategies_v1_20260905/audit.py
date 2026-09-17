"""Current execution audit does not trust historical PASS declarations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifacts import file_sha, write_new


def audit(directory):
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("turn-*.json"))]
    problems, resumed = [], []
    result_path = directory / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    if (directory / "failure.json").exists():
        problems.append({"code": "attempt_failure_artifact"})
    if not result or result.get("plannedTurns", len(rows)) != len(rows):
        problems.append({"code": "incomplete_or_unclosed_collection"})
    for row in rows:
        summary = row.get("traceSummary") or {}
        if row.get("runId") != row.get("expectedRunId"):
            problems.append({"turn": row["turn"], "code": "run_identity_mismatch"})
        if summary.get("agentStatus") != "ok" or summary.get("finalAction") in {"resume_rejected", "stop_turn", "context_only_answer_failed"}:
            problems.append({"turn": row["turn"], "code": "unsuccessful_terminal", "action": summary.get("finalAction")})
        resume = row.get("resumeRequest")
        if resume:
            if resume.get("answer") != row["query"]:
                problems.append({"turn": row["turn"], "code": "resume_answer_not_bound"})
            receipt = row["postState"]["domainState"].get("v2PendingClarification")
            if receipt and receipt.get("status") == "resolved" and receipt.get("runId") == resume.get("runId"):
                resumed.append(row["turn"])
        for call in row.get("modelCalls", []):
            if call.get("status") != "COMPLETED" or call.get("usage") is None:
                problems.append({"turn": row["turn"], "code": "model_call_failure_or_unknown_usage"})
    return {"status": "EXECUTION_AUDIT_PASS" if rows and not problems else "EXECUTION_AUDIT_HOLD",
            "turns": len(rows), "problems": problems, "sameIdentityClarificationResumeTurns": resumed,
            "qualityAcceptance": False, "formalComparativeAcceptance": False,
            "sources": {path.name: file_sha(path) for path in sorted(directory.glob("turn-*.json"))}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = audit(args.directory)
    write_new(args.output, value)
    print(json.dumps(value, ensure_ascii=False))
