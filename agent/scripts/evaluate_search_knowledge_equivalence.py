import json
import math
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge_equivalence import reviews_to_search_knowledge_review_dicts
from app.rag import (
    DEFAULT_TOP_K,
    EVAL_PATH,
    YELP_EVAL_PATH,
    get_embedding_model,
    get_qdrant_client,
    load_retrieval_cases,
)
from app.settings import settings


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "search_knowledge_equivalence_report.json"
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
    """Run the same Qdrant review retrieval as search_reviews, but batch embeddings."""

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


def _summarize_details(details: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(details)
    hits = sum(item["hit"] for item in details)
    hits_at_1 = sum(item["hitAt1"] for item in details)
    return {
        "total": total,
        "hits": hits,
        "hitRate": hits / total if total else 0,
        "hitsAt1": hits_at_1,
        "hitAt1Rate": hits_at_1 / total if total else 0,
        "mrr": (
            sum(item["reciprocalRank"] for item in details) / total
            if total
            else 0
        ),
    }


def _score_cases(
    cases: list[dict[str, Any]],
    results_by_question: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    for case in cases:
        retrieved = results_by_question[case["question"]]
        retrieved_ids = [item["reviewId"] for item in retrieved]
        relevant_ids = set(case["relevantReviewIds"])
        first_relevant_rank = next(
            (
                rank
                for rank, review_id in enumerate(retrieved_ids, start=1)
                if review_id in relevant_ids
            ),
            None,
        )
        details.append(
            {
                "caseId": case["id"],
                "hit": first_relevant_rank is not None,
                "hitAt1": first_relevant_rank == 1,
                "firstRelevantRank": first_relevant_rank,
                "reciprocalRank": 1 / first_relevant_rank if first_relevant_rank else 0.0,
                "expectedReviewIds": case["relevantReviewIds"],
                "retrievedReviewIds": retrieved_ids,
            }
        )
    return {**_summarize_details(details), "details": details}


def _compare_converted_results(
    cases: list[dict[str, Any]],
    baseline_by_question: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    exact_match_count = 0
    same_hit_count = 0
    candidate_regression_count = 0
    for case in cases:
        baseline_reviews = baseline_by_question[case["question"]]
        candidate_reviews = reviews_to_search_knowledge_review_dicts(baseline_reviews)
        baseline_ids = [item["reviewId"] for item in baseline_reviews]
        candidate_ids = [item["reviewId"] for item in candidate_reviews]
        relevant_ids = set(case["relevantReviewIds"])
        baseline_hit = bool(relevant_ids & set(baseline_ids))
        candidate_hit = bool(relevant_ids & set(candidate_ids))
        exact_match = baseline_ids == candidate_ids
        same_hit = baseline_hit == candidate_hit
        candidate_regression = baseline_hit and not candidate_hit
        exact_match_count += int(exact_match)
        same_hit_count += int(same_hit)
        candidate_regression_count += int(candidate_regression)
        details.append(
            {
                "caseId": case["id"],
                "expectedReviewIds": case["relevantReviewIds"],
                "baselineReviewIds": baseline_ids,
                "candidateReviewIds": candidate_ids,
                "exactMatch": exact_match,
                "baselineHit": baseline_hit,
                "candidateHit": candidate_hit,
                "sameHit": same_hit,
                "candidateRegression": candidate_regression,
            }
        )
    total = len(details)
    return {
        "total": total,
        "exactMatches": exact_match_count,
        "exactMatchRate": exact_match_count / total if total else 0,
        "sameHitCount": same_hit_count,
        "sameHitRate": same_hit_count / total if total else 0,
        "candidateRegressions": candidate_regression_count,
        "details": details,
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
    baseline_by_question = batch_search_reviews(questions, limit=DEFAULT_TOP_K)
    duration_ms = (time.perf_counter() - start) * 1000

    report: dict[str, Any] = {
        "topK": DEFAULT_TOP_K,
        "retrievalMode": "batch_search_reviews_once_then_search_knowledge_citation_conversion",
        "questionCount": len(questions),
        "retrievalDurationMs": duration_ms,
        "avgQuestionDurationMs": duration_ms / len(questions) if questions else 0,
        "datasets": {},
    }
    for name, cases in datasets.items():
        baseline_subset = {
            case["question"]: baseline_by_question[case["question"]]
            for case in cases
        }
        baseline_metrics = _score_cases(cases, baseline_subset)
        candidate_by_question = {
            question: reviews_to_search_knowledge_review_dicts(reviews)
            for question, reviews in baseline_subset.items()
        }
        candidate_metrics = _score_cases(cases, candidate_by_question)
        equivalence = _compare_converted_results(cases, baseline_subset)
        report["datasets"][name] = {
            "caseCount": len(cases),
            "baseline": {
                key: value
                for key, value in baseline_metrics.items()
                if key != "details"
            },
            "candidate": {
                key: value
                for key, value in candidate_metrics.items()
                if key != "details"
            },
            "equivalence": {
                key: value
                for key, value in equivalence.items()
                if key != "details"
            },
            "regressions": [
                detail
                for detail in equivalence["details"]
                if detail["candidateRegression"]
            ],
            "mismatches": [
                detail
                for detail in equivalence["details"]
                if not detail["exactMatch"]
            ],
        }
    return report


if __name__ == "__main__":
    report = build_report()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "Batch retrieval: "
        f"questions={report['questionCount']}, "
        f"total={report['retrievalDurationMs']:.2f}ms, "
        f"avg={report['avgQuestionDurationMs']:.2f}ms"
    )
    for name, result in report["datasets"].items():
        baseline = result["baseline"]
        candidate = result["candidate"]
        equivalence = result["equivalence"]
        print(f"{name}: cases={result['caseCount']}, topK={report['topK']}")
        print(
            "  baseline search_reviews: "
            f"Hit@{report['topK']}={baseline['hits']}/{result['caseCount']} "
            f"({baseline['hitRate']:.1%}), MRR={baseline['mrr']:.4f}"
        )
        print(
            "  candidate search_knowledge reviews-only shape: "
            f"Hit@{report['topK']}={candidate['hits']}/{result['caseCount']} "
            f"({candidate['hitRate']:.1%}), MRR={candidate['mrr']:.4f}"
        )
        print(
            "  equivalence: "
            f"exact={equivalence['exactMatches']}/{equivalence['total']} "
            f"({equivalence['exactMatchRate']:.1%}), "
            f"candidateRegressions={equivalence['candidateRegressions']}"
        )
    print(f"Wrote report to {REPORT_PATH}")
