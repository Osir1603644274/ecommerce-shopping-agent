"""Strict graded evaluation for evidence-carrying product retrieval runs."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def ndcg_at_10(ranked: list[int], grades: dict[int, int]) -> float:
    dcg = sum(
        ((2 ** grades.get(product_id, 0)) - 1) / math.log2(rank + 2)
        for rank, product_id in enumerate(ranked[:10])
    )
    ideal = sorted(grades.values(), reverse=True)[:10]
    ideal_dcg = sum(
        ((2 ** grade) - 1) / math.log2(rank + 2)
        for rank, grade in enumerate(ideal)
    )
    return dcg / ideal_dcg if ideal_dcg else 0.0


def recall_at_20(ranked: list[int], grades: dict[int, int]) -> float:
    relevant = {product_id for product_id, grade in grades.items() if grade > 0}
    return len(set(ranked[:20]) & relevant) / len(relevant) if relevant else 0.0


def hit_at_20(ranked: list[int], grades: dict[int, int]) -> float:
    relevant = {product_id for product_id, grade in grades.items() if grade > 0}
    return float(bool(set(ranked[:20]) & relevant))


def _result_id(item: Any) -> int:
    return int(item if isinstance(item, int) else item["productId"])


def evaluate_partition(
    qrels: list[dict[str, Any]],
    runs: dict[str, dict[str, list[Any]]],
    partition: str,
) -> dict[str, Any]:
    selected = [
        row for row in qrels
        if row["split"] == partition and row["reviewStatus"] == "human_confirmed"
    ]
    systems = sorted({system for query_runs in runs.values() for system in query_runs})
    metrics: dict[str, Any] = {}
    for system in systems:
        ndcgs: list[float] = []
        recalls: list[float] = []
        hits: list[float] = []
        violation_count = 0
        returned_count = 0
        claim_count = 0
        cited_claim_count = 0
        evaluated_queries = 0
        for qrel in selected:
            items = runs.get(qrel["queryId"], {}).get(system)
            if items is None:
                continue
            evaluated_queries += 1
            grades = {
                int(item["productId"]): int(item["grade"])
                for item in qrel["judgments"]
            }
            ranked = [_result_id(item) for item in items]
            ndcgs.append(ndcg_at_10(ranked, grades))
            recalls.append(recall_at_20(ranked, grades))
            hits.append(hit_at_20(ranked, grades))
            for item in items[:20]:
                if isinstance(item, int):
                    continue
                returned_count += 1
                violation_count += int(item.get("hardConstraintViolations", 0) > 0)
                for claim in item.get("factClaims", []):
                    claim_count += 1
                    cited_claim_count += int(bool(claim.get("evidenceRef")))
        metrics[system] = {
            "evaluatedQueries": evaluated_queries,
            "NDCG@10": sum(ndcgs) / len(ndcgs) if ndcgs else None,
            "Recall@20": sum(recalls) / len(recalls) if recalls else None,
            "Hit@20": sum(hits) / len(hits) if hits else None,
            "hardConstraintViolationRate": (
                violation_count / returned_count if returned_count else None
            ),
            "factEvidenceCoverage": (
                cited_claim_count / claim_count if claim_count else None
            ),
            "factClaimCount": claim_count,
        }
    expected = sum(row["split"] == partition for row in qrels)
    return {
        "partition": partition,
        "expectedQrelCount": expected,
        "humanConfirmedQrelCount": len(selected),
        "finalMetricsAllowed": expected > 0 and len(selected) == expected,
        "metrics": metrics,
    }


def evaluate_gate(
    validation: dict[str, Any],
    *,
    proposed_system: str,
    baseline_systems: list[str],
) -> dict[str, Any]:
    metrics = validation["metrics"]
    proposed = metrics.get(proposed_system)
    usable_baselines = [
        (name, metrics.get(name)) for name in baseline_systems
        if metrics.get(name, {}).get("NDCG@10") is not None
    ]
    if not proposed or proposed.get("NDCG@10") is None or not usable_baselines:
        return {"status": "unverified", "reason": "confirmed qrels or required runs are missing"}
    best_name, best = max(usable_baselines, key=lambda item: item[1]["NDCG@10"])
    relative_gain = (
        proposed["NDCG@10"] / best["NDCG@10"] - 1
        if best["NDCG@10"] > 0 else None
    )
    checks = {
        "ndcgRelativeGainAtLeast5Percent": relative_gain is not None and relative_gain >= 0.05,
        "recallDropAtMost1Point": (
            proposed["Recall@20"] is not None
            and best["Recall@20"] is not None
            and proposed["Recall@20"] >= best["Recall@20"] - 0.01
        ),
        "hardConstraintViolationRateZero": proposed["hardConstraintViolationRate"] == 0,
        "factEvidenceCoverage100Percent": proposed["factEvidenceCoverage"] == 1,
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "bestBaseline": best_name,
        "relativeNdcgGain": relative_gain,
        "checks": checks,
    }


def summarize_qrels(qrels: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in qrels:
        counts[row["category"]][row["split"]] += 1
    return {category: dict(partitions) for category, partitions in counts.items()}
