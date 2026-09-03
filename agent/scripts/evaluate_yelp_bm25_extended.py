import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import DEFAULT_TOP_K, YELP_EVAL_PATH, get_embedding_model, load_retrieval_cases  # noqa: E402
from app.rag_bm25 import (  # noqa: E402
    get_yelp_review_bm25_index,
    search_yelp_reviews_bm25,
    search_yelp_reviews_vector_then_bm25_rerank,
)
from app.rag_bm25_benchmark import (  # noqa: E402
    benchmark_retrievers,
    build_quality_grid,
    candidate_recall_report,
    prepare_candidate_features,
    rerank_prepared_candidates,
    score_ranked_results,
    select_quality_leaders,
)
from app.rag_quality import search_yelp_reviews  # noqa: E402


QUALITY_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "yelp_bm25_extended_report.json"
)
LATENCY_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "yelp_bm25_latency_report.json"
)
CANDIDATE_LIMITS = [10, 20, 50]
BM25_WEIGHTS = [0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]


def _without_details(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in report.items()
        if key != "details"
    }


def build_quality_report() -> dict[str, Any]:
    cases = load_retrieval_cases(YELP_EVAL_PATH)
    max_candidate_limit = max(CANDIDATE_LIMITS)

    vector_candidates = {
        case["id"]: search_yelp_reviews(
            case["question"],
            max_candidate_limit,
        )
        for case in cases
    }
    vector = score_ranked_results(
        cases,
        vector_candidates,
        top_k=DEFAULT_TOP_K,
    )

    index = get_yelp_review_bm25_index()
    bm25_ranked = {
        case["id"]: index.search(
            case["question"],
            limit=DEFAULT_TOP_K,
        )
        for case in cases
    }
    bm25 = score_ranked_results(
        cases,
        bm25_ranked,
        top_k=DEFAULT_TOP_K,
    )

    prepared_by_case_id = {
        case["id"]: prepare_candidate_features(
            case["question"],
            vector_candidates[case["id"]],
            index,
        )
        for case in cases
    }
    grid = build_quality_grid(
        cases,
        prepared_by_case_id,
        candidate_limits=CANDIDATE_LIMITS,
        bm25_weights=BM25_WEIGHTS,
        top_k=DEFAULT_TOP_K,
    )
    leaders = select_quality_leaders(grid)
    selected = leaders["selected"]
    selected_ranked = {
        case["id"]: rerank_prepared_candidates(
            prepared_by_case_id[case["id"]],
            candidate_limit=selected["candidateLimit"],
            bm25_weight=selected["bm25Weight"],
        )
        for case in cases
    }
    selected_hybrid = score_ranked_results(
        cases,
        selected_ranked,
        top_k=DEFAULT_TOP_K,
    )

    return {
        "topK": DEFAULT_TOP_K,
        "sourceFilter": "yelp",
        "caseCount": len(cases),
        "candidateLimits": CANDIDATE_LIMITS,
        "bm25Weights": BM25_WEIGHTS,
        "methodology": {
            "qualityCandidates": (
                "fetch vector Top50 once per case, then reuse prefixes for all grids"
            ),
            "latencyIncluded": False,
            "caveat": (
                "exploratory grid uses the same 25 cases for selection and reporting"
            ),
        },
        "vector": _without_details(vector),
        "vectorDetails": vector["details"],
        "bm25": _without_details(bm25),
        "bm25Details": bm25["details"],
        "candidateRecall": [
            candidate_recall_report(
                cases,
                vector_candidates,
                candidate_limit=candidate_limit,
            )
            for candidate_limit in CANDIDATE_LIMITS
        ],
        "grid": grid,
        "leaders": leaders,
        "selectedHybrid": _without_details(selected_hybrid),
        "selectedHybridDetails": selected_hybrid["details"],
    }


