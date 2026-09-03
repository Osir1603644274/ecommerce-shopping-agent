import json
from pathlib import Path
from typing import Any

from app.knowledge import route_knowledge_sources


ROUTER_EVAL_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "router_cases.json"
)


def load_router_cases(path: Path = ROUTER_EVAL_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _source_metrics(details: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    source_names = sorted(
        {
            source
            for detail in details
            for source in [
                *detail["expectedSources"],
                *detail["actualSources"],
            ]
        }
    )
    metrics: dict[str, dict[str, Any]] = {}
    for source in source_names:
        true_positive = sum(
            source in detail["expectedSources"] and source in detail["actualSources"]
            for detail in details
        )
        false_positive = sum(
            source not in detail["expectedSources"] and source in detail["actualSources"]
            for detail in details
        )
        false_negative = sum(
            source in detail["expectedSources"] and source not in detail["actualSources"]
            for detail in details
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        metrics[source] = {
            "truePositive": true_positive,
            "falsePositive": false_positive,
            "falseNegative": false_negative,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return metrics


def evaluate_router_cases(
    cases: list[dict[str, Any]],
    *,
    router=route_knowledge_sources,
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    for case in cases:
        actual_sources = _ordered_unique(router(case["question"]))
        expected_sources = _ordered_unique(case["expectedSources"])
        exact_match = actual_sources == expected_sources
        expected_set = set(expected_sources)
        actual_set = set(actual_sources)
        details.append(
            {
                "caseId": case["id"],
                "category": case.get("category", "unknown"),
                "question": case["question"],
                "expectedSources": expected_sources,
                "actualSources": actual_sources,
                "exactMatch": exact_match,
                "missingSources": sorted(expected_set - actual_set),
                "extraSources": sorted(actual_set - expected_set),
            }
        )

    total = len(details)
    exact_matches = sum(detail["exactMatch"] for detail in details)
    by_category: dict[str, dict[str, Any]] = {}
    for detail in details:
        category = detail["category"]
        group = by_category.setdefault(
            category,
            {
                "total": 0,
                "exactMatches": 0,
                "accuracy": 0.0,
            },
        )
        group["total"] += 1
        group["exactMatches"] += int(detail["exactMatch"])
    for group in by_category.values():
        group["accuracy"] = (
            group["exactMatches"] / group["total"]
            if group["total"]
            else 0.0
        )

    return {
        "caseCount": total,
        "exactMatches": exact_matches,
        "accuracy": exact_matches / total if total else 0.0,
        "byCategory": dict(sorted(by_category.items())),
        "bySource": _source_metrics(details),
        "failures": [
            detail
            for detail in details
            if not detail["exactMatch"]
        ],
        "details": details,
    }


def evaluate_router(path: Path = ROUTER_EVAL_PATH) -> dict[str, Any]:
    return evaluate_router_cases(load_router_cases(path))
