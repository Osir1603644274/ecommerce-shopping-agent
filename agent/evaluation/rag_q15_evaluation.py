import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from recommendation.metrics import ndcg_at_k

from app.rag_bm25 import search_yelp_reviews_bm25
from app.rag_content_reranker import (
    CONTENT_RERANKER_PROMPT_VERSION,
    rerank_reviews_with_content_judge,
)
from app.rag_context_selection import select_reranked_review_context
from app.rag_fusion import fuse_by_normalized_score
from .rag_fuzzy_discovery_evaluation import (
    score_fuzzy_discovery_case,
    summarize_fuzzy_discovery_details,
)
from app.rag_grounded_answer import (
    FAITHFULNESS_JUDGE_PROMPT_VERSION,
    GROUNDED_ANSWER_PROMPT_VERSION,
    GroundedAnswerGenerator,
    FaithfulnessJudge,
    audit_grounded_answer,
    generate_grounded_shop_answer,
    judge_grounded_answer_faithfulness,
)
from app.rag_quality import search_yelp_reviews
from app.settings import settings


AGENT_ROOT = Path(__file__).resolve().parents[1]
TEST_QUESTIONS_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "fuzzy_shop_discovery_test_questions.json"
)
TEST_CASES_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "fuzzy_shop_discovery_test_cases.json"
)
TEST_POOL_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "fuzzy_shop_discovery_test_label_pool.json"
)
Q15_QREL_VERSION = "fuzzy-shop-zh-test-v1"

Retriever = Callable[[str, int], list[dict[str, Any]]]
ContentReranker = Callable[
    [str, list[dict[str, Any]]],
    Awaitable[list[dict[str, Any]]],
]


def load_q15_test_cases(
    cases_path: Path = TEST_CASES_PATH,
    questions_path: Path = TEST_QUESTIONS_PATH,
    pool_path: Path = TEST_POOL_PATH,
) -> list[dict[str, Any]]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    sealed = json.loads(questions_path.read_text(encoding="utf-8"))
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    methodology = payload.get("methodology", {})
    if methodology.get("split") != "test":
        raise ValueError("Q15 cases must remain test-only")
    if methodology.get("qrelVersion") != Q15_QREL_VERSION:
        raise ValueError("unexpected Q15 qrel version")
    if methodology.get("retrievalResultsUsedForTuning") is not False:
        raise ValueError("Q15 test must not be marked as tuning data")

    sealed_by_id = {str(case["id"]): case for case in sealed.get("cases", [])}
    pool_by_id = {str(case["id"]): case for case in pool.get("cases", [])}
    cases = list(payload.get("cases") or [])
    seen_case_ids: set[str] = set()
    for case in cases:
        case_id = str(case.get("id") or "")
        if not case_id or case_id in seen_case_ids:
            raise ValueError("Q15 case IDs must be non-empty and unique")
        seen_case_ids.add(case_id)
        sealed_case = sealed_by_id.get(case_id)
        if sealed_case is None:
            raise ValueError(f"{case_id} was not sealed before pooling")
        for field in ("question", "intent"):
            if case.get(field) != sealed_case.get(field):
                raise ValueError(f"{case_id} changed sealed {field}")

        judgments = list(case.get("relevanceJudgments") or [])
        if len(judgments) < 2:
            raise ValueError(f"{case_id} needs multiple relevant shops")
        pooled = {
            (str(item["reviewId"]), int(item["shopId"]))
            for item in pool_by_id.get(case_id, {}).get("candidates", [])
        }
        seen_shop_ids: set[int] = set()
        for judgment in judgments:
            shop_id = int(judgment["shopId"])
            if shop_id in seen_shop_ids:
                raise ValueError(f"{case_id} contains duplicate shopId {shop_id}")
            seen_shop_ids.add(shop_id)
            if int(judgment["relevance"]) not in {1, 2, 3}:
                raise ValueError(f"{case_id} has invalid relevance")
            review_ids = list(judgment.get("supportingReviewIds") or [])
            if not review_ids:
                raise ValueError(f"{case_id} shopId {shop_id} has no evidence")
            for review_id in review_ids:
                if (str(review_id), shop_id) not in pooled:
                    raise ValueError(
                        f"{case_id} evidence {review_id} was not in the sealed pool"
                    )
    if set(sealed_by_id) != seen_case_ids:
        raise ValueError("Q15 qrels do not match all sealed questions")
    return cases


