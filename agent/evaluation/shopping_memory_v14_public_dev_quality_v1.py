"""Evaluate frozen Memory V14 A/B/C ranking on public-development data."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DATASET_SHA256 = "91fa5aab4cc02fcaf8124e7e3e152ec4ec068379bc09ff2fd0e0af36c9c33111"
DATA_REPORT_SHA256 = "f06d16692a071ca145018abce54079429a787bcc6dc6a137e841e8d9994905f1"
DATA_RECEIPT_SHA256 = "ece2d90aa4ec36d083f0a47e2e341de8ce90f814eb202fc22fab6800a269af59"
GOVERNANCE_SHA256 = "335a69409036dce216d6a3c19401a3268b00ed1752613c8f224826588e78be0e"
MEMORY_WEIGHT = 0.08
BOOTSTRAP_SEED = 20260830
BOOTSTRAP_ITERATIONS = 10_000


class QualityError(RuntimeError):
    """Fail-closed public quality evaluation error."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if type(value) is not dict:
        raise QualityError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for number, raw in enumerate(path.read_bytes().splitlines(), 1):
        value = json.loads(raw)
        if type(value) is not dict:
            raise QualityError(f"expected JSON object at line {number}")
        output.append(value)
    return output


def rank_candidates(candidates: Sequence[Mapping[str, Any]], *, use_memory: bool) -> list[Mapping[str, Any]]:
    if not use_memory:
        return sorted(candidates, key=lambda row: (int(row["baseRank"]), str(row["productId"])))
    return sorted(
        candidates,
        key=lambda row: (
            -(float(row["baseScore"]) + MEMORY_WEIGHT * float(row["memoryScore"])),
            int(row["baseRank"]), str(row["productId"]),
        ),
    )


