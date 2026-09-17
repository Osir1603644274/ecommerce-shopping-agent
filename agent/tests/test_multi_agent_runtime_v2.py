from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domains.ecommerce.models import CandidateScope
from app.evidence_research_v1 import (
    NoInvestigationNeeded,
    ResearchDecisionOutputV1,
    ResearchFindingV1,
    ResearchMergeGuardV1,
    UnresolvedResearchGapV1,
)
from app.multi_agent_runtime_v2 import (
    extract_investigation_maps_v2,
    run_multi_agent_research_v2,
)
from app.schemas import ToolTrace


class _MergeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def eval(self, _script, _numkeys, key, claim, _ttl):
        current = self.values.get(key)
        if current is None:
            self.values[key] = claim
            return [1, claim]
        if current == claim:
            return [0, current]
        return [-1, current]


def _scope() -> CandidateScope:
    return CandidateScope(
        scopeId="scope-runtime-1",
        taskId="task-runtime-1",
        sourceRevision=3,
        sourcePlanId="plan-1",
        sourceStepId="step-1",
        category="phone",
        candidatePoolIds=[1, 2, 3],
        rankedItemIds=[1, 2, 3],
        visibleProductIds=[1, 2, 3],
        requirementsSnapshot=[],
        brandAvoidancesSnapshot=[],
        evidenceRefs=["product:1:title"],
        createdAt="2026-09-02T00:00:00Z",
        status="active",
    )


def _validated_results() -> list[dict]:
    return [{
        "tool": "search_products",
        "validationSummary": {
            "requiresProductCandidates": {
                "hardUnknownsByProduct": {
                    "1": ["battery_health"],
                    "2": ["battery_health"],
                    "3": ["battery_health"],
                },
                "conflictsByProduct": {},
                "unknownsByProduct": {
                    "1": ["screen_originality"],
                    "2": ["screen_originality"],
                    "3": ["screen_originality"],
                },
            }
        },
    }]


def _displayed_results_without_validator_gaps() -> list[dict]:
    return [{
        "tool": "search_products",
        "validationSummary": {
            "requiresProductCandidates": {
                "hardUnknownsByProduct": {},
                "conflictsByProduct": {},
                "unknownsByProduct": {},
                "productPresentations": [
                    {"productId": 1},
                    {"productId": 2},
                    {"productId": 3},
                ],
            }
        },
    }]


def _active_scope_state() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task-runtime-1",
        revision=4,
        task_type="ecommerce_guide",
        unknowns=[],
        domain_state={
            "candidateScope": _scope().model_dump(by_alias=True, mode="json"),
            "shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "requirements": [],
                "brandAvoidances": [],
                "candidateIds": [],
                "comparedIds": [],
                "useCases": [],
                "evidenceStatus": "missing",
            },
        },
    )


def test_extracts_only_scope_bound_validator_gaps() -> None:
    hard, conflicts, unknowns = extract_investigation_maps_v2(
        _validated_results(), candidate_scope=_scope()
    )
    assert hard == {
        1: ("battery_health",),
        2: ("battery_health",),
        3: ("battery_health",),
    }
    assert conflicts == {}
    assert unknowns[1] == ("screen_originality",)


def test_out_of_scope_validator_gap_fails_closed() -> None:
    forged = _validated_results()
    forged[0]["validationSummary"]["requiresProductCandidates"][
        "unknownsByProduct"
    ]["99"] = ["camera_quality"]
    with pytest.raises(ValueError, match="out-of-scope"):
        extract_investigation_maps_v2(forged, candidate_scope=_scope())


def test_no_gap_does_not_start_child_agent() -> None:
    async def run() -> None:
        with pytest.raises(NoInvestigationNeeded):
            await run_multi_agent_research_v2(
                candidate_scope=_scope(),
                task_revision=4,
                parent_run_id="run-parent",
                parent_context_binding_hash="c" * 64,
                research_goal="核验候选证据",
                hard_unknowns_by_product={},
                conflicts_by_product={},
                unknowns_by_product={},
                tool_caller=lambda *_args, **_kwargs: None,
                decide=lambda *_args, **_kwargs: None,
                merge_guard=ResearchMergeGuardV1(client=_MergeRedis()),
            )

    asyncio.run(run())


