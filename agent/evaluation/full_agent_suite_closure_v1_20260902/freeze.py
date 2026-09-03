"""Freeze the current Python Agent source and test set."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    target = PACKAGE / "manifest.json"
    if target.exists() or (PACKAGE / "attempt001").exists():
        raise RuntimeError("already frozen or consumed")
    paths = sorted([* (ROOT / "agent/app").rglob("*.py"), * (ROOT / "agent/tests").rglob("*.py")])
    source = {path.relative_to(ROOT).as_posix(): sha(path) for path in paths}
    manifest = {"schemaVersion": "full-agent-suite-manifest-v1", "status": "FROZEN_BEFORE_EXECUTION", "attemptId": "attempt001", "sourceHashes": source, "sourceCount": len(source), "runnerSha256": sha(PACKAGE / "runner.py"), "preregistrationSha256": sha(PACKAGE / "preregistration.md")}
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "FROZEN", "sourceCount": len(source), "manifestSha256": sha(target)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

