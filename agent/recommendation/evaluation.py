"""Offline evaluation helpers for FunRec recommendation baselines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from recommendation.baselines import popular_item_scores, popular_recommendations
from recommendation.hybrid import (
    build_item_type_lookup,
    hybrid_recommendations,
    three_way_hybrid_recommendations,
)
from recommendation.itemcf import (
    DEFAULT_SIMILARITY_PATH,
    build_item_similarity_table,
    itemcf_recommendations,
    load_similarity_table,
)
from recommendation.metrics import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    relevance_from_target_interactions,
)
from recommendation.movielens import (
    DEFAULT_OUTPUT_PATH as DEFAULT_CASES_PATH,
    DEFAULT_SPLITS_OUTPUT_PATH,
)


DEFAULT_REPORT_PATH = Path(__file__).parent / "reports" / "popular_baseline_report.json"
DEFAULT_ITEMCF_REPORT_PATH = Path(__file__).parent / "reports" / "itemcf_report.json"
DEFAULT_HYBRID_REPORT_PATH = Path(__file__).parent / "reports" / "hybrid_report.json"
DEFAULT_HYBRID_GRID_REPORT_PATH = Path(__file__).parent / "reports" / "hybrid_weight_grid_report.json"
DEFAULT_STRICT_HYBRID_REPORT_PATH = Path(__file__).parent / "reports" / "strict_hybrid_experiment_report.json"
DEFAULT_STRICT_THREE_WAY_HYBRID_REPORT_PATH = (
    Path(__file__).parent / "reports" / "strict_three_way_hybrid_experiment_report.json"
)


def load_cases(cases_path: Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load processed recommendation evaluation cases."""
    if not cases_path.exists():
        raise FileNotFoundError(
            f"Recommendation cases file not found: {cases_path}. "
            "Run `python -m scripts.prepare_movielens_cases` first."
        )
    return json.loads(cases_path.read_text(encoding="utf-8"))


def load_case_splits(splits_path: Path = DEFAULT_SPLITS_OUTPUT_PATH) -> dict[str, Any]:
    """Load processed train / validation / test recommendation case splits."""
    if not splits_path.exists():
        raise FileNotFoundError(
            f"Recommendation case splits file not found: {splits_path}. "
            "Run `python -m scripts.prepare_movielens_splits` first."
        )
    return json.loads(splits_path.read_text(encoding="utf-8"))