def test_real_child_handoff_fetches_once_and_returns_compact_parent_projection() -> None:
    tool_calls: list[tuple[str, dict]] = []
    decision_views: list[dict] = []

    async def tool_caller(name: str, arguments: dict) -> ToolTrace:
        tool_calls.append((name, arguments))
        return ToolTrace(
            tool=name,
            ok=True,
            durationMs=4.2,
            detail={
                "products": [
                    {
                        "id": candidate_id,
                        "attributes": [
                            {
                                "key": "battery_health",
                                "status": "known",
                                "value": "90_plus" if candidate_id == 1 else "80_90",
                                "evidenceRef": f"product:{candidate_id}:attribute:battery_health",
                            },
                            {
                                "key": "screen_originality",
                                "status": "unknown",
                                "value": None,
                                "evidenceRef": None,
                            },
                        ],
                    }
                    for candidate_id in (1, 2, 3)
                ]
            },
        )

    async def decide(view: dict) -> ResearchDecisionOutputV1:
        decision_views.append(view)
        return ResearchDecisionOutputV1(
            findings=tuple(
                ResearchFindingV1(
                    candidateId=candidate_id,
                    evidenceGapKey="battery_health",
                    verdict="SATISFIED",
                    evidenceRefs=(
                        f"product:{candidate_id}:attribute:battery_health",
                    ),
                    summary="结构化详情已核验电池健康。",
                    sourceAuthority="TOOL_FACT",
                )
                for candidate_id in (1, 2, 3)
            ),
            unresolved=tuple(
                UnresolvedResearchGapV1(
                    candidateId=candidate_id,
                    evidenceGapKey="screen_originality",
                    reason="结构化详情缺少该字段",
                )
                for candidate_id in (1, 2, 3)
            ),
            stopReason="COMPLETE",
        )

    hard, conflicts, unknowns = extract_investigation_maps_v2(
        _validated_results(), candidate_scope=_scope()
    )
    result = asyncio.run(
        run_multi_agent_research_v2(
            candidate_scope=_scope(),
            task_revision=4,
            parent_run_id="run-parent",
            parent_context_binding_hash="c" * 64,
            research_goal="核验候选商品的电池与屏幕证据",
            hard_unknowns_by_product=hard,
            conflicts_by_product=conflicts,
            unknowns_by_product=unknowns,
            tool_caller=tool_caller,
            decide=decide,
            merge_guard=ResearchMergeGuardV1(client=_MergeRedis()),
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
    )

    assert tool_calls == [("get_product_details", {"productIds": [1, 2, 3]})]
    assert len(decision_views) == 1
    assert result.request_envelope.sequence == 1
    assert result.reply_envelope.sequence == 2
    assert result.merge_receipt.outcome == "ACCEPTED"
    assert result.decision_support["bestVerifiedCandidateIds"] == [1]
    projection = result.parent_projection()
    serialized = str(projection)
    assert "verifiedToolObservation" not in serialized
    assert "products" not in serialized
    assert all(
        not item.get("evidenceRefs")
        for item in projection["report"].get("unresolved", [])
    )


def test_production_hook_runs_child_and_returns_only_parent_projection(monkeypatch) -> None:
    from app import llm
    import app.multi_agent_runtime_v2 as runtime

    monkeypatch.setattr(llm.settings, "multi_agent_v2_enabled", True)
    expected = {
        "schemaVersion": "multi-agent-parent-projection-v2",
        "routeClass": "RESEARCH_ELIGIBLE",
        "report": {"findings": [], "unresolved": []},
        "candidateDecisionSupport": {},
    }
    fake_result = SimpleNamespace(parent_projection=lambda: expected)
    run_child = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(runtime, "run_multi_agent_research_v2", run_child)
    trace = MagicMock()
    state = SimpleNamespace(
        task_id="task-runtime-1",
        revision=4,
        domain_state={
            "candidateScope": _scope().model_dump(by_alias=True, mode="json")
        },
    )

    projection = asyncio.run(
        llm._maybe_run_multi_agent_research_v2(
            state=state,
            validated_results=_validated_results(),
            final_answer_view=SimpleNamespace(context_hash="c" * 64),
            run_id="run-parent",
            user_message="核验后推荐",
            client=SimpleNamespace(),
            tool_transport=AsyncMock(),
            trace_builder=trace,
            remaining_budget_seconds=12.0,
        )
    )

    assert projection == expected
    run_child.assert_awaited_once()
    trace.start_phase.assert_called_once_with("evidence_research_agent")
    trace.end_phase.assert_called_once_with("validated_and_merged")
    trace.mark_degraded.assert_not_called()


def test_scope_capability_request_derives_only_allowlisted_gap_keys() -> None:
    from app import llm

    state = _active_scope_state()
    assert llm._active_scope_research_gap_keys_v2(
        state,
        "刚才推荐的这三款，比较打游戏是否流畅、散热和相机表现；没有证据标为未知",
    ) == (
        "gaming_performance",
        "thermal_performance",
        "camera_quality",
    )
    assert llm._active_scope_research_gap_keys_v2(
        state,
        "刚才推荐的这三款，再核验售后服务",
    ) == ()
    assert llm._active_scope_research_gap_keys_v2(
        state,
        "推荐三款并比较游戏和相机表现",
    ) == ()


def test_scope_capability_request_reaches_child_for_displayed_three(monkeypatch) -> None:
    from app import llm
    import app.multi_agent_runtime_v2 as runtime

    monkeypatch.setattr(llm.settings, "multi_agent_v2_enabled", True)
    expected = {
        "schemaVersion": "multi-agent-parent-projection-v2",
        "report": {"findings": [], "unresolved": []},
    }
    fake_result = SimpleNamespace(parent_projection=lambda: expected)
    run_child = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(runtime, "run_multi_agent_research_v2", run_child)
    trace = MagicMock()

    projection = asyncio.run(
        llm._maybe_run_multi_agent_research_v2(
            state=_active_scope_state(),
            validated_results=_displayed_results_without_validator_gaps(),
            final_answer_view=SimpleNamespace(context_hash="c" * 64),
            run_id="run-parent",
            user_message=(
                "刚才推荐的这三款，比较打游戏是否流畅、散热和相机表现；"
                "没有证据就标未知"
            ),
            client=SimpleNamespace(),
            tool_transport=AsyncMock(),
            trace_builder=trace,
            remaining_budget_seconds=12.0,
        )
    )

    assert projection == expected
    kwargs = run_child.await_args.kwargs
    assert kwargs["unknowns_by_product"] == {
        1: ("camera_quality", "gaming_performance", "thermal_performance"),
        2: ("camera_quality", "gaming_performance", "thermal_performance"),
        3: ("camera_quality", "gaming_performance", "thermal_performance"),
    }


def test_displayed_candidate_derivation_rejects_out_of_scope_identity() -> None:
    from app import llm

    forged = _displayed_results_without_validator_gaps()
    forged[0]["validationSummary"]["requiresProductCandidates"][
        "productPresentations"
    ][2]["productId"] = 99
    with pytest.raises(ValueError, match="not bound"):
        llm._displayed_candidate_ids_v2(
            forged,
            candidate_scope=_scope(),
        )


def test_production_hook_failure_is_recorded_and_falls_back(monkeypatch) -> None:
    from app import llm
    import app.multi_agent_runtime_v2 as runtime

    monkeypatch.setattr(llm.settings, "multi_agent_v2_enabled", True)
    monkeypatch.setattr(
        runtime,
        "run_multi_agent_research_v2",
        AsyncMock(side_effect=RuntimeError("child failed")),
    )
    trace = MagicMock()
    state = SimpleNamespace(
        task_id="task-runtime-1",
        revision=4,
        domain_state={
            "candidateScope": _scope().model_dump(by_alias=True, mode="json")
        },
    )
    projection = asyncio.run(
        llm._maybe_run_multi_agent_research_v2(
            state=state,
            validated_results=_validated_results(),
            final_answer_view=SimpleNamespace(context_hash="c" * 64),
            run_id="run-parent",
            user_message="核验后推荐",
            client=SimpleNamespace(),
            tool_transport=AsyncMock(),
            trace_builder=trace,
            remaining_budget_seconds=12.0,
        )
    )

    assert projection is None
    trace.end_phase.assert_called_once_with("failed_closed_to_single_agent")
    trace.mark_degraded.assert_called_once_with("multi_agent_v2_failed_closed")


def test_final_answer_prompt_receives_validated_child_report_without_raw_tool_data() -> None:
    from app import llm

    captured: dict = {}

    class _Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="证据不足，保持未知。"))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    view = SimpleNamespace(
        goal="核验相机后推荐",
        validated_results=[],
        allowed_facts=[],
        unknowns=["camera_quality"],
        evidence_refs=[],
        answer_format={},
        long_term_memory=[],
    )
    projection = {
        "schemaVersion": "multi-agent-parent-projection-v2",
        "routeClass": "RESEARCH_ELIGIBLE",
        "report": {
            "findings": [],
            "unresolved": [
                {
                    "candidateId": 1,
                    "evidenceGapKey": "camera_quality",
                    "reason": "missing",
                }
            ],
        },
        "candidateDecisionSupport": {"bestVerifiedCandidateIds": [1]},
        "rawToolObservation": {"mustNotAppear": True},
    }
    answer = asyncio.run(
        llm._generate_final_answer(
            client,
            messages=[],
            tool_traces=[],
            on_answer_delta=None,
            fallback="fallback",
            final_answer_view=view,
            multi_agent_projection=projection,
        )
    )

    prompt = captured["messages"][1]["content"]
    assert answer == "证据不足，保持未知。"
    assert "EvidenceResearchAgent 已验证回执" in prompt
    assert "camera_quality" in prompt
    assert "mustNotAppear" not in prompt
    assert "UNKNOWN 与 unresolved" in prompt


