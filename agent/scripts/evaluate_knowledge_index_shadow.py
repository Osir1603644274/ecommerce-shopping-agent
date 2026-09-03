import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge import search_knowledge_index  # noqa: E402
from app.knowledge_retrieval_quality import (  # noqa: E402
    evaluate_retrieval_quality,
)
from evaluation.rag_dual_recall_evaluation import build_yelp_corpus_snapshot  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "knowledge_index_shadow_report.json"
)


def metric_delta(current, shadow):
    return {
        key: shadow[key] - current[key]
        for key in (
            "completeHitRate",
            "relevantChunkRecall",
            "meanRelevantReciprocalRank",
        )
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    parser.add_argument("--data-version")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    current = evaluate_retrieval_quality()
    shadow = evaluate_retrieval_quality(retrieve=search_knowledge_index)
    report = {
        "experiment": "RAG-QUALITY-13 unified knowledge index shadow comparison",
        "dataVersion": args.data_version,
        "corpusSnapshot": build_yelp_corpus_snapshot(),
        "evaluationUse": "regression only; no parameter selection",
        "scope": {
            "sourceSelection": "explicit expectedSources from each case",
            "routerEvaluated": False,
            "reviewShopIdFilterApplied": False,
            "agentEntityResolutionEvaluated": False,
            "note": (
                "reviewFilter and shopEntity labels are retained in the dataset "
                "but are not passed to either retriever in this shadow comparison"
            ),
        },
        "current": current,
        "shadow": shadow,
        "delta": {
            "overall": metric_delta(current["overall"], shadow["overall"]),
            "validation": metric_delta(
                current["bySplit"]["validation"],
                shadow["bySplit"]["validation"],
            ),
            "test": metric_delta(
                current["bySplit"]["test"],
                shadow["bySplit"]["test"],
            ),
        },
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = args.report_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(args.report_path)
    for name, result in (("current", current), ("shadow", shadow)):
        overall = result["overall"]
        print(
            f"{name}: complete={overall['completeHits']}/{overall['caseCount']} "
            f"chunkRecall={overall['relevantChunkRecall']:.2%} "
            f"meanRR={overall['meanRelevantReciprocalRank']:.4f}"
        )
    print(f"Wrote {args.report_path}")
