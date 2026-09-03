import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import DEFAULT_TOP_K, YELP_EVAL_PATH, load_retrieval_cases  # noqa: E402
from app.rag_quality import rerank_reviews_by_lexical_overlap, search_yelp_reviews  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "yelp_lexical_rerank_report.json"
)
LEXICAL_WEIGHT_GRID = [0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0]


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


def _compare_details(
    cases: list[dict[str, Any]],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    baseline_details = {detail["caseId"]: detail for detail in baseline["details"]}
    candidate_details = {detail["caseId"]: detail for detail in candidate["details"]}
    comparisons = []
    for case in cases:
        case_id = case["id"]
        before = baseline_details[case_id]
        after = candidate_details[case_id]
        comparisons.append(
            {
                "caseId": case_id,
                "question": case["question"],
                "expectedReviewIds": case["relevantReviewIds"],
                "baselineHit": before["hit"],
                "candidateHit": after["hit"],
                "baselineFirstRelevantRank": before["firstRelevantRank"],
                "candidateFirstRelevantRank": after["firstRelevantRank"],
                "baselineReviewIds": before["retrievedReviewIds"],
                "candidateReviewIds": after["retrievedReviewIds"],
                "improved": (not before["hit"]) and after["hit"],
                "regressed": before["hit"] and (not after["hit"]),
            }
        )
    return comparisons


def build_report() -> dict[str, Any]:
    cases = load_retrieval_cases(YELP_EVAL_PATH)

    start = time.perf_counter()
    candidates_by_case_id = {
        case["id"]: search_yelp_reviews(case["question"], 10)
        for case in cases
    }
    retrieval_duration_ms = (time.perf_counter() - start) * 1000

    baseline_ranked = {
        case_id: reviews[:DEFAULT_TOP_K]
        for case_id, reviews in candidates_by_case_id.items()
    }
    baseline = _score_ranked_results(cases, baseline_ranked)

    grid = []
    for lexical_weight in LEXICAL_WEIGHT_GRID:
        ranked = {
            case["id"]: rerank_reviews_by_lexical_overlap(
                case["question"],
                candidates_by_case_id[case["id"]],
                vector_weight=1.0,
                lexical_weight=lexical_weight,
            )
            for case in cases
        }
        score = _score_ranked_results(cases, ranked)
        grid.append(
            {
                "lexicalWeight": lexical_weight,
                **{
                    key: value
                    for key, value in score.items()
                    if key != "details"
                },
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
    best_weight = best_grid_item["lexicalWeight"]
    best_ranked = {
        case["id"]: rerank_reviews_by_lexical_overlap(
            case["question"],
            candidates_by_case_id[case["id"]],
            vector_weight=1.0,
            lexical_weight=best_weight,
        )
        for case in cases
    }
    candidate = _score_ranked_results(cases, best_ranked)
    comparisons = _compare_details(cases, baseline, candidate)

    return {
        "topK": DEFAULT_TOP_K,
        "sourceFilter": "yelp",
        "candidateLimit": 10,
        "reranker": "vector_top10_plus_lexical_overlap",
        "retrievalDurationMs": retrieval_duration_ms,
        "weights": {
            "vector": 1.0,
            "lexical": best_weight,
        },
        "grid": grid,
        "baseline": {
            key: value
            for key, value in baseline.items()
            if key != "details"
        },
        "candidate": {
            key: value
            for key, value in candidate.items()
            if key != "details"
        },
        "improvements": [
            item for item in comparisons if item["improved"]
        ],
        "regressions": [
            item for item in comparisons if item["regressed"]
        ],
        "comparisons": comparisons,
    }


if __name__ == "__main__":
    report = build_report()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    baseline = report["baseline"]
    candidate = report["candidate"]
    print("Yelp lexical rerank eval")
    print(
        "baseline: "
        f"Hit@{report['topK']}={baseline['hits']}/{baseline['total']} "
        f"({baseline['hitRate']:.1%}), "
        f"Hit@1={baseline['hitsAt1']}/{baseline['total']} "
        f"({baseline['hitAt1Rate']:.1%}), "
        f"MRR={baseline['mrr']:.4f}"
    )
    print(
        "candidate: "
        f"Hit@{report['topK']}={candidate['hits']}/{candidate['total']} "
        f"({candidate['hitRate']:.1%}), "
        f"Hit@1={candidate['hitsAt1']}/{candidate['total']} "
        f"({candidate['hitAt1Rate']:.1%}), "
        f"MRR={candidate['mrr']:.4f}"
    )
    print(f"bestLexicalWeight={report['weights']['lexical']}")
    print(f"improvements={len(report['improvements'])}")
    print(f"regressions={len(report['regressions'])}")
    print(f"Wrote {REPORT_PATH}")
