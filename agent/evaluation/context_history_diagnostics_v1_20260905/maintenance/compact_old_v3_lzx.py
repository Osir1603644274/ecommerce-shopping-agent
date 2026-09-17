"""Stronger reversible compression of exactly three previously verified old logs."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new
from .probe_lzx import allocated_bytes


def run(output):
    began = now()
    output = output.resolve()
    journal = output.with_name(output.stem + "_before.json")
    if output.parent != HERE.resolve() or output.exists() or journal.exists():
        raise ValueError("fresh_output_and_journal_required")
    probe_path = HERE / "storage_lzx_probe001/result.json"
    if json.loads(probe_path.read_text(encoding="utf-8"))["status"] != "ROUNDTRIP_PASS":
        raise ValueError("local_codec_roundtrip_required")
    prior_path = HERE / "closed_v3_state_logs_ntfs_compression001.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    before_path = HERE / "closed_v3_state_logs_ntfs_compression001_before.json"
    if prior["status"] != "VERIFIED" or digest(before_path) != prior["beforeJournalSha256"]:
        raise ValueError("prior_receipt_changed")
    before = json.loads(before_path.read_text(encoding="utf-8"))
    allowed = {HERE / f"core48_v3_{arm}001" for arm in "ABC"}
    if {Path(t["directory"]) for t in before["terminals"]} != allowed:
        raise ValueError("exact_old_terminal_scope_required")
    if {Path(f["path"]) for f in before["files"]} != {d / "state_transitions.jsonl" for d in allowed}:
        raise ValueError("exact_three_logs_required")
    inventory = subprocess.run(["powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','codex.exe','redis-server.exe') } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    if inventory.returncode:
        raise ValueError("process_inventory_failed")
    processes = json.loads(inventory.stdout or "[]")
    if isinstance(processes, dict):
        processes = [processes]
    checks = []
    for terminal in before["terminals"]:
        directory = Path(terminal["directory"])
        if directory.is_symlink() or directory.is_junction() or directory.resolve().parent != HERE.resolve():
            raise ValueError("linked_attempt_rejected")
        target = str(directory).replace("\\", "/").casefold()
        if any(target in (p.get("CommandLine") or "").replace("\\", "/").casefold() for p in processes):
            raise ValueError("old_attempt_running")
        checks.append((directory / "result.json", terminal["resultSha256"]))
        checks.extend((directory / name, expected) for name, expected in terminal["turnHashes"].items())
    checks.extend((Path(row["path"]), row["sha256"]) for row in before["files"])
    for path, expected in checks:
        if path.parent not in allowed or path.is_symlink() or not path.is_file() or path.resolve().parent != path.parent.resolve():
            raise ValueError("linked_or_outside_file")
        if digest(path) != expected:
            raise ValueError("old_artifact_changed")
    files = [{**row, "allocatedBytesBefore": allocated_bytes(Path(row["path"]))} for row in before["files"]]
    free_before = shutil.disk_usage(HERE).free
    write_new(journal, {"beganAt": began, "at": now(), "files": files, "checkedFiles": len(checks),
        "priorReceiptSha256": digest(prior_path), "priorJournalSha256": digest(before_path),
        "codecProbeSha256": digest(probe_path), "sourceSha256": digest(Path(__file__)),
        "matchingProcesses": [], "operation": "EXACT_OLD_LOGS_LZX_COMPRESSION_NO_DELETE"})
    commands = []
    for row in files:
        command = ["compact.exe", "/c", "/f", "/exe:lzx", "/q", row["path"]]
        done = subprocess.run(command, capture_output=True, text=True,
            encoding="oem", errors="replace", timeout=300)
        record = {"command": command, "exitCode": done.returncode, "stdout": done.stdout,
            "stderr": done.stderr, "allocatedBytesAfter": allocated_bytes(Path(row["path"]))}
        commands.append(record)
        if done.returncode:
            break
    changed = [str(path) for path, expected in checks if digest(path) != expected]
    success = len(commands) == 3 and not changed and all(c["exitCode"] == 0 for c in commands)
    result = {"at": now(), "status": "VERIFIED" if success else "HOLD", "filesChecked": len(checks),
        "filesCompressed": len(commands), "changedFiles": changed, "deletedFiles": 0, "commands": commands,
        "freeBytesBefore": free_before, "freeBytesAfter": shutil.disk_usage(HERE).free,
        "allocatedBytesBefore": sum(f["allocatedBytesBefore"] for f in files),
        "allocatedBytesAfter": sum(allocated_bytes(Path(f["path"])) for f in files),
        "beforeJournal": str(journal), "beforeJournalSha256": digest(journal),
        "recovery": "compact.exe /u /exe on the exact three logs, then /c to restore NTFS compression. No file deleted or relocated.",
        "timingCaveat": "Overlaps development C; retain raw times and record environment deviation, not formal latency acceptance.",
        "oldOutcomeCaveat": "Old PASS/HOLD outcomes and all source bytes unchanged; storage success is not experiment acceptance."}
    write_new(output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "commands"}))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
