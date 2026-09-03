"""Build the first pending-review used-phone human qrel candidate pool."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.evaluation.used_phone_human_qrel_v1 import (
    DEFAULT_CONFUSION_AUDIT_DEPTH,
    DEFAULT_CROSS_ENCODER_MODEL,
    DEFAULT_CROSS_ENCODER_REVISION,
    LocalCrossEncoder,
    build_bundle,
)


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    annotation_dir = (
        repository / "data" / "annotations" / "ecommerce" /
        "used_phone_human_qrel_v1"
    )
    sparse_dir = (
        repository / "data" / "derived" / "ecommerce" /
        "used_phone_real_query_qrel_v1"
    )
    synthetic_price_dir = (
        repository / "data" / "derived" / "ecommerce" /
        "used_phone_synthetic_reference_price_v1"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-queries", type=Path, default=annotation_dir / "seed_queries.jsonl")
    parser.add_argument("--documents", type=Path, default=sparse_dir / "documents.jsonl")
    parser.add_argument("--sparse-qrels", type=Path, default=sparse_dir / "qrels.jsonl")
    parser.add_argument(
        "--synthetic-prices",
        type=Path,
        default=synthetic_price_dir / "prices.jsonl",
    )
    parser.add_argument(
        "--synthetic-price-manifest",
        type=Path,
        default=synthetic_price_dir / "manifest.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=annotation_dir / "review_batches" / "batch_001.jsonl",
    )
    parser.add_argument("--manifest", type=Path, default=annotation_dir / "manifest.json")
    parser.add_argument("--pool-depth", type=int, default=12)
    parser.add_argument("--cross-encoder-input-depth", type=int, default=50)
    parser.add_argument(
        "--confusion-audit-depth",
        type=int,
        default=DEFAULT_CONFUSION_AUDIT_DEPTH,
    )
    parser.add_argument("--include-vector", action="store_true")
    parser.add_argument("--include-cross-encoder", action="store_true")
    parser.add_argument("--allow-model-download", action="store_true")
    parser.add_argument("--cross-encoder-model", default=DEFAULT_CROSS_ENCODER_MODEL)
    parser.add_argument("--cross-encoder-revision", default=DEFAULT_CROSS_ENCODER_REVISION)
    parser.add_argument("--cross-encoder-local-path", type=Path)
    parser.add_argument(
        "--model-cache",
        type=Path,
        default=repository / ".cache" / "huggingface",
    )
    parser.add_argument("--cross-encoder-batch-size", type=int, default=32)
    parser.add_argument(
        "--preserve-existing-review",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--existing-review",
        type=Path,
        help="Optional prior batch whose human judgments must be preserved.",
    )
    args = parser.parse_args()
    cross_encoder = None
    if args.include_cross_encoder:
        cross_encoder = LocalCrossEncoder(
            model_name=args.cross_encoder_model,
            revision=args.cross_encoder_revision,
            cache_dir=args.model_cache,
            model_path=args.cross_encoder_local_path,
            local_files_only=not args.allow_model_download,
            batch_size=args.cross_encoder_batch_size,
        )
    manifest = build_bundle(
        seed_path=args.seed_queries,
        document_path=args.documents,
        sparse_qrel_path=args.sparse_qrels,
        output_path=args.output,
        manifest_path=args.manifest,
        pool_depth=args.pool_depth,
        cross_encoder_input_depth=args.cross_encoder_input_depth,
        confusion_audit_depth=args.confusion_audit_depth,
        include_vector=args.include_vector,
        cross_encoder=cross_encoder,
        existing_review_path=(
            args.existing_review
            if args.existing_review is not None
            else args.output
            if args.preserve_existing_review and args.output.exists()
            else None
        ),
        synthetic_price_path=args.synthetic_prices,
        synthetic_price_manifest_path=args.synthetic_price_manifest,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
