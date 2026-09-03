"""Create reproducible role-aware Qrel artifacts and fixed baseline report."""
from __future__ import annotations

import argparse
from pathlib import Path

from agent.evaluation.used_phone_human_qrel_phase_v1 import (
    audit_full_catalog_residuals, build_second_human_blind_package,
    run_preregistered_baselines, write_json,
)
from agent.evaluation.used_phone_human_qrel_v1 import LocalCrossEncoder, read_jsonl


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    annotation = repository / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v1"
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", type=Path, default=annotation / "review_batches" / "batch_001.jsonl")
    parser.add_argument("--documents", type=Path, default=repository / "data" / "derived" / "ecommerce" / "used_phone_real_query_qrel_v1" / "documents.jsonl")
    parser.add_argument("--output-dir", type=Path, default=annotation / "artifacts")
    parser.add_argument("--cross-encoder-local-path", type=Path, default=repository / ".cache" / "models" / "bge-reranker-base-2cfc18c")
    parser.add_argument("--include-sealed-test", action="store_true", help="requires completed second-human review before use")
    args = parser.parse_args()
    if args.include_sealed_test:
        parser.error(
            "sealed-test release is unavailable: complete independent second-human "
            "review and adjudication before adding a dedicated release workflow"
        )
    rows, documents = read_jsonl(args.review), read_jsonl(args.documents)
    audit = audit_full_catalog_residuals(review_rows=rows, documents=documents)
    write_json(args.output_dir / "full_catalog_pool_residual_audit_v1.json", audit)
    blind = build_second_human_blind_package(rows)
    write_json(args.output_dir / "second_human_blind_review_manifest_v1.json", {
        "schemaVersion": "used-phone-human-qrel-second-review-manifest-v1", "status": "PENDING_SECOND_HUMAN_REVIEW_NOT_GOLD",
        "queryIds": [row["queryId"] for row in blind], "reviewerKind": "independent_human", "aiMayNotBeCountedAsSecondHuman": True,
        "output": "review_batches/second_human_blind_batch_001.json"})
    write_json(annotation / "review_batches" / "second_human_blind_batch_001.json", blind)
    encoder = LocalCrossEncoder(cache_dir=repository / ".cache" / "huggingface", model_path=args.cross_encoder_local_path)
    report = run_preregistered_baselines(review_rows=rows, documents=documents, cross_encoder=encoder, include_sealed_test=args.include_sealed_test)
    write_json(args.output_dir / "preregistered_primary_retrieval_baselines_v1.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
