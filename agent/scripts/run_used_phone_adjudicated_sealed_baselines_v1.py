"""Run the authorized one-shot offline baselines on adjudicated sealed Qrels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.evaluation.used_phone_human_qrel_phase_v1 import (
    freeze_adjudicated_sealed_human_qrels,
    run_adjudicated_sealed_baselines,
    write_json,
)
from agent.evaluation.used_phone_human_qrel_v1 import LocalCrossEncoder, read_jsonl


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    annotation = repository / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v1"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize-one-shot-sealed-evaluation", action="store_true")
    parser.add_argument("--first-review", type=Path, default=annotation / "review_batches" / "batch_001.jsonl")
    parser.add_argument("--second-review", type=Path, default=annotation / "review_batches" / "second_human_completed_batch_001.json")
    parser.add_argument("--adjudication", type=Path, default=annotation / "review_batches" / "human_adjudication_batch_001.json")
    parser.add_argument("--sealed-freeze", type=Path, default=annotation / "sealed" / "adjudicated_sealed_qrels_v1.json")
    parser.add_argument("--documents", type=Path, default=repository / "data" / "derived" / "ecommerce" / "used_phone_real_query_qrel_v1" / "documents.jsonl")
    parser.add_argument("--cross-encoder-local-path", type=Path, default=repository / ".cache" / "models" / "bge-reranker-base-2cfc18c")
    parser.add_argument("--output", type=Path, default=annotation / "artifacts" / "adjudicated_sealed_one_shot_offline_baselines_v1.json")
    args = parser.parse_args()
    if not args.authorize_one_shot_sealed_evaluation:
        parser.error("explicit --authorize-one-shot-sealed-evaluation is required")
    if args.output.exists():
        parser.error(f"refusing to overwrite one-shot sealed evaluation: {args.output}")
    try:
        first = read_jsonl(args.first_review)
        second = json.loads(args.second_review.read_text(encoding="utf-8"))
        adjudication = json.loads(args.adjudication.read_text(encoding="utf-8"))
        sealed = json.loads(args.sealed_freeze.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read one-shot sealed input: {exc}")
    authenticated = freeze_adjudicated_sealed_human_qrels(
        first, second, adjudication
    )
    if sealed != authenticated:
        parser.error("sealed freeze does not match authenticated human inputs")
    encoder = LocalCrossEncoder(
        cache_dir=repository / ".cache" / "huggingface",
        model_path=args.cross_encoder_local_path,
    )
    report = run_adjudicated_sealed_baselines(
        review_rows=first,
        documents=read_jsonl(args.documents),
        sealed_freeze=sealed,
        cross_encoder=encoder,
    )
    write_json(args.output, report)
    print(json.dumps({
        "output": str(args.output),
        "status": report["status"],
        "queryIds": report["queryIds"],
        "productionReleaseAllowed": report["productionReleaseAllowed"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