def ranking_metrics(ranked: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    grades = [int(row["relevanceGrade"]) for row in ranked]
    ideal = sorted(grades, reverse=True)

    def dcg(values: Sequence[int], depth: int) -> float:
        return sum((2 ** grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(values[:depth], 1))

    def ndcg(depth: int) -> float:
        denominator = dcg(ideal, depth)
        return dcg(grades, depth) / denominator if denominator > 0 else 0.0

    first_grade2 = next((rank for rank, grade in enumerate(grades[:50], 1) if grade >= 2), 0)
    top10 = ranked[:10]
    return {
        "nDCGAt10": ndcg(10),
        "nDCGAt50": ndcg(50),
        "grade2HitAt10": 1.0 if any(int(row["relevanceGrade"]) >= 2 for row in top10) else 0.0,
        "grade3HitAt10": 1.0 if any(int(row["relevanceGrade"]) == 3 for row in top10) else 0.0,
        "grade2MRRAt50": 1.0 / first_grade2 if first_grade2 else 0.0,
        "preferenceSatisfactionAt10": sum(float(row["memoryScore"]) for row in top10) / len(top10),
    }


def means(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    if not rows:
        raise QualityError("empty metric rows")
    return {key: sum(row[key] for row in rows) / len(rows) for key in rows[0]}


def percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def cluster_bootstrap(
    rows: Sequence[tuple[str, float]], *, seed: int = BOOTSTRAP_SEED,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> dict[str, float | int | str]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for cluster, delta in rows:
        grouped[cluster].append(delta)
    clusters = sorted(grouped)
    if not clusters:
        raise QualityError("empty bootstrap")
    cluster_means = {key: sum(values) / len(values) for key, values in grouped.items()}
    generator = random.Random(seed)
    samples = [
        sum(cluster_means[generator.choice(clusters)] for _ in clusters) / len(clusters)
        for _ in range(iterations)
    ]
    return {
        "unit": "conversationId", "clusterCount": len(clusters),
        "iterations": iterations, "seed": seed,
        "meanDelta": sum(cluster_means.values()) / len(cluster_means),
        "p2_5": percentile(samples, 0.025), "p97_5": percentile(samples, 0.975),
    }


def evaluate(dataset_path: Path, data_report_path: Path, data_receipt_path: Path, governance_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    for path, digest, label in (
        (dataset_path, DATASET_SHA256, "dataset"),
        (data_report_path, DATA_REPORT_SHA256, "data report"),
        (data_receipt_path, DATA_RECEIPT_SHA256, "data receipt"),
        (governance_path, GOVERNANCE_SHA256, "governance"),
    ):
        if sha256(path) != digest:
            raise QualityError(f"{label} hash mismatch")
    data_report = read_json(data_report_path)
    governance = read_json(governance_path)
    if data_report.get("decision") != "PUBLIC_DEV_MULTIPOSITIVE_DATA_ACCEPT":
        raise QualityError("data gate is not accepted")
    if governance.get("boundedDecision") != "BOUNDED_GOVERNANCE_ACCEPT" or governance.get("failedCaseCount") != 0:
        raise QualityError("governance gate is not accepted")

    rows = [row for row in read_jsonl(dataset_path) if row.get("eligible") is True]
    if len(rows) != 225:
        raise QualityError("eligible public dev cardinality mismatch")
    per_query: list[dict[str, Any]] = []
    arm_values: dict[str, list[dict[str, float]]] = {"A": [], "B": [], "C": []}
    candidate_failures = 0
    bc_failures = 0
    rerank_latencies: list[float] = []
    context_bytes: list[int] = []
    for row in rows:
        candidates = row.get("candidates")
        if type(candidates) is not list or len(candidates) != 200:
            raise QualityError("candidate depth mismatch")
        started = time.perf_counter_ns()
        a = rank_candidates(candidates, use_memory=False)
        b = rank_candidates(candidates, use_memory=True)
        c = rank_candidates(candidates, use_memory=True)
        rerank_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        ids = {str(item["productId"]) for item in candidates}
        preserved = all({str(item["productId"]) for item in arm} == ids for arm in (a, b, c))
        candidate_failures += not preserved
        bc_equal = [item["productId"] for item in b] == [item["productId"] for item in c]
        bc_failures += not bc_equal
        metrics = {"A": ranking_metrics(a), "B": ranking_metrics(b), "C": ranking_metrics(c)}
        for arm in arm_values:
            arm_values[arm].append(metrics[arm])
        context = {
            "categoryId": row.get("categoryId"),
            "preferences": row.get("activePreferences"),
        }
        context_bytes.append(len(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")))
        per_query.append({
            "questionId": row.get("questionId"), "conversationId": row.get("conversationId"),
            "candidateSetPreserved": preserved, "bEqualsC": bc_equal,
            "top10": {arm: [item["productId"] for item in ranked[:10]] for arm, ranked in (("A", a), ("B", b), ("C", c))},
            "metrics": metrics,
        })

    aggregate = {arm: means(values) for arm, values in arm_values.items()}
    deltas = {
        metric: cluster_bootstrap([
            (str(row["conversationId"]), float(row["metrics"]["C"][metric]) - float(row["metrics"]["A"][metric]))
            for row in per_query
        ])
        for metric in ("nDCGAt10", "grade2HitAt10", "preferenceSatisfactionAt10")
    }
    latency_p95 = percentile(rerank_latencies, 0.95)
    gates = {
        "eligibleRowsAtLeast200": len(rows) >= 200,
        "candidateSetPreserved": candidate_failures == 0,
        "armBEqualsArmCOnSafePositiveFixtures": bc_failures == 0,
        "contextBytesAtMost4096": max(context_bytes) <= 4096,
        "rerankP95AtMost100Ms": latency_p95 <= 100.0,
        "cMinusANDCGAt10BootstrapLowerBoundPositive": float(deltas["nDCGAt10"]["p2_5"]) > 0.0,
        "cGrade2HitAt10NotLowerThanA": aggregate["C"]["grade2HitAt10"] >= aggregate["A"]["grade2HitAt10"],
    }
    report = {
        "schemaVersion": "shopping-memory-v14-public-dev-quality-report-v1",
        "decision": "PUBLIC_DEV_MEMORY_RERANK_DESCRIPTIVE_ACCEPT" if all(gates.values()) else "HOLD_PUBLIC_DEV_MEMORY_RERANK",
        "split": "public-development", "memoryWeight": MEMORY_WEIGHT,
        "arms": {
            "A": "no long-term memory",
            "B": "direct tuple memory on safe positive fixtures",
            "C": "governed projection; equivalent to B only on these safe positive fixtures",
        },
        "counts": {
            "eligibleRows": len(rows), "candidateSetFailures": candidate_failures,
            "bCIdentityFailures": bc_failures,
        },
        "metrics": aggregate,
        "cMinusABootstrap": deltas,
        "rerankLatencyMs": {
            "p50": percentile(rerank_latencies, 0.5), "p95": latency_p95,
            "max": max(rerank_latencies),
        },
        "contextBytes": {"max": max(context_bytes), "mean": sum(context_bytes) / len(context_bytes)},
        "governanceEvidence": {
            "decision": governance["boundedDecision"], "cases": governance["caseCount"],
            "passed": governance["passedCaseCount"], "failed": governance["failedCaseCount"],
            "sha256": GOVERNANCE_SHA256,
        },
        "gates": gates,
        "explicitBoundaries": {
            "publicDevelopmentOnly": True, "lambdaSelection": False,
            "validationRead": False, "sealedRead": False,
            "modelCalls": 0, "providerTokenUsage": "NOT_AVAILABLE_NO_MODEL_RUN",
            "productionSwitchAuthority": False,
        },
    }
    return report, per_query


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def materialize(
    output_dir: Path, report: Mapping[str, Any], rows: Iterable[Mapping[str, Any]],
    *, runner_path: Path, contract_path: Path,
) -> Path:
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
        "schemaVersion": "shopping-memory-v14-public-dev-quality-receipt-v1",
        "decision": report["decision"], "memoryWeight": MEMORY_WEIGHT,
        "runnerSha256": sha256(runner_path), "contractSha256": sha256(contract_path),
        "reportSha256": sha256(report_path), "traceSha256": sha256(trace_path),
        "modelCalls": 0, "validationRead": False, "sealedAccess": "NONE",
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
