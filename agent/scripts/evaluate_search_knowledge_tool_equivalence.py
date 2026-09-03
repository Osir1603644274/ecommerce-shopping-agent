import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import (  # noqa: E402
    DEFAULT_TOP_K,
    EVAL_PATH,
    YELP_EVAL_PATH,
    get_embedding_model,
    get_qdrant_client,
    load_retrieval_cases,
)
from app.settings import settings  # noqa: E402
from app.tool_equivalence import (  # noqa: E402
    build_search_knowledge_tool_detail_from_reviews,
    build_search_reviews_tool_detail,
    compare_review_tool_outputs,
)
from app.tools import search_knowledge_tool, search_reviews_tool  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "search_knowledge_tool_equivalence_report.json"
)


def _point_to_review(point) -> dict[str, Any]:
    payload = point.payload
    return {
        "reviewId": payload["reviewId"],
        "shopId": payload["shopId"],
        "shopName": payload["shopName"],
        "text": payload["text"],
        "originalText": payload.get("originalText"),
        "contentZh": payload.get("contentZh"),
        "source": payload.get("source"),
        "language": payload.get("language"),
        "translationStatus": payload.get("translationStatus"),
        "score": point.score,
    }


def batch_search_reviews(
    questions: list[str],
    *,
    limit: int = DEFAULT_TOP_K,
) -> dict[str, list[dict[str, Any]]]:
    """Batch the same Qdrant vector retrieval used by search_reviews_tool."""

    model = get_embedding_model()
    vectors = list(model.embed(questions))
    client = get_qdrant_client()
    results: dict[str, list[dict[str, Any]]] = {}
    for question, vector in zip(questions, vectors, strict=True):
        result = client.query_points(
            collection_name=settings.rag_collection_name,
            query=vector.tolist(),
            limit=limit,
            with_payload=True,
        )
        results[question] = [_point_to_review(point) for point in result.points]
    return results


def _summarize_comparisons(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(comparisons)
    exact_matches = sum(item["exactMatch"] for item in comparisons)
    same_hits = sum(item["sameHit"] for item in comparisons)
    candidate_regressions = sum(item["candidateRegression"] for item in comparisons)
    candidate_sources_ok = sum(item["candidateSourcesOk"] for item in comparisons)
    return {
        "total": total,
        "exactMatches": exact_matches,
        "exactMatchRate": exact_matches / total if total else 0,
        "sameHits": same_hits,
        "sameHitRate": same_hits / total if total else 0,
        "candidateRegressions": candidate_regressions,
        "candidateSourcesOk": candidate_sources_ok,
        "candidateSourcesOkRate": candidate_sources_ok / total if total else 0,
    }


def _compare_dataset(
    cases: list[dict[str, Any]],
    reviews_by_question: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    comparisons: list[dict[str, Any]] = []
    for case in cases:
        question = case["question"]
        reviews = reviews_by_question[question]
        legacy_detail = build_search_reviews_tool_detail(question, reviews)
        candidate_detail = build_search_knowledge_tool_detail_from_reviews(question, reviews)
        comparisons.append(
            compare_review_tool_outputs(
                case,
                legacy_output=legacy_detail,
                candidate_output=candidate_detail,
            )
        )

    return {
        **_summarize_comparisons(comparisons),
        "mismatches": [
            item
            for item in comparisons
            if not item["exactMatch"]
        ],
        "regressions": [
            item
            for item in comparisons
            if item["candidateRegression"]
        ],
        "sourceMismatches": [
            item
            for item in comparisons
            if not item["candidateSourcesOk"]
        ],
        "details": comparisons,
    }


async def _live_tool_smoke(case: dict[str, Any]) -> dict[str, Any]:
    """Call the actual async tools once to prove the wrappers are wired correctly."""

    question = case["question"]
    start = time.perf_counter()
    legacy_output = await search_reviews_tool(question)
    legacy_wall_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    candidate_output = await search_knowledge_tool(question, ["reviews"])
    candidate_wall_ms = (time.perf_counter() - start) * 1000
    comparison = compare_review_tool_outputs(
        case,
        legacy_output=legacy_output,
        candidate_output=candidate_output,
    )
    return {
        "caseId": case["id"],
        "question": question,
        "legacyToolOk": legacy_output.ok,
        "candidateToolOk": candidate_output.ok,
        "legacyWallMs": legacy_wall_ms,
        "candidateWallMs": candidate_wall_ms,
        "comparison": comparison,
    }


def build_report() -> dict[str, Any]:
    datasets = {
        "legacy": load_retrieval_cases(EVAL_PATH),
        "yelp": load_retrieval_cases(YELP_EVAL_PATH),
    }
    questions = list(
        dict.fromkeys(
            case["question"]
            for cases in datasets.values()
            for case in cases
        )
    )

    start = time.perf_counter()
    reviews_by_question = batch_search_reviews(questions, limit=DEFAULT_TOP_K)
    retrieval_duration_ms = (time.perf_counter() - start) * 1000

    report: dict[str, Any] = {
        "topK": DEFAULT_TOP_K,
        "evaluationMode": "batch_qdrant_retrieval_then_tool_detail_shape_equivalence",
        "questionCount": len(questions),
        "retrievalDurationMs": retrieval_duration_ms,
        "avgQuestionDurationMs": retrieval_duration_ms / len(questions) if questions else 0,
        "datasets": {},
    }
    for name, cases in datasets.items():
        report["datasets"][name] = _compare_dataset(cases, reviews_by_question)

    first_case = datasets["legacy"][0]
    report["liveToolSmoke"] = asyncio.run(_live_tool_smoke(first_case))
    return report


if __name__ == "__main__":
    report = build_report()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "Tool equivalence eval: "
        f"questions={report['questionCount']}, "
        f"batchRetrieval={report['retrievalDurationMs']:.2f}ms"
    )
    for name, dataset in report["datasets"].items():
        print(
            f"{name}: "
            f"cases={dataset['total']}, "
            f"exact={dataset['exactMatches']}/{dataset['total']}, "
            f"sameHit={dataset['sameHits']}/{dataset['total']}, "
            f"regressions={dataset['candidateRegressions']}, "
            f"sourcesOk={dataset['candidateSourcesOk']}/{dataset['total']}"
        )
    smoke = report["liveToolSmoke"]
    print(
        "live smoke: "
        f"legacyOk={smoke['legacyToolOk']}, "
        f"candidateOk={smoke['candidateToolOk']}, "
        f"exact={smoke['comparison']['exactMatch']}"
    )
    print(f"Wrote {REPORT_PATH}")
