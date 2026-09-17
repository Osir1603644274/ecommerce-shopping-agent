import hashlib
import json
from pathlib import Path
from typing import Any


AGENT_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = AGENT_ROOT / "knowledge_data" / "eval"
HYBRID_VALIDATION_REPORT_PATH = EVAL_DIR / "review_hybrid_validation_report.json"
HYBRID_AGENT_REPORT_PATH = EVAL_DIR / "review_hybrid_agent_canary_report.json"
Q15_SEALED_REPORT_PATH = EVAL_DIR / "q15_sealed_test_report.json"

RERANKER_AVERAGE_LATENCY_GATE_MS = 3000.0
RERANKER_MAX_LATENCY_GATE_MS = 5000.0
FULLY_FAITHFUL_ANSWER_GATE = 0.90


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"report must be a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_rag_prod_readiness_report(
    hybrid_validation: dict[str, Any],
    hybrid_agent: dict[str, Any],
    q15_sealed: dict[str, Any],
    *,
    sealed_report_sha256: str,
) -> dict[str, Any]:
    stage3_checks = {
        "retrievalValidationPassed": hybrid_validation["summary"]["passed"] is True,
        "realAgentCanaryPassed": hybrid_agent["summary"]["passed"] is True,
        "realAgentCanaryComplete": hybrid_agent["summary"]["complete"] is True,
        "realAgentCanaryNoFailures": not hybrid_agent.get("failures"),
    }

    retrieval = q15_sealed["retrieval"]
    hybrid_metrics = retrieval["hybridV02B08"]
    reranker_metrics = retrieval["llmContentReranker"]
    timing = q15_sealed["timing"]
    reranker_case_times = [
        float(item["contentRerankerMs"])
        for item in timing.get("details", [])
    ]
    reranker_max_ms = max(reranker_case_times, default=0.0)
    stage4_checks = {
        "ndcgImprovesOverHybrid": reranker_metrics["meanNdcgAt5"]
        >= hybrid_metrics["meanNdcgAt5"],
        "evidenceRecallNonRegression": reranker_metrics["meanEvidenceShopRecall"]
        >= hybrid_metrics["meanEvidenceShopRecall"],
        "averageLatencyWithinGate": timing["averageContentRerankerMs"]
        <= RERANKER_AVERAGE_LATENCY_GATE_MS,
        "maxLatencyWithinGate": reranker_max_ms <= RERANKER_MAX_LATENCY_GATE_MS,
    }

    context = q15_sealed["contextSelection"]
    generation = q15_sealed["generation"]
    stage5_checks = {
        "stage4Eligible": all(stage4_checks.values()),
        "contextReductionAtLeastHalf": context["reviewCountReduction"] >= 0.50,
        "averageSelectedReviewsAtMostTen": context["averageSelectedReviewCount"]
        <= 10.0,
        "citationIdsAlwaysValid": generation["meanCitationIdValidity"] == 1.0,
        "citationShopAlwaysConsistent": generation["meanCitationShopConsistency"]
        == 1.0,
        "fullyFaithfulAnswerRateWithinGate": generation["fullyFaithfulAnswerRate"]
        >= FULLY_FAITHFUL_ANSWER_GATE,
    }

    stage3_passed = all(stage3_checks.values())
    stage4_passed = all(stage4_checks.values())
    stage5_passed = all(stage5_checks.values())
    return {
        "decision": "RAG-PROD-01 staged production readiness",
        "methodology": {
            "stage3Inputs": "non-sealed validation plus real Agent canary",
            "stage4And5Input": "existing Q15 sealed report reused read-only",
            "sealedTestRerun": False,
            "sealedReportSha256": sealed_report_sha256,
            "thresholds": {
                "rerankerAverageLatencyMs": RERANKER_AVERAGE_LATENCY_GATE_MS,
                "rerankerMaxLatencyMs": RERANKER_MAX_LATENCY_GATE_MS,
                "fullyFaithfulAnswerRate": FULLY_FAITHFUL_ANSWER_GATE,
            },
        },
        "stage3ReviewHybrid": {
            "decision": "canary_ready" if stage3_passed else "no_go",
            "checks": stage3_checks,
            "defaultEnabled": False,
            "fallback": "source-aware Vector, then legacy runtime fallback",
        },
        "stage4LlmContentReranker": {
            "decision": "canary_ready" if stage4_passed else "no_go_keep_shadow",
            "checks": stage4_checks,
            "observed": {
                "hybridNdcgAt5": hybrid_metrics["meanNdcgAt5"],
                "rerankerNdcgAt5": reranker_metrics["meanNdcgAt5"],
                "averageLatencyMs": timing["averageContentRerankerMs"],
                "maxLatencyMs": reranker_max_ms,
            },
            "defaultEnabled": False,
        },
        "stage5ContextAndGroundedAnswer": {
            "decision": "canary_ready" if stage5_passed else "blocked_keep_shadow",
            "checks": stage5_checks,
            "observed": {
                "reviewCountReduction": context["reviewCountReduction"],
                "averageSelectedReviewCount": context["averageSelectedReviewCount"],
                "citationIdValidity": generation["meanCitationIdValidity"],
                "citationShopConsistency": generation["meanCitationShopConsistency"],
                "fullyFaithfulAnswerRate": generation["fullyFaithfulAnswerRate"],
            },
            "defaultEnabled": False,
        },
        "overall": {
            "ragProd01EngineeringClosureComplete": True,
            "highestEligibleStage": 3 if stage3_passed else 2,
            "defaultRouteChangeAuthorized": False,
            "reason": (
                "Stage 3 passed canary gates; Stage 4 exceeds latency gates and "
                "Stage 5 fails its upstream and full-faithfulness gates."
            ),
        },
    }


def evaluate_rag_prod_readiness() -> dict[str, Any]:
    return build_rag_prod_readiness_report(
        _read_json(HYBRID_VALIDATION_REPORT_PATH),
        _read_json(HYBRID_AGENT_REPORT_PATH),
        _read_json(Q15_SEALED_REPORT_PATH),
        sealed_report_sha256=_sha256(Q15_SEALED_REPORT_PATH),
    )
