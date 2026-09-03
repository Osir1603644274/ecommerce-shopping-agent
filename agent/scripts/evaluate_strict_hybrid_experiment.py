"""Run strict train / validation / test Hybrid recommendation experiment."""

from recommendation.evaluation import (
    DEFAULT_STRICT_HYBRID_REPORT_PATH,
    evaluate_and_save_strict_hybrid_experiment,
)


if __name__ == "__main__":
    report = evaluate_and_save_strict_hybrid_experiment()
    selected = report["weightSelection"]
    test_metrics = report["test"]["hybrid"]["metrics"]
    print(f"Saved strict Hybrid experiment report to {DEFAULT_STRICT_HYBRID_REPORT_PATH}")
    print(
        "Selected on validation: "
        f"popularWeight={selected['bestPopularWeight']}, "
        f"itemcfWeight={selected['bestItemcfWeight']}, "
        f"primaryMetric={selected['primaryMetric']}"
    )
    print(
        "Test Hybrid: "
        f"Hit@K={test_metrics['hitRateAtK']:.4f}, "
        f"Precision@K={test_metrics['precisionAtK']:.4f}, "
        f"Recall@K={test_metrics['recallAtK']:.4f}, "
        f"MRR={test_metrics['mrr']:.4f}, "
        f"NDCG@K={test_metrics['ndcgAtK']:.4f}"
    )