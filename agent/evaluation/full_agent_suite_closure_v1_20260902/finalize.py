"""Verify receipt bindings and checksum the full-suite evidence package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


PACKAGE = Path(__file__).resolve().parent
FILES = [
    "preregistration.md", "freeze.py", "runner.py", "manifest.json",
    "attempt001/started.json", "attempt001/junit.xml", "attempt001/stdout.txt",
    "attempt001/stderr.txt", "attempt001/result.json", "attempt001/receipt.json",
    "FINAL_EVIDENCE_AND_DECISION_2026-09-02.md",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    result = json.loads((PACKAGE / "attempt001/result.json").read_text(encoding="utf-8"))
    receipt = json.loads((PACKAGE / "attempt001/receipt.json").read_text(encoding="utf-8"))
    checks = {
        "allFilesPresent": all((PACKAGE / name).is_file() for name in FILES),
        "boundedAccept": result["status"] == "BOUNDED_FULL_AGENT_SUITE_ACCEPT",
        "zeroFailures": result["failed"] == 0 and result["errors"] == 0,
        "countConsistent": result["tests"] == result["passed"] + result["failed"] + result["errors"] + result["skipped"],
        "startedBound": receipt["startedSha256"] == sha(PACKAGE / "attempt001/started.json"),
        "junitBound": receipt["junitSha256"] == sha(PACKAGE / "attempt001/junit.xml"),
        "stdoutBound": receipt["stdoutSha256"] == sha(PACKAGE / "attempt001/stdout.txt"),
        "stderrBound": receipt["stderrSha256"] == sha(PACKAGE / "attempt001/stderr.txt"),
        "resultBound": receipt["resultSha256"] == sha(PACKAGE / "attempt001/result.json"),
    }
    (PACKAGE / "SHA256SUMS.txt").write_text("".join(f"{sha(PACKAGE / name)}  {name}\n" for name in FILES), encoding="utf-8", newline="\n")
    verification = {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks, "checkedFiles": len(FILES)}
    (PACKAGE / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    return 0 if verification["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

