"""Lossless storage maintenance from an immutable, verified closure manifest."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, write_new


def run(output):
    closure_path = HERE / "core72_v6_closeout001.json"
    closure = json.loads(closure_path.read_text(encoding="utf-8"))
    files = []
    for record in closure["records"]:
        directory = Path(record["directory"]).resolve()
        if directory.parent != HERE.resolve() or directory.name not in {f"core72_v6_{arm}001" for arm in "ABC"}:
            raise ValueError("unapproved_directory")
        for name, expected in record["sourceHashes"].items():
            path = directory / name
            if not path.resolve().is_relative_to(directory) or path.is_symlink():
                raise ValueError("linked_or_outside_target")
            if path.stat().st_size >= 65536:
                if digest(path) != expected:
                    raise ValueError("closed_artifact_changed:" + str(path))
                files.append({"path": str(path), "sha256": expected, "bytes": path.stat().st_size})
    before = shutil.disk_usage(HERE).free
    journal = output.with_name(output.stem + "_before.json")
    write_new(journal, {"sourceClosure": str(closure_path), "sourceClosureSha256": digest(closure_path),
        "files": files, "freeBytesBefore": before, "operation": "NTFS_COMPRESSION_NO_CONTENT_EDIT_NO_DELETE",
        "scriptSha256": digest(Path(__file__))})
    statuses = []
    for offset in range(0, len(files), 24):
        batch = files[offset:offset + 24]
        process = subprocess.run(["compact.exe", "/c", "/i", "/q", *[row["path"] for row in batch]],
            capture_output=True, text=True, errors="replace", timeout=120)
        statuses.append({"offset": offset, "exitCode": process.returncode,
            "stdout": process.stdout, "stderr": process.stderr})
        if process.returncode:
            break
    changed = [row["path"] for row in files if digest(Path(row["path"])) != row["sha256"]]
    value = {"status": "VERIFIED" if not changed and all(row["exitCode"] == 0 for row in statuses) else "HOLD",
        "filesChecked": len(files), "changedFiles": changed, "deletedFiles": 0,
        "freeBytesBefore": before, "freeBytesAfter": shutil.disk_usage(HERE).free,
        "journal": str(journal), "journalSha256": digest(journal), "commands": statuses,
        "recovery": "compact.exe /u on the same exact files reverses NTFS compression.",
        "note": "Free-space delta may include other activity; byte equality is verified independently."}
    write_new(output, value)
    print(json.dumps({key: value[key] for key in ("status", "filesChecked", "changedFiles", "freeBytesBefore", "freeBytesAfter", "deletedFiles")}))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
