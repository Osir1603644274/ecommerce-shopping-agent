import math
from collections.abc import Callable
from typing import Any

from .bm25 import BM25Index


def first_relevant_rank(
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


def score_ranked_results(
    cases: list[dict[str, Any]],
    ranked_by_case_id: dict[str, list[dict[str, Any]]],
    *,
    top_k: int,
    include_details: bool = True,
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    for case in cases:
        ranked = ranked_by_case_id[case["id"]]
        retrieved_ids = [
            str(item["reviewId"])
            for item in ranked[:top_k]
        ]
        first_rank = first_relevant_rank(
            retrieved_ids,
            [str(review_id) for review_id in case["relevantReviewIds"]],
        )
        details.append(
            {
                "caseId": case["id"],
                "hit": first_rank is not None,
                "hitAt1": first_rank == 1,
                "firstRelevantRank": first_rank,
                "reciprocalRank": 1 / first_rank if first_rank else 0.0,
                "expectedReviewIds": case["relevantReviewIds"],
                "retrievedReviewIds": retrieved_ids,
            }
        )

    total = len(details)
    hits = sum(detail["hit"] for detail in details)
    hits_at_1 = sum(detail["hitAt1"] for detail in details)
    report: dict[str, Any] = {
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
    if include_details:
        report["details"] = details
    return report


def candidate_recall_report(
    cases: list[dict[str, Any]],
    candidates_by_case_id: dict[str, list[dict[str, Any]]],
    *,
    candidate_limit: int,
) -> dict[str, Any]:
    hits = 0
    missed_case_ids: list[str] = []
    for case in cases:
        relevant_ids = {
            str(review_id)
            for review_id in case["relevantReviewIds"]
        }
        candidate_ids = {
            str(item["reviewId"])
            for item in candidates_by_case_id[case["id"]][:candidate_limit]
        }
        if relevant_ids & candidate_ids:
            hits += 1
        else:
            missed_case_ids.append(case["id"])

    total = len(cases)
    return {
        "candidateLimit": candidate_limit,
        "total": total,
        "hits": hits,
        "hitRate": hits / total if total else 0.0,
        "missedCaseIds": missed_case_ids,
    }


def prepare_candidate_features(
    question: str,
    candidates: list[dict[str, Any]],
    index: BM25Index,
) -> list[dict[str, Any]]:
    candidate_ids = {
        str(review["reviewId"])
        for review in candidates
    }
    bm25_by_review_id = index.score_by_doc_id(question, candidate_ids)
    return [
        {
            **review,
            "originalRank": original_rank,
            "vectorScore": float(review.get("score") or 0.0),
            "bm25Score": float(
                bm25_by_review_id.get(str(review["reviewId"]), 0.0)
            ),
        }
        for original_rank, review in enumerate(candidates, start=1)
    ]


def rerank_prepared_candidates(
    prepared_candidates: list[dict[str, Any]],
    *,
    candidate_limit: int,
    bm25_weight: float,
) -> list[dict[str, Any]]:
    reranked = [
        {
            **review,
            "rerankScore": (
                float(review["vectorScore"])
                + bm25_weight * float(review["bm25Score"])
            ),
        }
        for review in prepared_candidates[:candidate_limit]
    ]
    reranked.sort(
        key=lambda review: (
            -float(review["rerankScore"]),
            int(review["originalRank"]),
        )
    )
    return reranked


def build_quality_grid(
    cases: list[dict[str, Any]],
    prepared_by_case_id: dict[str, list[dict[str, Any]]],
    *,
    candidate_limits: list[int],
    bm25_weights: list[float],
    top_k: int,
) -> list[dict[str, Any]]:
    grid: list[dict[str, Any]] = []
    for candidate_limit in candidate_limits:
        for bm25_weight in bm25_weights:
            ranked = {
                case["id"]: rerank_prepared_candidates(
                    prepared_by_case_id[case["id"]],
                    candidate_limit=candidate_limit,
                    bm25_weight=bm25_weight,
                )
                for case in cases
            }
            metrics = score_ranked_results(
                cases,
                ranked,
                top_k=top_k,
                include_details=False,
            )
            grid.append(
                {
                    "candidateLimit": candidate_limit,
                    "bm25Weight": bm25_weight,
                    **metrics,
                }
            )
    return grid


def select_quality_leaders(
    grid: list[dict[str, Any]],
) -> dict[str, Any]:
    if not grid:
        raise ValueError("quality grid must not be empty")

    best_metrics = max(
        (item["hits"], item["mrr"], item["hitsAt1"])
        for item in grid
    )
    tied = [
        item
        for item in grid
        if (item["hits"], item["mrr"], item["hitsAt1"])
        == best_metrics
    ]
    selected = min(
        tied,
        key=lambda item: (
            item["candidateLimit"],
            item["bm25Weight"],
        ),
    )
    return {
        "selectionRule": (
            "maximize hits, then mrr, then hitsAt1; "
            "for exact ties choose smaller candidateLimit, then smaller bm25Weight"
        ),
        "selected": selected,
        "tieCount": len(tied),
        "ties": tied,
    }


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(
        math.ceil(percentile_value * len(ordered)) - 1,
        0,
    )
    return ordered[index]


def summarize_durations(durations_ms: list[float]) -> dict[str, Any]:
    return {
        "requestCount": len(durations_ms),
        "totalMs": sum(durations_ms),
        "avgMs": (
            sum(durations_ms) / len(durations_ms)
            if durations_ms
            else 0.0
        ),
        "p50Ms": percentile(durations_ms, 0.50),
        "p95Ms": percentile(durations_ms, 0.95),
        "minMs": min(durations_ms) if durations_ms else 0.0,
        "maxMs": max(durations_ms) if durations_ms else 0.0,
    }


def benchmark_retrievers(
    cases: list[dict[str, Any]],
    retrievers: dict[str, Callable[[str], list[dict[str, Any]]]],
    *,
    repeats: int,
) -> dict[str, Any]:
    if repeats <= 0:
        raise ValueError("repeats must be positive")

    import time

    durations_by_name = {name: [] for name in retrievers}
    method_names = list(retrievers)
    for repeat_index in range(repeats):
        rotated_names = (
            method_names[repeat_index % len(method_names) :]
            + method_names[: repeat_index % len(method_names)]
        )
        for case in cases:
            for name in rotated_names:
                start = time.perf_counter()
                retrievers[name](case["question"])
                durations_by_name[name].append(
                    (time.perf_counter() - start) * 1000
                )

    return {
        name: summarize_durations(durations)
        for name, durations in durations_by_name.items()
    }
