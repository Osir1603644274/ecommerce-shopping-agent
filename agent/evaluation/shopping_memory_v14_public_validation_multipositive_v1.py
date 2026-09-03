"""Build frozen Memory V14 multi-positive data on public validation."""
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
    INDEX_SHA256,
    RetrievalDevError,
    fts_query,
    read_jsonl,
    sha256,
    shopping_query,
)


VALIDATION_SHA256 = "cbd91240d733c7c3a7bf507e155ce181f28829a8c1c248aacaa44a4be09185b3"
CANDIDATE_DEPTH = 200


def evaluate(index_path: Path, category_path: Path, aspect_path: Path, validation_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    for path, digest, label in (
        (index_path, INDEX_SHA256, "index"),
        (category_path, CATEGORY_SHA256, "category"),
        (aspect_path, ASPECT_SHA256, "aspect"),
        (validation_path, VALIDATION_SHA256, "public validation"),
    ):
        if sha256(path) != digest:
            raise RetrievalDevError(f"{label} hash mismatch")
    all_rows = read_jsonl(validation_path)
    rows = [row for row in all_rows if row.get("questionType") == "single_product"]
    if len(all_rows) != 200 or len(rows) != 100:
        raise RetrievalDevError("public validation cardinality mismatch")

    pool = FullCatalogPool(index_path, category_path, aspect_path)
    dataset: list[dict[str, Any]] = []
    eligibility: list[dict[str, Any]] = []
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
            support = {
                f"{key}={value}": pool.tuple_support(category_id, key, value)
                for key, value in preferences
            }
            if any(count < MINIMUM_TUPLE_SUPPORT for count in support.values()):
                reasons.add("PREFERENCE_TUPLE_SUPPORT_BELOW_FIVE")
            candidates = pool.candidates(
                fts_query(row.get("query")), category_id, historical_id, limit=CANDIDATE_DEPTH
            )
            if len(candidates) != CANDIDATE_DEPTH:
                reasons.add("CANDIDATE_DEPTH_NOT_TWO_HUNDRED")
            if historical_id in {candidate.product_id for candidate in candidates}:
                raise RetrievalDevError("historical product leaked")
            matches = pool.candidate_matches(candidates, preferences) if preferences else {}
            maximum = max(
                (-candidate.raw_bm25 if candidate.raw_bm25 is not None else 0.0)
                for candidate in candidates
            ) if candidates else 0.0
            candidate_rows: list[dict[str, Any]] = []
            grade2_count = 0
            exact_count = 0
            for rank, candidate in enumerate(candidates, 1):
                matched = len(matches.get(candidate.product_rowid, set()))
                grade = relevance_grade(matched, len(preferences)) if preferences else 0
                grade2_count += grade >= 2
                exact_count += grade == 3
                relevance = -candidate.raw_bm25 if candidate.raw_bm25 is not None else 0.0
                candidate_rows.append({
                    "productId": candidate.product_id, "baseRank": rank,
                    "baseScore": relevance / maximum if maximum > 0 else 0.0,
                    "candidateSource": candidate.source,
                    "matchedPreferenceCount": matched,
                    "activePreferenceCount": len(preferences),
                    "memoryScore": matched / len(preferences) if preferences else 0.0,
                    "relevanceGrade": grade,
                })
            if grade2_count == 0:
                reasons.add("NO_GRADE2_ALTERNATIVE_IN_FIXED_CANDIDATES")
            accepted = not reasons
            eligibility.append({
                "questionId": row.get("questionId"), "conversationId": row.get("conversationId"),
                "eligible": accepted, "reasons": sorted(reasons),
                "activePreferenceCount": len(preferences), "tupleSupport": support,
                "grade2OrBetterAlternativeCount": grade2_count,
                "exactAlternativeCount": exact_count,
            })
            dataset.append({
                "questionId": row.get("questionId"), "conversationId": row.get("conversationId"),
                "queryText": shopping_query(row.get("query")), "categoryId": category_id,
                "historicalProductId": historical_id,
                "activePreferences": [
                    {"attributeKey": key, "normalizedValue": value} for key, value in preferences
                ],
                "eligible": accepted, "candidates": candidate_rows,
            })
    finally:
        pool.close()

    eligible = [row for row in eligibility if row["eligible"]]
    reason_counts: dict[str, int] = {}
    for row in eligibility:
        for reason in row["reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    coverage = len(eligible) / len(rows)
    gates = {
        "publicValidationSingleProductCountExact100": len(rows) == 100,
        "candidateDepthExact200All": all(len(row["candidates"]) == CANDIDATE_DEPTH for row in dataset),
        "historicalProductExcludedAll": all(
            row["historicalProductId"] not in {item["productId"] for item in row["candidates"]}
            for row in dataset
        ),
        "minimumEligible75": len(eligible) >= 75,
        "minimumEligibleCoverage075": coverage >= 0.75,
    }
    report = {
        "schemaVersion": "shopping-memory-v14-public-validation-multipositive-report-v1",
        "decision": "PUBLIC_VALIDATION_MULTIPOSITIVE_DATA_ACCEPT" if all(gates.values()) else "HOLD_PUBLIC_VALIDATION_MULTIPOSITIVE_DATA",
        "split": "public-validation-contaminated-regression", "candidatePoolDepth": CANDIDATE_DEPTH,
        "counts": {
            "allRows": len(all_rows), "singleProductRows": len(rows),
            "eligibleRows": len(eligible), "eligibleCoverage": coverage,
        },
        "eligibilityReasonCounts": dict(sorted(reason_counts.items())), "gates": gates,
        "inputs": {
            "indexSha256": INDEX_SHA256, "categorySha256": CATEGORY_SHA256,
            "aspectSha256": ASPECT_SHA256, "validationSha256": VALIDATION_SHA256,
        },
        "explicitBoundaries": {
            "memoryRerank": False, "lambdaSelection": False,
            "validationRead": True, "sealedRead": False,
            "productionSwitchAuthority": False,
        },
    }
    return report, dataset, eligibility


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def materialize(
    output_dir: Path, report: Mapping[str, Any], dataset: Iterable[Mapping[str, Any]],
    eligibility: Iterable[Mapping[str, Any]], *, runner_path: Path, contract_path: Path,
) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True)
    report_path = output_dir / "report.json"
    dataset_path = output_dir / "dataset.jsonl"
    eligibility_path = output_dir / "eligibility.jsonl"
    report_path.write_bytes(_canonical(report))
    with dataset_path.open("wb") as stream:
        for row in dataset:
            stream.write(_canonical(row))
    with eligibility_path.open("wb") as stream:
        for row in eligibility:
            stream.write(_canonical(row))
    receipt = {
        "schemaVersion": "shopping-memory-v14-public-validation-multipositive-receipt-v1",
        "decision": report["decision"], "runnerSha256": sha256(runner_path),
        "contractSha256": sha256(contract_path), "reportSha256": sha256(report_path),
        "datasetSha256": sha256(dataset_path), "eligibilitySha256": sha256(eligibility_path),
        "validationRead": True, "sealedAccess": "NONE",
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_bytes(_canonical(receipt))
    files = (report_path, dataset_path, eligibility_path, receipt_path)
    (output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in files), encoding="ascii"
    )
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--categories", type=Path, required=True)
    parser.add_argument("--aspects", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    report, dataset, eligibility = evaluate(args.index, args.categories, args.aspects, args.validation)
    print(materialize(
        args.output_dir, report, dataset, eligibility,
        runner_path=Path(__file__), contract_path=args.contract,
    ))


if __name__ == "__main__":
    main()
