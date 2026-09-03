import hashlib
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from app.knowledge.reviews import REVIEW_SOURCE_NAME
from app.knowledge.unified_search import search_knowledge_index
from app.rag import load_retrieval_cases, load_reviews, search_reviews
from app.rag_bm25 import (
    prepare_review_bm25_indexes,
    search_all_reviews_bm25,
    search_yelp_reviews_bm25,
)
from app.rag_bm25_benchmark import score_ranked_results
from app.rag_fusion import fuse_by_normalized_score
from .rag_fuzzy_discovery_evaluation import (
    load_fuzzy_shop_discovery_validation_cases,
    score_fuzzy_discovery_case,
    summarize_fuzzy_discovery_details,
)


Retriever = Callable[[str, int], list[dict[str, Any]]]
DATASET_VERSION = "mixed-review-validation-v1"
CANDIDATE_LIMIT = 30
SEED_TOP_K = 3
YELP_SHOP_TOP_K = 5
VECTOR_WEIGHT = 0.20
BM25_WEIGHT = 0.80


def search_unified_reviews_vector(
    question: str,
    limit: int,
) -> list[dict[str, Any]]:
    result = search_knowledge_index(
        question,
        sources=[REVIEW_SOURCE_NAME],
        limit=limit,
    )
    scores = {
        citation.chunk_id: citation.score
        for citation in result.citations
    }
    return [
        {
            "reviewId": chunk.metadata.get("reviewId") or chunk.source_id,
            "shopId": chunk.metadata.get("shopId"),
            "shopName": chunk.metadata.get("shopName"),
            "text": chunk.content,
            "source": chunk.metadata.get("source"),
            "language": chunk.language,
            "score": scores.get(chunk.chunk_id),
        }
        for chunk in result.chunks
    ]


def _legacy_vector(question: str, limit: int) -> list[dict[str, Any]]:
    return search_reviews(question, limit)


def _default_retrievers() -> dict[str, Retriever]:
    return {
        "legacyVectorAll": _legacy_vector,
        "unifiedVectorAll": search_unified_reviews_vector,
        "bm25YelpOnly": search_yelp_reviews_bm25,
        "bm25AllReviews": search_all_reviews_bm25,
    }


