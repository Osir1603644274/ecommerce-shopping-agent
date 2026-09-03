"""Run Hybrid weight grid search on processed MovieLens cases."""

from recommendation.evaluation import (
    DEFAULT_HYBRID_GRID_REPORT_PATH,
    evaluate_and_save_hybrid_weight_grid,
)


if __name__ == "__main__":
    report = evaluate_and_save_hybrid_weight_grid()
    best = report["bestResult"]
    print(f"Saved Hybrid weight grid report to {DEFAULT_HYBRID_GRID_REPORT_PATH}")
    if best:
        metrics = best["metrics"]
        print(
            "Best: "
            f"popularWeight={best['popularWeight']}, "
            f"itemcfWeight={best['itemcfWeight']}, "
            f"primaryMetric={report['primaryMetric']}, "
            f"Hit@K={metrics['hitRateAtK']:.4f}, "
            f"Precision@K={metrics['precisionAtK']:.4f}, "
            f"Recall@K={metrics['recallAtK']:.4f}, "
            f"MRR={metrics['mrr']:.4f}, "
            f"NDCG@K={metrics['ndcgAtK']:.4f}"
        )