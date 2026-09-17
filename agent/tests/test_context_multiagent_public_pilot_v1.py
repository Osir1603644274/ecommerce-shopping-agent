from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.evidence_research_v1 import (
    ResearchDecisionOutputV1,
    ResearchFindingV1,
    build_investigation_set_v1,
    build_research_report_v1,
    build_research_request_v1,
)
from evaluation.context_multiagent_public_pilot_v1 import (
    FinalAnswerV1,
    candidate_scope,
    exact_mcnemar,
    load_jsonl,
    paired_bootstrap_delta,
    report_truth,
    score_final,
    score_report,
    tool_trace_for,
)


ROOT = Path(__file__).resolve().parents[2]
DATASET = (
    ROOT
    / "agent/evaluation/assets/context_multiagent_public_pilot_v1_20260831/scenarios.jsonl"
)
CATALOG = (
    ROOT
    / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/catalog.jsonl"
)
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def _research_fixture():
    case = next(
        row for row in load_jsonl(DATASET) if row["routeGold"] == "RESEARCH_ELIGIBLE"
    )
    catalog = {int(row["itemId"]): row for row in load_jsonl(CATALOG)}
    scope = candidate_scope(case)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    investigation = build_investigation_set_v1(
        candidate_scope=scope,
        task_id=scope.task_id,
        task_revision=1,
        hard_unknowns_by_product={},
        unknowns_by_product={
            candidate_id: case["evidenceGapKeys"]
            for candidate_id in scope.ranked_item_ids
        },
        expires_at=deadline,
    ).investigation_set
    trace = tool_trace_for(investigation.candidate_ids, catalog)
    request = build_research_request_v1(
        investigation_set=investigation,
        candidate_scope=scope,
        task_id=scope.task_id,
        task_revision=1,
        parent_run_id="parent-1",
        handoff_id="handoff-1",
        child_run_id="child-1",
        parent_context_binding_hash=HASH_C,
        child_context_binding_hash=HASH_D,
        capability_grant_hash=HASH_E,
        research_goal=case["currentQuery"],
        deadline_at=deadline,
        max_tool_calls=1,
        max_model_decisions=1,
    )
    return case, scope, investigation, trace, request


def test_public_pilot_source_is_33_clusters_with_frozen_route_distribution() -> None:
    rows = load_jsonl(DATASET)
    assert len(rows) == 33
    assert len({row["scenarioId"] for row in rows}) == 33
    counts = {}
    for row in rows:
        counts[row["routeGold"]] = counts.get(row["routeGold"], 0) + 1
    assert counts == {
        "MUST_CLARIFY": 4,
        "MUST_DIRECT": 20,
        "RESEARCH_ELIGIBLE": 7,
        "RESEARCH_REQUIRED": 2,
    }


def test_shared_scope_and_investigation_preserve_rank_and_exact_ids() -> None:
    _case, scope, investigation, trace, request = _research_fixture()
    assert request.candidate_ids == investigation.candidate_ids
    assert list(request.candidate_ids) == list(scope.ranked_item_ids)
    assert [product["id"] for product in trace.detail["products"]] == list(
        request.candidate_ids
    )


def test_deterministic_scorer_accepts_complete_grounded_report_and_answer() -> None:
    _case, _scope, _investigation, trace, request = _research_fixture()
    truth = report_truth(request, trace)
    findings = tuple(
        ResearchFindingV1(
            candidateId=candidate_id,
            evidenceGapKey=gap,
            verdict=verdict,
            evidenceRefs=refs,
            summary=("verified" if verdict == "SATISFIED" else "unknown"),
            sourceAuthority=("TOOL_FACT" if verdict == "SATISFIED" else "INSUFFICIENT"),
        )
        for (candidate_id, gap), (verdict, refs) in truth.items()
    )
    report = build_research_report_v1(
        request=request,
        decision=ResearchDecisionOutputV1(
            findings=findings,
            unresolved=(),
            stopReason="COMPLETE",
        ),
        tool_trace=trace,
    )
    assert score_report(report, truth) == {
        "correct": len(truth),
        "total": len(truth),
        "precision": 1.0,
        "complete": True,
    }
    products = trace.detail["products"]
    from evaluation.context_multiagent_public_pilot_v1 import candidate_risk_score

    best = max(products, key=candidate_risk_score)["id"]
    unknown_keys = sorted(
        {
            gap
            for _candidate_id, gap in truth
            if all(
                truth[(candidate_id, gap)][0] == "UNKNOWN"
                for candidate_id in request.candidate_ids
            )
        }
    )
    answer = FinalAnswerV1.model_validate(
        {
            "answer": "基于已核验的机况证据，优先选择风险更低的一款；缺失性能实测时不作猜测。",
            "selectedCandidateIds": [best],
            "claims": [
                {
                    "candidateId": item.candidate_id,
                    "evidenceGapKey": item.evidence_gap_key,
                    "verdict": item.verdict,
                    "evidenceRefs": list(item.evidence_refs),
                }
                for item in findings
            ],
            "acknowledgedUnknownKeys": unknown_keys,
        }
    )
    scored = score_final(
        answer,
        report=report,
        truth=truth,
        tool_trace=trace,
        candidate_ids=request.candidate_ids,
    )
    assert scored["taskSuccess"]
    assert scored["claimPrecision"] == 1.0


def test_statistics_are_deterministic_and_cluster_paired() -> None:
    assert exact_mcnemar([True, False, False], [True, True, False]) == {
        "ctx1bOnlySuccess": 0,
        "ma1OnlySuccess": 1,
        "discordant": 1,
        "exactTwoSidedP": 1.0,
    }
    first = paired_bootstrap_delta([1, 2, 3], [2, 4, 4], seed=7, iterations=200)
    second = paired_bootstrap_delta([1, 2, 3], [2, 4, 4], seed=7, iterations=200)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