def _measure_cache_reset_call(
    retrieve: Any,
    question: str,
    *,
    clear_embedding: bool,
    clear_bm25: bool,
) -> float:
    if clear_embedding:
        get_embedding_model.cache_clear()
    if clear_bm25:
        get_yelp_review_bm25_index.cache_clear()
    start = time.perf_counter()
    retrieve(question)
    return (time.perf_counter() - start) * 1000


def build_latency_report(
    quality_report: dict[str, Any],
    *,
    repeats: int,
) -> dict[str, Any]:
    cases = load_retrieval_cases(YELP_EVAL_PATH)
    selected = quality_report["leaders"]["selected"]
    candidate_limit = int(selected["candidateLimit"])
    bm25_weight = float(selected["bm25Weight"])

    retrievers = {
        "vector": lambda question: search_yelp_reviews(
            question,
            DEFAULT_TOP_K,
        ),
        "bm25": lambda question: search_yelp_reviews_bm25(
            question,
            DEFAULT_TOP_K,
        ),
        "hybrid": lambda question: search_yelp_reviews_vector_then_bm25_rerank(
            question,
            DEFAULT_TOP_K,
            candidate_limit=candidate_limit,
            bm25_weight=bm25_weight,
        ),
    }
    first_question = cases[0]["question"]
    cache_reset_single_run_ms = {
        "vector": _measure_cache_reset_call(
            retrievers["vector"],
            first_question,
            clear_embedding=True,
            clear_bm25=False,
        ),
        "bm25": _measure_cache_reset_call(
            retrievers["bm25"],
            first_question,
            clear_embedding=False,
            clear_bm25=True,
        ),
        "hybrid": _measure_cache_reset_call(
            retrievers["hybrid"],
            first_question,
            clear_embedding=True,
            clear_bm25=True,
        ),
    }

    for retrieve in retrievers.values():
        retrieve(first_question)
    warm = benchmark_retrievers(
        cases,
        retrievers,
        repeats=repeats,
    )
    return {
        "topK": DEFAULT_TOP_K,
        "sourceFilter": "yelp",
        "caseCount": len(cases),
        "repeats": repeats,
        "hybridConfiguration": {
            "candidateLimit": candidate_limit,
            "bm25Weight": bm25_weight,
        },
        "methodology": {
            "warm": (
                "each method executes its complete retrieval path for every timed request"
            ),
            "cacheResetSingleRun": (
                "clears process-local model/index caches; not a full machine cold start"
            ),
            "timingUnit": "milliseconds",
        },
        "cacheResetSingleRunMs": cache_reset_single_run_ms,
        "warm": warm,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate extended Yelp BM25 quality and end-to-end latency."
    )
    parser.add_argument(
        "--latency-repeats",
        type=int,
        default=3,
        help="Number of complete 25-case warm latency rounds.",
    )
    parser.add_argument(
        "--quality-only",
        action="store_true",
        help="Skip end-to-end latency measurements.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    quality_report = build_quality_report()
    QUALITY_REPORT_PATH.write_text(
        json.dumps(quality_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    selected = quality_report["leaders"]["selected"]
    print("Yelp extended BM25 quality")
    print(
        f"selected candidateLimit={selected['candidateLimit']} "
        f"bm25Weight={selected['bm25Weight']} "
        f"Hit@{quality_report['topK']}={selected['hits']}/{selected['total']} "
        f"MRR={selected['mrr']:.4f}"
    )
    print(f"Wrote {QUALITY_REPORT_PATH}")

    if not args.quality_only:
        latency_report = build_latency_report(
            quality_report,
            repeats=args.latency_repeats,
        )
        LATENCY_REPORT_PATH.write_text(
            json.dumps(latency_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for name, metrics in latency_report["warm"].items():
            print(
                f"{name}: avg={metrics['avgMs']:.2f}ms "
                f"p50={metrics['p50Ms']:.2f}ms "
                f"p95={metrics['p95Ms']:.2f}ms"
            )
        print(f"Wrote {LATENCY_REPORT_PATH}")
