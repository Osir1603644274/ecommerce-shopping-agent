from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[2]
AGENT = ROOT / "agent"
BACKEND = ROOT / "backend"
MANIFEST = PACKAGE / "manifest.json"
ATTEMPT = PACKAGE / "attempt001"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def verify_sources(manifest: dict) -> list[dict]:
    rows = []
    for rel, expected in manifest["sourceHashes"].items():
        path = ROOT / rel
        actual = sha(path) if path.is_file() else None
        rows.append({"path": rel, "expected": expected, "actual": actual, "ok": actual == expected})
    return rows


def run(command: list[str], cwd: Path, env: dict[str, str]) -> dict:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return {
        "command": command,
        "exitCode": completed.returncode,
        "durationMs": round((time.perf_counter() - started) * 1000, 3),
        "output": completed.stdout,
    }


def java_report() -> dict:
    classes = (
        "com.example.locallife.ordering.CommerceHttpContractTests",
        "com.example.locallife.payment.PaymentServiceIntegrationTests",
    )
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    files = []
    for name in classes:
        path = BACKEND / "target" / "surefire-reports" / f"TEST-{name}.xml"
        files.append(str(path.relative_to(ROOT)).replace("\\", "/"))
        root = ET.parse(path).getroot()
        for field in totals:
            totals[field] += int(root.attrib.get(field, "0"))
    return {**totals, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    manifest = load_manifest()
    checks = verify_sources(manifest)
    if not all(row["ok"] for row in checks):
        print(json.dumps({"sourceChecks": checks}, ensure_ascii=False, indent=2))
        return 2
    if args.preflight:
        print(json.dumps({"status": "PREFLIGHT_ACCEPT", "sourceChecks": checks}, ensure_ascii=False, indent=2))
        return 0
    if ATTEMPT.exists():
        raise SystemExit("attempt001 already exists; formal attempt is immutable")
    ATTEMPT.mkdir(parents=False)
    started = {
        "schemaVersion": "transaction-command-recovery-started-v1",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "manifestSha256": sha(MANIFEST),
        "sourceChecks": checks,
    }
    (ATTEMPT / "started.json").write_text(
        json.dumps(started, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + str(AGENT)
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    py = run(
        [
            str(python), "-m", "pytest",
            str(AGENT / "tests" / "test_transaction_agent_runtime.py"),
            str(AGENT / "tests" / "test_transaction_command_recovery_redis.py"),
            str(AGENT / "tests" / "test_ecommerce_transactions.py"),
            "-q",
        ],
        AGENT,
        env,
    )
    (ATTEMPT / "python-tests.txt").write_text(py["output"], encoding="utf-8")

    java_home = Path.home() / ".jdks" / "jdk-21" / "jdk-21.0.10"
    java_env = dict(env)
    java_env["JAVA_HOME"] = str(java_home)
    java_env["PATH"] = str(java_home / "bin") + os.pathsep + java_env.get("PATH", "")
    java = run(
        [
            str(BACKEND / "mvnw.cmd"), "-q",
            "-Dtest=CommerceHttpContractTests,PaymentServiceIntegrationTests",
            "test",
        ],
        BACKEND,
        java_env,
    )
    (ATTEMPT / "java-tests.txt").write_text(java["output"], encoding="utf-8")
    reports = java_report() if java["exitCode"] == 0 else {"tests": 0, "failures": 1, "errors": 0, "skipped": 0, "files": []}

    settings_text = (AGENT / "app" / "settings.py").read_text(encoding="utf-8")
    inbox_text = (AGENT / "app" / "graph" / "tool_inbox_v2.py").read_text(encoding="utf-8")
    gates = {
        "sourceHashesMatch": all(row["ok"] for row in checks),
        "pythonRecoverySuitePass": py["exitCode"] == 0 and "14 passed" in py["output"] and "skipped" not in py["output"],
        "javaIntegrationPass": java["exitCode"] == 0 and reports["failures"] == 0 and reports["errors"] == 0 and reports["skipped"] == 0,
        "transactionDefaultOff": "agent_transaction_enabled: bool = False" in settings_text,
        "toolInboxReadOnlyUnchanged": '_READ_ONLY = frozenset({"search_products", "get_product_details", "compare_products", "rerank_products_in_scope"})' in inbox_text,
    }
    accepted = all(gates.values())
    decision = "BOUNDED_TRANSACTION_COMMAND_RECOVERY_ACCEPT" if accepted else "HOLD_TRANSACTION_COMMAND_RECOVERY"
    result = {
        "schemaVersion": "transaction-command-recovery-result-v1",
        "decision": decision,
        "gates": gates,
        "python": {k: v for k, v in py.items() if k != "output"},
        "java": {**{k: v for k, v in java.items() if k != "output"}, "reports": reports},
        "scenarioCount": len(json.loads((PACKAGE / "scenarios.json").read_text(encoding="utf-8"))["scenarioFamilies"]),
        "boundaries": [
            "ToolInbox remains read-only",
            "TransactionAgent remains default-off",
            "No cross-system exactly-once claim",
            "Real Redis plus Spring/H2 integration; no multi-host or production traffic claim",
        ],
    }
    result_path = ATTEMPT / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "schemaVersion": "transaction-command-recovery-receipt-v1",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "manifestSha256": sha(MANIFEST),
        "startedSha256": sha(ATTEMPT / "started.json"),
        "resultSha256": sha(result_path),
        "pythonOutputSha256": sha(ATTEMPT / "python-tests.txt"),
        "javaOutputSha256": sha(ATTEMPT / "java-tests.txt"),
    }
    receipt_path = ATTEMPT / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    report = f"""# Transaction Command Recovery V1 result

- Decision: `{decision}`
- Frozen scenarios: `{result['scenarioCount']}`
- Python recovery suite: exit `{py['exitCode']}`; real Redis restart case required and not skipped.
- Java Spring/H2 integration: `{reports['tests']}` tests, `{reports['failures']}` failures, `{reports['errors']}` errors, `{reports['skipped']}` skipped.
- Default: TransactionAgent remains off; ToolInbox remains read-only.

This bounded result accepts durable confirmed-command retention, authority
reconciliation and key-stable replay. It does not claim distributed atomicity,
cross-system exactly-once, production readiness, or default enablement.
"""
    (ATTEMPT / "RESULT.md").write_text(report, encoding="utf-8")

    checksum_paths = [
        PACKAGE / "preregistration.md", PACKAGE / "scenarios.json", PACKAGE / "runner.py", MANIFEST,
        ATTEMPT / "started.json", ATTEMPT / "python-tests.txt", ATTEMPT / "java-tests.txt",
        result_path, receipt_path, ATTEMPT / "RESULT.md",
    ]
    checksum_text = "".join(f"{sha(path)}  {path.relative_to(PACKAGE).as_posix()}\n" for path in checksum_paths)
    (PACKAGE / "SHA256SUMS.txt").write_text(checksum_text, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
