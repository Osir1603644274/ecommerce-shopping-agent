"""Exact, terminal, owned cohort child only; reversible NTFS compression."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new


def run(directory, output):
    raw = directory
    directory = directory.resolve()
    if raw.is_symlink() or directory.parent.parent != HERE.resolve():
        raise ValueError("only_declared_nested_cohort_child_allowed")
    manifest = json.loads((directory.parent / "started.json").read_text(encoding="utf-8"))
    jobs = [job for job in manifest.get("jobs", []) if Path(job.get("output", "")).resolve() == directory]
    if manifest.get("kind") != "SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL" or len(jobs) != 1:
        raise ValueError("cohort_target_not_uniquely_bound")
    supervisor = Path(jobs[0]["supervisor"]).resolve()
    if supervisor.parent != directory.parent:
        raise ValueError("supervisor_not_sibling")
    terminal = json.loads((supervisor / "result.json").read_text(encoding="utf-8"))
    if terminal.get("childExitCode") != 0 or not (directory / "result.json").exists():
        raise ValueError("successful_closed_attempt_required")
    # Inspect only process metadata; do not rely on stale PID existence alone.
    inventory = subprocess.run(["powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','redis-server.exe','codex.exe') } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"], capture_output=True, text=True,
        encoding="utf-8", timeout=30)
    if inventory.returncode:
        raise ValueError("process_inventory_failed")
    processes = json.loads(inventory.stdout or "[]")
    if isinstance(processes, dict):
        processes = [processes]
    target = str(directory).replace("\\", "/").casefold()
    maintenance_pids = {os.getpid(), os.getppid()}
    if any(row.get("ProcessId") not in maintenance_pids and
           target in (row.get("CommandLine") or "").replace("\\", "/").casefold() for row in processes):
        raise ValueError("target_process_still_running")
    files = []
    for path in directory.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError("linked_target_rejected")
        if path.is_file() and path.stat().st_size >= 65536:
            files.append({"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path)})
    before = shutil.disk_usage(HERE).free
    journal = output.with_name(output.stem + "_before.json")
    write_new(journal, {"at": now(), "directory": str(directory), "files": files,
        "supervisorTerminalHash": digest(supervisor / "result.json"), "freeBytesBefore": before,
        "operation": "REVERSIBLE_NTFS_COMPRESSION_NO_DELETE", "sourceSha256": digest(Path(__file__))})
    results = []
    for offset in range(0, len(files), 24):
        process = subprocess.run(["compact.exe", "/c", "/i", "/q", *[row["path"] for row in files[offset:offset + 24]]],
            capture_output=True, text=True, errors="replace", timeout=120)
        results.append({"offset": offset, "exitCode": process.returncode, "stdout": process.stdout, "stderr": process.stderr})
        if process.returncode:
            break
    changed = [row["path"] for row in files if digest(Path(row["path"])) != row["sha256"]]
    value = {"at": now(), "status": "VERIFIED" if not changed and all(row["exitCode"] == 0 for row in results) else "HOLD",
        "directory": str(directory), "filesChecked": len(files), "changedFiles": changed,
        "freeBytesBefore": before, "freeBytesAfter": shutil.disk_usage(HERE).free,
        "deletedFiles": 0, "commands": results, "beforeJournal": str(journal), "beforeJournalSha256": digest(journal),
        "recovery": "compact.exe /u for these exact files reverses compression.",
        "timingCaveat": "Storage maintenance may overlap another development arm. Preserve its times; do not promote this single-order development timing to formal latency evidence."}
    write_new(output, value)
    print(json.dumps({key: value[key] for key in ("status", "filesChecked", "changedFiles", "freeBytesBefore", "freeBytesAfter", "deletedFiles")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.directory, args.output)
