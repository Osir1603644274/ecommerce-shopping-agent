from pathlib import Path
from typing import Any, Callable

from app.knowledge import SearchKnowledgeResult, search_knowledge
from .knowledge_router_evaluation import ROUTER_EVAL_PATH, load_router_cases


SOURCE_TYPE_TO_SOURCE_NAME = {
    "review": "reviews",
    "merchant_doc": "merchant_docs",
    "policy_doc": "policy_docs",
}

# These questions intentionally ask "this shop" style follow-ups without naming
# the shop. They are useful router cases, but citation retrieval cannot be judged
# fairly without conversation context or an entity filter.
CONTEXT_DEPENDENT_CASE_IDS = {
    "router-merchant-003",
}


def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _citation_source_names(result: SearchKnowledgeResult) -> list[str]:
    source_names: list[str] = []
    for citation in result.citations:
        source_name = SOURCE_TYPE_TO_SOURCE_NAME.get(
            citation.source_type,
            citation.source_type,
        )
        if source_name not in source_names:
            source_names.append(source_name)
    return source_names


def _required_citation_sources(case: dict[str, Any]) -> list[str]:
    if case["id"] in CONTEXT_DEPENDENT_CASE_IDS:
        return []
    return _ordered_unique(case["expectedSources"])


def evaluate_mixed_search_cases(
    cases: list[dict[str, Any]],
    *,
    retrieve: Callable[[str], SearchKnowledgeResult] = search_knowledge,
) -> dict[str, Any]:
    """Evaluate search_knowledge with router auto-selection and real citations.

    This is stricter than the router eval: it checks both selected sources and
    whether citations came back from the expected source types.
    """

    details: list[dict[str, Any]] = []
    for case in cases:
        result = retrieve(case["question"])
        expected_sources = _ordered_unique(case["expectedSources"])
        actual_sources = _ordered_unique(result.trace.selected_sources)
        citation_sources = _citation_source_names(result)
        required_citation_sources = _required_citation_sources(case)
        selected_exact = actual_sources == expected_sources
        citation_covered = all(
            source in citation_sources
            for source in required_citation_sources
        )
        details.append(
            {
                "caseId": case["id"],
                "category": case.get("category", "unknown"),
                "question": case["question"],
                "expectedSources": expected_sources,
                "actualSources": actual_sources,
                "selectedExact": selected_exact,
                "citationSources": citation_sources,
                "requiredCitationSources": required_citation_sources,
                "citationCovered": citation_covered,
                "missingCitationSources": [
                    source
                    for source in required_citation_sources
                    if source not in citation_sources
                ],
                "citationCount": len(result.citations),
                "returnedCount": result.trace.returned_count,
                "candidateCount": result.trace.candidate_count,
                "durationMs": result.trace.duration_ms,
            }
        )

    total = len(details)
    selected_exact_count = sum(detail["selectedExact"] for detail in details)
    citation_covered_count = sum(detail["citationCovered"] for detail in details)
    by_category: dict[str, dict[str, Any]] = {}
    for detail in details:
        category = detail["category"]
        group = by_category.setdefault(
            category,
            {
                "total": 0,
                "selectedExact": 0,
                "citationCovered": 0,
                "selectedAccuracy": 0.0,
                "citationCoverageRate": 0.0,
            },
        )
        group["total"] += 1
        group["selectedExact"] += int(detail["selectedExact"])
        group["citationCovered"] += int(detail["citationCovered"])
    for group in by_category.values():
        group["selectedAccuracy"] = (
            group["selectedExact"] / group["total"]
            if group["total"]
            else 0.0
        )
        group["citationCoverageRate"] = (
            group["citationCovered"] / group["total"]
            if group["total"]
            else 0.0
        )

    return {
        "caseCount": total,
        "selectedExact": selected_exact_count,
        "selectedAccuracy": selected_exact_count / total if total else 0.0,
        "citationCovered": citation_covered_count,
        "citationCoverageRate": citation_covered_count / total if total else 0.0,
        "contextDependentCases": sorted(CONTEXT_DEPENDENT_CASE_IDS),
        "byCategory": dict(sorted(by_category.items())),
        "failures": [
            detail
            for detail in details
            if not detail["selectedExact"] or not detail["citationCovered"]
        ],
        "details": details,
    }


def evaluate_mixed_search(path: Path = ROUTER_EVAL_PATH) -> dict[str, Any]:
    return evaluate_mixed_search_cases(load_router_cases(path))
