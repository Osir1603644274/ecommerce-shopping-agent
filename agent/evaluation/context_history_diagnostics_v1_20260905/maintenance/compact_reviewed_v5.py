"""Compress only three exact, complete and independently reviewed old attempts."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new


def run(output):
    mapping_path = HERE / "core48_v5_vivo_complete_review_resume001/mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json"
    review_result = mapping_path.parent / "result.json"
    if json.loads(review_result.read_text(encoding="utf-8"))["status"] != "DEVELOPMENT_EVERY_TURN_REVIEW_REQUIRES_CLAIM_AUDIT":
        raise ValueError("old_review_not_complete")
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    allowed = {HERE / f"core48_v5_vivo_{arm}001" for arm in "ABC"}
    directories = [Path(row["directory"]) for row in mapping]
    if set(directories) != allowed or len(directories) != 3:
        raise ValueError("exact_three_old_attempts_required")
    inventory = subprocess.run(["powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','codex.exe','redis-server.exe') } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    if inventory.returncode:
        raise ValueError("process_inventory_failed")
    processes = json.loads(inventory.stdout or "[]")
    if isinstance(processes, dict):
        processes = [processes]
    files, terminals = [], {}
    for row, directory in zip(mapping, directories):
        if directory.is_symlink() or directory.resolve().parent != HERE.resolve():
            raise ValueError("linked_or_outside_attempt")
        target = str(directory).replace("\\", "/").casefold()
        if any(target in (process.get("CommandLine") or "").replace("\\", "/").casefold() for process in processes):
            raise ValueError("old_attempt_still_running")
        terminal = directory / "result.json"
        if json.loads(terminal.read_text(encoding="utf-8"))["status"] != "INTEGRATION_PASS":
            raise ValueError("old_attempt_not_complete")
        expected_names = {f"turn-{turn:02d}.json" for turn in range(1, 49)}
        if set(row["turnHashes"]) != expected_names:
            raise ValueError("old_review_turn_coverage_mismatch")
        for name, expected in row["turnHashes"].items():
            if digest(directory / name) != expected:
                raise ValueError("old_reviewed_turn_changed")
        terminals[str(terminal)] = digest(terminal)
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("linked_artifact_rejected")
            if path.is_file() and path.stat().st_size >= 65536:
                files.append({"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path)})
    journal = output.with_name(output.stem + "_before.json")
    before = shutil.disk_usage(HERE).free
    write_new(journal, {"at": now(), "files": files, "mappingPath": str(mapping_path),
        "mappingSha256": digest(mapping_path), "reviewResultSha256": digest(review_result),
        "attemptTerminalHashes": terminals, "freeBytesBefore": before, "processInventory": processes,
        "operation": "REVERSIBLE_NTFS_COMPRESSION_NO_DELETE", "sourceSha256": digest(Path(__file__))})
    commands = []
    for offset in range(0, len(files), 24):
        command = ["compact.exe", "/c", "/i", "/q", *[row["path"] for row in files[offset:offset + 24]]]
        done = subprocess.run(command, capture_output=True, text=True, encoding="oem", errors="replace", timeout=120)
        commands.append({"offset": offset, "exitCode": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
        if done.returncode:
            break
    changed = [row["path"] for row in files if digest(Path(row["path"])) != row["sha256"]]
    value = {"at": now(), "status": "VERIFIED" if not changed and all(row["exitCode"] == 0 for row in commands) else "HOLD",
        "filesChecked": len(files), "changedFiles": changed, "deletedFiles": 0,
        "freeBytesBefore": before, "freeBytesAfter": shutil.disk_usage(HERE).free,
        "beforeJournal": str(journal), "beforeJournalSha256": digest(journal), "commands": commands,
        "recovery": "compact.exe /u on these exact files reverses metadata compression; original bytes preserved.",
        "timingCaveat": "Overlaps general72 development B. Preserve all times; do not promote this cohort timing to formal latency acceptance."}
    write_new(output, value)
    print(json.dumps({key: value[key] for key in ("status", "filesChecked", "changedFiles", "freeBytesBefore", "freeBytesAfter", "deletedFiles")}))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