def _history_interactions(cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    interactions: list[dict[str, Any]] = []
    for case in cases:
        interactions.extend(case.get("history", []))
    return interactions


def _popular_ranking(popular_scores: Mapping[int, float], limit: int) -> list[int]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    return [
        int(item_id)
        for item_id, _ in sorted(
            popular_scores.items(),
            key=lambda pair: (-float(pair[1]), int(pair[0])),
        )[:limit]
    ]


def _filter_seen_items(candidate_ids: Iterable[int], seen_ids: set[int], limit: int) -> list[int]:
    recommendations: list[int] = []
    for item_id in candidate_ids:
        if item_id in seen_ids:
            continue
        recommendations.append(item_id)
        if len(recommendations) >= limit:
            break
    return recommendations


def _first_relevant_rank(recommended_ids: Iterable[int], relevant_ids: Iterable[int]) -> int | None:
    relevant = set(relevant_ids)
    for rank, item_id in enumerate(recommended_ids, start=1):
        if item_id in relevant:
            return rank
    return None


def _mean(values: Iterable[float]) -> float:
    values_list = list(values)
    return sum(values_list) / len(values_list) if values_list else 0.0


def _summarize_details(details: Iterable[dict[str, Any]]) -> dict[str, float]:
    detail_list = list(details)
    return {
        "hitRateAtK": _mean(1.0 if item["hit"] else 0.0 for item in detail_list),
        "precisionAtK": _mean(item["precisionAtK"] for item in detail_list),
        "recallAtK": _mean(item["recallAtK"] for item in detail_list),
        "mrr": _mean(item["reciprocalRank"] for item in detail_list),
        "ndcgAtK": _mean(item["ndcgAtK"] for item in detail_list),
    }


def _build_detail(
    case: dict[str, Any],
    recommended_ids: list[int],
    relevant_ids: list[int],
    top_k: int,
) -> dict[str, Any]:
    history = case.get("history", [])
    relevance_by_id = relevance_from_target_interactions(case.get("targetInteractions", []))
    first_rank = _first_relevant_rank(recommended_ids, relevant_ids)
    return {
        "caseId": case.get("caseId"),
        "userId": case.get("userId"),
        "recommendedItemIds": recommended_ids,
        "relevantItemIds": relevant_ids,
        "historyCount": len(history),
        "targetCount": len(case.get("targetInteractions", [])),
        "hit": first_rank is not None,
        "firstRelevantRank": first_rank,
        "precisionAtK": precision_at_k(recommended_ids, relevant_ids, top_k),
        "recallAtK": recall_at_k(recommended_ids, relevant_ids, top_k),
        "reciprocalRank": reciprocal_rank(recommended_ids, relevant_ids),
        "ndcgAtK": ndcg_at_k(recommended_ids, relevance_by_id, top_k),
    }


def evaluate_popular_from_scores(
    cases: Iterable[dict[str, Any]],
    popular_scores: Mapping[int, float],
    *,
    top_k: int = 10,
    candidate_limit: int = 1000,
    baseline_name: str = "global_popular_from_train",
) -> dict[str, Any]:
    """Evaluate a fixed global popularity ranking on cases."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if candidate_limit <= 0:
        raise ValueError("candidate_limit must be positive")

    candidate_ids = _popular_ranking(popular_scores, candidate_limit)
    details: list[dict[str, Any]] = []
    for case in cases:
        history = case.get("history", [])
        seen_ids = {int(item["itemId"]) for item in history}
        relevant_ids = [int(item_id) for item_id in case.get("relevantItemIds", [])]
        recommended_ids = _filter_seen_items(candidate_ids, seen_ids, top_k)
        details.append(_build_detail(case, recommended_ids, relevant_ids, top_k))

    return {
        "baseline": baseline_name,
        "topK": top_k,
        "candidateLimit": candidate_limit,
        "candidateCount": len(candidate_ids),
        "caseCount": len(details),
        "excludedSeenItems": True,
        "metrics": _summarize_details(details),
        "details": details,
    }


def evaluate_popular_baseline(
    cases: Iterable[dict[str, Any]],
    *,
    top_k: int = 10,
    candidate_limit: int = 1000,
) -> dict[str, Any]:
    """Evaluate a global popular-item baseline on processed recommendation cases."""
    case_list = list(cases)
    candidate_ids = popular_recommendations(
        _history_interactions(case_list),
        limit=candidate_limit,
    )
    popular_scores = {item_id: float(len(candidate_ids) - index) for index, item_id in enumerate(candidate_ids)}
    return evaluate_popular_from_scores(
        case_list,
        popular_scores,
        top_k=top_k,
        candidate_limit=candidate_limit,
        baseline_name="global_popular",
    )


def evaluate_itemcf(
    cases: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    *,
    top_k: int = 10,
) -> dict[str, Any]:
    """Evaluate ItemCF recommendations using a prebuilt similarity table."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    case_list = list(cases)
    raw_similarity_table = similarity_table.get("similarityTable", similarity_table)
    details: list[dict[str, Any]] = []

    for case in case_list:
        history = case.get("history", [])
        relevant_ids = [int(item_id) for item_id in case.get("relevantItemIds", [])]
        recommended_ids = itemcf_recommendations(
            history,
            similarity_table,
            limit=top_k,
        )
        details.append(_build_detail(case, recommended_ids, relevant_ids, top_k))

    return {
        "baseline": "itemcf",
        "similarityAlgorithm": similarity_table.get("algorithm", "unknown"),
        "topK": top_k,
        "caseCount": len(details),
        "excludedSeenItems": True,
        "similarityItemCount": len(raw_similarity_table),
        "metrics": _summarize_details(details),
        "details": details,
    }


def evaluate_hybrid(
    cases: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    *,
    top_k: int = 10,
    popular_weight: float = 1.0,
    itemcf_weight: float = 1.0,
    popular_scores: Mapping[int, float] | None = None,
) -> dict[str, Any]:
    """Evaluate a Popular + ItemCF hybrid recommendation baseline."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    case_list = list(cases)
    raw_similarity_table = similarity_table.get("similarityTable", similarity_table)
    global_popular_scores = (
        dict(popular_scores)
        if popular_scores is not None
        else popular_item_scores(_history_interactions(case_list))
    )
    details: list[dict[str, Any]] = []

    for case in case_list:
        history = case.get("history", [])
        relevant_ids = [int(item_id) for item_id in case.get("relevantItemIds", [])]
        recommended_ids = hybrid_recommendations(
            history,
            similarity_table,
            global_popular_scores,
            popular_weight=popular_weight,
            itemcf_weight=itemcf_weight,
            limit=top_k,
        )
        details.append(_build_detail(case, recommended_ids, relevant_ids, top_k))

    return {
        "baseline": "popular_itemcf_hybrid",
        "similarityAlgorithm": similarity_table.get("algorithm", "unknown"),
        "topK": top_k,
        "caseCount": len(details),
        "excludedSeenItems": True,
        "popularItemCount": len(global_popular_scores),
        "similarityItemCount": len(raw_similarity_table),
        "popularWeight": popular_weight,
        "itemcfWeight": itemcf_weight,
        "metrics": _summarize_details(details),
        "details": details,
    }


def evaluate_three_way_hybrid(
    cases: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    items: Iterable[dict[str, Any]],
    *,
    top_k: int = 10,
    popular_weight: float = 1.0,
    itemcf_weight: float = 1.0,
    type_weight: float = 1.0,
    popular_scores: Mapping[int, float] | None = None,
) -> dict[str, Any]:
    """Evaluate a Popular + ItemCF + TypeId preference hybrid baseline."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    case_list = list(cases)
    raw_similarity_table = similarity_table.get("similarityTable", similarity_table)
    item_list = list(items)
    item_type_lookup = build_item_type_lookup(item_list)
    global_popular_scores = (
        dict(popular_scores)
        if popular_scores is not None
        else popular_item_scores(_history_interactions(case_list))
    )
    details: list[dict[str, Any]] = []

    for case in case_list:
        history = case.get("history", [])
        relevant_ids = [int(item_id) for item_id in case.get("relevantItemIds", [])]
        recommended_ids = three_way_hybrid_recommendations(
            history,
            similarity_table,
            global_popular_scores,
            item_type_lookup,
            popular_weight=popular_weight,
            itemcf_weight=itemcf_weight,
            type_weight=type_weight,
            limit=top_k,
        )
        details.append(_build_detail(case, recommended_ids, relevant_ids, top_k))

    return {
        "baseline": "popular_itemcf_type_hybrid",
        "similarityAlgorithm": similarity_table.get("algorithm", "unknown"),
        "topK": top_k,
        "caseCount": len(details),
        "excludedSeenItems": True,
        "popularItemCount": len(global_popular_scores),
        "similarityItemCount": len(raw_similarity_table),
        "itemCatalogCount": len(item_list),
        "itemTypeCount": len(set(item_type_lookup.values())),
        "popularWeight": popular_weight,
        "itemcfWeight": itemcf_weight,
        "typeWeight": type_weight,
        "metrics": _summarize_details(details),
        "details": details,
    }


def evaluate_hybrid_weight_grid(
    cases: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    *,
    popular_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0),
    itemcf_weights: Iterable[float] = (1.0,),
    top_k: int = 10,
    primary_metric: str = "ndcgAtK",
    popular_scores: Mapping[int, float] | None = None,
) -> dict[str, Any]:
    """Run a grid search over Hybrid blending weights."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    allowed_metrics = {"hitRateAtK", "precisionAtK", "recallAtK", "mrr", "ndcgAtK"}
    if primary_metric not in allowed_metrics:
        raise ValueError("unsupported primary_metric")

    case_list = list(cases)
    popular_weight_list = list(popular_weights)
    itemcf_weight_list = list(itemcf_weights)
    results: list[dict[str, Any]] = []

    for popular_weight in popular_weight_list:
        for itemcf_weight in itemcf_weight_list:
            if popular_weight < 0 or itemcf_weight < 0:
                raise ValueError("weights must be non-negative")
            if popular_weight == 0 and itemcf_weight == 0:
                continue

            report = evaluate_hybrid(
                case_list,
                similarity_table,
                top_k=top_k,
                popular_weight=popular_weight,
                itemcf_weight=itemcf_weight,
                popular_scores=popular_scores,
            )
            results.append(
                {
                    "popularWeight": popular_weight,
                    "itemcfWeight": itemcf_weight,
                    "metrics": report["metrics"],
                }
            )

    ranked_results = sorted(
        results,
        key=lambda item: (
            -item["metrics"][primary_metric],
            -item["metrics"]["hitRateAtK"],
            -item["metrics"]["mrr"],
            item["popularWeight"],
            item["itemcfWeight"],
        ),
    )

    return {
        "experiment": "hybrid_weight_grid",
        "topK": top_k,
        "caseCount": len(case_list),
        "primaryMetric": primary_metric,
        "popularWeights": popular_weight_list,
        "itemcfWeights": itemcf_weight_list,
        "bestResult": ranked_results[0] if ranked_results else None,
        "results": ranked_results,
    }


def evaluate_three_way_hybrid_weight_grid(
    cases: Iterable[dict[str, Any]],
    similarity_table: dict[str, Any],
    items: Iterable[dict[str, Any]],
    *,
    popular_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    itemcf_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    type_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    top_k: int = 10,
    primary_metric: str = "ndcgAtK",
    popular_scores: Mapping[int, float] | None = None,
) -> dict[str, Any]:
    """Run a grid search over Popular + ItemCF + TypeId blending weights."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    allowed_metrics = {"hitRateAtK", "precisionAtK", "recallAtK", "mrr", "ndcgAtK"}
    if primary_metric not in allowed_metrics:
        raise ValueError("unsupported primary_metric")

    case_list = list(cases)
    item_list = list(items)
    popular_weight_list = list(popular_weights)
    itemcf_weight_list = list(itemcf_weights)
    type_weight_list = list(type_weights)
    results: list[dict[str, Any]] = []

    for popular_weight in popular_weight_list:
        for itemcf_weight in itemcf_weight_list:
            for type_weight in type_weight_list:
                if popular_weight < 0 or itemcf_weight < 0 or type_weight < 0:
                    raise ValueError("weights must be non-negative")
                if popular_weight == 0 and itemcf_weight == 0 and type_weight == 0:
                    continue

                report = evaluate_three_way_hybrid(
                    case_list,
                    similarity_table,
                    item_list,
                    top_k=top_k,
                    popular_weight=popular_weight,
                    itemcf_weight=itemcf_weight,
                    type_weight=type_weight,
                    popular_scores=popular_scores,
                )
                results.append(
                    {
                        "popularWeight": popular_weight,
                        "itemcfWeight": itemcf_weight,
                        "typeWeight": type_weight,
                        "metrics": report["metrics"],
                    }
                )

    ranked_results = sorted(
        results,
        key=lambda item: (
            -item["metrics"][primary_metric],
            -item["metrics"]["hitRateAtK"],
            -item["metrics"]["mrr"],
            item["popularWeight"],
            item["itemcfWeight"],
            item["typeWeight"],
        ),
    )

    return {
        "experiment": "three_way_hybrid_weight_grid",
        "topK": top_k,
        "caseCount": len(case_list),
        "primaryMetric": primary_metric,
        "popularWeights": popular_weight_list,
        "itemcfWeights": itemcf_weight_list,
        "typeWeights": type_weight_list,
        "bestResult": ranked_results[0] if ranked_results else None,
        "results": ranked_results,
    }


