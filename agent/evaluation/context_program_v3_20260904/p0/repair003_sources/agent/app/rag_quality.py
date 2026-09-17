import json
import math
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .rag import DEFAULT_TOP_K, YELP_EVAL_PATH, load_retrieval_cases, search_reviews


DEFAULT_ANALYSIS_TOP_K = 10


def search_yelp_reviews(question: str, limit: int) -> list[dict[str, Any]]:
    return search_reviews(question, limit, source="yelp")


def _text_terms(text: str) -> list[str]:
    lowered = text.lower()
    terms = [term for term in re.findall(r"[a-z0-9]+", lowered) if len(term) >= 2]
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.extend(
        "".join(chinese_chars[index : index + 2])
        for index in range(max(len(chinese_chars) - 1, 0))
    )
    return [term for term in terms if len(term) >= 2]


def lexical_overlap_score(query: str, document: str) -> float:
    query_terms = set(_text_terms(query))
    if not query_terms:
        return 0.0
    document_terms = Counter(_text_terms(document))
    if not document_terms:
        return 0.0
    matched = sum(
        math.log1p(document_terms[term])
        for term in query_terms
        if term in document_terms
    )
    return matched / len(query_terms)


def rerank_reviews_by_lexical_overlap(
    question: str,
    reviews: list[dict[str, Any]],
    *,
    vector_weight: float = 1.0,
    lexical_weight: float = 5.0,
) -> list[dict[str, Any]]:
    if not reviews:
        return []

    reranked: list[dict[str, Any]] = []
    for original_rank, review in enumerate(reviews, start=1):
        vector_score = float(review.get("score") or 0.0)
        lexical_score = lexical_overlap_score(question, str(review.get("text", "")))
        combined_score = vector_weight * vector_score + lexical_weight * lexical_score
        reranked.append(
            {
                **review,
                "originalRank": original_rank,
                "vectorScore": vector_score,
                "lexicalScore": lexical_score,
                "rerankScore": combined_score,
            }
        )

    reranked.sort(
        key=lambda review: (
            -float(review["rerankScore"]),
            int(review["originalRank"]),
        )
    )
    return reranked


def search_yelp_reviews_with_lexical_rerank(
    question: str,
    limit: int,
    *,
    candidate_limit: int = DEFAULT_ANALYSIS_TOP_K,
    vector_weight: float = 1.0,
    lexical_weight: float = 5.0,
) -> list[dict[str, Any]]:
    candidates = search_yelp_reviews(question, candidate_limit)
    return rerank_reviews_by_lexical_overlap(
        question,
        candidates,
        vector_weight=vector_weight,
        lexical_weight=lexical_weight,
    )[:limit]


def _shorten(text: str | None, limit: int = 260) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _retrieved_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rank": index,
            "reviewId": item["reviewId"],
            "shopName": item.get("shopName"),
            "score": item.get("score"),
            "text": _shorten(item.get("text")),
        }
        for index, item in enumerate(items, start=1)
    ]


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


def _failure_reasons(
    *,
    first_relevant_rank: int | None,
    top_k: int,
    retrieved_ids: list[str],
    challenge_types: list[str],
) -> list[str]:
    reasons: list[str] = []
    if first_relevant_rank is None:
        reasons.append("semantic_miss_not_in_analysis_top_k")
    elif first_relevant_rank > top_k:
        reasons.append("top_k_too_small")

    if any(review_id.startswith("review-") for review_id in retrieved_ids[:top_k]):
        reasons.append("seed_review_interference")

    if "service_constraint" in challenge_types:
        reasons.append("service_experience_queries_are_weak")
    if "time_constraint" in challenge_types:
        reasons.append("time_constraint_queries_are_weak")
    if "multi_constraint" in challenge_types:
        reasons.append("multi_constraint_queries_are_weak")
    if "implicit_intent" in challenge_types:
        reasons.append("implicit_intent_queries_are_weak")

    return reasons


