"""Build the evaluator-owned non-sealed input bundle for retrieval experiments.

This preparation step may read the historical source files.  The later ranking
process reads only this derived bundle, so sealed rows are absent from its
address space rather than filtered after loading.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE_QUERY_PATH = ROOT / "data/annotations/ecommerce/used_phone_human_qrel_v1/seed_queries.jsonl"
SOURCE_REVIEW_PATH = ROOT / "data/annotations/ecommerce/used_phone_human_qrel_v1/review_batches/batch_001.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "data/derived/ecommerce/product_retrieval_nonsealed_bundle_v1_20260828_r1"
QUERY_IDS = ("uphq-001", "uphq-002", "uphq-005", "uphq-007", "uphq-010")
SEALED_QUERY_IDS = ("uphq-008", "uphq-009", "uphq-012")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def build_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_queries = {row["queryId"]: row for row in read_jsonl(SOURCE_QUERY_PATH)}
    source_reviews = {row["queryId"]: row for row in read_jsonl(SOURCE_REVIEW_PATH)}
    if not set(QUERY_IDS).issubset(source_queries) or not set(QUERY_IDS).issubset(source_reviews):
        raise ValueError("registered non-sealed query identity is incomplete")
    queries: list[dict[str, Any]] = []
    qrels: list[dict[str, Any]] = []
    for query_id in QUERY_IDS:
        query = source_queries[query_id]
        review = source_reviews[query_id]
        if query.get("split") == "sealed_test" or review.get("split") == "sealed_test":
            raise ValueError("registered non-sealed row resolves to sealed_test")
        if query.get("evaluationRole") != "primary_retrieval" or review.get("evaluationRole") != "primary_retrieval":
            raise ValueError("registered query role mismatch")
        requirements = []
        units = {"brand": "text", "model": "text", "price_minor": "CNY_MINOR"}
        for requirement in query.get("intent", {}).get("hardConstraints", []):
            requirements.append({
                "key": requirement["key"],
                "operator": requirement["operator"],
                "value": requirement["value"],
                "unit": units.get(requirement["key"], "enum"),
                "priority": "hard",
                "source": "frozen_query_intent",
            })
        queries.append({
            "schemaVersion": "product-retrieval-nonsealed-query-v1",
            "queryId": query_id,
            "query": query["retrievalQuery"],
            "category": "手机",
            "requirements": requirements,
        })
        for candidate in review.get("candidates", []):
            grade = (candidate.get("judgment") or {}).get("grade")
            if type(grade) is int and 0 <= grade <= 3:
                qrels.append({
                    "schemaVersion": "product-retrieval-nonsealed-qrel-v1",
                    "queryId": query_id,
                    "productId": int(candidate["productId"]),
                    "grade": grade,
                })
    return queries, sorted(qrels, key=lambda row: (row["queryId"], row["productId"]))


def validate_bundle(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    query_path = output_dir / "queries_public.jsonl"
    qrel_path = output_dir / "qrels_evaluator.jsonl"
    manifest_path = output_dir / "manifest.json"
    queries = read_jsonl(query_path)
    qrels = read_jsonl(qrel_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if [row["queryId"] for row in queries] != list(QUERY_IDS):
        raise ValueError("public query order or identity mismatch")
    if set(row["queryId"] for row in qrels) != set(QUERY_IDS):
        raise ValueError("qrel query coverage mismatch")
    serialized = canonical({"queries": queries, "qrels": qrels, "manifest": manifest})
    if any(query_id in serialized for query_id in SEALED_QUERY_IDS):
        raise ValueError("sealed identity leaked into non-sealed bundle")
    for name, path, rows in (
        ("queries_public.jsonl", query_path, len(queries)),
        ("qrels_evaluator.jsonl", qrel_path, len(qrels)),
    ):
        pin = manifest["artifacts"][name]
        if pin != {"bytes": path.stat().st_size, "rows": rows, "sha256": sha256(path)}:
            raise ValueError(f"{name} binding mismatch")
    if manifest["sources"]["queries"]["sha256"] != sha256(SOURCE_QUERY_PATH):
        raise ValueError("source query binding mismatch")
    if manifest["sources"]["reviews"]["sha256"] != sha256(SOURCE_REVIEW_PATH):
        raise ValueError("source review binding mismatch")
    expected_queries, expected_qrels = build_rows()
    if queries != expected_queries or qrels != expected_qrels:
        raise ValueError("non-sealed derivation replay mismatch")


def materialize(output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty bundle: {output_dir}")
    queries, qrels = build_rows()
    output_dir.mkdir(parents=True, exist_ok=True)
    query_path = output_dir / "queries_public.jsonl"
    qrel_path = output_dir / "qrels_evaluator.jsonl"
    query_path.write_text("".join(canonical(row) + "\n" for row in queries), encoding="utf-8")
    qrel_path.write_text("".join(canonical(row) + "\n" for row in qrels), encoding="utf-8")
    manifest = {
        "schemaVersion": "product-retrieval-nonsealed-bundle-v1",
        "status": "NONSEALED_EVALUATOR_BUNDLE_READY",
        "queryIds": list(QUERY_IDS),
        "sealedRowsAbsent": True,
        "artifacts": {
            "queries_public.jsonl": {"bytes": query_path.stat().st_size, "rows": len(queries), "sha256": sha256(query_path)},
            "qrels_evaluator.jsonl": {"bytes": qrel_path.stat().st_size, "rows": len(qrels), "sha256": sha256(qrel_path)},
        },
        "sources": {
            "queries": {"path": SOURCE_QUERY_PATH.relative_to(ROOT).as_posix(), "sha256": sha256(SOURCE_QUERY_PATH)},
            "reviews": {"path": SOURCE_REVIEW_PATH.relative_to(ROOT).as_posix(), "sha256": sha256(SOURCE_REVIEW_PATH)},
        },
        "separation": "Ranking process reads queries_public first and opens qrels_evaluator only after predictions; source files are not ranking-process inputs.",
    }
    (output_dir / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
    validate_bundle(output_dir)
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        validate_bundle(args.output_dir)
        print(canonical({"verified": str(args.output_dir.resolve()), "status": "PASS"}))
    else:
        output = materialize(args.output_dir)
        print(canonical({"outputDir": str(output.resolve()), "status": "READY"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
