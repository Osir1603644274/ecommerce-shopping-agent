import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.rag_shop_aggregation_evaluation import (  # noqa: E402
    build_shop_aggregation_validation_report,
)


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "shop_aggregation_validation_report.json"
)


if __name__ == "__main__":
    report = build_shop_aggregation_validation_report()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("mode threshold reviewHit shopHit@1 shopMRR coverage evidence")
    for config in report["configurations"]:
        metrics = config["metrics"]
        print(
            f"{config['mode']:7} {config['thresholdLabel']:>9} "
            f"{metrics['relevantReviewHits']}/{metrics['caseCount']} "
            f"{metrics['targetShopHitsAt1']}/{metrics['caseCount']} "
            f"{metrics['targetShopMrr']:.3f} "
            f"{metrics['averageShopCoverage']:.3f} "
            f"{metrics['averageEvidenceCount']:.2f}"
        )
    print(f"Wrote {REPORT_PATH}")
