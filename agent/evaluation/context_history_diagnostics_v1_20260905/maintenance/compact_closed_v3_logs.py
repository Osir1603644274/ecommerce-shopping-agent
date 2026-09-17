"""Reversible storage-only maintenance of three exact, terminal v3 state logs.

HOLD outcomes remain HOLD. This does not assert experiment or quality acceptance.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new


def run(output):
    began = now()
    output = output.resolve()
    if output.parent != HERE.resolve() or output.exists():
        raise ValueError("new_direct_child_output_required")
    journal = output.with_name(output.stem + "_before.json")
    if journal.exists():
        raise ValueError("journal_already_exists")
    inventory = subprocess.run(["powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','codex.exe','redis-server.exe') } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    if inventory.returncode:
        raise ValueError("process_inventory_failed")
    processes = json.loads(inventory.stdout or "[]")
    if isinstance(processes, dict):
        processes = [processes]
    files, terminals = [], []
    for arm in "ABC":
        directory = HERE / f"core48_v3_{arm}001"
        if directory.is_symlink() or directory.is_junction() or directory.resolve().parent != HERE.resolve():
            raise ValueError("linked_or_outside_attempt")
        target = str(directory).replace("\\", "/").casefold()
        if any(target in (p.get("CommandLine") or "").replace("\\", "/").casefold() for p in processes):
            raise ValueError("old_attempt_still_running")
        result_path = directory / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (result.get("status") not in {"INTEGRATION_PASS", "INTEGRATION_HOLD"}
                or result.get("turns") != 48 or result.get("plannedTurns") != 48
                or result.get("dataCollectionComplete") is not True):
            raise ValueError("old_collection_not_terminal")
        expected = {f"turn-{n:02d}.json" for n in range(1, 49)}
        turns = list(directory.glob("turn-*.json"))
        if {p.name for p in turns} != expected:
            raise ValueError("incomplete_turn_files")
        for path in [result_path, *turns, directory / "state_transitions.jsonl"]:
            if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
                raise ValueError("linked_or_missing_artifact")
        terminals.append({"directory": str(directory), "resultSha256": digest(result_path),
            "statusUnchanged": result["status"], "unsafeTerminalTurns": result.get("unsafeTerminalTurns"),
            "turnHashes": {p.name: digest(p) for p in turns}})
        path = directory / "state_transitions.jsonl"
        files.append({"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path)})
    before = shutil.disk_usage(HERE).free
    write_new(journal, {"beganAt": began, "at": now(), "files": files, "terminals": terminals,
        "freeBytesBefore": before, "matchingProcesses": [], "processInventorySucceeded": True,
        "operation": "REVERSIBLE_NTFS_COMPRESSION_NO_DELETE", "sourceSha256": digest(Path(__file__)),
        "scope": "Only three named state logs; no scientific acceptance or claim of complete historical usage accounting."})
    commands = []
    for row in files:
        done = subprocess.run(["compact.exe", "/c", "/i", "/q", row["path"]],
            capture_output=True, text=True, encoding="oem", errors="replace", timeout=120)
        commands.append({"path": row["path"], "exitCode": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
        if done.returncode:
            break
    changed = [row["path"] for row in files if digest(Path(row["path"])) != row["sha256"]]
    for terminal in terminals:
        directory = Path(terminal["directory"])
        if digest(directory / "result.json") != terminal["resultSha256"]:
            changed.append(str(directory / "result.json"))
        changed.extend(str(directory / name) for name, expected in terminal["turnHashes"].items()
            if digest(directory / name) != expected)
    value = {"at": now(), "status": "VERIFIED" if not changed and len(commands) == 3 and all(c["exitCode"] == 0 for c in commands) else "HOLD",
        "filesCompressed": len(commands), "filesChecked": 150, "changedFiles": changed, "deletedFiles": 0,
        "freeBytesBefore": before, "freeBytesAfter": shutil.disk_usage(HERE).free,
        "beforeJournal": str(journal), "beforeJournalSha256": digest(journal), "commands": commands,
        "recovery": "compact.exe /u on the three exact logs reverses NTFS compression; original bytes preserved.",
        "timingCaveat": "Concurrent general72 development B; retain raw times, not formal latency acceptance."}
    write_new(output, value)
    print(json.dumps({k: v for k, v in value.items() if k != "commands"}))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