def _score_generated_answer(
    case: dict[str, Any],
    answer: dict[str, Any],
) -> dict[str, Any]:
    judgments = {
        int(item["shopId"]): item for item in case["relevanceJudgments"]
    }
    relevance_by_shop = {
        shop_id: float(item["relevance"])
        for shop_id, item in judgments.items()
    }
    recommended_shop_ids = [
        int(item["shopId"])
        for item in answer.get("recommendations", [])
        if item.get("shopId") is not None
    ]
    unique_recommended_shop_ids = list(dict.fromkeys(recommended_shop_ids))
    relevant_recommendations = sum(
        shop_id in judgments for shop_id in recommended_shop_ids
    )
    matched_relevant_shops = len(set(recommended_shop_ids) & set(judgments))

    reason_citation_count = 0
    exact_qrel_citation_count = 0
    for recommendation in answer.get("recommendations", []):
        shop_id = recommendation.get("shopId")
        citations = list(recommendation.get("citationReviewIds") or [])
        reason_citation_count += len(citations)
        judgment = judgments.get(shop_id)
        if judgment is None:
            continue
        expected_ids = {
            str(review_id) for review_id in judgment["supportingReviewIds"]
        }
        exact_qrel_citation_count += sum(
            str(review_id) in expected_ids for review_id in citations
        )

    return {
        "recommendedShopIds": recommended_shop_ids,
        "recommendationHit": bool(matched_relevant_shops),
        "recommendationPrecision": (
            relevant_recommendations / len(recommended_shop_ids)
            if recommended_shop_ids
            else 0.0
        ),
        "recommendationRecall": matched_relevant_shops / len(judgments),
        "recommendationNdcgAt3": ndcg_at_k(
            unique_recommended_shop_ids,
            relevance_by_shop,
            3,
        ),
        "reasonCitationCount": reason_citation_count,
        "exactQrelReasonCitationPrecision": (
            exact_qrel_citation_count / reason_citation_count
            if reason_citation_count
            else 0.0
        ),
        "qrelCaveat": "supportingReviewIds are pooled and non-exhaustive",
    }


def _average(details: list[dict[str, Any]], key: str) -> float:
    return (
        sum(float(detail[key]) for detail in details) / len(details)
        if details
        else 0.0
    )


