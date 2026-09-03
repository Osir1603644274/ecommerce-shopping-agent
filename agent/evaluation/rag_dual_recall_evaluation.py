import hashlib
import time
from collections.abc import Callable
from typing import Any

from app.rag import DEFAULT_TOP_K, YELP_EVAL_PATH, load_retrieval_cases, load_reviews
from app.rag_bm25 import search_yelp_reviews_bm25
from app.rag_bm25_benchmark import score_ranked_results
from app.rag_fusion import fuse_by_normalized_score, fuse_by_rrf
from app.rag_quality import search_yelp_reviews


Retriever = Callable[[str, int], list[dict[str, Any]]]


def build_yelp_corpus_snapshot(
    reviews: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected = reviews if reviews is not None else load_reviews()
    yelp_reviews = sorted(
        (review for review in selected if review.get("source") == "yelp"),
        key=lambda review: str(review["reviewId"]),
    )
    digest = hashlib.sha256()
    for review in yelp_reviews:
        digest.update(str(review["reviewId"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(review.get("text") or "").encode("utf-8"))
        digest.update(b"\n")
    return {
        "reviewCount": len(yelp_reviews),
        "translatedCount": sum(
            review.get("translationStatus") == "translated"
            for review in yelp_reviews
        ),
        "indexedLanguageCounts": {
            language: sum(
                review.get("language") == language for review in yelp_reviews
            )
            for language in sorted(
                {str(review.get("language")) for review in yelp_reviews}
            )
        },
        "retrievalTextSha256": digest.hexdigest(),
    }


def _without_details(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "details"}


def _candidate_union_report(
    cases: list[dict[str, Any]],
    vector_by_case: dict[str, list[dict[str, Any]]],
    bm25_by_case: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    hits = 0
    overlap_sizes: list[int] = []
    union_sizes: list[int] = []
    missed_case_ids: list[str] = []
    for case in cases:
        case_id = str(case["id"])
        vector_ids = {
            str(item["reviewId"]) for item in vector_by_case[case_id]
        }
        bm25_ids = {
            str(item["reviewId"]) for item in bm25_by_case[case_id]
        }
        relevant_ids = {
            str(review_id) for review_id in case["relevantReviewIds"]
        }
        candidate_ids = vector_ids | bm25_ids
        overlap_sizes.append(len(vector_ids & bm25_ids))
        union_sizes.append(len(candidate_ids))
        if candidate_ids & relevant_ids:
            hits += 1
        else:
            missed_case_ids.append(case_id)
    total = len(cases)
    return {
        "total": total,
        "hits": hits,
        "hitRate": hits / total if total else 0.0,
        "averageOverlapSize": (
            sum(overlap_sizes) / total if total else 0.0
        ),
        "averageUnionSize": sum(union_sizes) / total if total else 0.0,
        "missedCaseIds": missed_case_ids,
    }


def build_dual_recall_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    vector_retrieve: Retriever = search_yelp_reviews,
    bm25_retrieve: Retriever = search_yelp_reviews_bm25,
    candidate_limit: int = 10,
    top_k: int = DEFAULT_TOP_K,
    rrf_k: int = 60,
    weighted_vector: float = 0.20,
    weighted_bm25: float = 0.80,
    data_version: str | None = None,
    corpus_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_cases = (
        cases if cases is not None else load_retrieval_cases(YELP_EVAL_PATH)
    )
    vector_by_case: dict[str, list[dict[str, Any]]] = {}
    bm25_by_case: dict[str, list[dict[str, Any]]] = {}
    retrieval_timing: dict[str, list[float]] = {"vector": [], "bm25": []}
    for case in selected_cases:
        case_id = str(case["id"])
        question = str(case["question"])
        start = time.perf_counter()
        vector_by_case[case_id] = vector_retrieve(question, candidate_limit)
        retrieval_timing["vector"].append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        bm25_by_case[case_id] = bm25_retrieve(question, candidate_limit)
        retrieval_timing["bm25"].append((time.perf_counter() - start) * 1000)

    min_max_by_case = {
        str(case["id"]): fuse_by_normalized_score(
            vector_by_case[str(case["id"])],
            bm25_by_case[str(case["id"])],
            normalization="min_max",
        )
        for case in selected_cases
    }
    max_by_case = {
        str(case["id"]): fuse_by_normalized_score(
            vector_by_case[str(case["id"])],
            bm25_by_case[str(case["id"])],
            normalization="max",
        )
        for case in selected_cases
    }
    weighted_min_max_by_case = {
        str(case["id"]): fuse_by_normalized_score(
            vector_by_case[str(case["id"])],
            bm25_by_case[str(case["id"])],
            normalization="min_max",
            vector_weight=weighted_vector,
            bm25_weight=weighted_bm25,
        )
        for case in selected_cases
    }
    rrf_by_case = {
        str(case["id"]): fuse_by_rrf(
            vector_by_case[str(case["id"])],
            bm25_by_case[str(case["id"])],
            rrf_k=rrf_k,
        )
        for case in selected_cases
    }
    ranked_methods = {
        "vector": vector_by_case,
        "bm25": bm25_by_case,
        "minMaxFusion": min_max_by_case,
        "minMaxFusionV02B08": weighted_min_max_by_case,
        "maxFusion": max_by_case,
        "rrf": rrf_by_case,
    }
    scored = {
        name: score_ranked_results(
            selected_cases,
            ranked,
            top_k=top_k,
        )
        for name, ranked in ranked_methods.items()
    }
    return {
        "methodology": {
            "dataset": "existing 25-case Yelp exploration set",
            "testRead": True,
            "selectionUse": False,
            "candidateLimitPerRetriever": candidate_limit,
            "topK": top_k,
            "scoreFusionWeights": {"vector": 1.0, "bm25": 1.0},
            "frozenWeightedFusion": {
                "vector": weighted_vector,
                "bm25": weighted_bm25,
                "selectionUse": False,
            },
            "rrfK": rrf_k,
            "caveat": "mechanism comparison only; all cases were used by earlier experiments",
        },
        "caseCount": len(selected_cases),
        "dataVersion": data_version,
        "corpusSnapshot": corpus_snapshot,
        "candidateUnion": _candidate_union_report(
            selected_cases,
            vector_by_case,
            bm25_by_case,
        ),
        "retrievalTiming": {
            name: {
                "averageMs": sum(values) / len(values) if values else 0.0,
                "measurementsMs": values,
            }
            for name, values in retrieval_timing.items()
        },
        "methods": {
            name: {
                "metrics": _without_details(report),
                "details": report["details"],
            }
            for name, report in scored.items()
        },
    }
