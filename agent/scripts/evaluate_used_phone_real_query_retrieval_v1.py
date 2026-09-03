"""Run the frozen real-query retrieval ablation and write one JSON report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.evaluation.used_phone_real_query_retrieval_v1 import evaluate_bundle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bundle",
        type=Path,
        default=Path(
            "data/derived/ecommerce/used_phone_real_query_qrel_v1"
        ),
    )
    parser.add_argument("--split", choices=("train", "test"), default="train")
    parser.add_argument("--include-vector", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_bundle(
        args.bundle.resolve(),
        split=args.split,
        include_vector=args.include_vector,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
