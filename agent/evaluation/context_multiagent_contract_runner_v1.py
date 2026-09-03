"""Execute the frozen Context + Multi-Agent V1 contract coverage matrix.

Programmatic mutations are contract evidence, not independent effectiveness
samples. The runner verifies frozen source hashes before executing tests and
emits one deterministic receipt per concrete pytest node.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "evaluation" / "context-contract-matrix-v1.json"
DEFAULT_OUTPUT = (
    ROOT
    / "agent"
    / "evaluation"
    / "runs"
    / "context_multiagent_contract_v1_attempt001"
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReportCollector:
    def __init__(self) -> None:
        self.outcomes: dict[str, str] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call":
            self.outcomes[report.nodeid.replace("\\", "/")] = report.outcome.upper()


def git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def mutation_id(node_id: str) -> str:
    if "[" not in node_id:
        return "base"
    return node_id.rsplit("[", 1)[1].removesuffix("]")


def execute(matrix_path: Path, output_dir: Path, attempt_id: str) -> int:
    matrix_bytes = matrix_path.read_bytes()
    matrix = json.loads(matrix_bytes.decode("utf-8"))
    if matrix.get("status") != "FROZEN_BEFORE_MATRIX_EXECUTION":
        raise RuntimeError("contract matrix is not frozen before execution")

    actual_source_hashes: dict[str, str] = {}
    for relative, expected in matrix["sourceFiles"].items():
        actual = file_hash(ROOT / relative)
        actual_source_hashes[relative] = actual
        if actual != expected:
            raise RuntimeError(
                f"source hash mismatch for {relative}: expected {expected}, got {actual}"
            )

    requested = [
        node_id
        for rule in matrix["rules"]
        for node_id in rule["testNodeIds"]
    ]
    if len(requested) != len(set(requested)):
        raise RuntimeError("a concrete contract test node appears more than once")

    collector = ReportCollector()
    original_cwd = Path.cwd()
    try:
        # pytest node ids are rooted at agent/ for this repository.
        import os

        os.chdir(ROOT / "agent")
        exit_code = int(
            pytest.main(
                ["-q", "--disable-warnings", *requested],
                plugins=[collector],
            )
        )
    finally:
        os.chdir(original_cwd)

    concrete_results: list[dict[str, Any]] = []
    for rule in matrix["rules"]:
        for index, node_id in enumerate(rule["testNodeIds"], start=1):
            observed = collector.outcomes.get(node_id, "NOT_COLLECTED")
            actual_outcome = "PASS" if observed == "PASSED" else "FAIL"
            descriptor = {
                "baseCaseId": rule["baseCaseId"],
                "sourceClusterId": rule["sourceClusterId"],
                "ruleId": rule["ruleId"],
                "mutationId": mutation_id(node_id),
                "testNodeId": node_id,
                "sourceHashes": actual_source_hashes,
            }
            input_hash = sha256_json(descriptor)
            receipt_payload = {
                **descriptor,
                "inputHash": input_hash,
                "expectedOutcome": rule["expectedOutcome"],
                "actualOutcome": actual_outcome,
                "pytestOutcome": observed,
            }
            concrete_results.append(
                {
                    "caseId": f"{rule['ruleId']}:{index:02d}",
                    **receipt_payload,
                    "receiptHash": sha256_json(receipt_payload),
                }
            )

    failed = [
        case
        for case in concrete_results
        if case["actualOutcome"] != case["expectedOutcome"]
    ]
    status = "CONTRACT_ACCEPT" if exit_code == 0 and not failed else "CONTRACT_HOLD"
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "schemaVersion": "context-multiagent-contract-result-v1",
        "attemptId": attempt_id,
        "status": status,
        "completedAt": completed_at,
        "gitHead": git_head(),
        "matrixPath": str(matrix_path.relative_to(ROOT)).replace("\\", "/"),
        "matrixSha256": hashlib.sha256(matrix_bytes).hexdigest(),
        "runnerPath": str(Path(__file__).relative_to(ROOT)).replace("\\", "/"),
        "runnerSha256": file_hash(Path(__file__)),
        "sourceHashes": actual_source_hashes,
        "pytestExitCode": exit_code,
        "caseCount": len(concrete_results),
        "passedCount": len(concrete_results) - len(failed),
        "failedCount": len(failed),
        "independentEffectivenessSampleCount": 0,
        "productionDefaultsChanged": False,
        "sealedDataUsed": False,
    }
    result["resultHash"] = sha256_json(
        {key: value for key, value in result.items() if key != "completedAt"}
    )

    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "cases.jsonl").write_text(
        "".join(canonical_json(case) + "\n" for case in concrete_results),
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "schemaVersion": "context-multiagent-contract-run-receipt-v1",
        "attemptId": attempt_id,
        "resultHash": result["resultHash"],
        "casesSha256": file_hash(output_dir / "cases.jsonl"),
        "resultSha256": file_hash(output_dir / "result.json"),
        "decision": status,
    }
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "CONTRACT_ACCEPT" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--attempt-id", default="context-multiagent-contract-v1-attempt001"
    )
    args = parser.parse_args()
    matrix_path = args.matrix if args.matrix.is_absolute() else ROOT / args.matrix
    output_dir = (
        args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    )
    return execute(matrix_path, output_dir, args.attempt_id)


if __name__ == "__main__":
    raise SystemExit(main())