def analyze_yelp_retrieval_failures(
    *,
    cases_path: Path = YELP_EVAL_PATH,
    retrieve: Callable[[str, int], list[dict[str, Any]]] = search_yelp_reviews,
    top_k: int = DEFAULT_TOP_K,
    analysis_top_k: int = DEFAULT_ANALYSIS_TOP_K,
) -> dict[str, Any]:
    """Analyze failed Yelp review retrieval cases with a deeper Top-K probe."""

    cases = load_retrieval_cases(cases_path)
    return analyze_yelp_retrieval_failure_cases(
        cases,
        retrieve=retrieve,
        top_k=top_k,
        analysis_top_k=analysis_top_k,
    )


def analyze_yelp_retrieval_failure_cases(
    cases: list[dict[str, Any]],
    *,
    retrieve: Callable[[str, int], list[dict[str, Any]]] = search_reviews,
    top_k: int = DEFAULT_TOP_K,
    analysis_top_k: int = DEFAULT_ANALYSIS_TOP_K,
) -> dict[str, Any]:
    """Analyze supplied Yelp retrieval cases without reading from disk."""

    details: list[dict[str, Any]] = []
    for case in cases:
        retrieved = retrieve(case["question"], analysis_top_k)
        retrieved_ids = [item["reviewId"] for item in retrieved]
        first_relevant_rank = _first_relevant_rank(
            retrieved_ids,
            case["relevantReviewIds"],
        )
        hit_at_top_k = first_relevant_rank is not None and first_relevant_rank <= top_k
        top_k_items = retrieved[:top_k]
        failure_reasons = (
            []
            if hit_at_top_k
            else _failure_reasons(
                first_relevant_rank=first_relevant_rank,
                top_k=top_k,
                retrieved_ids=retrieved_ids,
                challenge_types=case["challengeTypes"],
            )
        )
        details.append(
            {
                "caseId": case["id"],
                "question": case["question"],
                "difficulty": case["difficulty"],
                "challengeTypes": case["challengeTypes"],
                "expectedReviewIds": case["relevantReviewIds"],
                "expectedEvidenceText": _shorten(case.get("evidenceText"), 520),
                "hitAtTopK": hit_at_top_k,
                "firstRelevantRank": first_relevant_rank,
                "topKReviewIds": [item["reviewId"] for item in top_k_items],
                "topKResults": _retrieved_summary(top_k_items),
                "analysisTopKReviewIds": retrieved_ids,
                "analysisTopKResults": _retrieved_summary(retrieved),
                "failureReasons": failure_reasons,
            }
        )

    failures = [detail for detail in details if not detail["hitAtTopK"]]
    recovered_beyond_top_k = [
        detail
        for detail in failures
        if detail["firstRelevantRank"] is not None
    ]
    reason_counter = Counter(
        reason
        for detail in failures
        for reason in detail["failureReasons"]
    )
    challenge_counter = Counter(
        challenge_type
        for detail in failures
        for challenge_type in detail["challengeTypes"]
    )

    return {
        "topK": top_k,
        "analysisTopK": analysis_top_k,
        "sourceFilter": "yelp",
        "caseCount": len(details),
        "hitAtTopK": len(details) - len(failures),
        "hitAtTopKRate": (len(details) - len(failures)) / len(details) if details else 0.0,
        "failureCount": len(failures),
        "failureRate": len(failures) / len(details) if details else 0.0,
        "recoveredBeyondTopK": len(recovered_beyond_top_k),
        "recoveredBeyondTopKCaseIds": [
            detail["caseId"]
            for detail in recovered_beyond_top_k
        ],
        "failureReasons": dict(reason_counter.most_common()),
        "failureChallengeTypes": dict(challenge_counter.most_common()),
        "failures": failures,
        "details": details,
    }


def write_yelp_retrieval_failure_analysis(
    output_path: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    report = analyze_yelp_retrieval_failures(**kwargs)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report
