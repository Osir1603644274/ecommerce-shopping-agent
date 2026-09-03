"""Evaluate the FunRec global popular-item baseline on MovieLens cases."""

from recommendation.evaluation import (
    DEFAULT_REPORT_PATH,
    evaluate_and_save_popular_baseline,
)


if __name__ == "__main__":
    report = evaluate_and_save_popular_baseline()
    metrics = report["metrics"]
    print(f"Saved popular baseline report to {DEFAULT_REPORT_PATH}")
    print(f"Cases: {report['caseCount']}, TopK: {report['topK']}")
    print(
        "Metrics: "
        f"Hit@K={metrics['hitRateAtK']:.4f}, "
        f"Precision@K={metrics['precisionAtK']:.4f}, "
        f"Recall@K={metrics['recallAtK']:.4f}, "
        f"MRR={metrics['mrr']:.4f}, "
        f"NDCG@K={metrics['ndcgAtK']:.4f}"
    )