def test_deterministic_parent_renderer_never_borrows_refs_for_unknown_claims() -> None:
    from app.llm import _render_multi_agent_parent_answer_v2

    view = SimpleNamespace(validated_results=[])
    projection = {
        "schemaVersion": "multi-agent-parent-projection-v2",
        "report": {
            "findings": [
                {
                    "candidateId": 1,
                    "evidenceGapKey": "gaming_performance",
                    "verdict": "UNKNOWN",
                    "evidenceRefs": ["product:1:attribute:battery_health"],
                    "summary": "不得采用的错误引用",
                },
                {
                    "candidateId": 1,
                    "evidenceGapKey": "battery_health",
                    "verdict": "SATISFIED",
                    "evidenceRefs": ["product:1:attribute:battery_health"],
                    "summary": "电池健康 90% 以上",
                },
            ],
            "unresolved": [
                {
                    "candidateId": 2,
                    "evidenceGapKey": "thermal_performance",
                    "reason": "missing",
                }
            ],
        },
        "candidateDecisionSupport": {
            "bestVerifiedCandidateIds": [1],
            "candidates": [
                {"candidateId": 1},
                {"candidateId": 2},
            ],
        },
    }

    answer = _render_multi_agent_parent_answer_v2(view, projection)
    assert answer is not None
    assert "游戏表现：未核实" in answer
    assert "电池健康：满足" in answer
    assert "散热表现：未核实" in answer
    assert "不得采用的错误引用" not in answer
    assert "product:1:attribute" not in answer
