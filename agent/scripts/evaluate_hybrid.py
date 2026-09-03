"""Evaluate Popular + ItemCF hybrid recommendations on processed MovieLens cases."""

from recommendation.evaluation import (
    DEFAULT_HYBRID_REPORT_PATH,
    evaluate_and_save_hybrid,
)


if __name__ == "__main__":
    report = evaluate_and_save_hybrid()
    metrics = report["metrics"]
    print(f"Saved Hybrid report to {DEFAULT_HYBRID_REPORT_PATH}")
    print(
        "Summary: "
        f"cases={report['caseCount']}, "
        f"topK={report['topK']}, "
        f"popularWeight={report['popularWeight']}, "
        f"itemcfWeight={report['itemcfWeight']}, "
        f"Hit@K={metrics['hitRateAtK']:.4f}, "
        f"Precision@K={metrics['precisionAtK']:.4f}, "
        f"Recall@K={metrics['recallAtK']:.4f}, "
        f"MRR={metrics['mrr']:.4f}, "
        f"NDCG@K={metrics['ndcgAtK']:.4f}"
    )