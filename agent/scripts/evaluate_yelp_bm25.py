import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import DEFAULT_TOP_K, YELP_EVAL_PATH, evaluate_retrieval_cases, load_retrieval_cases  # noqa: E402
from app.rag_bm25 import get_yelp_review_bm25_index  # noqa: E402
from app.rag_bm25 import (  # noqa: E402
    search_yelp_reviews_bm25,
)
from app.rag_quality import search_yelp_reviews  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "yelp_bm25_report.json"
)
BM25_WEIGHT_GRID = [0.02, 0.05, 0.1, 0.2, 0.5, 1.0]


def _metrics_without_details(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in report.items()
        if key != "details"
    }


def _first_relevant_rank(
    retrieved_ids: list[str],
    relevant_review_ids: list[str],
) -> int | None:
    relevant_set = set(relevant_review_ids)
    return next(
        (
            rank
            for rank, review_id in enumerate(retrieved_ids, start=1)
            if review_id in relevant_set
        ),
        None,
    )


def _summarize_details(details: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(details)
    hits = sum(detail["hit"] for detail in details)
    hits_at_1 = sum(detail["hitAt1"] for detail in details)
    return {
        "total": total,
        "hits": hits,
        "hitRate": hits / total if total else 0.0,
        "hitsAt1": hits_at_1,
        "hitAt1Rate": hits_at_1 / total if total else 0.0,
        "mrr": (
            sum(detail["reciprocalRank"] for detail in details) / total
            if total
            else 0.0
        ),
    }


def _score_ranked_results(
    cases: list[dict[str, Any]],
    ranked_by_case_id: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    for case in cases:
        ranked = ranked_by_case_id[case["id"]]
        retrieved_ids = [item["reviewId"] for item in ranked[:DEFAULT_TOP_K]]
        first_relevant_rank = _first_relevant_rank(
            retrieved_ids,
            case["relevantReviewIds"],
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


def _rerank_candidates_with_bm25(
    question: str,
    candidates: list[dict[str, Any]],
    *,
    bm25_weight: float,
) -> list[dict[str, Any]]:
    index = get_yelp_review_bm25_index()
    candidate_ids = {review["reviewId"] for review in candidates}
    bm25_by_review_id = index.score_by_doc_id(question, candidate_ids)
    reranked = []
    for original_rank, review in enumerate(candidates, start=1):
        vector_score = float(review.get("score") or 0.0)
        bm25_score = float(bm25_by_review_id.get(review["reviewId"], 0.0))
        reranked.append(
            {
                **review,
                "originalRank": original_rank,
                "vectorScore": vector_score,
                "bm25Score": bm25_score,
                "rerankScore": vector_score + bm25_weight * bm25_score,
            }
        )
    reranked.sort(
        key=lambda review: (
            -float(review["rerankScore"]),
            int(review["originalRank"]),
        )
    )
    return reranked


def build_report() -> dict[str, Any]:
    cases = load_retrieval_cases(YELP_EVAL_PATH)
    bm25 = evaluate_retrieval_cases(cases, search_yelp_reviews_bm25, limit=DEFAULT_TOP_K)

    candidates_by_case_id = {
        case["id"]: search_yelp_reviews(case["question"], 10)
        for case in cases
    }
    vector = _score_ranked_results(
        cases,
        {
            case_id: candidates[:DEFAULT_TOP_K]
            for case_id, candidates in candidates_by_case_id.items()
        },
    )

    grid = []
    for bm25_weight in BM25_WEIGHT_GRID:
        ranked = {
            case["id"]: _rerank_candidates_with_bm25(
                case["question"],
                candidates_by_case_id[case["id"]],
                bm25_weight=bm25_weight,
            )
            for case in cases
        }
        candidate = _score_ranked_results(
            cases,
            ranked,
        )
        grid.append(
            {
                "bm25Weight": bm25_weight,
                **_metrics_without_details(candidate),
            }
        )

    best_grid_item = max(
        grid,
        key=lambda item: (
            item["hits"],
            item["mrr"],
            item["hitsAt1"],
        ),
    )
    best_weight = best_grid_item["bm25Weight"]
    hybrid = _score_ranked_results(
        cases,
        {
            case["id"]: _rerank_candidates_with_bm25(
                case["question"],
                candidates_by_case_id[case["id"]],
                bm25_weight=best_weight,
            )
            for case in cases
        },
    )

    return {
        "topK": DEFAULT_TOP_K,
        "sourceFilter": "yelp",
        "candidateLimit": 10,
        "vector": _metrics_without_details(vector),
        "bm25": _metrics_without_details(bm25),
        "hybrid": _metrics_without_details(hybrid),
        "bestBm25Weight": best_weight,
        "grid": grid,
    }


if __name__ == "__main__":
    report = build_report()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Yelp BM25 eval")
    for name in ["vector", "bm25", "hybrid"]:
        item = report[name]
        print(
            f"{name}: "
            f"Hit@{report['topK']}={item['hits']}/{item['total']} "
            f"({item['hitRate']:.1%}), "
            f"Hit@1={item['hitsAt1']}/{item['total']} "
            f"({item['hitAt1Rate']:.1%}), "
            f"MRR={item['mrr']:.4f}"
        )
    print(f"bestBm25Weight={report['bestBm25Weight']}")
    print(f"Wrote {REPORT_PATH}")
