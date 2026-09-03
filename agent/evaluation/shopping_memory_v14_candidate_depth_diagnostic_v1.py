"""Public-development-only candidate-depth diagnostic for Shopping Memory V14."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from evaluation.shopping_memory_v14_retrieval_dev_v2 import (
    CATEGORY_SHA256,
    DEV_SHA256,
    INDEX_SHA256,
    FullCatalogFts,
    RetrievalDevError,
    fts_query,
    read_jsonl,
    sha256,
    shopping_query,
)


DEPTHS = (50, 100, 200, 500, 1000, 2000, 5000)


def target_flags(
    engine: FullCatalogFts,
    query: str,
    category_id: str,
    target_id: str,
) -> tuple[bool, bool]:
    category_member = engine.connection.execute(
        "SELECT 1 FROM product_locator AS locator "
        "JOIN category_db.product_category AS category "
        "ON category.product_rowid=locator.rowid "
        "WHERE locator.product_id=? AND category.category_id=? LIMIT 1",
        (target_id, category_id),
    ).fetchone() is not None
    text_match = engine.connection.execute(
        "SELECT 1 FROM product_fts JOIN category_db.product_category AS category "
        "ON category.product_rowid=product_fts.rowid "
        "WHERE product_fts MATCH ? AND product_fts.product_id=? "
        "AND category.category_id=? LIMIT 1",
        (query, target_id, category_id),
    ).fetchone() is not None
    return category_member, text_match


def recall_curve(ranks: Sequence[int], depths: Sequence[int] = DEPTHS) -> dict[str, float]:
    if not ranks:
        raise RetrievalDevError("empty rank sequence")
    return {
        str(depth): sum(1 <= rank <= depth for rank in ranks) / len(ranks)
        for depth in depths
    }


def evaluate(index_path: Path, category_path: Path, dev_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if sha256(index_path) != INDEX_SHA256:
        raise RetrievalDevError("index hash mismatch")
    if sha256(category_path) != CATEGORY_SHA256:
        raise RetrievalDevError("category sidecar hash mismatch")
    if sha256(dev_path) != DEV_SHA256:
        raise RetrievalDevError("public dev hash mismatch")
    all_rows = read_jsonl(dev_path)
    rows = [row for row in all_rows if row.get("questionType") == "single_product"]
    if len(all_rows) != 600 or len(rows) != 300:
        raise RetrievalDevError("public dev cardinality mismatch")

    engine = FullCatalogFts(index_path, category_path)
    per_query: list[dict[str, Any]] = []
    try:
        for row in rows:
            target = row["targetProducts"][0]
            episode = row["memoryEpisodes"][0]
            target_id = str(target["productId"])
            category_id = str(episode["categoryId"])
            if category_id != target.get("categoryId"):
                raise RetrievalDevError("task/target category mismatch")
            query = fts_query(row.get("query"))
            category_member, text_match = target_flags(engine, query, category_id, target_id)
            started = time.perf_counter_ns()
            candidates = engine.search(query, category_id, limit=max(DEPTHS))
            latency_ms = (time.perf_counter_ns() - started) / 1_000_000
            candidate_ids = [item[0] for item in candidates]
            rank = candidate_ids.index(target_id) + 1 if target_id in candidate_ids else 0
            per_query.append({
                "questionId": row.get("questionId"),
                "conversationId": row.get("conversationId"),
                "queryText": shopping_query(row.get("query")),
                "ftsQuery": query,
                "taskCategoryId": category_id,
                "targetProductId": target_id,
                "targetCategoryMember": category_member,
                "targetTextMatch": text_match,
                "retrievedCountAtMaxDepth": len(candidate_ids),
                "targetRankAtMost5000": rank,
                "latencyMs": latency_ms,
            })
    finally:
        engine.close()

    ranks = [int(row["targetRankAtMost5000"]) for row in per_query]
    latencies = sorted(float(row["latencyMs"]) for row in per_query)
    p95_index = round((len(latencies) - 1) * 0.95)
    report = {
        "schemaVersion": "shopping-memory-v14-candidate-depth-diagnostic-v1",
        "split": "public-development",
        "decision": "DESCRIPTIVE_PUBLIC_DEV_DIAGNOSTIC",
        "counts": {
            "allRows": len(all_rows),
            "singleProductRows": len(rows),
            "targetCategoryMembers": sum(row["targetCategoryMember"] for row in per_query),
            "targetTextMatches": sum(row["targetTextMatch"] for row in per_query),
            "targetRanksObservedAtMost5000": sum(rank > 0 for rank in ranks),
        },
        "macroTargetRecallByDepth": recall_curve(ranks),
        "candidateCountAtMaxDepth": {
            "min": min(row["retrievedCountAtMaxDepth"] for row in per_query),
            "max": max(row["retrievedCountAtMaxDepth"] for row in per_query),
        },
        "latencyMsAtDepth5000": {
            "p50": latencies[(len(latencies) - 1) // 2],
            "p95": latencies[p95_index],
            "max": max(latencies),
        },
        "explicitBoundaries": {
            "qualitySelection": False,
            "memoryRerank": False,
            "validationRead": False,
            "sealedRead": False,
            "productionSwitchAuthority": False,
        },
    }
    return report, per_query


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def materialize(
    output_dir: Path,
    report: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    runner_path: Path,
    contract_path: Path,
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
        "schemaVersion": "shopping-memory-v14-candidate-depth-diagnostic-receipt-v1",
        "decision": report["decision"],
        "indexSha256": INDEX_SHA256,
        "categorySha256": CATEGORY_SHA256,
        "devSha256": DEV_SHA256,
        "runnerSha256": sha256(runner_path),
        "contractSha256": sha256(contract_path),
        "reportSha256": sha256(report_path),
        "traceSha256": sha256(trace_path),
        "validationRead": False,
        "sealedAccess": "NONE",
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
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    report, rows = evaluate(args.index, args.categories, args.dev)
    print(materialize(args.output_dir, report, rows, runner_path=Path(__file__), contract_path=args.contract))


if __name__ == "__main__":
    main()
