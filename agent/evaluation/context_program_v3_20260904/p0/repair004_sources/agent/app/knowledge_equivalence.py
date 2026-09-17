from collections.abc import Callable
from typing import Any

from .knowledge import REVIEW_SOURCE_NAME, search_knowledge
from .knowledge.reviews import reviews_to_citations
from .rag import DEFAULT_TOP_K, search_reviews


ReviewRetriever = Callable[[str, int], list[dict[str, Any]]]


def search_knowledge_reviews(question: str, limit: int = DEFAULT_TOP_K) -> list[dict[str, Any]]:
    """Expose search_knowledge(..., sources=["reviews"]) as a review retriever.

    This adapter lets existing RAG retrieval evaluation compare the old direct
    Qdrant retriever with the new unified Agent knowledge entrypoint.
    """

    result = search_knowledge(question, sources=[REVIEW_SOURCE_NAME], limit=limit)
    return citations_to_review_dicts(result.citations)


def citations_to_review_dicts(citations) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for citation in citations:
        review_id = str(citation.metadata.get("reviewId") or citation.source_id)
        review = {
            "reviewId": review_id,
            "score": citation.score,
            **citation.metadata,
        }
        if citation.title:
            review["title"] = citation.title
        if citation.quote:
            review["text"] = citation.quote
        reviews.append(review)
    return reviews


def reviews_to_search_knowledge_review_dicts(
    reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert legacy review dicts through the same Citation shape used by search_knowledge."""

    return citations_to_review_dicts(reviews_to_citations(reviews))


def compare_review_retrievers(
    cases: list[dict[str, Any]],
    *,
    baseline: ReviewRetriever = search_reviews,
    candidate: ReviewRetriever = search_knowledge_reviews,
    limit: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Compare two review retrievers by returned TopK review ids."""

    details: list[dict[str, Any]] = []
    exact_match_count = 0
    same_hit_count = 0
    candidate_regression_count = 0
    for case in cases:
        baseline_reviews = baseline(case["question"], limit)
        candidate_reviews = candidate(case["question"], limit)
        baseline_ids = [str(item["reviewId"]) for item in baseline_reviews]
        candidate_ids = [str(item["reviewId"]) for item in candidate_reviews]
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
                "question": case["question"],
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
        "topK": limit,
        "total": total,
        "exactMatches": exact_match_count,
        "exactMatchRate": exact_match_count / total if total else 0,
        "sameHitCount": same_hit_count,
        "sameHitRate": same_hit_count / total if total else 0,
        "candidateRegressions": candidate_regression_count,
        "details": details,
    }
