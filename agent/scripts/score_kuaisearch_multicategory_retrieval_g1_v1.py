"""Score a frozen KuaiSearch ranking file and optionally emit a blind G2 pool."""

from __future__ import annotations

import argparse
import json
import hashlib
import secrets
from pathlib import Path

from evaluation.kuaisearch_multicategory_retrieval_scorer_v1 import (
    build_blind_pool,
    load_categories,
    load_human_sample_labels,
    load_labels_dataset_dir,
    load_rankings,
    load_safe_documents,
    load_safe_queries,
    load_source_qrels,
    score_rankings,
    sha256_file,
    write_blind_pool,
)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rankings", type=Path, required=True)
    parser.add_argument("--ranking-manifest", type=Path, required=True)
    parser.add_argument("--g0-dir", type=Path, required=True)
    parser.add_argument("--labels-dataset-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pool-output-dir", "--g2-pool-output-dir", "--g2-pool-output", dest="pool_output_dir", type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=0)
    args = parser.parse_args()

    # All inputs are validated before creating either output directory.
    report = score_rankings(
        rankings_path=args.rankings,
        ranking_manifest_path=args.ranking_manifest,
        g0_dir=args.g0_dir,
        labels_dataset_dir=args.labels_dataset_dir,
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed,
    )
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    score_path = args.output_dir / "score.json"
    score_path.write_bytes(_canonical(report))
    manifest = {"schemaVersion": report["schemaVersion"], "status": "SCORED_SOURCE_PAIR_DIAGNOSTICS", "score": {"path": score_path.name, "sha256": sha256_file(score_path), "bytes": score_path.stat().st_size}, "rankingManifestSha256": report["rankingManifestSha256"], "g0ManifestSha256": report["g0ManifestSha256"], "labelsDatasetManifestSha256": report["labelsDatasetManifestSha256"], "inputs": report["inputs"]}
    (args.output_dir / "manifest.json").write_bytes(_canonical(manifest))

    if args.pool_output_dir is not None:
        ranking_rows = load_rankings(args.rankings)
        labels = load_labels_dataset_dir(args.labels_dataset_dir)
        qrels = load_source_qrels(labels["sourceQrels"])
        queries_path = args.g0_dir / "queries.jsonl"
        documents_path = args.g0_dir / "documents.jsonl"
        queries = load_safe_queries(queries_path)
        documents = load_safe_documents(documents_path)
        blind_secret = secrets.token_bytes(32)
        pool = build_blind_pool(ranking_rows, qrels, blind_secret=blind_secret, queries=queries, documents=documents)
        write_blind_pool(args.pool_output_dir, pool, salt_sha256=hashlib.sha256(blind_secret).hexdigest(), input_pins={
            "g0Manifest": {"path": str((args.g0_dir / "manifest.json").resolve()), "sha256": sha256_file(args.g0_dir / "manifest.json")},
            "queries": {"path": str(queries_path.resolve()), "sha256": sha256_file(queries_path), "rows": len(queries)},
            "documents": {"path": str(documents_path.resolve()), "sha256": sha256_file(documents_path), "rows": len(documents)},
            "rankings": {"path": str(args.rankings.resolve()), "sha256": sha256_file(args.rankings), "rows": 2535},
            "rankingManifest": {"path": str(args.ranking_manifest.resolve()), "sha256": sha256_file(args.ranking_manifest)},
            "qrels": {"path": str(labels["sourceQrels"].resolve()), "sha256": sha256_file(labels["sourceQrels"]), "rows": 507},
            "labelsDatasetManifest": {"path": str(labels["manifest"].resolve()), "sha256": labels["manifestSha256"]},
            "poolDepth": 20,
            "strategies": ["bm25_fields", "bm25_title", "dense_title", "rrf_bm25_dense", "rrf_bm25_dense_ce"],
        })
    print(json.dumps({"score": str(score_path), "pool": str(args.pool_output_dir) if args.pool_output_dir else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
