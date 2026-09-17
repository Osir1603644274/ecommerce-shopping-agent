"""Append-only experiment artifacts; refuses to replace prior evidence."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def append(path, value):
    with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def capture_sources(output):
    paths = sorted((ROOT / "agent/app").rglob("*.py")) + sorted(HERE.glob("*.py"))
    paths += [ROOT / "agent/evaluation/context_codex_abc_pilot_v1_20260904/pilot.py",
              ROOT / "agent/evaluation/context_codex_abc_pilot_v1_20260904/inputs/base_instructions.txt",
              ROOT / "agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/lane_runtime.py"]
    hashes = {}
    for source in paths:
        relative = source.relative_to(ROOT)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(source.read_bytes())
        hashes[relative.as_posix()] = file_sha(target)
    write_new(output / "manifest.json", {"at": now(), "sources": hashes})
    return hashes


def verify_sources(hashes):
    changed = [relative for relative, digest in hashes.items() if file_sha(ROOT / relative) != digest]
    if changed:
        raise RuntimeError("source_changed_during_attempt:" + ",".join(changed))


def baseline():
    out = HERE / "baseline"
    out.mkdir(exist_ok=False)
    paths = sorted((ROOT / "agent/app").rglob("*.py"))
    paths += [ROOT / "agent/tests/fake_redis.py", ROOT / "agent/evaluation/context_codex_abc_pilot_v1_20260904/pilot.py",
              ROOT / "agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/lane_runtime.py"]
    hashes = {}
    for source in paths:
        relative = source.relative_to(ROOT)
        target = out / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(source.read_bytes())
        hashes[relative.as_posix()] = file_sha(source)
    write_new(out / "manifest.json", {"at": now(), "sources": hashes,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "priorDirtyStatus": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True, encoding="utf-8"),
        "note": "Pre-existing changes belong to the user. No reset, checkout, commit or old attempt mutation."})
    print(canonical({"status": "BASELINE_SAVED", "files": len(hashes), "path": str(out)}))


if __name__ == "__main__":
    baseline()