def evaluate_strict_hybrid_experiment(
    case_splits: dict[str, Any],
    *,
    top_k: int = 10,
    popular_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0),
    itemcf_weights: Iterable[float] = (1.0,),
    primary_metric: str = "ndcgAtK",
) -> dict[str, Any]:
    """Train signals on train, tune weights on validation, evaluate once on test."""
    train_cases = list(case_splits.get("train", []))
    validation_cases = list(case_splits.get("validation", []))
    test_cases = list(case_splits.get("test", []))
    if not train_cases or not validation_cases or not test_cases:
        raise ValueError("case_splits must contain non-empty train, validation, and test")

    train_popular_scores = popular_item_scores(_history_interactions(train_cases))
    train_similarity_table = build_item_similarity_table(train_cases)

    validation_grid = evaluate_hybrid_weight_grid(
        validation_cases,
        train_similarity_table,
        top_k=top_k,
        popular_weights=popular_weights,
        itemcf_weights=itemcf_weights,
        primary_metric=primary_metric,
        popular_scores=train_popular_scores,
    )
    best = validation_grid["bestResult"]
    if best is None:
        raise ValueError("weight grid produced no results")

    best_popular_weight = best["popularWeight"]
    best_itemcf_weight = best["itemcfWeight"]

    return {
        "experiment": "strict_train_validation_test_hybrid",
        "topK": top_k,
        "splitMethod": case_splits.get("splitMethod"),
        "randomSeed": case_splits.get("randomSeed"),
        "splitCounts": case_splits.get(
            "counts",
            {
                "train": len(train_cases),
                "validation": len(validation_cases),
                "test": len(test_cases),
                "total": len(train_cases) + len(validation_cases) + len(test_cases),
            },
        ),
        "signalSource": {
            "popularScores": "train history interactions only",
            "itemSimilarityTable": "train cases only",
        },
        "weightSelection": {
            "split": "validation",
            "primaryMetric": primary_metric,
            "bestPopularWeight": best_popular_weight,
            "bestItemcfWeight": best_itemcf_weight,
            "bestValidationMetrics": best["metrics"],
        },
        "validation": {
            "popular": evaluate_popular_from_scores(
                validation_cases,
                train_popular_scores,
                top_k=top_k,
                baseline_name="global_popular_train_signal",
            ),
            "itemcf": evaluate_itemcf(validation_cases, train_similarity_table, top_k=top_k),
            "hybridGrid": validation_grid,
        },
        "test": {
            "popular": evaluate_popular_from_scores(
                test_cases,
                train_popular_scores,
                top_k=top_k,
                baseline_name="global_popular_train_signal",
            ),
            "itemcf": evaluate_itemcf(test_cases, train_similarity_table, top_k=top_k),
            "hybrid": evaluate_hybrid(
                test_cases,
                train_similarity_table,
                top_k=top_k,
                popular_weight=best_popular_weight,
                itemcf_weight=best_itemcf_weight,
                popular_scores=train_popular_scores,
            ),
        },
    }


