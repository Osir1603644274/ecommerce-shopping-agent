"""Reversible NTFS compression of exact closed experiment files, byte verified."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new


def run(output):
    # Do not add storage I/O competition to the currently collecting v5 cohort.
    for arm in "ABC":
        live = HERE / f"core48_v5_vivo_{arm}001"
        if not ((live / "result.json").exists() or (live / "failure.json").exists()):
            raise ValueError("wait_for_v5_cohort_to_close")
    targets = [HERE / f"core72_v4_{arm}001" for arm in "ABC"]
    files = []
    for directory in targets:
        directory = directory.resolve()
        if directory.parent != HERE.resolve() or directory.is_symlink():
            raise ValueError("unexpected_target_path")
        if not ((directory / "result.json").exists() or (directory / "failure.json").exists()):
            raise ValueError("target_not_closed")
        for path in directory.rglob("*"):
            if path.is_symlink() or (path.is_file() and not path.resolve().is_relative_to(directory)):
                raise ValueError("linked_target_not_supported")
            if path.is_file() and path.stat().st_size >= 65536:
                files.append(path)
    before = {str(p.relative_to(HERE)): {"sha256": file_sha(p), "bytes": p.stat().st_size} for p in files}
    journal = output.with_name(output.stem + "_before.json")
    start_free = shutil.disk_usage(HERE).free
    write_new(journal, {"operation": "NTFS_LOSSLESS_COMPRESSION_ONLY", "targets": [str(p) for p in targets],
        "files": before, "freeBytes": start_free, "sourceSha256": file_sha(__file__),
        "recovery": "compact.exe /u on the same exact files reverses compression; no file is deleted."})
    commands = []
    for offset in range(0, len(files), 32):
        batch = files[offset:offset + 32]
        result = subprocess.run(["compact.exe", "/c", "/i", "/q", *map(str, batch)],
            capture_output=True, text=True, errors="replace", timeout=300)
        commands.append({"offset": offset, "files": len(batch), "exitCode": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode != 0:
            break
    changed = [name for name, entry in before.items() if file_sha(HERE / name) != entry["sha256"]]
    value = {"status": "LOSSLESS_COMPRESSION_VERIFIED" if not changed and all(c["exitCode"] == 0 for c in commands) else "HOLD",
        "filesChecked": len(before), "changedContentFiles": changed, "commands": commands,
        "beforeJournal": str(journal), "beforeJournalSha256": file_sha(journal),
        "freeBytesBefore": start_free, "freeBytesAfter": shutil.disk_usage(HERE).free,
        "deletedFiles": 0, "modelCalls": 0, "sourceSha256": file_sha(__file__),
        "note": "Only completed v4 artifacts; no live v5 files or user services targeted. Free-space delta includes unrelated filesystem activity; content hashes are the preservation check. Compression is reversible via compact /u."}
    write_new(output, value)
    print(json.dumps({key: value[key] for key in ("status", "filesChecked", "changedContentFiles", "freeBytesBefore", "freeBytesAfter", "deletedFiles")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
