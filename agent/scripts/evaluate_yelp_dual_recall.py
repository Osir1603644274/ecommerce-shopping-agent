import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.rag_dual_recall_evaluation import (  # noqa: E402
    build_dual_recall_report,
    build_yelp_corpus_snapshot,
)


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "yelp_dual_recall_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    parser.add_argument("--data-version")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    report = build_dual_recall_report(
        data_version=args.data_version,
        corpus_snapshot=build_yelp_corpus_snapshot(),
    )
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("method hit@3 hit@1 mrr")
    for name, result in report["methods"].items():
        metrics = result["metrics"]
        print(
            f"{name:12} "
            f"{metrics['hits']}/{metrics['total']} "
            f"{metrics['hitsAt1']}/{metrics['total']} "
            f"{metrics['mrr']:.4f}"
        )
    union = report["candidateUnion"]
    print(
        "unionRecall="
        f"{union['hits']}/{union['total']} "
        f"avgOverlap={union['averageOverlapSize']:.2f} "
        f"avgUnion={union['averageUnionSize']:.2f}"
    )
    print(f"Wrote {args.report_path}")
