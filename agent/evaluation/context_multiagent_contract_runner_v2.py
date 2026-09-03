"""Execute the active V1.2 Context + Multi-Agent contract test matrix.

Every concrete pytest node from the frozen test files is one contract case.
Parameterized nodes are recorded as mutations of the same source cluster and
are never counted as independent effectiveness samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "evaluation" / "context-contract-execution-v2.json"
DEFAULT_OUTPUT = (
    ROOT
    / "agent"
    / "evaluation"
    / "runs"
    / "context_multiagent_contract_v2_attempt001"
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Collector:
    def __init__(self) -> None:
        self.outcomes: dict[str, str] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call":
            self.outcomes[report.nodeid.replace("\\", "/")] = report.outcome.upper()


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def execute(manifest_path: Path, output_dir: Path, attempt_id: str) -> int:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("status") != "FROZEN_BEFORE_EXECUTION":
        raise RuntimeError("active contract execution manifest is not frozen")
    source_hashes: dict[str, str] = {}
    for relative, expected in manifest["sourceFiles"].items():
        actual = file_hash(ROOT / relative)
        source_hashes[relative] = actual
        if actual != expected:
            raise RuntimeError(
                f"source hash mismatch for {relative}: expected {expected}, got {actual}"
            )

    collector = Collector()
    original_cwd = Path.cwd()
    try:
        os.chdir(ROOT / "agent")
        exit_code = int(
            pytest.main(
                ["-q", "--disable-warnings", *manifest["testFiles"]],
                plugins=[collector],
            )
        )
    finally:
        os.chdir(original_cwd)

    if len(collector.outcomes) != manifest["expectedConcreteCaseCount"]:
        raise RuntimeError(
            "concrete contract case count changed after freeze: "
            f"expected {manifest['expectedConcreteCaseCount']}, "
            f"got {len(collector.outcomes)}"
        )
    cases: list[dict[str, Any]] = []
    for node_id, pytest_outcome in sorted(collector.outcomes.items()):
        file_name, test_name = node_id.split("::", 1)
        base_name = test_name.split("[", 1)[0]
        mutation = (
            test_name.rsplit("[", 1)[1].removesuffix("]")
            if "[" in test_name
            else "base"
        )
        descriptor = {
            "baseCaseId": base_name,
            "sourceClusterId": f"{file_name}::{base_name}",
            "ruleId": base_name.removeprefix("test_"),
            "mutationId": mutation,
            "testNodeId": node_id,
            "sourceHashes": source_hashes,
        }
        input_hash = sha256_json(descriptor)
        actual = "PASS" if pytest_outcome == "PASSED" else "FAIL"
        receipt_payload = {
            **descriptor,
            "inputHash": input_hash,
            "expectedOutcome": "PASS",
            "actualOutcome": actual,
            "pytestOutcome": pytest_outcome,
        }
        cases.append(
            {
                "caseId": "ccv2-" + sha256_json(descriptor)[:16],
                **receipt_payload,
                "receiptHash": sha256_json(receipt_payload),
            }
        )

    failed = [case for case in cases if case["actualOutcome"] != "PASS"]
    status = "CONTRACT_ACCEPT" if exit_code == 0 and not failed else "CONTRACT_HOLD"
    summary = {
        "schemaVersion": "context-multiagent-contract-result-v2",
        "attemptId": attempt_id,
        "status": status,
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gitHead": git_head(),
        "manifestPath": str(manifest_path.relative_to(ROOT)).replace("\\", "/"),
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "runnerPath": str(Path(__file__).relative_to(ROOT)).replace("\\", "/"),
        "runnerSha256": file_hash(Path(__file__)),
        "caseCount": len(cases),
        "passedCount": len(cases) - len(failed),
        "failedCount": len(failed),
        "independentEffectivenessSampleCount": 0,
        "modelCalls": 0,
        "sealedDataUsed": False,
        "productionDefaultsChanged": False,
    }
    summary["resultHash"] = sha256_json(
        {key: value for key, value in summary.items() if key != "completedAt"}
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    cases_path = output_dir / "cases.jsonl"
    cases_path.write_text(
        "".join(canonical_json(case) + "\n" for case in cases),
        encoding="utf-8",
        newline="\n",
    )
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "schemaVersion": "context-multiagent-contract-run-receipt-v2",
        "attemptId": attempt_id,
        "decision": status,
        "resultHash": summary["resultHash"],
        "casesSha256": file_hash(cases_path),
        "summarySha256": file_hash(summary_path),
    }
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status == "CONTRACT_ACCEPT" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--attempt-id", default="context-multiagent-contract-v2-attempt001"
    )
    args = parser.parse_args()
    manifest = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    return execute(manifest, output, args.attempt_id)


if __name__ == "__main__":
    raise SystemExit(main())
