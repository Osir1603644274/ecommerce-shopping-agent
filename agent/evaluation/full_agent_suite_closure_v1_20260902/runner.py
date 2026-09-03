"""Run the one-shot frozen full Agent pytest suite and persist evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parent
ATTEMPT = PACKAGE / "attempt001"
MANIFEST = PACKAGE / "manifest.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["status"] != "FROZEN_BEFORE_EXECUTION" or sha(Path(__file__)) != manifest["runnerSha256"]:
        raise RuntimeError("runner not frozen")
    for relative, expected in manifest["sourceHashes"].items():
        if sha(ROOT / relative) != expected:
            raise RuntimeError(f"source changed: {relative}")
    if ATTEMPT.exists():
        raise RuntimeError("attempt consumed")
    ATTEMPT.mkdir(parents=True, exist_ok=False)
    started_path = ATTEMPT / "started.json"
    started_path.write_text(json.dumps({"schemaVersion": "full-agent-suite-started-v1", "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "manifestSha256": sha(MANIFEST), "runnerSha256": sha(Path(__file__)), "sourceCount": manifest["sourceCount"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    junit = ATTEMPT / "junit.xml"
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT};{ROOT / 'agent'}"
    completed = subprocess.run(["python", "-m", "pytest", "agent/tests", "-q", f"--junitxml={junit}"], cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    (ATTEMPT / "stdout.txt").write_text(completed.stdout, encoding="utf-8", newline="\n")
    (ATTEMPT / "stderr.txt").write_text(completed.stderr, encoding="utf-8", newline="\n")
    root = ElementTree.parse(junit).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise RuntimeError("junit testsuite missing")
    total = int(suite.attrib["tests"])
    failures = int(suite.attrib.get("failures", "0"))
    errors = int(suite.attrib.get("errors", "0"))
    skipped = int(suite.attrib.get("skipped", "0"))
    passed = total - failures - errors - skipped
    status = "BOUNDED_FULL_AGENT_SUITE_ACCEPT" if completed.returncode == 0 and failures == 0 and errors == 0 else "HOLD_FULL_AGENT_SUITE"
    result = {"schemaVersion": "full-agent-suite-result-v1", "status": status, "tests": total, "passed": passed, "failed": failures, "errors": errors, "skipped": skipped, "elapsedSeconds": float(suite.attrib.get("time", "0")), "returnCode": completed.returncode, "scope": "frozen_python_agent_unit_and_integration_tests_not_external_production_readiness"}
    result_path = ATTEMPT / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    (ATTEMPT / "receipt.json").write_text(json.dumps({"status": status, "startedSha256": sha(started_path), "junitSha256": sha(junit), "stdoutSha256": sha(ATTEMPT / "stdout.txt"), "stderrSha256": sha(ATTEMPT / "stderr.txt"), "resultSha256": sha(result_path)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "BOUNDED_FULL_AGENT_SUITE_ACCEPT" else 2


if __name__ == "__main__":
    raise SystemExit(main())

