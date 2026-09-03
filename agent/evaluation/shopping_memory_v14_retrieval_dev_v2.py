"""Evaluate TaskState-category-filtered full-catalog retrieval on public dev."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


INDEX_SHA256 = "c23bf88d9116c91d36bef6603a295e2cf921fafb544912a83ae8bb0f545686cb"
CATEGORY_SHA256 = "c9acbdf4fca8bff5ada9ef69adcab95264a85fb6c35a9cbf233279fe67ccbbe3"
DEV_SHA256 = "f7fc366dd9b78188ebd11c8953f6e6f41cb31583824bccfe373a741068c95973"
SOURCE_REVISION = "9a8a2a1c13f0d88de070238352bcf71f98ca851f"
CANDIDATE_DEPTH = 50


class RetrievalDevError(RuntimeError):
    """Fail-closed public development retrieval error."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    output = []
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            value = json.loads(raw)
            if type(value) is not dict:
                raise RetrievalDevError(f"non-object JSONL row at {path}:{number}")
            output.append(value)
    return output


def tokenize(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    terms: list[str] = []
    current: list[str] = []
    for character in normalized:
        if unicodedata.category(character)[:1] in {"L", "N"}:
            current.append(character)
        elif current:
            terms.append("".join(current))
            current = []
    if current:
        terms.append("".join(current))
    return tuple(dict.fromkeys(terms))


def shopping_query(raw: object) -> str:
    if type(raw) is not str:
        raise RetrievalDevError("query is not text")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise RetrievalDevError("query is blank")
    return lines[-1]


def fts_query(raw: object) -> str:
    terms = tokenize(shopping_query(raw))
    if not terms:
        raise RetrievalDevError("query has no searchable terms")
    return " OR ".join(f'"{term}"' for term in terms)


class FullCatalogFts:
    def __init__(self, database_path: Path, category_path: Path) -> None:
        uri = database_path.resolve().as_uri() + "?mode=ro&immutable=1"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.execute("PRAGMA query_only=ON")
        category_uri = category_path.resolve().as_uri() + "?mode=ro&immutable=1"
        self.connection.execute("ATTACH DATABASE ? AS category_db", (category_uri,))

    def close(self) -> None:
        self.connection.close()

    def search(
        self,
        query: str,
        category_id: str,
        *,
        limit: int = CANDIDATE_DEPTH,
    ) -> list[tuple[str, float]]:
        rows = self.connection.execute(
            "SELECT product_fts.product_id,bm25(product_fts) AS score "
            "FROM product_fts JOIN category_db.product_category AS category "
            "ON category.product_rowid=product_fts.rowid "
            "WHERE product_fts MATCH ? AND category.category_id=? "
            "ORDER BY score ASC,product_fts.product_id ASC LIMIT ?",
            (query, category_id, limit),
        ).fetchall()
        return [(str(product_id), float(score)) for product_id, score in rows]

    def contains(self, product_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM product_locator WHERE product_id=?", (product_id,)
        ).fetchone() is not None


def ranking_metrics(candidate_ids: Sequence[str], target_id: str) -> dict[str, float]:
    try:
        rank = candidate_ids.index(target_id) + 1
    except ValueError:
        rank = 0
    return {
        "targetRank": rank,
        "targetRecallAt50": 1.0 if 1 <= rank <= 50 else 0.0,
        "hitAt10": 1.0 if 1 <= rank <= 10 else 0.0,
        "nDCGAt10": 1.0 / math.log2(rank + 1) if 1 <= rank <= 10 else 0.0,
        "MRRAt50": 1.0 / rank if 1 <= rank <= 50 else 0.0,
    }


def percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def evaluate(
    database_path: Path,
    category_path: Path,
    dev_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if sha256(database_path) != INDEX_SHA256:
        raise RetrievalDevError("index hash mismatch")
    if sha256(category_path) != CATEGORY_SHA256:
        raise RetrievalDevError("category sidecar hash mismatch")
    if sha256(dev_path) != DEV_SHA256:
        raise RetrievalDevError("public dev hash mismatch")
    all_rows = read_jsonl(dev_path)
    rows = [row for row in all_rows if row.get("questionType") == "single_product"]
    if len(all_rows) != 600 or len(rows) != 300:
        raise RetrievalDevError("public dev cardinality mismatch")

    engine = FullCatalogFts(database_path, category_path)
    per_query: list[dict[str, Any]] = []
    try:
        for row in rows:
            targets = row.get("targetProducts")
            if type(targets) is not list or len(targets) != 1 or type(targets[0]) is not dict:
                raise RetrievalDevError("invalid single-product target")
            target_id = targets[0].get("productId")
            if type(target_id) is not str or not target_id:
                raise RetrievalDevError("invalid target product id")
            episodes = row.get("memoryEpisodes")
            if type(episodes) is not list or len(episodes) != 1 or type(episodes[0]) is not dict:
                raise RetrievalDevError("invalid task category fixture")
            task_category_id = episodes[0].get("categoryId")
            target_category_id = targets[0].get("categoryId")
            if type(task_category_id) is not str or task_category_id != target_category_id:
                raise RetrievalDevError("task/target category mismatch")
            query = fts_query(row.get("query"))
            started = time.perf_counter_ns()
            candidates = engine.search(query, task_category_id)
            latency_ms = (time.perf_counter_ns() - started) / 1_000_000
            candidate_ids = [item[0] for item in candidates]
            metrics = ranking_metrics(candidate_ids, target_id)
            per_query.append({
                "questionId": row.get("questionId"),
                "conversationId": row.get("conversationId"),
                "queryText": shopping_query(row.get("query")),
                "ftsQuery": query,
                "taskCategoryId": task_category_id,
                "targetProductId": target_id,
                "targetInIndex": engine.contains(target_id),
                "candidateCount": len(candidate_ids),
                "candidateIdsUnique": len(candidate_ids) == len(set(candidate_ids)),
                "candidateIds": candidate_ids,
                "latencyMs": latency_ms,
                **metrics,
            })
    finally:
        engine.close()

    count = len(per_query)
    latencies = [row["latencyMs"] for row in per_query]
    means = {
        key: sum(row[key] for row in per_query) / count
        for key in ("targetRecallAt50", "hitAt10", "nDCGAt10", "MRRAt50")
    }
    latency_p50 = percentile(latencies, 0.50)
    latency_p95 = percentile(latencies, 0.95)
    gates = {
        "publicDevSingleProductCountExact": count == 300,
        "allTargetsPresentInIndex": all(row["targetInIndex"] for row in per_query),
        "candidateIdsUnique": all(row["candidateIdsUnique"] for row in per_query),
        "noEmptyCandidateLists": all(row["candidateCount"] > 0 for row in per_query),
        "candidateDepthExact50": all(row["candidateCount"] == CANDIDATE_DEPTH for row in per_query),
        "macroTargetRecallAt50AtLeast095": means["targetRecallAt50"] >= 0.95,
        "p95LatencyAtMost100Ms": latency_p95 <= 100.0,
    }
    report = {
        "schemaVersion": "shopping-memory-v14-retrieval-dev-report-v2",
        "split": "public-development",
        "decision": "PUBLIC_DEV_CATEGORY_FILTERED_RETRIEVAL_ACCEPT" if all(gates.values()) else "HOLD_PUBLIC_DEV_CATEGORY_FILTERED_RETRIEVAL",
        "sourceRevision": SOURCE_REVISION,
        "index": {
            "path": str(database_path),
            "sha256": INDEX_SHA256,
            "categoryPath": str(category_path),
            "categorySha256": CATEGORY_SHA256,
            "candidateDepth": 50,
            "taskCategorySource": "oracle-normalized current TaskState fixture, fixed for all future memory arms",
        },
        "counts": {
            "allPublicDevQuestions": len(all_rows),
            "singleProductQuestions": count,
            "targetHitsAt50": int(sum(row["targetRecallAt50"] for row in per_query)),
            "emptyCandidateLists": sum(row["candidateCount"] == 0 for row in per_query),
        },
        "macroMetrics": means,
        "latencyMs": {
            "p50": latency_p50,
            "p95": latency_p95,
            "max": max(latencies),
        },
        "gates": gates,
        "explicitBoundaries": {
            "memoryRerank": False,
            "lambdaSelection": False,
            "validationRead": False,
            "sealedRead": False,
            "productionSwitchAuthority": False,
        },
    }
    return report, per_query


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def materialize(
    output_dir: Path,
    report: dict[str, Any],
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
    _write_json(report_path, report)
    with trace_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    receipt = {
        "schemaVersion": "shopping-memory-v14-retrieval-dev-receipt-v2",
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
    _write_json(receipt_path, receipt)
    files = [report_path, trace_path, receipt_path]
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
    print(materialize(
        args.output_dir,
        report,
        rows,
        runner_path=Path(__file__),
        contract_path=args.contract,
    ))


if __name__ == "__main__":
    main()
