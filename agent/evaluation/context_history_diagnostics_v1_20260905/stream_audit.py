"""Same execution checks as the historical auditor, one raw turn at a time."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha, write_new


def audit(directory):
    paths = sorted(directory.glob("turn-*.json"))
    problems, resumed, sources = [], [], {}
    result_path = directory / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    if (directory / "failure.json").exists():
        problems.append({"code": "attempt_failure_artifact"})
    if not result or result.get("plannedTurns", len(paths)) != len(paths):
        problems.append({"code": "incomplete_or_unclosed_collection"})
    for path in paths:
        row = json.loads(path.read_text(encoding="utf-8"))
        sources[path.name] = file_sha(path)
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
    return {"status": "EXECUTION_AUDIT_PASS" if paths and not problems else "EXECUTION_AUDIT_HOLD",
        "turns": len(paths), "problems": problems, "sameIdentityClarificationResumeTurns": resumed,
        "qualityAcceptance": False, "formalComparativeAcceptance": False, "sources": sources}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = audit(args.directory)
    write_new(args.output, {**value, "auditSourceSha256": file_sha(__file__), "memoryStrategy": "ONE_RAW_TURN_AT_A_TIME"})
    print(json.dumps({k: value[k] for k in ("status", "turns", "problems")}, ensure_ascii=False))
