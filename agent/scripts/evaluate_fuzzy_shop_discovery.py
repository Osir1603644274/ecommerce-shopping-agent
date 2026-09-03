import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.rag_fuzzy_discovery_evaluation import (  # noqa: E402
    build_fuzzy_shop_discovery_validation_report,
)


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "fuzzy_shop_discovery_validation_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    report = build_fuzzy_shop_discovery_validation_report()
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("method precision@5 recall@5 ndcg@5 mrr evidence-recall")
    for name, result in report["methods"].items():
        metrics = result["metrics"]
        print(
            f"{name:15} "
            f"{metrics['meanPrecisionAt5']:.4f} "
            f"{metrics['meanRecallAt5']:.4f} "
            f"{metrics['meanNdcgAt5']:.4f} "
            f"{metrics['mrr']:.4f} "
            f"{metrics['meanEvidenceShopRecall']:.4f}"
        )
    print(f"Wrote {args.report_path}")
