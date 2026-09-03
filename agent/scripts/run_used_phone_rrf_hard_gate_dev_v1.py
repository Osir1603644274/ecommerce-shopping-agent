"""Run the non-sealed RRF Top-50 plus deterministic hard-gate baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.evaluation.used_phone_human_qrel_phase_v1 import (
    run_nonsealed_rrf_hard_gate_baseline,
    write_json,
)
from agent.evaluation.used_phone_human_qrel_v1 import read_jsonl


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    annotation = repository / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v1"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, default=annotation / "review_batches" / "batch_001.jsonl")
    parser.add_argument("--documents", type=Path, default=repository / "data" / "derived" / "ecommerce" / "used_phone_real_query_qrel_v1" / "documents.jsonl")
    parser.add_argument("--output", type=Path, default=annotation / "artifacts" / "rrf_hard_gate_development_validation_v1.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite hard-gate baseline: {args.output}")
    report = run_nonsealed_rrf_hard_gate_baseline(
        review_rows=read_jsonl(args.review),
        documents=read_jsonl(args.documents),
    )
    write_json(args.output, report)
    print(json.dumps({
        "output": str(args.output),
        "status": report["status"],
        "staticMetricQueryIds": report["staticMetricQueryIds"],
        "sealedDataRead": report["sealedDataRead"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