def _corpus_snapshot(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    source_counts = Counter(str(review.get("source") or "unknown") for review in reviews)
    shops_by_source: dict[str, set[int]] = defaultdict(set)
    digest = hashlib.sha256()
    for review in sorted(reviews, key=lambda item: str(item["reviewId"])):
        source = str(review.get("source") or "unknown")
        shops_by_source[source].add(int(review["shopId"]))
        digest.update(str(review["reviewId"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(review.get("text") or "").encode("utf-8"))
        digest.update(b"\n")
    return {
        "reviewCount": len(reviews),
        "reviewCountsBySource": dict(sorted(source_counts.items())),
        "shopCountsBySource": {
            source: len(shop_ids)
            for source, shop_ids in sorted(shops_by_source.items())
        },
        "userReviewCount": source_counts.get("user", 0),
        "snapshotSha256": digest.hexdigest(),
    }


def _retrieve_methods(
    question: str,
    retrievers: dict[str, Retriever],
    *,
    candidate_limit: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, float]]:
    ranked: dict[str, list[dict[str, Any]]] = {}
    timings: dict[str, float] = {}
    for name, retrieve in retrievers.items():
        started = time.perf_counter()
        ranked[name] = retrieve(question, candidate_limit)
        timings[name] = (time.perf_counter() - started) * 1000

    vector = ranked["unifiedVectorAll"]
    yelp_bm25 = ranked["bm25YelpOnly"]
    all_bm25 = ranked["bm25AllReviews"]
    ranked["hybridYelpBm25V02B08"] = fuse_by_normalized_score(
        vector,
        yelp_bm25,
        vector_weight=VECTOR_WEIGHT,
        bm25_weight=BM25_WEIGHT,
    )
    ranked["hybridAllBm25V02B08"] = fuse_by_normalized_score(
        vector,
        all_bm25,
        vector_weight=VECTOR_WEIGHT,
        bm25_weight=BM25_WEIGHT,
    )
    return ranked, timings


def _known_seed_candidate_recall(
    cases: list[dict[str, Any]],
    ranked_by_case: dict[str, list[dict[str, Any]]],
    *,
    candidate_limit: int,
) -> dict[str, Any]:
    hits = 0
    missed: list[str] = []
    for case in cases:
        expected = {str(value) for value in case["relevantReviewIds"]}
        retrieved = {
            str(item["reviewId"])
            for item in ranked_by_case[str(case["id"])][:candidate_limit]
        }
        if expected & retrieved:
            hits += 1
        else:
            missed.append(str(case["id"]))
    return {
        "candidateLimit": candidate_limit,
        "hits": hits,
        "total": len(cases),
        "hitRate": hits / len(cases) if cases else 0.0,
        "missedCaseIds": missed,
    }


def _source_exposure(
    ranked_by_case: dict[str, list[dict[str, Any]]],
    *,
    top_k: int,
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for ranked in ranked_by_case.values():
        counts.update(
            str(item.get("source") or "unknown")
            for item in ranked[:top_k]
        )
    return {
        "topK": top_k,
        "resultCountsBySource": dict(sorted(counts.items())),
        "totalResults": sum(counts.values()),
    }


def _known_yelp_review_ids(case: dict[str, Any]) -> set[str]:
    return {
        str(review_id)
        for judgment in case["relevanceJudgments"]
        for review_id in judgment.get("supportingReviewIds") or []
    }


def _compact_yelp_details(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for detail in details:
        compact.append(
            {
                **{
                    key: value
                    for key, value in detail.items()
                    if key != "topShops"
                },
                "topShops": [
                    {
                        key: value
                        for key, value in shop.items()
                        if key != "evidence"
                    }
                    for shop in detail["topShops"]
                ],
            }
        )
    return compact


def _candidate_pool(
    strata: list[
        tuple[
            str,
            str,
            list[dict[str, Any]],
            dict[str, dict[str, list[dict[str, Any]]]],
        ]
    ],
    *,
    pool_methods: tuple[str, ...],
    pool_depth: int,
    judgment_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    judgment_records = (
        judgment_artifact.get("judgments", [])
        if judgment_artifact is not None
        else []
    )
    judgments: dict[tuple[str, str], dict[str, Any]] = {}
    for record in judgment_records:
        key = (str(record["caseId"]), str(record["reviewId"]))
        if key in judgments:
            raise ValueError(f"duplicate mixed-corpus judgment: {key}")
        relevance = record.get("relevance")
        if not isinstance(relevance, int) or isinstance(relevance, bool) or not 0 <= relevance <= 3:
            raise ValueError(f"mixed-corpus relevance must be an integer from 0 to 3: {key}")
        judgments[key] = record

    pooled_cases: list[dict[str, Any]] = []
    candidate_count = 0
    matched_judgment_keys: set[tuple[str, str]] = set()
    for stratum, expected_source, cases, ranked_by_method in strata:
        for case in cases:
            case_id = str(case["id"])
            known_ids = (
                {str(value) for value in case["relevantReviewIds"]}
                if stratum == "seed_known_evidence"
                else _known_yelp_review_ids(case)
            )
            candidates: dict[str, dict[str, Any]] = {}
            for method in pool_methods:
                for rank, item in enumerate(
                    ranked_by_method[method][case_id][:pool_depth],
                    start=1,
                ):
                    source = str(item.get("source") or "unknown")
                    review_id = str(item["reviewId"])
                    if source == expected_source or review_id in known_ids:
                        continue
                    candidate = candidates.setdefault(
                        review_id,
                        {
                            "reviewId": review_id,
                            "source": source,
                            "shopId": item.get("shopId"),
                            "shopName": item.get("shopName"),
                            "text": str(item.get("text") or ""),
                            "observedIn": [],
                        },
                    )
                    candidate["observedIn"].append(
                        {"method": method, "rank": rank}
                    )
            if candidates:
                rows = list(candidates.values())
                for row in rows:
                    ranks = [entry["rank"] for entry in row["observedIn"]]
                    row["priority"] = "high" if min(ranks) <= 3 else "normal"
                    judgment_key = (case_id, str(row["reviewId"]))
                    judgment = judgments.get(judgment_key)
                    if judgment is None:
                        row["humanJudgment"] = None
                    else:
                        matched_judgment_keys.add(judgment_key)
                        row["humanJudgment"] = {
                            "relevance": judgment["relevance"],
                            "label": judgment["label"],
                            "rationale": judgment["rationale"],
                            "annotatorType": (
                                judgment_artifact.get("methodology", {}).get(
                                    "annotatorType",
                                    "unspecified",
                                )
                            ),
                            "annotatedAt": judgment_artifact.get("methodology", {}).get(
                                "annotatedAt"
                            ),
                        }
                rows.sort(
                    key=lambda item: (
                        item["priority"] != "high",
                        min(entry["rank"] for entry in item["observedIn"]),
                        str(item["reviewId"]),
                    )
                )
                candidate_count += len(rows)
                pooled_cases.append(
                    {
                        "caseId": case_id,
                        "stratum": stratum,
                        "question": str(case["question"]),
                        "expectedSourceFromExistingLabels": expected_source,
                        "existingKnownReviewIds": sorted(known_ids),
                        "crossSourceCandidates": rows,
                    }
                )
    priority_counts = Counter(
        candidate["priority"]
        for case in pooled_cases
        for candidate in case["crossSourceCandidates"]
    )
    unknown_judgments = sorted(set(judgments) - matched_judgment_keys)
    if unknown_judgments:
        raise ValueError(f"judgments do not match the generated candidate pool: {unknown_judgments}")
    judged_rows = [
        (case["stratum"], candidate)
        for case in pooled_cases
        for candidate in case["crossSourceCandidates"]
        if candidate["humanJudgment"] is not None
    ]
    judged_by_priority = Counter(candidate["priority"] for _, candidate in judged_rows)
    judged_by_relevance = Counter(
        str(candidate["humanJudgment"]["relevance"])
        for _, candidate in judged_rows
    )
    judged_by_stratum = Counter(stratum for stratum, _ in judged_rows)
    high_priority_count = priority_counts.get("high", 0)
    judged_high_priority_count = judged_by_priority.get("high", 0)
    top3_by_method: dict[str, Any] = {}
    for method in pool_methods:
        method_rows: list[tuple[str, dict[str, Any]]] = []
        for case in pooled_cases:
            for candidate in case["crossSourceCandidates"]:
                if candidate["humanJudgment"] is None:
                    continue
                if any(
                    observation["method"] == method and observation["rank"] <= 3
                    for observation in candidate["observedIn"]
                ):
                    method_rows.append((case["caseId"], candidate))
        relevance_counts = Counter(
            str(candidate["humanJudgment"]["relevance"])
            for _, candidate in method_rows
        )
        irrelevant_case_ids = sorted(
            {
                case_id
                for case_id, candidate in method_rows
                if candidate["humanJudgment"]["relevance"] == 0
            }
        )
        top3_by_method[method] = {
            "candidateCount": len(method_rows),
            "countsByRelevance": dict(sorted(relevance_counts.items())),
            "irrelevantCaseCount": len(irrelevant_case_ids),
            "irrelevantCaseIds": irrelevant_case_ids,
        }
    return {
        "methodology": {
            "datasetVersion": DATASET_VERSION,
            "split": "validation candidate pool only",
            "sealedDataRead": False,
            "requiresHumanJudgment": True,
            "poolMethods": list(pool_methods),
            "poolDepth": pool_depth,
            "instruction": (
                "Unjudged cross-source candidates are not negative. Assistant-reviewed "
                "labels are disclosed separately from independent human judgments."
            ),
        },
        "caseCount": len(pooled_cases),
        "candidateCount": candidate_count,
        "candidateCountsByPriority": dict(sorted(priority_counts.items())),
        "judgmentSummary": {
            "judgedCount": len(judged_rows),
            "unjudgedCount": candidate_count - len(judged_rows),
            "judgedCountsByPriority": dict(sorted(judged_by_priority.items())),
            "judgedCountsByRelevance": dict(sorted(judged_by_relevance.items())),
            "judgedCountsByStratum": dict(sorted(judged_by_stratum.items())),
            "top3CrossSourceByMethod": top3_by_method,
            "highPriorityComplete": (
                high_priority_count > 0
                and judged_high_priority_count == high_priority_count
            ),
        },
        "cases": pooled_cases,
    }


def build_mixed_review_corpus_artifacts(
    *,
    seed_cases: list[dict[str, Any]] | None = None,
    yelp_cases: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    retrievers: dict[str, Retriever] | None = None,
    prepare: Callable[[], Any] | None = None,
    candidate_limit: int = CANDIDATE_LIMIT,
    judgment_artifact: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if candidate_limit <= 0:
        raise ValueError("candidate_limit must be positive")
    selected_seed = seed_cases if seed_cases is not None else load_retrieval_cases()
    selected_yelp = (
        yelp_cases
        if yelp_cases is not None
        else load_fuzzy_shop_discovery_validation_cases()
    )
    selected_reviews = reviews if reviews is not None else load_reviews()
    selected_retrievers = retrievers if retrievers is not None else _default_retrievers()
    selected_prepare = (
        prepare_review_bm25_indexes
        if retrievers is None and prepare is None
        else prepare
    )
    required = {
        "legacyVectorAll",
        "unifiedVectorAll",
        "bm25YelpOnly",
        "bm25AllReviews",
    }
    if set(selected_retrievers) != required:
        raise ValueError("retrievers must provide the four fixed base methods")

    initialization_start = time.perf_counter()
    if selected_prepare is not None:
        selected_prepare()
    initialization_ms = (time.perf_counter() - initialization_start) * 1000

    seed_ranked: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    yelp_ranked: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    durations: dict[str, list[float]] = defaultdict(list)
    for cases, ranked_target in (
        (selected_seed, seed_ranked),
        (selected_yelp, yelp_ranked),
    ):
        for case in cases:
            case_id = str(case["id"])
            ranked, timings = _retrieve_methods(
                str(case["question"]),
                selected_retrievers,
                candidate_limit=candidate_limit,
            )
            for method, rows in ranked.items():
                ranked_target[method][case_id] = rows
            for method, duration_ms in timings.items():
                durations[method].append(duration_ms)

    seed_methods: dict[str, Any] = {}
    for method, ranked_by_case in seed_ranked.items():
        seed_methods[method] = {
            "knownEvidenceTop3": score_ranked_results(
                selected_seed,
                ranked_by_case,
                top_k=SEED_TOP_K,
            ),
            "knownEvidenceCandidateRecall": _known_seed_candidate_recall(
                selected_seed,
                ranked_by_case,
                candidate_limit=candidate_limit,
            ),
            "sourceExposure": _source_exposure(
                ranked_by_case,
                top_k=SEED_TOP_K,
            ),
        }

    yelp_methods: dict[str, Any] = {}
    for method, ranked_by_case in yelp_ranked.items():
        details = [
            score_fuzzy_discovery_case(
                case,
                ranked_by_case[str(case["id"])],
                top_k=YELP_SHOP_TOP_K,
                evidence_per_shop=3,
            )
            for case in selected_yelp
        ]
        yelp_methods[method] = {
            "knownShopQrelMetrics": summarize_fuzzy_discovery_details(
                details,
                top_k=YELP_SHOP_TOP_K,
            ),
            "details": _compact_yelp_details(details),
            "sourceExposure": _source_exposure(
                ranked_by_case,
                top_k=YELP_SHOP_TOP_K,
            ),
        }

    pool = _candidate_pool(
        [
            ("seed_known_evidence", "seed", selected_seed, seed_ranked),
            ("yelp_shop_discovery", "yelp", selected_yelp, yelp_ranked),
        ],
        pool_methods=("bm25AllReviews", "hybridAllBm25V02B08"),
        pool_depth=10,
        judgment_artifact=judgment_artifact,
    )
    seed_yelp_only = seed_methods["hybridYelpBm25V02B08"]["knownEvidenceTop3"]
    seed_all = seed_methods["hybridAllBm25V02B08"]["knownEvidenceTop3"]
    yelp_yelp_only = yelp_methods["hybridYelpBm25V02B08"]["knownShopQrelMetrics"]
    yelp_all = yelp_methods["hybridAllBm25V02B08"]["knownShopQrelMetrics"]
    report = {
        "methodology": {
            "datasetVersion": DATASET_VERSION,
            "split": "validation diagnostics only",
            "sealedDataRead": False,
            "selectionUse": False,
            "candidateLimitPerRetriever": candidate_limit,
            "fixedHybridWeights": {
                "vector": VECTOR_WEIGHT,
                "bm25": BM25_WEIGHT,
                "retunedHere": False,
            },
            "strata": {
                "seedKnownEvidence": (
                    "27 historical seed review-ID cases; known evidence is non-exhaustive"
                ),
                "yelpShopDiscovery": (
                    "8 pooled Yelp shop-level cases; qrels are non-exhaustive"
                ),
            },
            "scoringRule": (
                "Report strata independently. Do not average seed review-ID metrics "
                "with Yelp shop-level NDCG. Unjudged cross-source candidates are not negative."
            ),
        },
        "corpusSnapshot": _corpus_snapshot(selected_reviews),
        "retrievalTiming": {
            "initializationMs": initialization_ms,
            "queries": {
                method: {
                    "requestCount": len(values),
                    "averageMs": sum(values) / len(values) if values else 0.0,
                    "maxMs": max(values) if values else 0.0,
                }
                for method, values in sorted(durations.items())
            },
        },
        "strata": {
            "seedKnownEvidence": {
                "caseCount": len(selected_seed),
                "methods": seed_methods,
            },
            "yelpShopDiscovery": {
                "caseCount": len(selected_yelp),
                "methods": yelp_methods,
            },
        },
        "comparison": {
            "seedHybridKnownEvidenceTop3": {
                "yelpOnlyBm25": {
                    key: seed_yelp_only[key]
                    for key in ("hits", "total", "hitRate", "mrr")
                },
                "allReviewBm25": {
                    key: seed_all[key]
                    for key in ("hits", "total", "hitRate", "mrr")
                },
            },
            "yelpHybridKnownQrels": {
                "yelpOnlyBm25": yelp_yelp_only,
                "allReviewBm25": yelp_all,
            },
        },
        "candidatePoolSummary": {
            "caseCount": pool["caseCount"],
            "candidateCount": pool["candidateCount"],
            "candidateCountsByPriority": pool["candidateCountsByPriority"],
            "judgmentSummary": pool["judgmentSummary"],
            "requiresAdditionalJudgment": pool["judgmentSummary"]["unjudgedCount"] > 0,
        },
        "decision": {
            "defaultAgentChanged": False,
            "hybridCanaryBm25CorpusChanged": True,
            "hybridCanaryBm25Corpus": "all review sources",
            "eligibleForDefaultSwitch": False,
            "status": (
                "all_review_canary_high_priority_assistant_labeled"
                if pool["judgmentSummary"]["highPriorityComplete"]
                else "all_review_canary_requires_mixed_labels"
            ),
            "reasons": [
                "Yelp-only BM25 has no lexical coverage for seed known evidence",
                "all-review BM25 changes Yelp known-qrel ranking",
                (
                    "all "
                    f"{pool['judgmentSummary']['judgedCountsByPriority'].get('high', 0)} "
                    "high-priority cross-source candidates have disclosed assistant-reviewed labels"
                    if pool["judgmentSummary"]["highPriorityComplete"]
                    else "high-priority cross-source candidates remain unjudged"
                ),
                "normal-priority and same-source qrels remain non-exhaustive",
                "there are currently no persistent user-source reviews to validate",
            ],
        },
    }
    return report, pool
