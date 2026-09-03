"""CLI for preparing (not running) KuaiSearch retrieval G0."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "agent") not in sys.path:
    sys.path.insert(0, str(ROOT / "agent"))

from evaluation.kuaisearch_multicategory_retrieval_contract_v1 import FINAL_CODE_PIN_BASENAMES, prepare_g0


def default_code_paths() -> list[Path]:
    evaluation = ROOT / "agent" / "evaluation"
    scripts = ROOT / "agent" / "scripts"
    by_name = {
        "kuaisearch_multicategory_retrieval_contract_v1.py": evaluation / "kuaisearch_multicategory_retrieval_contract_v1.py",
        "prepare_kuaisearch_multicategory_retrieval_g0_v1.py": scripts / "prepare_kuaisearch_multicategory_retrieval_g0_v1.py",
        "kuaisearch_multicategory_retrieval_baseline_v1.py": evaluation / "kuaisearch_multicategory_retrieval_baseline_v1.py",
        "run_kuaisearch_multicategory_retrieval_baseline_v1.py": scripts / "run_kuaisearch_multicategory_retrieval_baseline_v1.py",
        "kuaisearch_multicategory_retrieval_scorer_v1.py": evaluation / "kuaisearch_multicategory_retrieval_scorer_v1.py",
        "score_kuaisearch_multicategory_retrieval_g1_v1.py": scripts / "score_kuaisearch_multicategory_retrieval_g1_v1.py",
    }
    return [by_name[name] for name in FINAL_CODE_PIN_BASENAMES]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--breadth-manifest", type=Path, default=ROOT / "data/benchmarks/ecommerce/kuaisearch_multicategory_breadth_v1_20260824_human_r1/manifest.json")
    p.add_argument("--breadth-queries", type=Path, default=ROOT / "data/benchmarks/ecommerce/kuaisearch_multicategory_breadth_v1_20260824_human_r1/queries.jsonl")
    p.add_argument("--documents", type=Path, required=True)
    p.add_argument("--dense-cache", type=Path, required=True)
    p.add_argument("--cross-encoder-cache", type=Path, required=True)
    p.add_argument("--cross-encoder-deps", type=Path, required=True)
    p.add_argument("--code-path", type=Path, action="append", default=[])
    p.add_argument("--top-k", type=int, default=100)
    p.add_argument("--seed", type=int, default=0)
    return p


def main() -> int:
    args = parser().parse_args()
    code_paths = args.code_path or default_code_paths()
    result = prepare_g0(output_dir=args.output, breadth_manifest=args.breadth_manifest, breadth_queries=args.breadth_queries,
                        documents=args.documents, dense_cache=args.dense_cache, cross_encoder_cache=args.cross_encoder_cache,
                        cross_encoder_deps=args.cross_encoder_deps, code_paths=code_paths,
                        top_k=args.top_k, seed=args.seed)
    print(f"prepared {result['g0Version']}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
