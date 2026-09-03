"""Bind and replay distinct A/B/C memory governance without relevance claims.

This repair runner closes the V14 evidence gap where B and C called the same
ranking function.  It replays the frozen A/B/C scenarios (B = raw records,
C = production governance resolver) and the production V3 paired governance
runner.  It deliberately does not read a quality split or authorize defaults.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from evaluation import shopping_memory_abc_v1 as abc
from evaluation import shopping_memory_v13_governance_v1 as governance


ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n").encode("utf-8")


def app_source_manifest() -> tuple[bytes, str, int]:
    rows = []
    app_root = ROOT / "agent" / "app"
    for path in sorted(app_root.rglob("*.py"), key=lambda item: item.relative_to(ROOT).as_posix()):
        if "__pycache__" in path.parts:
            continue
        rows.append(f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}\n")
    payload = "".join(rows).encode("ascii")
    return payload, hashlib.sha256(payload).hexdigest(), len(rows)


def require_hash(path: Path, expected: object, label: str) -> None:
    if type(expected) is not str or sha256(path) != expected:
        raise ValueError(f"{label} hash mismatch")


def execute(*, contract_path: Path, output_dir: Path) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if contract.get("schemaVersion") != "shopping-memory-v14-distinct-abc-governance-contract-v1":
        raise ValueError("contract version mismatch")

    paths = contract["paths"]
    hashes = contract["hashes"]
    abc_runner = ROOT / paths["abcRunner"]
    governance_runner = ROOT / paths["governanceRunner"]
    asset_dir = ROOT / paths["abcAssetDir"]
    catalog_path = ROOT / paths["catalog"]
    governance_contract = ROOT / paths["governanceContract"]
    require_hash(Path(__file__), hashes["repairRunnerSha256"], "repair runner")
    require_hash(abc_runner, hashes["abcRunnerSha256"], "ABC runner")
    require_hash(governance_runner, hashes["governanceRunnerSha256"], "governance runner")
    require_hash(asset_dir / "manifest.json", hashes["abcManifestSha256"], "ABC manifest")
    require_hash(asset_dir / "scenarios.jsonl", hashes["abcScenariosSha256"], "ABC scenarios")
    require_hash(catalog_path, hashes["catalogSha256"], "catalog")
    require_hash(governance_contract, hashes["governanceContractSha256"], "governance contract")
    app_manifest, app_tree_hash, app_file_count = app_source_manifest()
    if app_tree_hash != hashes["appSourceTreeSha256"]:
        raise ValueError("app source tree hash mismatch")

    output_dir.mkdir(parents=True, exist_ok=False)
    abc_score = abc.evaluate(asset_dir, output_dir / "abc")
    governance_report_path = governance.materialize(output_dir / "governance")
    governance_report = json.loads(governance_report_path.read_text(encoding="utf-8"))
    traces = [
        json.loads(line) for line in (output_dir / "abc" / "traces.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    b_c_divergence = sum(
        row["arms"]["B"]["appliedEntryIds"] != row["arms"]["C"]["appliedEntryIds"]
        for row in traces
    )
    production_v3_cases = sum(
        row["execution_layer"] in {
            "production_v3_runtime", "production_v3_client", "production_context_projector",
        }
        for row in governance_report["cases"]
    )
    c_metrics = abc_score["metrics"]["C"]
    b_metrics = abc_score["metrics"]["B"]
    gates = {
        "abcControlledSliceAccepted": abc_score["verdict"] == "ACCEPT_CONTROLLED_MEMORY_SLICE",
        "bAndCDivergeOnAtLeastOneCase": b_c_divergence > 0,
        "bNaiveFalseInfluenceObserved": b_metrics["falseInfluenceRate"] > 0,
        "cApplicationPrecisionOne": c_metrics["applicationPrecision"] == 1.0,
        "cApplicationRecallOne": c_metrics["applicationRecall"] == 1.0,
        "cFalseInfluenceZero": c_metrics["falseInfluenceRate"] == 0.0,
        "productionGovernance32Of32": (
            governance_report["caseCount"] == 32
            and governance_report["failedCaseCount"] == 0
            and governance_report["boundedDecision"] == "BOUNDED_GOVERNANCE_ACCEPT"
        ),
        "productionV3OrContextCasesPresent": production_v3_cases > 0,
        "defaultsRemainHold": (
            abc_score["productionActivation"] == "HOLD_DEFAULT_OFF"
            and governance_report["productionDefaultDecision"] == "HOLD_UNCHANGED"
        ),
    }
    decision = (
        "BOUNDED_DISTINCT_ABC_GOVERNANCE_ACCEPT"
        if all(gates.values()) else "HOLD_DISTINCT_ABC_GOVERNANCE"
    )
    (output_dir / "app-source-manifest.txt").write_bytes(app_manifest)
    report = {
        "schemaVersion": "shopping-memory-v14-distinct-abc-governance-report-v1",
        "decision": decision,
        "arms": {
            "A": "no long-term memory",
            "B": "raw recalled records used directly without governance",
            "C": "production resolver plus production V3/context paired gates",
        },
        "counts": {
            "abcCases": len(traces),
            "bCDivergenceCases": b_c_divergence,
            "productionGovernanceCases": governance_report["caseCount"],
            "productionV3OrContextCases": production_v3_cases,
            "appSourceFilesBound": app_file_count,
        },
        "metrics": {"B": b_metrics, "C": c_metrics},
        "gates": gates,
        "inputs": {
            **hashes,
            "contractSha256": sha256(contract_path),
        },
        "explicitBoundaries": {
            "independentRelevanceQuality": False,
            "shoppingCompanionOracleQuality": False,
            "sealedOrConfirmationRead": False,
            "modelCalls": 0,
            "productionSwitchAuthority": False,
        },
    }
    report_path = output_dir / "report.json"
    report_path.write_bytes(canonical(report))
    receipt = {
        "schemaVersion": "shopping-memory-v14-distinct-abc-governance-receipt-v1",
        "decision": decision,
        "reportSha256": sha256(report_path),
        "abcScoreSha256": sha256(output_dir / "abc" / "score.json"),
        "abcTracesSha256": sha256(output_dir / "abc" / "traces.jsonl"),
        "governanceReportSha256": sha256(governance_report_path),
        "appSourceManifestSha256": sha256(output_dir / "app-source-manifest.txt"),
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_bytes(canonical(receipt))
    files = [
        output_dir / "app-source-manifest.txt", report_path, receipt_path,
        output_dir / "abc" / "score.json", output_dir / "abc" / "traces.jsonl",
        output_dir / "abc" / "receipt.json", governance_report_path,
        output_dir / "governance" / "SHA256SUMS.txt",
    ]
    (output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.relative_to(output_dir).as_posix()}\n" for path in files),
        encoding="ascii",
    )
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(execute(contract_path=args.contract.resolve(), output_dir=args.output_dir.resolve()))


if __name__ == "__main__":
    main()
