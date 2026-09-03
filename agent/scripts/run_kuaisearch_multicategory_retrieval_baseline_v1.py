"""CLI for the SUT-only KuaiSearch G0/G1 ranking run.

The query input must already be the SUT-safe projection containing exactly
``queryId``, ``query`` and ``split``.  This CLI has no judgment input argument.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


CROSS_ENCODER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


def _default_dense_cache() -> Path:
    return Path(__file__).resolve().parents[1] / ".cache" / "fastembed"


def _default_cross_cache() -> Path:
    return Path(__file__).resolve().parents[1] / "outputs" / "01a02842-36cb-7bb2-8fb2-46a897266376" / "hf_cache"


def _default_cross_deps() -> Path:
    return Path(__file__).resolve().parents[1] / "outputs" / "01a02842-36cb-7bb2-8fb2-46a897266376" / "cross_encoder_deps"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline KuaiSearch retrieval ranking baseline.")
    parser.add_argument("--g0-dir", type=Path, help="Required formal-run G0 bundle; derives documents, queries, and model roots.")
    parser.add_argument("--documents", type=Path, help="Fixture-only retrieval documents JSONL.")
    parser.add_argument("--queries", type=Path, help="Fixture-only SUT-safe query JSONL.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Fresh run directory; non-empty directories are rejected.")
    parser.add_argument("--dense-cache-dir", type=Path, default=_default_dense_cache())
    parser.add_argument("--cross-encoder-cache-dir", type=Path, default=_default_cross_cache())
    parser.add_argument("--cross-encoder-deps", type=Path, default=_default_cross_deps())
    parser.add_argument("--cross-encoder-revision", default=CROSS_ENCODER_REVISION)
    parser.add_argument("--skip-cross-encoder", action="store_true", help="G0 rank-only smoke; omit CE system.")
    parser.add_argument("--allow-noncanonical-documents", action="store_true", help="Fixture-only: skip the frozen documents SHA/count pin.")
    return parser


def _prepare_cross_encoder_deps(deps_dir: Path) -> str:
    """Prepare local dependencies before importing any model package."""

    resolved = str(Path(deps_dir).resolve())
    if not Path(resolved).is_dir():
        raise SystemExit(f"CrossEncoder dependency directory missing: {resolved}")
    if resolved in sys.path:
        sys.path.remove(resolved)
    sys.path.insert(0, resolved)
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fixture_mode = bool(args.allow_noncanonical_documents)
    if fixture_mode:
        if args.g0_dir:
            raise SystemExit("--g0-dir cannot be combined with fixture-only --allow-noncanonical-documents")
        if not args.documents or not args.queries:
            raise SystemExit("fixture mode requires --documents and --queries")
    else:
        if not args.g0_dir:
            raise SystemExit("formal mode requires --g0-dir")
        if args.documents or args.queries:
            raise SystemExit("formal mode derives documents and queries from --g0-dir")
        if args.skip_cross_encoder:
            raise SystemExit("formal mode requires CrossEncoder and repeat verification")
        if args.cross_encoder_revision != CROSS_ENCODER_REVISION:
            raise SystemExit("formal mode fixes the pinned CrossEncoder revision")
    if not args.skip_cross_encoder and fixture_mode:
        _prepare_cross_encoder_deps(args.cross_encoder_deps)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from evaluation.kuaisearch_multicategory_retrieval_baseline_v1 import (
        DenseIndex,
        CrossEncoderReranker,
        IndexedBM25,
        RankingInputError,
        build_receipt,
        load_documents,
        load_queries,
        rank_queries,
        sha256_file,
        validate_g0_directory,
        write_run,
    )
    if fixture_mode:
        documents_path = args.documents
        queries_path = args.queries
        dense_cache_dir = args.dense_cache_dir
        cross_cache_dir = args.cross_encoder_cache_dir
        cross_deps_dir = args.cross_encoder_deps
    else:
        try:
            g0 = validate_g0_directory(args.g0_dir)
        except Exception as exc:
            raise SystemExit(str(exc)) from exc
        # Only a fully verified G0 may contribute a dependency directory to
        # sys.path.  This happens before Dense/CE construction, but after the
        # safe baseline+contract import and all G0 hash checks.
        _prepare_cross_encoder_deps(g0["crossEncoderDeps"])
        documents_path = g0["documents"]
        queries_path = g0["queries"]
        dense_cache_dir = g0["denseCache"]
        cross_cache_dir = g0["crossEncoderCache"]
        cross_deps_dir = g0["crossEncoderDeps"]
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty output: {args.output_dir}")
    documents = load_documents(documents_path)
    if not fixture_mode and len(documents) != 46_079:
        raise SystemExit("documents row count does not match the frozen full corpus")
    queries = load_queries(queries_path)
    if not fixture_mode and len(queries) != 507:
        raise SystemExit("query projection row count must be 507")
    def rss_bytes():
        try:
            import psutil
            return int(psutil.Process().memory_info().rss)
        except Exception:
            return None

    rss_before = rss_bytes()
    rss_samples = [rss_before]
    started = time.perf_counter()
    bm25 = IndexedBM25(documents)
    rss_samples.append(rss_bytes())
    dense = DenseIndex(documents, cache_dir=dense_cache_dir)
    rss_samples.append(rss_bytes())
    cross_encoder = None if args.skip_cross_encoder else CrossEncoderReranker({doc.doc_id: doc for doc in documents}, cache_dir=cross_cache_dir, revision=(CROSS_ENCODER_REVISION if not fixture_mode else args.cross_encoder_revision), deps_dir=cross_deps_dir)
    rss_samples.append(rss_bytes())
    rows, query_latency = rank_queries(documents, queries, dense=dense, bm25=bm25, cross_encoder=cross_encoder)
    rss_after = rss_bytes()
    rss_samples.append(rss_after)
    if not fixture_mode and (query_latency.get("repeatVerifiedQueries") != 507 or query_latency.get("repeatMismatchCount") != 0):
        raise SystemExit("formal CrossEncoder repeat verification did not cover 507 queries with zero mismatches")
    receipt = build_receipt(documents_path=documents_path, queries_path=queries_path, documents=documents, queries=queries, bm25=bm25, dense=dense, cross_encoder=cross_encoder, query_latency=query_latency, rss_before=rss_before, rss_after=rss_after, rss_samples=rss_samples, code_paths=[Path(__file__), Path(__file__).resolve().parents[1] / "evaluation" / "kuaisearch_multicategory_retrieval_baseline_v1.py"])
    if not fixture_mode:
        receipt["g0Binding"] = g0["g0Binding"]
    receipt["wallClockMs"] = round((time.perf_counter() - started) * 1000, 4)
    write_run(output_dir=args.output_dir, rows=rows, receipt=receipt, strict=not fixture_mode)
    print(json.dumps({"outputDir": str(args.output_dir.resolve()), "rankingRowCount": len(rows), "systems": sorted({row["strategy"] for row in rows})}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
