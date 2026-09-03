"""Run frozen Memory V14 quality metrics on contaminated public validation."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from evaluation.shopping_memory_v14_public_dev_quality_v1 import (
    BOOTSTRAP_ITERATIONS,
    BOOTSTRAP_SEED,
    GOVERNANCE_SHA256,
    MEMORY_WEIGHT,
    QualityError,
    cluster_bootstrap,
    means,
    percentile,
    rank_candidates,
    ranking_metrics,
    read_json,
    read_jsonl,
    sha256,
)


DATASET_SHA256 = "7aa11b18e4d957634c1d79ff73341d2729bb4c241ef343435ecb08f670381dec"
DATA_REPORT_SHA256 = "a01806331fb2efda27167fa4878e3e9090b6c9189ae845c18178e5466f5cd850"
DATA_RECEIPT_SHA256 = "830811a3711a770f61faf56876864c7395e5b010bef713acb45278b6beff28e8"


def evaluate(dataset_path: Path, data_report_path: Path, data_receipt_path: Path, governance_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    for path, digest, label in (
        (dataset_path, DATASET_SHA256, "validation dataset"),
        (data_report_path, DATA_REPORT_SHA256, "validation data report"),
        (data_receipt_path, DATA_RECEIPT_SHA256, "validation data receipt"),
        (governance_path, GOVERNANCE_SHA256, "governance"),
    ):
        if sha256(path) != digest:
            raise QualityError(f"{label} hash mismatch")
    data_report = read_json(data_report_path)
    governance = read_json(governance_path)
    if data_report.get("decision") != "PUBLIC_VALIDATION_MULTIPOSITIVE_DATA_ACCEPT":
        raise QualityError("validation data gate is not accepted")
    if governance.get("boundedDecision") != "BOUNDED_GOVERNANCE_ACCEPT" or governance.get("failedCaseCount") != 0:
        raise QualityError("governance gate is not accepted")
    rows = [row for row in read_jsonl(dataset_path) if row.get("eligible") is True]
    if len(rows) != 84:
        raise QualityError("eligible validation cardinality mismatch")

    arms: dict[str, list[dict[str, float]]] = {"A": [], "B": [], "C": []}
    per_query: list[dict[str, Any]] = []
    candidate_failures = 0
    bc_failures = 0
    latencies: list[float] = []
    context_bytes: list[int] = []
    for row in rows:
        candidates = row.get("candidates")
        if type(candidates) is not list or len(candidates) != 200:
            raise QualityError("candidate depth mismatch")
        started = time.perf_counter_ns()
        ranked = {
            "A": rank_candidates(candidates, use_memory=False),
            "B": rank_candidates(candidates, use_memory=True),
            "C": rank_candidates(candidates, use_memory=True),
        }
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        candidate_ids = {str(item["productId"]) for item in candidates}
        preserved = all({str(item["productId"]) for item in values} == candidate_ids for values in ranked.values())
        bc_equal = [item["productId"] for item in ranked["B"]] == [item["productId"] for item in ranked["C"]]
        candidate_failures += not preserved
        bc_failures += not bc_equal
        metrics = {arm: ranking_metrics(values) for arm, values in ranked.items()}
        for arm in arms:
            arms[arm].append(metrics[arm])
        context = {"categoryId": row.get("categoryId"), "preferences": row.get("activePreferences")}
        context_bytes.append(len(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")))
        per_query.append({
            "questionId": row.get("questionId"), "conversationId": row.get("conversationId"),
            "candidateSetPreserved": preserved, "bEqualsC": bc_equal, "metrics": metrics,
            "top10": {arm: [item["productId"] for item in values[:10]] for arm, values in ranked.items()},
        })

    aggregate = {arm: means(values) for arm, values in arms.items()}
    deltas = {
        metric: cluster_bootstrap([
            (str(row["conversationId"]), float(row["metrics"]["C"][metric]) - float(row["metrics"]["A"][metric]))
            for row in per_query
        ], seed=BOOTSTRAP_SEED, iterations=BOOTSTRAP_ITERATIONS)
        for metric in ("nDCGAt10", "grade2HitAt10", "preferenceSatisfactionAt10")
    }
    p95 = percentile(latencies, 0.95)
    gates = {
        "eligibleRowsAtLeast75": len(rows) >= 75,
        "candidateSetPreserved": candidate_failures == 0,
        "armBEqualsArmCOnSafePositiveFixtures": bc_failures == 0,
        "contextBytesAtMost4096": max(context_bytes) <= 4096,
        "rerankP95AtMost100Ms": p95 <= 100.0,
        "cMinusANDCGAt10BootstrapLowerBoundPositive": float(deltas["nDCGAt10"]["p2_5"]) > 0.0,
        "cGrade2HitAt10NotLowerThanA": aggregate["C"]["grade2HitAt10"] >= aggregate["A"]["grade2HitAt10"],
    }
    report = {
        "schemaVersion": "shopping-memory-v14-public-validation-quality-report-v1",
        "decision": "PUBLIC_VALIDATION_REGRESSION_ACCEPT" if all(gates.values()) else "HOLD_PUBLIC_VALIDATION_REGRESSION",
        "productionDecision": "HOLD_EXTERNAL_CONFIRMATION",
        "split": "public-validation-contaminated-regression", "memoryWeight": MEMORY_WEIGHT,
        "counts": {"eligibleRows": len(rows), "candidateSetFailures": candidate_failures, "bCIdentityFailures": bc_failures},
        "metrics": aggregate, "cMinusABootstrap": deltas,
        "rerankLatencyMs": {"p50": percentile(latencies, 0.5), "p95": p95, "max": max(latencies)},
        "contextBytes": {"max": max(context_bytes), "mean": sum(context_bytes) / len(context_bytes)},
        "governanceEvidence": {
            "decision": governance["boundedDecision"], "cases": governance["caseCount"],
            "passed": governance["passedCaseCount"], "failed": governance["failedCaseCount"],
            "sha256": GOVERNANCE_SHA256,
        },
        "gates": gates,
        "explicitBoundaries": {
            "contaminatedPublicRegression": True, "lambdaSelection": False,
            "modelCalls": 0, "sealedRead": False,
            "providerTokenUsage": "NOT_AVAILABLE_NO_MODEL_RUN",
            "productionSwitchAuthority": False,
        },
    }
    return report, per_query


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def materialize(output_dir: Path, report: Mapping[str, Any], rows: Iterable[Mapping[str, Any]], *, runner_path: Path, contract_path: Path) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True)
    report_path = output_dir / "report.json"
    trace_path = output_dir / "per-query.jsonl"
    report_path.write_bytes(_canonical(report))
    with trace_path.open("wb") as stream:
        for row in rows:
            stream.write(_canonical(row))
    receipt = {
        "schemaVersion": "shopping-memory-v14-public-validation-quality-receipt-v1",
        "decision": report["decision"], "productionDecision": report["productionDecision"],
        "memoryWeight": MEMORY_WEIGHT, "runnerSha256": sha256(runner_path),
        "contractSha256": sha256(contract_path), "reportSha256": sha256(report_path),
        "traceSha256": sha256(trace_path), "modelCalls": 0, "sealedAccess": "NONE",
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_bytes(_canonical(receipt))
    files = (report_path, trace_path, receipt_path)
    (output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in files), encoding="ascii"
    )
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--data-report", type=Path, required=True)
    parser.add_argument("--data-receipt", type=Path, required=True)
    parser.add_argument("--governance", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    report, rows = evaluate(args.dataset, args.data_report, args.data_receipt, args.governance)
    print(materialize(args.output_dir, report, rows, runner_path=Path(__file__), contract_path=args.contract))


if __name__ == "__main__":
    main()
