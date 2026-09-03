import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge_retrieval_quality import evaluate_retrieval_quality  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "retrieval_quality_report.json"
)


if __name__ == "__main__":
    report = evaluate_retrieval_quality()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    overall = report["overall"]
    print(
        "Knowledge retrieval quality: "
        f"complete={overall['completeHits']}/{overall['caseCount']} "
        f"chunkRecall={overall['relevantChunkRecall']:.2%} "
        f"meanRR={overall['meanRelevantReciprocalRank']:.4f}"
    )
    for split, metrics in report["bySplit"].items():
        print(
            f"{split}: complete={metrics['completeHits']}/{metrics['caseCount']} "
            f"chunkRecall={metrics['relevantChunkRecall']:.2%}"
        )
    print(f"Wrote {REPORT_PATH}")
