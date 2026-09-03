import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge import search_knowledge, search_knowledge_index  # noqa: E402
from app.knowledge_retrieval_quality import (  # noqa: E402
    evaluate_retrieval_quality_cases,
    load_retrieval_quality_cases,
)
from evaluation.knowledge_source_aware_evaluation import (  # noqa: E402
    build_source_aware_validation_report,
)
from evaluation.rag_dual_recall_evaluation import build_yelp_corpus_snapshot  # noqa: E402


DEFAULT_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "source_aware_validation_zh_v1_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--data-version", default="knowledge-zh-v1")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    validation_cases = [
        case
        for case in load_retrieval_quality_cases()
        if case["split"] == "validation"
    ]
    current = evaluate_retrieval_quality_cases(
        validation_cases,
        retrieve=lambda query, sources, limit: search_knowledge(
            query,
            sources=sources,
            limit=limit,
        ),
    )
    pure_vector = evaluate_retrieval_quality_cases(
        validation_cases,
        retrieve=lambda query, sources, limit: search_knowledge_index(
            query,
            sources=sources,
            limit=limit,
        ),
    )
    source_aware = asyncio.run(
        build_source_aware_validation_report(validation_cases)
    )
    report = {
        "experiment": "RAG-QUALITY-13 source-aware unified index validation",
        "dataVersion": args.data_version,
        "corpusSnapshot": build_yelp_corpus_snapshot(),
        "current": current,
        "pureVector": pure_vector,
        "sourceAware": source_aware,
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = args.report_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(args.report_path)
    for name, result in (
        ("current", current),
        ("pureVector", pure_vector),
        ("sourceAware", source_aware["retrieval"]),
    ):
        metrics = result["overall"]
        print(
            f"{name}: complete={metrics['completeHits']}/{metrics['caseCount']} "
            f"chunkRecall={metrics['relevantChunkRecall']:.2%} "
            f"meanRR={metrics['meanRelevantReciprocalRank']:.4f}"
        )
    print(f"Wrote {args.report_path}")
