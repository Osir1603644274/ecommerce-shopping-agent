import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.rag_content_reranker_evaluation import (  # noqa: E402
    build_content_reranker_validation_report,
)


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "content_reranker_validation_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    report = await build_content_reranker_validation_report()
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = args.report_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(args.report_path)
    print("method precision@5 recall@5 ndcg@5 mrr evidence-recall")
    for name, result in report["methods"].items():
        metrics = result["metrics"]
        print(
            f"{name:20} "
            f"{metrics['meanPrecisionAt5']:.4f} "
            f"{metrics['meanRecallAt5']:.4f} "
            f"{metrics['meanNdcgAt5']:.4f} "
            f"{metrics['mrr']:.4f} "
            f"{metrics['meanEvidenceShopRecall']:.4f}"
        )
    print(f"Wrote {args.report_path}")


if __name__ == "__main__":
    asyncio.run(main())
