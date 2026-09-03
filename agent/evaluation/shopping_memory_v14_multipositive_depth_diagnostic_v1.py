"""Public-dev candidate-depth curve for distinct-product multi-positive qrels."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from evaluation.shopping_memory_v14_public_dev_multipositive_v1 import (
    ASPECT_SHA256,
    MINIMUM_TUPLE_SUPPORT,
    FullCatalogPool,
    active_preferences,
    relevance_grade,
)
from evaluation.shopping_memory_v14_retrieval_dev_v2 import (
    CATEGORY_SHA256,
    DEV_SHA256,
    INDEX_SHA256,
    RetrievalDevError,
    fts_query,
    read_jsonl,
    sha256,
)


DEPTHS = (50, 100, 200, 500, 1000, 2000, 5000)


def first_rank_at_least(grades: list[int], threshold: int) -> int:
    for rank, grade in enumerate(grades, 1):
        if grade >= threshold:
            return rank
    return 0


def evaluate(index_path: Path, category_path: Path, aspect_path: Path, dev_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    for path, digest, label in (
        (index_path, INDEX_SHA256, "index"),
        (category_path, CATEGORY_SHA256, "category"),
        (aspect_path, ASPECT_SHA256, "aspect"),
        (dev_path, DEV_SHA256, "public dev"),
    ):
        if sha256(path) != digest:
            raise RetrievalDevError(f"{label} hash mismatch")
    all_rows = read_jsonl(dev_path)
    rows = [row for row in all_rows if row.get("questionType") == "single_product"]
    if len(all_rows) != 600 or len(rows) != 300:
        raise RetrievalDevError("public dev cardinality mismatch")

    pool = FullCatalogPool(index_path, category_path, aspect_path)
    output: list[dict[str, Any]] = []
    try:
        for row in rows:
            reasons: set[str] = set()
            episode = row["memoryEpisodes"][0]
            historical = row["targetProducts"][0]
            category_id = str(episode["categoryId"])
            historical_id = str(historical["productId"])
            preferences = active_preferences(episode)
            keys = [key for key, _ in preferences]
            if not 1 <= len(preferences) <= 8:
                reasons.add("ACTIVE_PREFERENCE_COUNT_OUT_OF_RANGE")
            if len(keys) != len(set(keys)):
                reasons.add("ACTIVE_ATTRIBUTE_NOT_UNIQUE")
            if category_id != historical.get("categoryId"):
                reasons.add("CATEGORY_MISMATCH")
            if not set(preferences).issubset(pool.historical_aspects(historical_id)):
                reasons.add("HISTORICAL_PRODUCT_DOES_NOT_SUPPORT_PREFERENCE")
            supports = [pool.tuple_support(category_id, key, value) for key, value in preferences]
            if any(value < MINIMUM_TUPLE_SUPPORT for value in supports):
                reasons.add("PREFERENCE_TUPLE_SUPPORT_BELOW_FIVE")

            candidates = pool.candidates(
                fts_query(row.get("query")), category_id, historical_id, limit=max(DEPTHS)
            )
            if historical_id in {candidate.product_id for candidate in candidates}:
                raise RetrievalDevError("historical product leaked into diagnostic pool")
            matches = pool.candidate_matches(candidates, preferences) if preferences else {}
            grades = [
                relevance_grade(len(matches[candidate.product_rowid]), len(preferences))
                if preferences else 0
                for candidate in candidates
            ]
            output.append({
                "questionId": row.get("questionId"),
                "conversationId": row.get("conversationId"),
                "baseEligible": not reasons,
                "baseReasons": sorted(reasons),
                "candidateCountAt5000": len(candidates),
                "firstGrade3Rank": first_rank_at_least(grades, 3),
                "firstGrade2OrBetterRank": first_rank_at_least(grades, 2),
                "firstGrade1OrBetterRank": first_rank_at_least(grades, 1),
            })
    finally:
        pool.close()

    base_eligible = [row for row in output if row["baseEligible"]]
    curves: dict[str, Any] = {}
    for threshold, field in (
        (3, "firstGrade3Rank"), (2, "firstGrade2OrBetterRank"), (1, "firstGrade1OrBetterRank")
    ):
        curves[f"grade{threshold}OrBetter"] = {
            str(depth): {
                "eligibleRows": sum(
                    1 <= int(row[field]) <= depth for row in base_eligible
                ),
                "coverageAll300": sum(
                    1 <= int(row[field]) <= depth for row in base_eligible
                ) / len(rows),
                "coverageBaseEligible": sum(
                    1 <= int(row[field]) <= depth for row in base_eligible
                ) / len(base_eligible) if base_eligible else 0.0,
            }
            for depth in DEPTHS
        }
    reason_counts: dict[str, int] = {}
    for row in output:
        for reason in row["baseReasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    report = {
        "schemaVersion": "shopping-memory-v14-multipositive-depth-diagnostic-v1",
        "decision": "DESCRIPTIVE_PUBLIC_DEV_DEPTH_DIAGNOSTIC",
        "split": "public-development",
        "counts": {
            "singleProductRows": len(rows), "baseEligibleRows": len(base_eligible),
            "baseEligibleCoverage": len(base_eligible) / len(rows),
        },
        "baseIneligibilityReasonCounts": dict(sorted(reason_counts.items())),
        "candidateDepths": list(DEPTHS),
        "curves": curves,
        "candidateCountAt5000": {
            "min": min(row["candidateCountAt5000"] for row in output),
            "max": max(row["candidateCountAt5000"] for row in output),
        },
        "explicitBoundaries": {
            "qualitySelection": "candidate-depth development selection only",
            "memoryRerank": False, "lambdaSelection": False,
            "validationRead": False, "sealedRead": False,
            "productionSwitchAuthority": False,
        },
    }
    return report, output


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
        "schemaVersion": "shopping-memory-v14-multipositive-depth-diagnostic-receipt-v1",
        "decision": report["decision"], "runnerSha256": sha256(runner_path),
        "contractSha256": sha256(contract_path), "reportSha256": sha256(report_path),
        "traceSha256": sha256(trace_path), "validationRead": False, "sealedAccess": "NONE",
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
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--categories", type=Path, required=True)
    parser.add_argument("--aspects", type=Path, required=True)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    report, rows = evaluate(args.index, args.categories, args.aspects, args.dev)
    print(materialize(args.output_dir, report, rows, runner_path=Path(__file__), contract_path=args.contract))


if __name__ == "__main__":
    main()
