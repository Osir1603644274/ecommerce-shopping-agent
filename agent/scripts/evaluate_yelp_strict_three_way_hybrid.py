"""Run strict Yelp Popular + ItemCF + TypeId hybrid recommendation evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from recommendation.evaluation import (
    evaluate_strict_three_way_hybrid_experiment,
    save_report,
)
from recommendation.yelp_recommendation import (
    DEFAULT_YELP_RECOMMENDATION_CASES_PATH,
    DEFAULT_YELP_RECOMMENDATION_ITEMS_PATH,
)
from scripts.evaluate_yelp_strict_hybrid import (
    DEFAULT_YELP_CASE_SPLITS_PATH,
    parse_weights,
    split_cases,
)


DEFAULT_YELP_STRICT_THREE_WAY_REPORT_PATH = (
    Path(__file__).parents[1]
    / "recommendation"
    / "reports"
    / "yelp_strict_three_way_hybrid_experiment_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run strict Yelp Popular + ItemCF + TypeId hybrid experiment."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_YELP_RECOMMENDATION_CASES_PATH)
    parser.add_argument("--items", type=Path, default=DEFAULT_YELP_RECOMMENDATION_ITEMS_PATH)
    parser.add_argument("--splits-output", type=Path, default=DEFAULT_YELP_CASE_SPLITS_PATH)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_YELP_STRICT_THREE_WAY_REPORT_PATH)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-percent", type=int, default=70)
    parser.add_argument("--validation-percent", type=int, default=15)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--popular-weights",
        default="0,0.25,0.5,0.75,1",
        help="Comma-separated Popular weights for validation grid.",
    )
    parser.add_argument(
        "--itemcf-weights",
        default="0,0.25,0.5,0.75,1",
        help="Comma-separated ItemCF weights for validation grid.",
    )
    parser.add_argument(
        "--type-weights",
        default="0,0.25,0.5,0.75,1",
        help="Comma-separated TypeId preference weights for validation grid.",
    )
    parser.add_argument("--primary-metric", default="ndcgAtK")
    return parser.parse_args()


def load_items(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return list(payload.get("items", []))
    return list(payload)


def main() -> None:
    args = parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    items = load_items(args.items)
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

    report = evaluate_strict_three_way_hybrid_experiment(
        case_splits,
        items,
        top_k=args.top_k,
        popular_weights=parse_weights(args.popular_weights),
        itemcf_weights=parse_weights(args.itemcf_weights),
        type_weights=parse_weights(args.type_weights),
        primary_metric=args.primary_metric,
    )
    save_report(report, args.report_output)

    selected = report["weightSelection"]
    test_metrics = report["test"]["threeWayHybrid"]["metrics"]
    print(
        "Generated strict Yelp three-way Hybrid report: "
        f"train={case_splits['counts']['train']}, "
        f"validation={case_splits['counts']['validation']}, "
        f"test={case_splits['counts']['test']}. "
        f"Selected popularWeight={selected['bestPopularWeight']}, "
        f"itemcfWeight={selected['bestItemcfWeight']}, "
        f"typeWeight={selected['bestTypeWeight']} on validation. "
        f"Test Hit@{args.top_k}={test_metrics['hitRateAtK']:.4f}, "
        f"MRR={test_metrics['mrr']:.4f}, "
        f"NDCG@{args.top_k}={test_metrics['ndcgAtK']:.4f}. "
        f"Report: {args.report_output}"
    )


if __name__ == "__main__":
    main()
