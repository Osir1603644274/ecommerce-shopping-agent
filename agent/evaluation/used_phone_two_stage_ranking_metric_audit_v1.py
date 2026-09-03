"""Post-run, prediction-first metric audit for frozen v3 public-dev attempt-004.

This evaluator is diagnostic only.  It preserves the legacy v3 score and its
cross-cutoff ``rerankingRecallDelta`` while adding denominators, cutoff
ceilings, candidate-conditioned retention, a same-cutoff comparison, DCG
components, and failure clusters.  The production runner never imports this
module or reads its preregistration, legacy score, or dev judgments.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import used_phone_two_stage_ranking_scorer_v3 as base


SCHEMA_VERSION = "used-phone-two-stage-ranking-public-dev-metric-audit-v1"
EVALUATOR_IDENTITY_VERSION = (
    "used-phone-two-stage-ranking-evaluator-v3-metric-audit-prediction-first"
)
AUDIT_PREREGISTRATION_FILENAME = "public_dev_metric_audit_preregistration_v1.json"
AUDIT_PREREGISTRATION_SHA256 = (
    "2079f940200566efae68dfa708a357f28323bd21f5520c70f6f48c2aabad130b"
)
FROZEN_PREDICTION_SHA256 = (
    "8dd908fdea43528e98e66d5b069bc959afead150c181db8ffd3ec287077bdd3b"
)
FROZEN_MANIFEST_SHA256 = (
    "10f68c3b44d1ad937bdf67fc59b2a4eada834bb4ff952e3e52d8d5540bf1e6cc"
)
FROZEN_RUN_CONTRACT_SHA256 = (
    "00060ea903db3dd386f5c481eb29cb36eaf6c5f579acb4791458f9578beab555"
)
LEGACY_SCORE_SHA256 = (
    "b1385fdb981dc295831853f43ba96e0da009805d70fd3a02691898c015ef1624"
)
LEGACY_SCORE_SCHEMA_VERSION = base.SCHEMA_VERSION
EVALUATOR_ASSET_DIR = base.EVALUATOR_ASSET_DIR
REPO_ROOT = base.REPO_ROOT
EVALUATOR_FILES = {
    "agent/evaluation/used_phone_two_stage_ranking_scorer_v3.py": (
        REPO_ROOT / "agent" / "evaluation" / "used_phone_two_stage_ranking_scorer_v3.py"
    ),
    "agent/evaluation/used_phone_two_stage_ranking_metric_audit_v1.py": (
        Path(__file__).resolve()
    ),
    "agent/scripts/audit_used_phone_two_stage_ranking_metrics_v1.py": (
        REPO_ROOT / "agent" / "scripts"
        / "audit_used_phone_two_stage_ranking_metrics_v1.py"
    ),
}


def _mean(values: Iterable[float | None]) -> float | None:
    observed = [value for value in values if value is not None]
    return sum(observed) / len(observed) if observed else None


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def _dcg(gains: Iterable[int | float | None]) -> float:
    return sum(
        (gain or 0) / math.log2(index + 2)
        for index, gain in enumerate(gains)
    )


def _load_audit_preregistration(
    path: Path,
    *, prediction_sha256: str,
    manifest_sha256: str,
    run_contract_sha256: str,
) -> dict[str, Any]:
    resolved = path.resolve()
    if (
        resolved.name != AUDIT_PREREGISTRATION_FILENAME
        or resolved.parent != EVALUATOR_ASSET_DIR.resolve()
    ):
        raise ValueError(
            "metric-audit preregistration must come from the pinned evaluator asset directory"
        )
    payload = resolved.read_bytes()
    if hashlib.sha256(payload).hexdigest() != AUDIT_PREREGISTRATION_SHA256:
        raise ValueError("metric-audit preregistration SHA mismatch")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid metric-audit preregistration JSON") from exc
    if not isinstance(value, dict) or payload != base.canonical_bytes(value):
        raise ValueError("metric-audit preregistration must be canonical JSON")
    expected = {
        "schemaVersion": SCHEMA_VERSION.replace("metric-audit", "metric-audit-preregistration"),
        "split": "dev",
        "predictionSha256": prediction_sha256,
        "manifestSha256": manifest_sha256,
        "runContractSha256": run_contract_sha256,
        "legacyScoreSha256": LEGACY_SCORE_SHA256,
        "labelBoundary": "AI-designed product-grounded, not human gold",
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError("metric-audit preregistration semantic mismatch")
    if not isinstance(value.get("diagnosticDefinitions"), dict):
        raise ValueError("metric-audit diagnostic definitions missing")
    return value


def _load_legacy_score(
    path: Path,
    *, prediction_sha256: str,
    manifest_sha256: str,
    run_contract_sha256: str,
) -> dict[str, Any]:
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != LEGACY_SCORE_SHA256:
        raise ValueError("legacy score SHA mismatch")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid legacy score JSON") from exc
    if not isinstance(value, dict) or payload != base.canonical_bytes(value):
        raise ValueError("legacy score must be canonical JSON")
    inputs = value.get("inputs")
    if (
        value.get("schemaVersion") != LEGACY_SCORE_SCHEMA_VERSION
        or value.get("split") != "dev"
        or not isinstance(inputs, dict)
        or inputs.get("predictionSha256") != prediction_sha256
        or inputs.get("manifestSha256") != manifest_sha256
        or inputs.get("runContractSha256") != run_contract_sha256
    ):
        raise ValueError("legacy score is not bound to the authenticated run")
    return value


def _citation_identity(citation: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        citation.get(key)
        for key in ("itemId", "group", "source", "field", "lineNumber", "rawValue")
    )


def _case_diagnostics(
    prediction: Mapping[str, Any],
    judgments: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    pool = base._validate_id_list(
        prediction.get("candidatePoolIds"), maximum=50, field="candidatePoolIds"
    )
    final = base._validate_id_list(
        prediction.get("rankedItemIds"), maximum=20, field="rankedItemIds"
    )
    if any(item not in judgments for item in [*pool, *final]):
        raise ValueError("prediction item outside judgment universe")

    relevant = {item for item, row in judgments.items() if row["eligible"] is True}
    pool_set = set(pool)
    final_set = set(final)
    pool_top20 = pool[:20]
    relevant_total = len(relevant)
    pool_hits = len(pool_set & relevant)
    pool_top20_hits = len(set(pool_top20) & relevant)
    final_hits = len(final_set & relevant)
    pool_ceiling_hits = min(50, relevant_total)
    final_ceiling_hits = min(20, relevant_total)

    final_gains = [judgments[item]["gain"] for item in final[:10]]
    actual_dcg = _dcg(final_gains)
    ideal_gains = sorted(
        (row["gain"] or 0 for row in judgments.values()), reverse=True
    )[:10]
    ideal_dcg = _dcg(ideal_gains)
    returned_set_ideal_gains = sorted(
        (judgments[item]["gain"] or 0 for item in final), reverse=True
    )[:10]
    returned_set_ideal_dcg = _dcg(returned_set_ideal_gains)

    dropped = pool_set - final_set
    dropped_by_stratum = {
        stratum: sum(
            judgments[item]["judgmentStratum"] == stratum for item in dropped
        )
        for stratum in (
            "fully_satisfied",
            "soft_missing_or_unsatisfied",
            "hard_fail",
            "hard_unknown_or_conflict",
        )
    }
    top10 = final[:10]
    hard_fail_count = sum(
        judgments[item]["judgmentStratum"] == "hard_fail" for item in top10
    )
    unknown_conflict_count = sum(
        judgments[item]["judgmentStratum"] == "hard_unknown_or_conflict"
        for item in top10
    )
    valid_citations = {
        (
            item_id,
            check["group"],
            ref["source"],
            ref["field"],
            ref["lineNumber"],
            ref["rawValue"],
        )
        for item_id, row in judgments.items()
        for check in row["checks"]
        for ref in check["evidenceRefs"]
    }
    citations = prediction.get("evidenceCitations", [])
    citation_correct = sum(
        _citation_identity(citation) in valid_citations for citation in citations
    )

    pool_recall = _ratio(pool_hits, relevant_total)
    pool_top20_recall = _ratio(pool_top20_hits, relevant_total)
    final_recall = _ratio(final_hits, relevant_total)
    return {
        "actualDcgAt10": actual_dcg,
        "candidatePoolCeilingHitCountAt50": pool_ceiling_hits,
        "candidatePoolCeilingUtilizationAt50": _ratio(pool_hits, pool_ceiling_hits),
        "candidatePoolRecallAt50": pool_recall,
        "candidatePoolRecallCeilingAt50": _ratio(pool_ceiling_hits, relevant_total),
        "candidatePoolRelevantHitCount": pool_hits,
        "candidatePoolTop20Recall": pool_top20_recall,
        "candidatePoolTop20RelevantHitCount": pool_top20_hits,
        "caseId": prediction["caseId"],
        "citationAccuracy": _ratio(citation_correct, len(citations)),
        "citationCorrectCount": citation_correct,
        "citationCount": len(citations),
        "droppedPoolItemsByJudgmentStratum": dropped_by_stratum,
        "finalCeilingHitCountAt20": final_ceiling_hits,
        "finalCeilingUtilizationAt20": _ratio(final_hits, final_ceiling_hits),
        "finalRecallAt20": final_recall,
        "finalRecallCeilingAt20": _ratio(final_ceiling_hits, relevant_total),
        "finalRelevantHitCount": final_hits,
        "finalReturnedCount": len(final),
        "idealDcgAt10": ideal_dcg,
        "legacyRerankingRecallDelta": (
            final_recall - pool_recall
            if final_recall is not None and pool_recall is not None
            else None
        ),
        "missingTop10RankCount": 10 - len(top10),
        "ndcgAt10": _ratio(actual_dcg, ideal_dcg) or 0.0,
        "orderingEfficiencyAt10": _ratio(actual_dcg, returned_set_ideal_dcg),
        "orderingLossDcgAt10": returned_set_ideal_dcg - actual_dcg,
        "poolToFinalRelevantRetention": _ratio(final_hits, pool_hits),
        "relevantPrevalence": _ratio(relevant_total, len(judgments)),
        "relevantTotal": relevant_total,
        "returnedSetIdealDcgAt10": returned_set_ideal_dcg,
        "sameCutoffRecallDeltaAt20": (
            final_recall - pool_top20_recall
            if final_recall is not None and pool_top20_recall is not None
            else None
        ),
        "top10HardViolationCount": hard_fail_count,
        "top10HardViolationDenominator": 10,
        "top10HardViolationRate": hard_fail_count / 10,
        "top10ReturnedCount": len(top10),
        "top10UnknownOrConflictCount": unknown_conflict_count,
    }


def _failure_clusters(
    per_case: list[dict[str, Any]], preregistration: Mapping[str, Any],
) -> dict[str, Any]:
    def case_ids(predicate) -> list[str]:
        return [row["caseId"] for row in per_case if predicate(row)]

    retrieval_miss = {
        row["caseId"]: row["relevantTotal"] - row["candidatePoolRelevantHitCount"]
        for row in per_case
    }
    reranker_drop = {
        row["caseId"]: (
            row["candidatePoolRelevantHitCount"] - row["finalRelevantHitCount"]
        )
        for row in per_case
    }
    ordering_loss = {
        row["caseId"]: row["orderingLossDcgAt10"] for row in per_case
    }
    policy_eliminated = {
        row["caseId"]: {
            "hardFail": row["droppedPoolItemsByJudgmentStratum"]["hard_fail"],
            "hardUnknownOrConflict": row["droppedPoolItemsByJudgmentStratum"][
                "hard_unknown_or_conflict"
            ],
        }
        for row in per_case
    }
    case_roles = preregistration.get("caseRoles", {})
    state_cases = [
        row["caseId"] for row in per_case
        if row["caseId"] in case_roles
        and row["candidatePoolCeilingUtilizationAt50"] is not None
        and row["candidatePoolCeilingUtilizationAt50"] < 0.5
    ]
    return {
        "cutoffCeilingOrHighPrevalence": {
            "caseIds": case_ids(lambda row: row["relevantTotal"] > 20),
            "definition": "relevantTotal > final cutoff 20, so raw final recall cannot reach 1",
        },
        "orderingLoss": {
            "caseIds": [case_id for case_id, loss in ordering_loss.items() if loss > 1e-12],
            "dcgLossByCase": ordering_loss,
            "definition": "returned final set is not in its own gain-ideal top10 order",
        },
        "policyElimination": {
            "byCase": policy_eliminated,
            "definition": "pool items excluded from final whose judgment is hard_fail or hard_unknown_or_conflict",
        },
        "queryStateCombinationRisk": {
            "caseIds": state_cases,
            "registeredRoles": case_roles,
            "definition": "registered multi-turn/query-state case with below-50% pool ceiling utilization",
        },
        "rerankerDrop": {
            "byCase": reranker_drop,
            "caseIds": [case_id for case_id, count in reranker_drop.items() if count > 0],
            "definition": "relevant item is in candidate pool but absent from final",
            "total": sum(reranker_drop.values()),
        },
        "retrievalMiss": {
            "byCase": retrieval_miss,
            "caseIds": [case_id for case_id, count in retrieval_miss.items() if count > 0],
            "definition": "relevant item is absent from candidate pool",
            "total": sum(retrieval_miss.values()),
        },
    }


def audit_public_dev_metrics(
    *,
    predictions_path: Path,
    manifest_path: Path,
    legacy_score_path: Path,
    public_dev_judgments_path: Path,
    audit_preregistration_path: Path,
) -> dict[str, Any]:
    (
        predictions,
        prediction_sha256,
        manifest_sha256,
        run_contract_sha256,
    ) = base._authenticate_prediction_before_labels(
        predictions_path=predictions_path,
        manifest_path=manifest_path,
    )
    if (
        prediction_sha256 != FROZEN_PREDICTION_SHA256
        or manifest_sha256 != FROZEN_MANIFEST_SHA256
        or run_contract_sha256 != FROZEN_RUN_CONTRACT_SHA256
    ):
        raise ValueError("metric audit accepts only frozen attempt-004")
    preregistration = _load_audit_preregistration(
        audit_preregistration_path,
        prediction_sha256=prediction_sha256,
        manifest_sha256=manifest_sha256,
        run_contract_sha256=run_contract_sha256,
    )
    legacy_score = _load_legacy_score(
        legacy_score_path,
        prediction_sha256=prediction_sha256,
        manifest_sha256=manifest_sha256,
        run_contract_sha256=run_contract_sha256,
    )
    judgments_by_case = base._load_public_dev_judgments(public_dev_judgments_path)
    per_case = [
        _case_diagnostics(prediction, judgments_by_case[prediction["caseId"]])
        for prediction in predictions
    ]
    metrics = {
        "candidatePoolCeilingUtilizationAt50": _mean(
            row["candidatePoolCeilingUtilizationAt50"] for row in per_case
        ),
        "candidatePoolRecallAt50": _mean(
            row["candidatePoolRecallAt50"] for row in per_case
        ),
        "candidatePoolTop20Recall": _mean(
            row["candidatePoolTop20Recall"] for row in per_case
        ),
        "citationAccuracy": _ratio(
            sum(row["citationCorrectCount"] for row in per_case),
            sum(row["citationCount"] for row in per_case),
        ),
        "citationCount": sum(row["citationCount"] for row in per_case),
        "finalCeilingUtilizationAt20": _mean(
            row["finalCeilingUtilizationAt20"] for row in per_case
        ),
        "finalRecallAt20": _mean(row["finalRecallAt20"] for row in per_case),
        "legacyRerankingRecallDelta": _mean(
            row["legacyRerankingRecallDelta"] for row in per_case
        ),
        "ndcgAt10": _mean(row["ndcgAt10"] for row in per_case),
        "orderingEfficiencyAt10": _mean(
            row["orderingEfficiencyAt10"] for row in per_case
        ),
        "poolToFinalRelevantRetention": _mean(
            row["poolToFinalRelevantRetention"] for row in per_case
        ),
        "sameCutoffRecallDeltaAt20": _mean(
            row["sameCutoffRecallDeltaAt20"] for row in per_case
        ),
        "top10HardViolationRate": _mean(
            row["top10HardViolationRate"] for row in per_case
        ),
    }
    legacy_metrics = legacy_score.get("metrics")
    legacy_replication = {
        "candidatePoolRecallAt50": metrics["candidatePoolRecallAt50"],
        "citationAccuracy": metrics["citationAccuracy"],
        "citationCount": metrics["citationCount"],
        "finalRecallAt20": metrics["finalRecallAt20"],
        "ndcgAt10": metrics["ndcgAt10"],
        "rerankingRecallDelta": metrics["legacyRerankingRecallDelta"],
        "top10HardViolationRate": metrics["top10HardViolationRate"],
    }
    if legacy_metrics != legacy_replication:
        raise ValueError("metric audit did not exactly reproduce the frozen legacy score")
    return {
        "evaluatorIdentity": {
            "evaluatorCodeSha256": base._code_hashes(EVALUATOR_FILES),
            "evaluatorIdentityVersion": EVALUATOR_IDENTITY_VERSION,
            "productionCodeScopeSha256": base.PRODUCTION_CODE_SCOPE_SHA256,
        },
        "failureClusters": _failure_clusters(per_case, preregistration),
        "inputs": {
            "auditPreregistrationSha256": AUDIT_PREREGISTRATION_SHA256,
            "legacyScoreSha256": LEGACY_SCORE_SHA256,
            "manifestSha256": manifest_sha256,
            "predictionSha256": prediction_sha256,
            "publicDevJudgmentsSha256": base.PUBLIC_DEV_JUDGMENTS_SHA256,
            "runContractSha256": run_contract_sha256,
        },
        "labelBoundary": "AI-designed product-grounded, not human gold",
        "legacyMetricNotice": {
            "field": "rerankingRecallDelta",
            "status": "retained_for_historical_compatibility_only",
            "warning": "finalRecallAt20 minus candidatePoolRecallAt50 compares different cutoffs and is not reranker gain",
        },
        "metricDefinitions": preregistration["diagnosticDefinitions"],
        "metrics": metrics,
        "perCase": per_case,
        "purpose": "post-run dev diagnosis; not a quality gate",
        "schemaVersion": SCHEMA_VERSION,
        "split": "dev",
    }
