import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.rag_metadata_filter_evaluation import (  # noqa: E402
    build_metadata_filter_validation_report,
)


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "review_metadata_filter_report.json"
)


if __name__ == "__main__":
    report = build_metadata_filter_validation_report()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    before = report["unfilteredYelp"]
    after = report["filteredByShop"]
    print(
        "Review metadata filter validation: "
        f"Hit@3 {before['hits']}/{before['total']} -> "
        f"{after['hits']}/{after['total']}; "
        f"MRR {before['mrr']:.4f} -> {after['mrr']:.4f}"
    )
    print(f"Filter violations: {len(report['filterViolations'])}")
    print(f"Wrote {REPORT_PATH}")
