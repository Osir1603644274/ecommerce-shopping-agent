"""Run strict train / validation / test evaluation on Yelp recommendation cases."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from recommendation.evaluation import evaluate_strict_hybrid_experiment, save_report
from recommendation.yelp_recommendation import DEFAULT_YELP_RECOMMENDATION_CASES_PATH


DEFAULT_YELP_CASE_SPLITS_PATH = (
    Path(__file__).parents[1]
    / "recommendation"
    / "data"
    / "processed"
    / "yelp_recommendation_case_splits.json"
)
DEFAULT_YELP_STRICT_REPORT_PATH = (
    Path(__file__).parents[1]
    / "recommendation"
    / "reports"
    / "yelp_strict_hybrid_experiment_report.json"
)


def stable_bucket(case: dict[str, Any], seed: int) -> int:
    """Return a deterministic 0-99 bucket for a user case."""
    key = f"{seed}:{case.get('sourceUserId') or case.get('userId')}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def split_cases(
    cases: list[dict[str, Any]],
    *,
    seed: int = 42,
    train_percent: int = 70,
    validation_percent: int = 15,
) -> dict[str, Any]:
    """Split cases by user into deterministic train / validation / test buckets."""
    if train_percent <= 0 or validation_percent <= 0:
        raise ValueError("train_percent and validation_percent must be positive")
    if train_percent + validation_percent >= 100:
        raise ValueError("train_percent + validation_percent must be less than 100")

    train: list[dict[str, Any]] = []
    validation: list[dict[str, Any]] = []
    test: list[dict[str, Any]] = []
    validation_cutoff = train_percent + validation_percent

    for case in cases:
        bucket = stable_bucket(case, seed)
        if bucket < train_percent:
            train.append(case)
        elif bucket < validation_cutoff:
            validation.append(case)
        else:
            test.append(case)

    return {
        "splitMethod": "stable_hash_by_user",
        "randomSeed": seed,
        "splitPercents": {
            "train": train_percent,
            "validation": validation_percent,
            "test": 100 - train_percent - validation_percent,
        },
        "counts": {
            "train": len(train),
            "validation": len(validation),
            "test": len(test),
            "total": len(cases),
        },
        "train": train,
        "validation": validation,
        "test": test,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run strict Yelp Hybrid train/validation/test experiment.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_YELP_RECOMMENDATION_CASES_PATH)
    parser.add_argument("--splits-output", type=Path, default=DEFAULT_YELP_CASE_SPLITS_PATH)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_YELP_STRICT_REPORT_PATH)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-percent", type=int, default=70)
    parser.add_argument("--validation-percent", type=int, default=15)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--popular-weights",
        default="0,0.1,0.2,0.3,0.5,0.75,1,1.5,2,3,5",
        help="Comma-separated Popular weights for validation grid.",
    )
    parser.add_argument(
        "--itemcf-weights",
        default="0.5,0.75,1,1.25,1.5,2",
        help="Comma-separated ItemCF weights for validation grid.",
    )
    parser.add_argument("--primary-metric", default="ndcgAtK")
    return parser.parse_args()


def parse_weights(value: str) -> tuple[float, ...]:
    return tuple(float(item.strip()) for item in value.split(",") if item.strip())


def main() -> None:
    args = parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    case_splits = split_cases(
        cases,
        seed=args.seed,
        train_percent=args.train_percent,
        validation_percent=args.validation_percent,
    )
    args.splits_output.parent.mkdir(parents=True, exist_ok=True)
    args.splits_output.write_text(
        json.dumps(case_splits, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = evaluate_strict_hybrid_experiment(
        case_splits,
        top_k=args.top_k,
        popular_weights=parse_weights(args.popular_weights),
        itemcf_weights=parse_weights(args.itemcf_weights),
        primary_metric=args.primary_metric,
    )
    save_report(report, args.report_output)

    selected = report["weightSelection"]
    test_metrics = report["test"]["hybrid"]["metrics"]
    print(
        "Generated strict Yelp Hybrid report: "
        f"train={case_splits['counts']['train']}, "
        f"validation={case_splits['counts']['validation']}, "
        f"test={case_splits['counts']['test']}. "
        f"Selected popularWeight={selected['bestPopularWeight']}, "
        f"itemcfWeight={selected['bestItemcfWeight']} on validation. "
        f"Test Hit@{args.top_k}={test_metrics['hitRateAtK']:.4f}, "
        f"MRR={test_metrics['mrr']:.4f}, "
        f"NDCG@{args.top_k}={test_metrics['ndcgAtK']:.4f}. "
        f"Report: {args.report_output}"
    )


if __name__ == "__main__":
    main()