def evaluate_strict_three_way_hybrid_experiment(
    case_splits: dict[str, Any],
    items: Iterable[dict[str, Any]],
    *,
    top_k: int = 10,
    popular_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    itemcf_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    type_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    primary_metric: str = "ndcgAtK",
) -> dict[str, Any]:
    """Train signals on train, tune three-way weights on validation, test once."""
    train_cases = list(case_splits.get("train", []))
    validation_cases = list(case_splits.get("validation", []))
    test_cases = list(case_splits.get("test", []))
    if not train_cases or not validation_cases or not test_cases:
        raise ValueError("case_splits must contain non-empty train, validation, and test")

    item_list = list(items)
    train_popular_scores = popular_item_scores(_history_interactions(train_cases))
    train_similarity_table = build_item_similarity_table(train_cases)

    validation_grid = evaluate_three_way_hybrid_weight_grid(
        validation_cases,
        train_similarity_table,
        item_list,
        top_k=top_k,
        popular_weights=popular_weights,
        itemcf_weights=itemcf_weights,
        type_weights=type_weights,
        primary_metric=primary_metric,
        popular_scores=train_popular_scores,
    )
    best = validation_grid["bestResult"]
    if best is None:
        raise ValueError("weight grid produced no results")

    best_popular_weight = best["popularWeight"]
    best_itemcf_weight = best["itemcfWeight"]
    best_type_weight = best["typeWeight"]

    return {
        "experiment": "strict_train_validation_test_three_way_hybrid",
        "topK": top_k,
        "splitMethod": case_splits.get("splitMethod"),
        "randomSeed": case_splits.get("randomSeed"),
        "splitCounts": case_splits.get(
            "counts",
            {
                "train": len(train_cases),
                "validation": len(validation_cases),
                "test": len(test_cases),
                "total": len(train_cases) + len(validation_cases) + len(test_cases),
            },
        ),
        "signalSource": {
            "popularScores": "train history interactions only",
            "itemSimilarityTable": "train cases only",
            "typePreferenceScores": "each target user's history ratings only",
            "itemTypeLookup": "item catalog metadata",
        },
        "weightSelection": {
            "split": "validation",
            "primaryMetric": primary_metric,
            "bestPopularWeight": best_popular_weight,
            "bestItemcfWeight": best_itemcf_weight,
            "bestTypeWeight": best_type_weight,
            "bestValidationMetrics": best["metrics"],
        },
        "validation": {
            "popular": evaluate_popular_from_scores(
                validation_cases,
                train_popular_scores,
                top_k=top_k,
                baseline_name="global_popular_train_signal",
            ),
            "itemcf": evaluate_itemcf(validation_cases, train_similarity_table, top_k=top_k),
            "threeWayHybridGrid": validation_grid,
        },
        "test": {
            "popular": evaluate_popular_from_scores(
                test_cases,
                train_popular_scores,
                top_k=top_k,
                baseline_name="global_popular_train_signal",
            ),
            "itemcf": evaluate_itemcf(test_cases, train_similarity_table, top_k=top_k),
            "threeWayHybrid": evaluate_three_way_hybrid(
                test_cases,
                train_similarity_table,
                item_list,
                top_k=top_k,
                popular_weight=best_popular_weight,
                itemcf_weight=best_itemcf_weight,
                type_weight=best_type_weight,
                popular_scores=train_popular_scores,
            ),
        },
    }


