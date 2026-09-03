"""Compose distinct A/B/C governance with real Java/MySQL/Redis execution."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def execute(*, contract_path: Path, output_dir: Path) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if contract.get("schemaVersion") != "shopping-memory-v14-governance-acceptance-contract-v1":
        raise ValueError("contract version mismatch")
    inputs: dict[str, dict[str, str]] = contract["inputs"]
    loaded: dict[str, Any] = {}
    for name, item in inputs.items():
        path = ROOT / item["path"]
        if sha256(path) != item["sha256"]:
            raise ValueError(f"{name} hash mismatch")
        if path.suffix == ".json":
            loaded[name] = json.loads(path.read_text(encoding="utf-8"))

    distinct = loaded["distinctReport"]
    real = loaded["realInfraReport"]
    settings_text = (ROOT / inputs["pythonSettings"]["path"]).read_text(encoding="utf-8")
    java_text = (ROOT / inputs["javaSettings"]["path"]).read_text(encoding="utf-8")
    gates = {
        "distinctABCControlledAccept": distinct.get("decision") == "BOUNDED_DISTINCT_ABC_GOVERNANCE_ACCEPT",
        "distinctABCActuallyDiverges": distinct.get("counts", {}).get("bCDivergenceCases", 0) > 0,
        "realInfrastructureAccept": real.get("decision") == "REAL_INFRA_NO_MODEL_THREE_SESSION_V2_ACCEPT",
        "realConsentCommandProjection": real.get("checks", {}).get("realConsentCommandProjection") is True,
        "realProductionV3Resolver": real.get("checks", {}).get("productionV3Resolver") is True,
        "realProductionContextProjector": real.get("checks", {}).get("productionContextProjector") is True,
        "realProductionSoftRerankChangedOrder": real.get("checks", {}).get("productionSoftRerankChangedOrder") is True,
        "realCurrentTaskOverrideWins": real.get("checks", {}).get("currentTaskOverrideWins") is True,
        "realRevocationVisible": real.get("checks", {}).get("revocationVisible") is True,
        "realCandidateSetUnchanged": real.get("checks", {}).get("candidateSetUnchanged") is True,
        "modelTripwireZero": (
            real.get("modelTripwire", {}).get("getClientAttempts") == 0
            and real.get("modelTripwire", {}).get("modelObservationAttempts") == 0
        ),
        "pythonDefaultsFalse": (
            "memory_projection_client_enabled: bool = False" in settings_text
            and "memory_bff_enabled: bool = False" in settings_text
        ),
        "javaDefaultFalse": "${SHOPPING_MEMORY_ENABLED:false}" in java_text,
        "productionDefaultUnchanged": real.get("productionDefaultChanged") is False,
    }
    decision = "BOUNDED_REAL_PATH_GOVERNANCE_ACCEPT" if all(gates.values()) else "HOLD_REAL_PATH_GOVERNANCE"
    report = {
        "schemaVersion": "shopping-memory-v14-governance-acceptance-report-v1",
        "decision": decision,
        "gates": gates,
        "evidence": {
            "distinctABC": {
                "cases": distinct["counts"]["abcCases"],
                "bCDivergenceCases": distinct["counts"]["bCDivergenceCases"],
                "cFalseInfluenceRate": distinct["metrics"]["C"]["falseInfluenceRate"],
            },
            "realInfrastructure": {
                "sessions": real["sessionCount"],
                "taskStates": real["realTaskStateCount"],
                "databaseRevisions": real["databaseProjectionRevisions"],
                "claimUpperBound": real["claimUpperBound"],
            },
        },
        "inputHashes": {name: item["sha256"] for name, item in inputs.items()},
        "contractSha256": sha256(contract_path),
        "explicitBoundaries": {
            "independentRankingQuality": False,
            "realRetrieval": False,
            "llmExtraction": False,
            "browserRendering": False,
            "sealedOrConfirmationRead": False,
            "productionSwitchAuthority": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / "report.json"
    report_path.write_bytes(canonical(report))
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_bytes(canonical({
        "schemaVersion": "shopping-memory-v14-governance-acceptance-receipt-v1",
        "decision": decision,
        "runnerSha256": sha256(Path(__file__)),
        "contractSha256": sha256(contract_path),
        "reportSha256": sha256(report_path),
    }))
    (output_dir / "SHA256SUMS.txt").write_text(
        f"{sha256(report_path)}  report.json\n{sha256(receipt_path)}  receipt.json\n",
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
