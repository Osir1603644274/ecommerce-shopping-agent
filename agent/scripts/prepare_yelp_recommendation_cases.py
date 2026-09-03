"""Build a Yelp recommendation benchmark and run baseline reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from recommendation.baselines import popular_item_scores
from recommendation.evaluation import (
    evaluate_hybrid,
    evaluate_itemcf,
    evaluate_popular_from_scores,
)
from recommendation.itemcf import build_item_similarity_table, save_similarity_table
from recommendation.yelp import DEFAULT_RAW_DIR
from recommendation.yelp_recommendation import (
    DEFAULT_YELP_RECOMMENDATION_CASES_PATH,
    DEFAULT_YELP_RECOMMENDATION_ITEMS_PATH,
    convert_yelp_recommendation_cases,
)


DEFAULT_YELP_RECOMMENDATION_SIMILARITY_PATH = (
    Path(__file__).parents[1]
    / "recommendation"
    / "data"
    / "processed"
    / "yelp_item_similarity.json"
)
DEFAULT_YELP_RECOMMENDATION_REPORT_PATH = (
    Path(__file__).parents[1]
    / "recommendation"
    / "reports"
    / "yelp_recommendation_baseline_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Yelp history/target recommendation cases and baseline reports."
    )
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--cases-output", type=Path, default=DEFAULT_YELP_RECOMMENDATION_CASES_PATH)
    parser.add_argument("--items-output", type=Path, default=DEFAULT_YELP_RECOMMENDATION_ITEMS_PATH)
    parser.add_argument("--similarity-output", type=Path, default=DEFAULT_YELP_RECOMMENDATION_SIMILARITY_PATH)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_YELP_RECOMMENDATION_REPORT_PATH)
    parser.add_argument("--city", default="Philadelphia")
    parser.add_argument("--positive-threshold", type=float, default=4.0)
    parser.add_argument("--min-positive-interactions", type=int, default=5)
    parser.add_argument("--min-history-interactions", type=int, default=3)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--max-cases", type=int, default=1000)
    parser.add_argument("--min-business-review-count", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--itemcf-top-n", type=int, default=50)
    parser.add_argument("--popular-weight", type=float, default=0.3)
    parser.add_argument("--itemcf-weight", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = convert_yelp_recommendation_cases(
        raw_dir=args.raw_dir,
        cases_output_path=args.cases_output,
        items_output_path=args.items_output,
        city=args.city,
        positive_score_threshold=args.positive_threshold,
        min_positive_interactions=args.min_positive_interactions,
        min_history_interactions=args.min_history_interactions,
        train_ratio=args.train_ratio,
        max_cases=args.max_cases,
        min_business_review_count=args.min_business_review_count,
    )
    cases = result["cases"]
    similarity_table = build_item_similarity_table(
        cases,
        positive_score_threshold=args.positive_threshold,
        top_n=args.itemcf_top_n,
    )
    save_similarity_table(similarity_table, args.similarity_output)

    history_interactions = [
        interaction
        for case in cases
        for interaction in case.get("history", [])
    ]
    popular_scores = popular_item_scores(
        history_interactions,
        positive_score_threshold=args.positive_threshold,
    )
    popular_report = evaluate_popular_from_scores(
        cases,
        popular_scores,
        top_k=args.top_k,
        candidate_limit=max(1000, len(popular_scores)),
        baseline_name="yelp_global_popular",
    )
    itemcf_report = evaluate_itemcf(cases, similarity_table, top_k=args.top_k)
    hybrid_report = evaluate_hybrid(
        cases,
        similarity_table,
        top_k=args.top_k,
        popular_weight=args.popular_weight,
        itemcf_weight=args.itemcf_weight,
        popular_scores=popular_scores,
    )
    report = {
        "metadata": result["metadata"],
        "topK": args.top_k,
        "similarity": {
            "path": str(args.similarity_output),
            "algorithm": similarity_table["algorithm"],
            "itemCount": similarity_table["itemCount"],
            "contributingCaseCount": similarity_table["contributingCaseCount"],
        },
        "popular": popular_report["metrics"],
        "itemcf": itemcf_report["metrics"],
        "hybrid": {
            "popularWeight": args.popular_weight,
            "itemcfWeight": args.itemcf_weight,
            **hybrid_report["metrics"],
        },
        "caseDiagnostics": {
            "minHistoryCount": min((case["historyCount"] for case in cases), default=0),
            "maxHistoryCount": max((case["historyCount"] for case in cases), default=0),
            "minTargetCount": min((case["targetCount"] for case in cases), default=0),
            "maxTargetCount": max((case["targetCount"] for case in cases), default=0),
        },
        "reports": {
            "popular": popular_report,
            "itemcf": itemcf_report,
            "hybrid": hybrid_report,
        },
    }
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Generated Yelp recommendation benchmark: "
        f"{result['metadata']['caseCount']} cases, "
        f"{result['metadata']['candidateItemCount']} candidate items. "
        f"Popular Hit@{args.top_k}={popular_report['metrics']['hitRateAtK']:.4f}, "
        f"ItemCF Hit@{args.top_k}={itemcf_report['metrics']['hitRateAtK']:.4f}, "
        f"Hybrid Hit@{args.top_k}={hybrid_report['metrics']['hitRateAtK']:.4f}. "
        f"Report: {args.report_output}"
    )


if __name__ == "__main__":
    main()
