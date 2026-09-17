"""Read-only streaming closeout; never repairs or overwrites source attempts."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def inspect_attempt(directory):
    started = json.loads((directory / "started.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "source_snapshot/manifest.json").read_text(encoding="utf-8"))
    damaged = [name for name, value in manifest["sources"].items()
               if digest(directory / "source_snapshot" / name) != value]
    turns, inputs, outputs, unknown, call_ends = [], 0, 0, [], []
    hashes = {"started.json": digest(directory / "started.json"),
              "source_snapshot/manifest.json": digest(directory / "source_snapshot/manifest.json")}
    for path in sorted(directory.glob("turn-*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        summary = row.get("traceSummary") or {}
        turns.append({"turn": row["turn"], "durationMs": row["durationMs"],
            "runId": row["runId"], "expectedRunId": row["expectedRunId"],
            "status": summary.get("agentStatus"), "action": summary.get("finalAction"),
            "nativeOrdinals": [call["ordinal"] for call in row["modelCalls"]]})
        hashes[path.name] = digest(path)
        del row
    ledger = directory / "model_calls/ledger.jsonl"
    calls = {}
    with ledger.open(encoding="utf-8") as stream:
        for line in stream:
            if line.endswith("\n"):
                row = json.loads(line)
                calls[row["ordinal"]] = row
    hashes["model_calls/ledger.jsonl"] = digest(ledger)
    for ordinal, row in sorted(calls.items()):
        usage = row.get("usage") or {}
        if all(type(usage.get(key)) is int for key in ("input_tokens", "output_tokens")):
            inputs += usage["input_tokens"]
            outputs += usage["output_tokens"]
        else:
            unknown.append(ordinal)
        call_ends.append({"ordinal": ordinal, "status": row["status"], "at": row["at"],
                         "processExitCode": row.get("processExitCode")})
    # Hash every native file (including incomplete final invocation) for closure.
    for path in sorted((directory / "model_calls").glob("call-*/*")):
        if path.is_file():
            hashes[path.relative_to(directory).as_posix()] = digest(path)
    terminals = {}
    for name in ("failure.json", "result.json"):
        path = directory / name
        if path.exists():
            terminals[name] = json.loads(path.read_text(encoding="utf-8"))
            hashes[name] = digest(path)
    for name in ("redis/process.json", "redis/server.log"):
        if (directory / name).exists():
            hashes[name] = digest(directory / name)
    return {"directory": str(directory), "arm": started["arm"], "scriptSha256": started["scriptSha256"],
        "frozenSources": manifest["sources"], "damagedFrozenSources": damaged,
        "closedTurnCount": len(turns), "turns": turns, "nativeCallCount": len(calls),
        "knownNativeTokenSubtotal": inputs + outputs, "unknownUsageOrdinals": unknown,
        "completeNativeTokenTotal": None if unknown else inputs + outputs,
        "lastNativeCall": call_ends[-1] if call_ends else None,
        "unclosedOrFailedNativeCalls": [row for row in call_ends if row["status"] != "COMPLETED"],
        "runnerTerminalArtifacts": terminals,
        "runnerExitCode": None,
        "classification": "RECORDED_FAILURE" if "failure.json" in terminals else
            "RESULT_PRESENT" if "result.json" in terminals else "INTERRUPTED_WITHOUT_TERMINAL_CAUSE_UNKNOWN",
        "closedTurnTimeSubtotalMs": sum(row["durationMs"] for row in turns),
        "completeConversationMs": None,
        "sourceHashes": hashes, "comparativeAcceptance": False}


def run(output):
    # Full command-line inventory stays scoped to the three attempts/native pids.
    names = ["core72_v6_" + arm + "001" for arm in "ABC"]
    process = subprocess.run(["powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','redis-server.exe','codex.exe') } | "
        "Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | ConvertTo-Json -Depth 3 -Compress"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    if process.returncode:
        raise RuntimeError("process_inventory_failed")
    inventory = json.loads(process.stdout or "[]")
    if isinstance(inventory, dict):
        inventory = [inventory]
    all_pids = {row["ProcessId"] for row in inventory}
    records = []
    for name in names:
        directory = HERE / name
        owned = []
        for path in [directory / "redis/process.json", *(directory / "model_calls").glob("call-*/process.json")]:
            row = json.loads(path.read_text(encoding="utf-8"))
            if row["pid"] in all_pids:
                owned.append(row["pid"])
        active = [row for row in inventory if name in (row.get("CommandLine") or "")]
        if owned or active:
            raise RuntimeError("possible_owned_process_still_alive_verify_identity:" + name)
        record = inspect_attempt(directory)
        record["noRecordedOwnedPidAliveAtAudit"] = True
        record["noAttemptCommandAliveAtAudit"] = True
        records.append(record)
    write_new(output, {"kind": "IMMUTABLE_OLD_COHORT_CLOSEOUT", "at": now(),
        "records": records, "sameSourceCohort": all(row["frozenSources"] == records[0]["frozenSources"] for row in records),
        "sameScriptCohort": len({row["scriptSha256"] for row in records}) == 1,
        "processCheckExitCode": process.returncode,
        "warning": "No original runner exit code was persisted. Missing terminal cause stays unknown; absence of process is not a checkpoint recovery or successful completion.",
        "oldAttemptsModified": False, "complete72ComparisonPermitted": False})
    print(json.dumps([{key: row[key] for key in ("arm", "closedTurnCount", "nativeCallCount",
        "knownNativeTokenSubtotal", "unknownUsageOrdinals", "classification")} for row in records]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