def save_report(report: dict[str, Any], output_path: Path = DEFAULT_REPORT_PATH) -> None:
    """Save an evaluation report as readable UTF-8 JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def evaluate_and_save_popular_baseline(
    *,
    cases_path: Path = DEFAULT_CASES_PATH,
    output_path: Path = DEFAULT_REPORT_PATH,
    top_k: int = 10,
    candidate_limit: int = 1000,
) -> dict[str, Any]:
    """Load cases, evaluate the popular baseline, save the report, and return it."""
    cases = load_cases(cases_path)
    report = evaluate_popular_baseline(
        cases,
        top_k=top_k,
        candidate_limit=candidate_limit,
    )
    save_report(report, output_path)
    return report


def evaluate_and_save_itemcf(
    *,
    cases_path: Path = DEFAULT_CASES_PATH,
    similarity_path: Path = DEFAULT_SIMILARITY_PATH,
    output_path: Path = DEFAULT_ITEMCF_REPORT_PATH,
    top_k: int = 10,
) -> dict[str, Any]:
    """Load cases and a similarity table, evaluate ItemCF, save the report."""
    cases = load_cases(cases_path)
    similarity_table = load_similarity_table(similarity_path)
    report = evaluate_itemcf(
        cases,
        similarity_table,
        top_k=top_k,
    )
    save_report(report, output_path)
    return report


def evaluate_and_save_hybrid(
    *,
    cases_path: Path = DEFAULT_CASES_PATH,
    similarity_path: Path = DEFAULT_SIMILARITY_PATH,
    output_path: Path = DEFAULT_HYBRID_REPORT_PATH,
    top_k: int = 10,
    popular_weight: float = 1.0,
    itemcf_weight: float = 1.0,
) -> dict[str, Any]:
    """Load cases and a similarity table, evaluate Hybrid, save the report."""
    cases = load_cases(cases_path)
    similarity_table = load_similarity_table(similarity_path)
    report = evaluate_hybrid(
        cases,
        similarity_table,
        top_k=top_k,
        popular_weight=popular_weight,
        itemcf_weight=itemcf_weight,
    )
    save_report(report, output_path)
    return report


def evaluate_and_save_hybrid_weight_grid(
    *,
    cases_path: Path = DEFAULT_CASES_PATH,
    similarity_path: Path = DEFAULT_SIMILARITY_PATH,
    output_path: Path = DEFAULT_HYBRID_GRID_REPORT_PATH,
    top_k: int = 10,
    popular_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0),
    itemcf_weights: Iterable[float] = (1.0,),
    primary_metric: str = "ndcgAtK",
) -> dict[str, Any]:
    """Run Hybrid weight grid search, save the report, and return it."""
    cases = load_cases(cases_path)
    similarity_table = load_similarity_table(similarity_path)
    report = evaluate_hybrid_weight_grid(
        cases,
        similarity_table,
        top_k=top_k,
        popular_weights=popular_weights,
        itemcf_weights=itemcf_weights,
        primary_metric=primary_metric,
    )
    save_report(report, output_path)
    return report


def evaluate_and_save_strict_hybrid_experiment(
    *,
    splits_path: Path = DEFAULT_SPLITS_OUTPUT_PATH,
    output_path: Path = DEFAULT_STRICT_HYBRID_REPORT_PATH,
    top_k: int = 10,
    popular_weights: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0),
    itemcf_weights: Iterable[float] = (1.0,),
    primary_metric: str = "ndcgAtK",
) -> dict[str, Any]:
    """Load case splits, run strict Hybrid experiment, save the report."""
    case_splits = load_case_splits(splits_path)
    report = evaluate_strict_hybrid_experiment(
        case_splits,
        top_k=top_k,
        popular_weights=popular_weights,
        itemcf_weights=itemcf_weights,
        primary_metric=primary_metric,
    )
    save_report(report, output_path)
    return report