async def build_q15_test_report(
    cases: list[dict[str, Any]] | None = None,
    *,
    vector_retrieve: Retriever = search_yelp_reviews,
    bm25_retrieve: Retriever = search_yelp_reviews_bm25,
    content_rerank: ContentReranker = rerank_reviews_with_content_judge,
    answer_generator: GroundedAnswerGenerator = generate_grounded_shop_answer,
    faithfulness_judge: FaithfulnessJudge = judge_grounded_answer_faithfulness,
    candidate_limit: int = 30,
    top_k: int = 5,
    evidence_per_shop: int = 3,
    vector_weight: float = 0.20,
    bm25_weight: float = 0.80,
) -> dict[str, Any]:
    selected_cases = cases if cases is not None else load_q15_test_cases()
    hybrid_details: list[dict[str, Any]] = []
    reranker_details: list[dict[str, Any]] = []
    answer_scores: list[dict[str, Any]] = []
    deterministic_audits: list[dict[str, Any]] = []
    faithfulness_results: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    timings: list[dict[str, Any]] = []

    for case in selected_cases:
        question = str(case["question"])
        retrieval_start = time.perf_counter()
        vector, bm25 = await asyncio.gather(
            asyncio.to_thread(vector_retrieve, question, candidate_limit),
            asyncio.to_thread(bm25_retrieve, question, candidate_limit),
        )
        retrieval_ms = (time.perf_counter() - retrieval_start) * 1000
        hybrid = fuse_by_normalized_score(
            vector,
            bm25,
            normalization="min_max",
            vector_weight=vector_weight,
            bm25_weight=bm25_weight,
        )

        rerank_start = time.perf_counter()
        reranked = await content_rerank(question, hybrid)
        rerank_ms = (time.perf_counter() - rerank_start) * 1000
        selection = select_reranked_review_context(reranked)

        generation_start = time.perf_counter()
        answer = await answer_generator(question, selection)
        generation_ms = (time.perf_counter() - generation_start) * 1000
        deterministic_audit = audit_grounded_answer(answer, selection)

        judge_start = time.perf_counter()
        faithfulness = await faithfulness_judge(question, answer, selection)
        judge_ms = (time.perf_counter() - judge_start) * 1000

        # Test qrels are read only after retrieval, ranking, selection, generation,
        # and citation-only faithfulness judging have all completed.
        hybrid_score = score_fuzzy_discovery_case(
            case,
            hybrid,
            top_k=top_k,
            evidence_per_shop=evidence_per_shop,
        )
        reranker_score = score_fuzzy_discovery_case(
            case,
            reranked,
            top_k=top_k,
            evidence_per_shop=evidence_per_shop,
        )
        answer_score = _score_generated_answer(case, answer)
        hybrid_details.append(hybrid_score)
        reranker_details.append(reranker_score)
        answer_scores.append(answer_score)
        deterministic_audits.append(deterministic_audit)
        faithfulness_results.append(faithfulness)
        timings.append(
            {
                "caseId": str(case["id"]),
                "retrievalMs": retrieval_ms,
                "contentRerankerMs": rerank_ms,
                "generationMs": generation_ms,
                "faithfulnessJudgeMs": judge_ms,
                "totalMs": retrieval_ms + rerank_ms + generation_ms + judge_ms,
            }
        )
        details.append(
            {
                "caseId": str(case["id"]),
                "question": question,
                "hybridTopShops": hybrid_score["topShops"],
                "rerankerTopShops": reranker_score["topShops"],
                "contextSelection": {
                    "candidateReviewCount": selection["candidateReviewCount"],
                    "selectedReviewCount": selection["selectedReviewCount"],
                    "selectedCharacterCount": selection["selectedCharacterCount"],
                    "shops": selection["shops"],
                    "selectedReviews": [
                        {
                            "reviewId": str(item["reviewId"]),
                            "shopId": int(item["shopId"]),
                            "shopName": item.get("shopName"),
                            "selectedRole": item["selectedRole"],
                            "contextText": item["contextText"],
                        }
                        for item in selection["selectedReviews"]
                    ],
                },
                "answer": answer,
                "deterministicAudit": deterministic_audit,
                "faithfulness": faithfulness,
                "testScore": answer_score,
            }
        )

    case_count = len(details)
    total_candidates = sum(
        detail["contextSelection"]["candidateReviewCount"] for detail in details
    )
    total_selected = sum(
        detail["contextSelection"]["selectedReviewCount"] for detail in details
    )
    return {
        "methodology": {
            "split": "sealed test only",
            "testRead": True,
            "testRunPolicy": "one-time shadow evaluation; results must not tune this pipeline",
            "selectionUse": False,
            "qrelVersion": Q15_QREL_VERSION,
            "candidatePool": "independent Vector/BM25 Top-30 union",
            "hybrid": {
                "normalization": "min_max",
                "vectorWeight": vector_weight,
                "bm25Weight": bm25_weight,
            },
            "contentRerankerPromptVersion": CONTENT_RERANKER_PROMPT_VERSION,
            "groundedAnswerPromptVersion": GROUNDED_ANSWER_PROMPT_VERSION,
            "faithfulnessJudgePromptVersion": FAITHFULNESS_JUDGE_PROMPT_VERSION,
            "model": settings.deepseek_model,
            "qrelsVisibleToRetrievalRankingGenerationOrJudge": False,
            "knownLimitations": [
                "six manually written questions are a shadow gate, not a production benchmark",
                "qrels are pooled and non-exhaustive",
                "generation and faithfulness judge currently use the same model family",
                "translated Yelp reviews are not native Chinese user logs",
            ],
        },
        "caseCount": case_count,
        "retrieval": {
            "hybridV02B08": summarize_fuzzy_discovery_details(
                hybrid_details,
                top_k=top_k,
            ),
            "llmContentReranker": summarize_fuzzy_discovery_details(
                reranker_details,
                top_k=top_k,
            ),
        },
        "contextSelection": {
            "averageCandidateReviewCount": total_candidates / case_count if case_count else 0.0,
            "averageSelectedReviewCount": total_selected / case_count if case_count else 0.0,
            "reviewCountReduction": (
                1 - total_selected / total_candidates if total_candidates else 0.0
            ),
            "averageSelectedCharacterCount": (
                sum(
                    detail["contextSelection"]["selectedCharacterCount"]
                    for detail in details
                )
                / case_count
                if case_count
                else 0.0
            ),
        },
        "generation": {
            "recommendationHitRate": _average(answer_scores, "recommendationHit"),
            "meanRecommendationPrecision": _average(
                answer_scores,
                "recommendationPrecision",
            ),
            "meanRecommendationRecall": _average(
                answer_scores,
                "recommendationRecall",
            ),
            "meanRecommendationNdcgAt3": _average(
                answer_scores,
                "recommendationNdcgAt3",
            ),
            "meanExactQrelReasonCitationPrecision": _average(
                answer_scores,
                "exactQrelReasonCitationPrecision",
            ),
            "meanCitationIdValidity": _average(
                deterministic_audits,
                "citationIdValidity",
            ),
            "meanCitationShopConsistency": _average(
                deterministic_audits,
                "citationShopConsistency",
            ),
            "meanCitationCompleteness": _average(
                deterministic_audits,
                "citationCompleteness",
            ),
            "meanDeterministicValidRecommendationRate": _average(
                deterministic_audits,
                "deterministicValidRecommendationRate",
            ),
            "meanEntailedRecommendationRate": _average(
                faithfulness_results,
                "entailedRecommendationRate",
            ),
            "fullyFaithfulAnswerRate": _average(
                faithfulness_results,
                "fullyFaithful",
            ),
        },
        "timing": {
            "averageRetrievalMs": _average(timings, "retrievalMs"),
            "averageContentRerankerMs": _average(timings, "contentRerankerMs"),
            "averageGenerationMs": _average(timings, "generationMs"),
            "averageFaithfulnessJudgeMs": _average(timings, "faithfulnessJudgeMs"),
            "averageTotalMs": _average(timings, "totalMs"),
            "details": timings,
        },
        "details": details,
    }
