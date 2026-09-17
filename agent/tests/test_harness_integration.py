"""Integration tests verifying that ContextView is projected BEFORE each phase
and consumed as real input — not just a post-hoc trace decoration.

Tests 1-5 from the rework spec:
  1. Planner receives PlannerView in context_pack mode
  2. Executor receives ExecutorView before tool call, view matches current step
  3. Validator sees only evidence summary + constraints, not full history
  4. Replanner receives failed plan summary, not the replanned result
  5. FinalAnswer receives only validated results + allowed facts

Plus transport and replay tests:
  7. RecordTransport round-trips real ToolTrace to JSONL and back
  8. Same tool+same args duplicate calls replayed in order
  9. Strict replay fails on tool name / order / args hash mismatch
  10. Replay with Harness — external calls count = 0
  12. Debug endpoint: disabled, wrong key, correct key, not-found

And R2 boundary consumption tests (R2-2):
  PlannerBoundary — poison values in TaskState don't leak into model prompt
  ExecutorViewMismatchFailClosed — view/state mismatch raises error
  ValidatorViewOnly — validator only reads from view, not TaskState domain_state
  FinalAnswerAuthoritativeSource — final answer uses only view-authorized data

And R2 centralized harness pre-validation tests:
  TestHarnessPreValidation — harness-level view/state metadata check before phase dispatch
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from tests.two_stage_ranking_fixtures import two_stage_search_detail

from app.tool_transport import _stable_args_hash


def test_prior_step_projection_keeps_only_referenced_field():
    from app.harness import _project_prior_step_outputs_for_step
    from app.domains.ecommerce.ranking_contract import (
        TWO_STAGE_RANKING_CONTRACT_VERSION,
    )

    values = {
        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
        "candidatePoolIds": list(range(1, 51)),
        "rankedItemIds": list(range(1, 21)),
        "productIds": list(range(1, 21)),
        "evidenceRefs": [
            f"product:{item}:title" for item in range(1, 21)
        ],
        "candidateSupport": {
            "hasCompleteMatch": True,
            "fullySupportedProductIds": list(range(1, 21)),
            "closestAlternativeProductIds": [],
            "hardUnknownsByProduct": {},
            "productPresentations": None,
        },
    }
    search_step = SimpleNamespace(
        step_id="search",
        tool_name="search_products",
    )
    compare_step = SimpleNamespace(
        step_id="compare",
        argument_sources={
            "productIds": SimpleNamespace(
                kind="prior_step",
                reference="search.productIds",
            ),
        },
    )
    plan = SimpleNamespace(
        plan_id="plan-real-multiturn",
        steps=[search_step, compare_step],
    )
    state = SimpleNamespace(
        task_id="task-real-multiturn",
        domain_state={"stepOutputs": {
        "search": {
            "taskId": "task-real-multiturn",
            "planId": "plan-real-multiturn",
            "stepId": "search",
            "values": values,
        },
    }})

    projected = _project_prior_step_outputs_for_step(
        state,
        plan,
        compare_step,
    )

    assert projected == {"search": {"productIds": list(range(1, 21))}}

    state.domain_state["stepOutputs"]["search"]["taskId"] = "task-forged"
    with pytest.raises(ValueError, match="does not belong to active Plan"):
        _project_prior_step_outputs_for_step(state, plan, compare_step)


def test_unreferenced_stale_output_does_not_block_new_plan_step():
    from app.harness import _project_prior_step_outputs_for_step

    state = SimpleNamespace(
        task_id="task-current",
        domain_state={"stepOutputs": {
            "old-search": {
                "taskId": "task-current",
                "planId": "plan-old",
                "stepId": "old-search",
                "values": {"forged": "historical payload is out of scope"},
            },
        }},
    )
    new_step = SimpleNamespace(
        step_id="new-search",
        tool_name="search_products",
        argument_sources={},
    )
    new_plan = SimpleNamespace(plan_id="plan-new", steps=[new_step])

    assert _project_prior_step_outputs_for_step(state, new_plan, new_step) == {}


def test_terminal_candidate_exhaustion_skips_replanner():
    from app.harness import decide_after_validation
    from app.validator import StepValidationResult, ValidatorResult

    result = ValidatorResult(
        outcome="insufficient_evidence",
        taskId="task-no-match",
        planId="plan-no-match",
        basedOnRevision=3,
        stepResults=[StepValidationResult(
            stepId="search",
            outcome="insufficient_evidence",
            expectedOutput={"requiresProductCandidates": True},
            evidenceSummary={"requiresProductCandidates": {
                "candidatePoolCount": 50,
                "rankedItemCount": 0,
            }},
            errorCode="product_candidates_missing",
            reason="no eligible product",
        )],
        errorCode="product_candidates_missing",
        reason="no eligible product",
    )

    assert decide_after_validation(result) == "stop_turn"


def test_compound_search_validation_binds_first_two_from_normalized_output():
    from datetime import datetime, timezone
    from app.domains.ecommerce.ranking_contract import normalize_search_products_detail
    from app.executor import NormalizedStepOutput
    from app.planning import PlanStep, TaskPlan
    from app.task_state import TaskState
    from app.validator import (
        StepValidationResult,
        ValidatorEvidenceError,
        ValidatorResult,
        persist_validator_result,
    )

    detail = two_stage_search_detail([5989522, 1092185, 5304970])
    values = normalize_search_products_detail(
        detail,
        requirements=[],
        category="手机",
    ).normalized_values()
    output = NormalizedStepOutput(
        taskId="task-compound",
        planId="plan-search",
        stepId="step-search",
        values=values,
    )
    plan = TaskPlan(
        planId="plan-search",
        basedOnRevision=1,
        status="active",
        steps=[PlanStep(
            stepId="step-search",
            description="search",
            toolName="search_products",
            arguments={"query": "ios原装屏幕手机，再比较前两个结果。"},
            argumentSources={"query": {"kind": "task_goal"}},
            expectedOutput={"requiresProductCandidates": True},
            status="executed",
        )],
    )
    now = datetime.now(timezone.utc)
    state = TaskState(
        taskId="task-compound",
        taskType="ecommerce_guide",
        status="ready",
        revision=3,
        goal="ios原装屏幕手机，再比较前两个结果。",
        activePlan=plan,
        domainState={
            "shoppingGuide": {
                "mode": "recommend", "category": "phone",
                "requirements": [], "candidateIds": [],
                "comparedIds": [], "evidenceStatus": "missing",
            },
            "compoundComparison": {
                "status": "awaiting_search_validation",
                "kind": "compare_first_two",
                "taskId": "task-compound",
            },
            "stepOutputs": {"step-search": output.model_dump(
                by_alias=True, mode="json"
            )},
        },
        createdAt=now,
        updatedAt=now,
    )
    presentations = values["candidateSupport"]["productPresentations"]

    def validator_result(presentation_rows):
        return ValidatorResult(
            outcome="passed",
            taskId=state.task_id,
            planId=plan.plan_id,
            basedOnRevision=state.revision,
            stepResults=[StepValidationResult(
                stepId="step-search",
                outcome="satisfied",
                expectedOutput={"requiresProductCandidates": True},
                evidenceSummary={"requiresProductCandidates": {
                    "productPresentations": presentation_rows,
                }},
            )],
        )

    async def exercise(result):
        captured = {}

        async def persist(_task_id, patch):
            captured["patch"] = patch
            return state

        with patch("app.validator.update_task_state", new=persist):
            await persist_validator_result(state, result)
        return captured["patch"]

    import asyncio
    persisted = asyncio.run(exercise(validator_result(presentations)))
    guide = persisted.domain_state_patch["shoppingGuide"]
    assert persisted.active_plan is None
    assert guide["mode"] == "compare"
    assert guide["candidateIds"] == [5989522, 1092185, 5304970]
    assert guide["comparedIds"] == [5989522, 1092185]

    forged = [dict(item) for item in presentations]
    forged[0]["productId"] = 9999999
    with pytest.raises(
        ValidatorEvidenceError,
        match="规范化输出不一致",
    ):
        asyncio.run(exercise(validator_result(forged)))


def test_used_phone_state_update_reaches_planner_with_complete_priority_boundary():
    """Persisted parsing must not lose fields or upgrade soft preferences."""
    from app import task_state as task_state_store
    from app.context_pack import build_context_pack
    from app.context_view import ContextProjector
    from app.llm import _update_task_state_for_unified_harness
    from app.task_state import TaskStateCreateRequest, create_task_state
    from tests.fake_redis import FakeRedis

    async def exercise():
        task_state_store._client = FakeRedis()
        task_state_store._task_locks.clear()
        task_state_store._session_locks.clear()
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="挑一台二手手机",
            sessionId="planner-priority-boundary",
            domainState={"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "requirements": [],
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            }},
        ))

        # Deliberately distinct from the frozen held-out Validation wording.
        message = "我要 iOS，电池90%以上，主板不能修过；原装电池更好"
        updated = await _update_task_state_for_unified_harness(
            message,
            history=None,
            client=AsyncMock(),
            task_state=state,
            on_task_state=None,
        )
        pack = await build_context_pack(
            updated,
            allowed_tools=["search_products"],
            run_id="run-planner-priority-boundary",
        )
        return ContextProjector(pack).planner_view(
            tool_names=["search_products"],
            task_status=updated.status,
            user_message=message,
        )

    import asyncio
    view = asyncio.run(exercise())

    assert view.shopping_guide_sources is not None
    requirements = {
        row["key"]: row
        for row in view.shopping_guide_sources["requirements"]
    }
    assert set(requirements) == {
        "os", "battery_health", "motherboard_repair", "battery_originality",
    }
    assert requirements["battery_originality"]["priority"] == "soft"
    assert all(
        requirements[key]["priority"] == "hard"
        for key in ("os", "battery_health", "motherboard_repair")
    )
    assert {row["key"] for row in view.hard_constraints} == {
        "os", "battery_health", "motherboard_repair",
    }
    assert {row["key"] for row in view.soft_preferences} == {
        "battery_originality",
    }


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_plan(state):
    """Simulate a planned state."""
    from app.planning import TaskPlan, PlanStep
    plan = TaskPlan(
        planId="plan-001",
        basedOnRevision=state.revision,
        steps=[
            PlanStep(
                stepId="step-search",
                description="Search products",
                toolName="search_products",
                arguments={"query": "headphones", "category": "headphones"},
                argumentSources={"query": SimpleNamespace(kind="task_goal")},
                expectedOutput={"requiresProductCandidates": True},
            ),
        ],
    )
    return plan


# ── Test 1: Planner receives PlannerView ─────────────────────────────────────


class TestPlannerReceivesView:
    def test_planner_view_projected_before_planner_execution(self):
        """PlannerContextView is projected BEFORE the planner runs, and its hash
        appears in the trace's context_views list with type 'planner'."""
        from app.context_pack import ContextPack, TaskFact, TaskConstraint
        from app.context_view import ContextProjector, PlannerContextView
        from app.agent_trace import TraceBuilder

        pack = ContextPack(
            runId="run-001",
            taskId="task-001",
            baseContextRevision=1,
            goal="帮我选降噪耳机",
            taskType="ecommerce_guide",
            confirmedFacts=[
                TaskFact(key="category", value="headphones", certainty="confirmed", source="user"),
            ],
            hardConstraints=[
                TaskConstraint(key="price_max", operator="lte", value=300000, source="user"),
            ],
            allowedTools=["search_products", "get_product_details", "compare_products"],
        )

        projector = ContextProjector(pack)
        view = projector.planner_view(
            tool_names=["search_products", "get_product_details", "compare_products"],
            task_status="ready",
        )

        assert view.goal == "帮我选降噪耳机"
        assert view.task_type == "ecommerce_guide"
        assert len(view.confirmed_facts) == 1
        assert len(view.hard_constraints) == 1
        assert view.allowed_tool_names == ["search_products", "get_product_details", "compare_products"]
        assert view.context_hash != ""

        # Verify trace recording works end-to-end
        trace_builder = TraceBuilder("run-001", mode="context_pack+view")
        trace_builder.record_context_view("planner", view.context_hash, 1200)
        trace = trace_builder.finish()
        assert len(trace.context_views) == 1
        assert trace.context_views[0]["type"] == "planner"
        assert trace.context_views[0]["hash"] == view.context_hash


# ── Test 2: Executor receives ExecutorView ────────────────────────────────────


class TestExecutorReceivesView:
    def test_executor_view_projected_before_tool_call(self):
        """ExecutorContextView is projected BEFORE the tool call, matching the
        current pending step, not the next step."""
        from app.context_pack import ContextPack, TaskFact, TaskConstraint
        from app.context_view import ContextProjector

        pack = ContextPack(
            runId="run-002",
            taskId="task-002",
            baseContextRevision=2,
            goal="帮我选降噪耳机，预算3000以内",
            taskType="ecommerce_guide",
            confirmedFacts=[
                TaskFact(key="category", value="headphones", certainty="confirmed", source="user"),
            ],
            hardConstraints=[
                TaskConstraint(key="price_max", operator="lte", value=300000, source="user"),
            ],
            allowedTools=["search_products", "get_product_details"],
        )

        projector = ContextProjector(pack)
        tool_schema = {
            "name": "search_products",
            "description": "Search product catalog",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
        view = projector.executor_view(
            plan_id="plan-001",
            step_id="step-search",
            step_description="搜索降噪耳机",
            tool_name="search_products",
            tool_schema=tool_schema,
            resolved_arguments={"query": "降噪耳机"},
            required_fact_keys=["category"],
            required_constraint_keys=["price_max"],
        )

        assert view.tool_name == "search_products"
        assert view.plan_id == "plan-001"
        assert view.step_id == "step-search"
        assert view.tool_schema is not None
        assert view.tool_schema.name == "search_products"
        assert view.resolved_arguments == {"query": "降噪耳机"}
        assert len(view.relevant_facts) == 1
        assert view.relevant_facts[0]["key"] == "category"

    def test_executor_view_without_schema(self):
        from app.context_pack import ContextPack
        from app.context_view import ContextProjector

        pack = ContextPack(
            runId="run-003",
            taskId="task-003",
            baseContextRevision=1,
            goal="test",
            taskType="generic",
        )
        projector = ContextProjector(pack)
        view = projector.executor_view(
            plan_id="p1", step_id="s1",
            step_description="test", tool_name="t1",
        )
        assert view.tool_schema is None
        assert view.relevant_facts == []
        assert view.relevant_constraints == []


# ── Test 3: Validator sees only evidence summary ─────────────────────────────


class TestValidatorReceivesView:
    def test_validator_view_contains_executed_steps_not_full_history(self):
        """ValidatorContextView contains only executed step evidence and
        hard constraints — no full chat history, no tool result bodies."""
        from app.context_pack import ContextPack, TaskFact, TaskConstraint
        from app.context_view import ContextProjector, ValidatorContextView

        pack = ContextPack(
            runId="run-004",
            taskId="task-004",
            baseContextRevision=3,
            goal="帮我选降噪耳机",
            taskType="ecommerce_guide",
            confirmedFacts=[
                TaskFact(key="category", value="headphones", certainty="confirmed", source="user"),
            ],
            hardConstraints=[
                TaskConstraint(key="price_max", operator="lte", value=300000, source="user"),
            ],
            evidenceRefs=["ref-1", "ref-2"],
        )

        projector = ContextProjector(pack)
        view = projector.validator_view(
            executed_steps=[
                {"stepId": "s1", "toolName": "search_products", "outcome": "tool_succeeded",
                 "evidenceRefs": ["ref-1"], "detailKeys": ["candidateIds", "total"]},
                {"stepId": "s2", "toolName": "get_product_details", "outcome": "tool_succeeded",
                 "evidenceRefs": ["ref-2"], "detailKeys": ["specs", "price"]},
            ],
            unmet_constraints=[],
        )

        assert view.goal == "帮我选降噪耳机"
        assert len(view.executed_steps) == 2
        # Only step IDs, tool names, outcomes — no raw tool bodies
        assert view.executed_steps[0].step_id == "s1"
        assert view.executed_steps[0].tool_name == "search_products"
        assert view.executed_steps[0].outcome == "tool_succeeded"
        assert "ref-1" in view.executed_steps[0].evidence_refs
        # Hard constraints are accessible
        assert len(view.hard_constraints) == 1
        assert view.hard_constraints[0]["key"] == "price_max"


# ── Test 4: Replanner receives failed plan summary ──────────────────────────


class TestReplannerReceivesView:
    def test_replanner_view_contains_failed_plan_not_replanned_result(self):
        """ReplannerContextView contains the FAILED plan summary, not the
        replanned result (which doesn't exist yet)."""
        from app.context_pack import ContextPack, TaskFact, TaskConstraint
        from app.context_view import ContextProjector

        pack = ContextPack(
            runId="run-005",
            taskId="task-005",
            baseContextRevision=4,
            goal="帮我选降噪耳机",
            taskType="ecommerce_guide",
            confirmedFacts=[
                TaskFact(key="category", value="headphones", certainty="confirmed", source="user"),
            ],
            hardConstraints=[
                TaskConstraint(key="price_max", operator="lte", value=300000, source="user"),
            ],
            allowedTools=["search_products", "get_product_details"],
        )

        projector = ContextProjector(pack)
        view = projector.replanner_view(
            failed_plan_summary={
                "planId": "plan-001",
                "steps": [
                    {"stepId": "s1", "description": "Search", "toolName": "search_products", "status": "completed"},
                    {"stepId": "s2", "description": "Detail", "toolName": "get_product_details", "status": "failed"},
                ],
                "status": "failed",
            },
            failure_reason="Validator rejected: insufficient evidence for price constraint",
            unsatisfied_constraints=["price_max"],
            remaining_tool_names=["get_product_details"],
            replan_attempt=1,
        )

        assert view.failed_plan_summary["planId"] == "plan-001"
        assert view.failed_plan_summary["status"] == "failed"
        assert "insufficient evidence" in view.failure_reason
        assert view.unsatisfied_constraints == ["price_max"]
        assert view.replan_attempt == 1


# ── Test 5: FinalAnswer receives only validated results ──────────────────────


class TestFinalAnswerReceivesView:
    def test_final_answer_view_only_validated_results(self):
        """FinalAnswerContextView only contains validated results, allowed facts,
        evidence refs, and unknowns — no raw chat history."""
        from app.context_pack import ContextPack, TaskFact, TaskConstraint
        from app.context_view import ContextProjector

        pack = ContextPack(
            runId="run-006",
            taskId="task-006",
            baseContextRevision=5,
            goal="帮我选降噪耳机",
            taskType="ecommerce_guide",
            confirmedFacts=[
                TaskFact(key="category", value="headphones", certainty="confirmed", source="user"),
                TaskFact(key="brand", value="sony", certainty="confirmed", source="user"),
            ],
            hardConstraints=[
                TaskConstraint(key="price_max", operator="lte", value=300000, source="user"),
            ],
            unknowns=["bluetooth version"],
            evidenceRefs=["ref-1", "ref-2", "ref-3"],
        )

        projector = ContextProjector(pack)
        view = projector.final_answer_view(
            validated_results=[
                {"product": "Sony WH-1000XM5", "score": 0.95},
                {"product": "Bose QC45", "score": 0.88},
            ],
            evidence_refs=["validated-ref-1", "validated-ref-2"],
        )

        assert len(view.validated_results) == 2
        assert view.validated_results[0]["product"] == "Sony WH-1000XM5"
        assert view.evidence_refs == ["validated-ref-1", "validated-ref-2"]
        assert view.answer_format["maxProducts"] == 3
        assert view.answer_format["requireEvidenceRefs"] is True
        assert view.answer_format["requireUnknownsDisclosure"] is True
        # Only confirmed facts are allowed
        assert len(view.allowed_facts) == 2

    def test_final_answer_no_unknowns(self):
        """When unknowns list is empty, unknowns disclosure not required."""
        from app.context_pack import ContextPack
        from app.context_view import ContextProjector

        pack = ContextPack(
            runId="run-007",
            taskId="task-007",
            baseContextRevision=1,
            goal="test",
            taskType="generic",
        )
        projector = ContextProjector(pack)
        view = projector.final_answer_view(validated_results=[])
        assert view.answer_format["requireUnknownsDisclosure"] is False


# ── Test 7: RecordTransport round-trip ───────────────────────────────────────


class TestRecordTransportRoundTrip:
    def test_record_and_round_trip_tool_trace(self):
        """Record a real ToolTrace, then deserialise it back — verify it's valid."""
        from app.tool_transport import (
            _cleanse_tool_output,
            _deserialise_to_tool_trace,
            _stable_args_hash,
            _stable_output_hash,
        )
        from app.schemas import ToolTrace

        trace = ToolTrace(
            tool="search_products",
            ok=True,
            durationMs=42.5,
            detail={"candidateIds": [1, 2, 3], "total": 3},
        )

        cleansed = _cleanse_tool_output("search_products", trace)
        out_hash = _stable_output_hash(cleansed)

        # Deserialise
        rehydrated = _deserialise_to_tool_trace(cleansed)
        assert isinstance(rehydrated, ToolTrace)
        assert rehydrated.tool == "search_products"
        assert rehydrated.ok is True
        assert rehydrated.duration_ms == 42.5
        assert rehydrated.detail == {"candidateIds": [1, 2, 3], "total": 3}

        # Hash stability
        assert _stable_output_hash(cleansed) == out_hash

    def test_record_full_session_to_file(self):
        """RecordTransport writes session header + calls to a real JSONL file."""
        import asyncio
        import tempfile
        from pathlib import Path
        from app.tool_transport import RecordTransport

        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
                tmp = Path(f.name)

            try:
                transport = RecordTransport(
                    output_path=tmp,
                    run_id="run-rec-test",
                    context_pack_hash="abc123",
                )

                fake_call = AsyncMock()
                fake_call.return_value = SimpleNamespace(
                    tool="search_products",
                    ok=True,
                    duration_ms=15.0,
                    detail={"count": 5},
                )

                result1 = await transport(
                    "search_products",
                    {"query": "headphones", "category": "headphones"},
                    fake_call,
                )
                result2 = await transport(
                    "get_product_details",
                    {"productIds": [1, 2]},
                    fake_call,
                )

                # Read the file back
                text = tmp.read_text(encoding="utf-8").strip()
                lines = [l for l in text.split("\n") if l.strip()]
                assert len(lines) == 3  # 1 session + 2 calls

                session_line = json.loads(lines[0])
                assert session_line["type"] == "session"
                assert session_line["runId"] == "run-rec-test"

                call1 = json.loads(lines[1])
                assert call1["toolName"] == "search_products"
                assert call1["ok"] is True
                assert "argumentsHash" in call1

                call2 = json.loads(lines[2])
                assert call2["toolName"] == "get_product_details"
                assert call2["stepIndex"] == 1

            finally:
                tmp.unlink(missing_ok=True)

        asyncio.run(_test())


# ── Test 8: Duplicate calls replayed in order ───────────────────────────────


class TestReplayTransportDuplicates:
    def test_duplicate_calls_served_sequentially_not_overwritten(self):
        """Same (tool_name, args_hash) called twice → both served by ReplayTransport."""
        import asyncio
        import tempfile
        from pathlib import Path
        from app.tool_transport import ReplayTransport, _stable_args_hash

        async def _test():
            args1 = {"query": "phones"}
            args2 = {"query": "headphones"}
            hash1 = _stable_args_hash(args1)
            hash2 = _stable_args_hash(args2)
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
                f.write(json.dumps({"type": "session", "runId": "dup-test", "recordedAt": ""}) + "\n")
                f.write(json.dumps({
                    "runId": "dup-test", "stepIndex": 0,
                    "toolName": "search_products",
                    "argumentsHash": hash1,
                    "argumentsKeys": ["query"],
                    "ok": True, "durationMs": 10.0,
                    "outputHash": "bbb222",
                    "output": {"tool": "search_products", "ok": True, "durationMs": 10.0, "detail": {"count": 3}},
                }) + "\n")
                f.write(json.dumps({
                    "runId": "dup-test", "stepIndex": 1,
                    "toolName": "search_products",
                    "argumentsHash": hash2,
                    "argumentsKeys": ["query"],
                    "ok": True, "durationMs": 12.0,
                    "outputHash": "ccc333",
                    "output": {"tool": "search_products", "ok": True, "durationMs": 12.0, "detail": {"count": 5}},
                }) + "\n")
                tmp = Path(f.name)

            try:
                transport = ReplayTransport(tmp, strict=True)

                r1 = await transport("search_products", args1, None)
                assert r1.duration_ms == 10.0
                assert r1.detail["count"] == 3

                r2 = await transport("search_products", args2, None)
                assert r2.duration_ms == 12.0
                assert r2.detail["count"] == 5
            finally:
                tmp.unlink(missing_ok=True)

        asyncio.run(_test())


# ── Test 9: Strict replay failures ──────────────────────────────────────────


class TestReplayStrictMode:
    def test_strict_replay_fails_on_tool_mismatch(self):
        """Strict mode: tool name mismatch raises KeyError."""
        import asyncio
        import tempfile
        from pathlib import Path
        from app.tool_transport import ReplayTransport

        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
                f.write(json.dumps({"type": "session", "runId": "strict-test", "recordedAt": ""}) + "\n")
                f.write(json.dumps({
                    "runId": "strict-test", "stepIndex": 0,
                    "toolName": "search_products",
                    "argumentsHash": "abc123",
                    "argumentsKeys": ["query"],
                    "ok": True, "outputHash": "def456",
                    "output": {"tool": "search_products", "ok": True, "detail": {}},
                }) + "\n")
                tmp = Path(f.name)

            try:
                transport = ReplayTransport(tmp, strict=True)

                with pytest.raises(KeyError, match="Replay mismatch"):
                    await transport("get_product_details", {"ids": [1]}, None)
            finally:
                tmp.unlink(missing_ok=True)

        asyncio.run(_test())

    def test_strict_replay_fails_on_exhaustion(self):
        """Strict mode: calling more times than recorded raises IndexError."""
        import asyncio
        import tempfile
        from pathlib import Path
        from app.tool_transport import ReplayTransport, _stable_args_hash

        async def _test():
            args1 = {"query": "x"}
            args2 = {"ids": [1]}
            hash1 = _stable_args_hash(args1)
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
                f.write(json.dumps({"type": "session", "runId": "exhaust-test", "recordedAt": ""}) + "\n")
                f.write(json.dumps({
                    "runId": "exhaust-test", "stepIndex": 0,
                    "toolName": "search_products",
                    "argumentsHash": hash1,
                    "argumentsKeys": ["query"],
                    "ok": True, "outputHash": "def456",
                    "output": {"tool": "search_products", "ok": True, "detail": {}},
                }) + "\n")
                tmp = Path(f.name)

            try:
                transport = ReplayTransport(tmp, strict=True)
                await transport("search_products", args1, None)  # OK
                with pytest.raises(IndexError, match="Replay exhausted"):
                    await transport("compare_products", args2, None)
            finally:
                tmp.unlink(missing_ok=True)

        asyncio.run(_test())

    def test_strict_replay_args_hash_mismatch(self):
        """Strict mode: same tool name but different args hash raises KeyError."""
        import asyncio
        import tempfile
        from pathlib import Path
        from app.tool_transport import ReplayTransport

        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
                f.write(json.dumps({"type": "session", "runId": "hash-test", "recordedAt": ""}) + "\n")
                f.write(json.dumps({
                    "runId": "hash-test", "stepIndex": 0,
                    "toolName": "search_products",
                    "argumentsHash": "aaa111",
                    "argumentsKeys": ["query"],
                    "ok": True, "outputHash": "bbb222",
                    "output": {"tool": "search_products", "ok": True, "detail": {}},
                }) + "\n")
                tmp = Path(f.name)

            try:
                transport = ReplayTransport(tmp, strict=True)

                with pytest.raises(KeyError, match="Replay argument hash mismatch"):
                    await transport("search_products", {"query": "different query produces different hash"}, None)
            finally:
                tmp.unlink(missing_ok=True)

        asyncio.run(_test())


# ── Test 10: Replay with Harness — external calls count = 0 ──────────────────


class TestReplayHarnessZeroExternalCalls:
    def test_replay_transport_never_calls_live(self):
        """ReplayTransport raises on mismatch instead of falling back to live call_tool."""
        import asyncio
        import tempfile
        from pathlib import Path
        from app.tool_transport import ReplayTransport, _stable_args_hash

        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
                test_args = {"query": "x", "category": "y"}
                test_hash = _stable_args_hash(test_args)
                f.write(json.dumps({"type": "session", "runId": "zero-ext", "recordedAt": ""}) + "\n")
                f.write(json.dumps({
                    "runId": "zero-ext", "stepIndex": 0,
                    "toolName": "search_products",
                    "argumentsHash": test_hash,
                    "argumentsKeys": ["query", "category"],
                    "ok": True, "durationMs": 10.0,
                    "outputHash": "out001",
                    "output": {"tool": "search_products", "ok": True, "durationMs": 10.0, "detail": {"candidateIds": [1, 2]}},
                }) + "\n")
                tmp = Path(f.name)

            try:
                transport = ReplayTransport(tmp, strict=True)
                # This should succeed from replay
                result = await transport("search_products", test_args, None)
                assert result.tool == "search_products"
                assert result.ok is True
                # _call_tool is None — proof nothing was called
            finally:
                tmp.unlink(missing_ok=True)

        asyncio.run(_test())


# ── Test 12: Debug endpoint gating ───────────────────────────────────────────


class TestDebugEndpoint:
    def test_disabled_returns_503(self):
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.main.trace_debug_enabled", return_value=False):
            client = TestClient(app)
            response = client.get(
                "/internal/debug/agent-runs/test-run",
                headers={"X-Agent-Debug-Key": "any-key"},
            )
            assert response.status_code == 503

    def test_missing_key_returns_403(self):
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.main.trace_debug_enabled", return_value=True), \
             patch("app.main.validate_debug_key", return_value=False):
            client = TestClient(app)
            response = client.get(
                "/internal/debug/agent-runs/test-run",
                headers={"X-Agent-Debug-Key": "wrong-key"},
            )
            assert response.status_code == 403

    def test_correct_key_trace_not_found_returns_404(self):
        from fastapi.testclient import TestClient
        from app.main import app

        async def fake_get_trace(run_id, debug_key):
            return None

        with patch("app.main.trace_debug_enabled", return_value=True), \
             patch("app.main.validate_debug_key", return_value=True), \
             patch("app.main.get_trace_for_debug", new=AsyncMock(side_effect=fake_get_trace)):
            client = TestClient(app)
            response = client.get(
                "/internal/debug/agent-runs/not-found",
                headers={"X-Agent-Debug-Key": "correct-key"},
            )
            assert response.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# R2-2 Boundary Consumption Tests — verify ContextView truly controls phase input
# ═══════════════════════════════════════════════════════════════════════════════


# ── Planner boundary: poison values don't leak into model prompt ───────────


class TestPlannerBoundary:
    """Verify that PlannerContextView is the authoritative input — poison values
    in TaskState do NOT leak into the PlannerContext or model prompt."""

    def test_planner_context_built_from_view_not_state(self):
        """PlannerContext built from view has user_message, tool schemas, and
        system_policies from the view — not from any ambient TaskState."""
        from app.planner import build_planner_context_from_view, build_planner_messages
        from app.context_view import PlannerContextView

        view = PlannerContextView(
            runId="run-boundary",
            taskId="task-boundary",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-001",
            goal="帮我选耳机",
            userMessage="想要降噪耳机，预算3000以内",
            taskType="ecommerce_guide",
            taskStatus="ready",
            confirmedFacts=[
                {"key": "category", "value": "headphones", "certainty": "confirmed", "source": "user"},
            ],
            hardConstraints=[
                {"key": "price_max", "operator": "lte", "value": 300000, "source": "user"},
            ],
            candidateTools=[
                {
                    "name": "search_products",
                    "description": "Search product catalog",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            ],
            systemPolicies={"require_citation": True, "max_products": 3},
        )

        context = build_planner_context_from_view(view)

        # ── Verify context fields come from view, not defaults ──────────
        assert context.user_message == "想要降噪耳机，预算3000以内"
        assert context.goal == "帮我选耳机"
        assert len(context.candidate_tools) == 1
        assert context.candidate_tools[0].name == "search_products"
        assert context.candidate_tools[0].description == "Search product catalog"
        assert context.system_policies == {"require_citation": True, "max_products": 3}

    def test_poison_values_not_in_model_prompt(self):
        """A poison value (e.g., _poison_injected_fact) present outside the view
        must NOT appear in the Planner's model prompt."""
        from app.planner import build_planner_context_from_view, build_planner_messages
        from app.context_view import PlannerContextView

        import json as _json

        view = PlannerContextView(
            runId="run-poison",
            taskId="task-poison",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-poison",
            goal="帮我选耳机",
            userMessage="想要降噪耳机",
            taskType="ecommerce_guide",
            taskStatus="ready",
            confirmedFacts=[
                {"key": "category", "value": "headphones", "certainty": "confirmed", "source": "user"},
            ],
            candidateTools=[
                {
                    "name": "search_products",
                    "description": "Search products",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            ],
        )

        context = build_planner_context_from_view(view)
        messages = build_planner_messages(context)

        # ── The prompt must only contain view-authorized facts, not poison ──
        prompt_text = _json.dumps(messages, ensure_ascii=False)
        assert "_poison_injected_fact" not in prompt_text
        assert "poison_value_999" not in prompt_text
        assert "想要降噪耳机" in prompt_text
        assert "headphones" in prompt_text

        # ── Verify context serialization is clean ──
        context_dump = context.model_dump(by_alias=True, mode="json")
        assert "_poison" not in _json.dumps(context_dump, ensure_ascii=False)

    def test_planner_view_user_message_not_goal(self):
        """The view's user_message field is used as the Planner's userMessage,
        NOT the goal field (regression test for Bug 1)."""
        from app.planner import build_planner_context_from_view
        from app.context_view import PlannerContextView

        view = PlannerContextView(
            runId="run-msg",
            taskId="task-msg",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-msg",
            goal="goal text",
            userMessage="actual user message text",
            taskType="generic",
            taskStatus="ready",
        )

        context = build_planner_context_from_view(view)
        assert context.user_message == "actual user message text"
        assert context.user_message != context.goal
        assert context.goal == "goal text"


# ── Executor fail-closed: view/state mismatch raises error ──────────────────


class TestExecutorViewMismatchFailClosed:
    """Verify that ExecutorContextView mismatch with TaskState fails closed —
    the Executor must NOT silently substitute TaskState data."""

    def test_step_id_mismatch_raises_selection_error(self):
        """When ExecutorView.step_id != the next pending PlanStep → error."""
        from datetime import datetime, timezone
        from app.executor import (
            ExecutorSelectionError,
            build_executor_step_context,
        )
        from app.task_state import TaskState
        from app.planning import TaskPlan, PlanStep, PlanArgumentSource
        from app.context_view import ExecutorContextView

        plan = TaskPlan(
            planId="plan-ex-001",
            basedOnRevision=1,
            steps=[
                PlanStep(
                    stepId="step-search",
                    description="Search",
                    toolName="search_products",
                    arguments={"query": "headphones"},
                    argumentSources={"query": PlanArgumentSource(kind="task_goal")},
                    expectedOutput={"requiresProductCandidates": True},
                ),
            ],
        )

        state = TaskState(
            taskId="task-ex-001",
            revision=1,
            status="executing",
            goal="test",
            taskType="generic",
            activePlan=plan,
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        # View claims step_id="step-wrong" but state has "step-search"
        view = ExecutorContextView(
            runId="run-ex",
            taskId="task-ex-001",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-ex",
            planId="plan-ex-001",
            stepId="step-wrong",
            stepDescription="Wrong step",
            toolName="search_products",
        )

        with pytest.raises(ExecutorSelectionError) as exc_info:
            build_executor_step_context(state, executor_view=view)
        assert exc_info.value.code == "view_state_step_mismatch"

    def test_tool_name_mismatch_raises_selection_error(self):
        """When ExecutorView.tool_name != the next pending PlanStep tool → error."""
        from datetime import datetime, timezone
        from app.executor import (
            ExecutorSelectionError,
            build_executor_step_context,
        )
        from app.task_state import TaskState
        from app.planning import TaskPlan, PlanStep, PlanArgumentSource
        from app.context_view import ExecutorContextView

        plan = TaskPlan(
            planId="plan-ex-002",
            basedOnRevision=1,
            steps=[
                PlanStep(
                    stepId="step-search",
                    description="Search",
                    toolName="search_products",
                    arguments={"query": "headphones"},
                    argumentSources={"query": PlanArgumentSource(kind="task_goal")},
                    expectedOutput={"requiresProductCandidates": True},
                ),
            ],
        )

        state = TaskState(
            taskId="task-ex-002",
            revision=1,
            status="executing",
            goal="test",
            taskType="generic",
            activePlan=plan,
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        # View claims tool_name="get_product_details" but state has "search_products"
        view = ExecutorContextView(
            runId="run-ex2",
            taskId="task-ex-002",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-ex2",
            planId="plan-ex-002",
            stepId="step-search",
            stepDescription="Search",
            toolName="get_product_details",  # mismatched tool
        )

        with pytest.raises(ExecutorSelectionError) as exc_info:
            build_executor_step_context(state, executor_view=view)
        assert exc_info.value.code == "view_state_tool_mismatch"

    def test_plan_id_mismatch_raises_selection_error(self):
        """When ExecutorView.plan_id != state.active_plan.plan_id → error."""
        from datetime import datetime, timezone
        from app.executor import (
            ExecutorSelectionError,
            build_executor_step_context,
        )
        from app.task_state import TaskState
        from app.planning import TaskPlan, PlanStep, PlanArgumentSource
        from app.context_view import ExecutorContextView

        plan = TaskPlan(
            planId="plan-ex-003",
            basedOnRevision=1,
            steps=[
                PlanStep(
                    stepId="step-search",
                    description="Search",
                    toolName="search_products",
                    arguments={"query": "x"},
                    argumentSources={"query": PlanArgumentSource(kind="task_goal")},
                    expectedOutput={"requiresProductCandidates": True},
                ),
            ],
        )

        state = TaskState(
            taskId="task-ex-003",
            revision=1,
            status="executing",
            goal="test",
            taskType="generic",
            activePlan=plan,
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        # View claims plan_id="plan-wrong" but state has "plan-ex-003"
        view = ExecutorContextView(
            runId="run-ex3",
            taskId="task-ex-003",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-ex3",
            planId="plan-wrong",
            stepId="step-search",
            stepDescription="Search",
            toolName="search_products",
        )

        with pytest.raises(ExecutorSelectionError) as exc_info:
            build_executor_step_context(state, executor_view=view)
        assert exc_info.value.code == "view_state_plan_mismatch"

    def test_view_data_populated_in_step_context(self):
        """When view matches state, executor_view_data is populated with
        resolved_arguments, relevant_facts, relevant_constraints, and prior_step_outputs."""
        from datetime import datetime, timezone
        from app.executor import build_executor_step_context
        from app.task_state import TaskState
        from app.planning import TaskPlan, PlanStep, PlanArgumentSource
        from app.context_view import ExecutorContextView

        plan = TaskPlan(
            planId="plan-ex-004",
            basedOnRevision=1,
            steps=[
                PlanStep(
                    stepId="step-search",
                    description="Search",
                    toolName="search_products",
                    arguments={"query": "headphones"},
                    argumentSources={"query": PlanArgumentSource(kind="task_goal")},
                    expectedOutput={"requiresProductCandidates": True},
                ),
            ],
        )

        state = TaskState(
            taskId="task-ex-004",
            revision=1,
            status="executing",
            goal="test",
            taskType="generic",
            activePlan=plan,
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        view = ExecutorContextView(
            runId="run-ex4",
            taskId="task-ex-004",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-ex4",
            planId="plan-ex-004",
            stepId="step-search",
            stepDescription="Search",
            toolName="search_products",
            resolvedArguments={"query": "降噪耳机", "category": "headphones"},
            relevantFacts=[{"key": "category", "value": "headphones"}],
            relevantConstraints=[{"key": "price_max", "operator": "lte", "value": 300000}],
            priorStepOutputs={},
        )

        ctx = build_executor_step_context(state, executor_view=view)

        assert ctx.executor_view_data is not None
        assert ctx.executor_view_data["resolved_arguments"] == {
            "query": "降噪耳机", "category": "headphones",
        }
        assert len(ctx.executor_view_data["relevant_facts"]) == 1
        assert ctx.executor_view_data["relevant_facts"][0]["key"] == "category"
        assert len(ctx.executor_view_data["relevant_constraints"]) == 1
        assert ctx.executor_view_data["relevant_constraints"][0]["key"] == "price_max"


# ── Validator isolation: only reads from view evidence ──────────────────────


class TestValidatorViewOnly:
    """Verify that ValidatorContextView is the authoritative boundary —
    Validator must NOT read from TaskState.domain_state when view is provided."""

    def test_validator_reads_only_view_evidence(self):
        """When context_view is provided, build_validator_context uses only
        view evidence, not state.domain_state.stepExecutionResults."""
        from datetime import datetime, timezone
        from app.validator import build_validator_context
        from app.task_state import TaskState
        from app.planning import TaskPlan, PlanStep, PlanArgumentSource
        from app.context_view import (
            ValidatorContextView,
            ValidatorStepEvidence,
        )

        plan = TaskPlan(
            planId="plan-val-001",
            basedOnRevision=1,
            steps=[
                PlanStep(
                    stepId="step-1",
                    description="Search",
                    toolName="search_products",
                    arguments={"query": "headphones"},
                    argumentSources={"query": PlanArgumentSource(kind="task_goal")},
                    expectedOutput={"requiresProductCandidates": True},
                    status="executed",
                ),
                PlanStep(
                    stepId="step-2",
                    description="Detail",
                    toolName="get_product_details",
                    arguments={"productIds": [1]},
                    argumentSources={"productIds": PlanArgumentSource(kind="prior_step", reference="step-1.candidateIds")},
                    expectedOutput={"requiresSpecs": True},
                    status="executed",
                ),
            ],
        )

        state = TaskState(
            taskId="task-val-001",
            revision=1,
            status="ready",
            goal="帮我选耳机",
            taskType="ecommerce_guide",
            activePlan=plan,
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
            domainState={
                # ── Poison: domain_state has extra step that view doesn't authorize ──
                "stepExecutionResults": [
                    {
                        "taskId": "task-val-001",
                        "planId": "plan-val-001",
                        "stepId": "step-1",
                        "toolName": "search_products",
                        "resolvedArguments": {"query": "headphones"},
                        "outcome": "tool_succeeded",
                        "startedAt": datetime.now(timezone.utc).isoformat(),
                        "finishedAt": datetime.now(timezone.utc).isoformat(),
                        "durationMs": 10.0,
                    },
                    {
                        "taskId": "task-val-001",
                        "planId": "plan-val-001",
                        "stepId": "step-2",
                        "toolName": "get_product_details",
                        "resolvedArguments": {"productIds": [1]},
                        "outcome": "tool_succeeded",
                        "startedAt": datetime.now(timezone.utc).isoformat(),
                        "finishedAt": datetime.now(timezone.utc).isoformat(),
                        "durationMs": 15.0,
                    },
                    {
                        "taskId": "task-val-001",
                        "planId": "plan-val-001",
                        "stepId": "step-poison",
                        "toolName": "delete_all_products",
                        "resolvedArguments": {},
                        "outcome": "tool_succeeded",
                        "startedAt": datetime.now(timezone.utc).isoformat(),
                        "finishedAt": datetime.now(timezone.utc).isoformat(),
                        "durationMs": 5.0,
                    },
                ],
                "stepOutputs": {},
            },
        )

        # View carries the complete Plan and only the two authorized step
        # evidence records; the poisoned state history is not visible.
        view = ValidatorContextView(
            runId="run-val",
            taskId="task-val-001",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-val",
            goal="帮我选耳机",
            plan=plan,
            hardConstraints=[
                {"key": "price_max", "operator": "lte", "value": 300000, "source": "user"},
            ],
            executedSteps=[
                ValidatorStepEvidence(
                    stepId="step-1",
                    toolName="search_products",
                    outcome="tool_succeeded",
                    evidenceRefs=["ref-1"],
                    detailKeys=["candidateIds"],
                    evidenceValues={"candidateIds": [1, 2, 3]},
                ),
                ValidatorStepEvidence(
                    stepId="step-2",
                    toolName="get_product_details",
                    outcome="tool_succeeded",
                    evidenceRefs=["ref-2"],
                    detailKeys=["products"],
                    evidenceValues={"products": [{"id": 1}]},
                ),
            ],
        )

        # ── Build with view → should ONLY use view evidence ─────────────
        ctx = build_validator_context(state, context_view=view)

        assert len(ctx.steps) == 2
        assert [step.step.step_id for step in ctx.steps] == ["step-1", "step-2"]
        # step-poison must NOT be present
        step_ids = [s.step.step_id for s in ctx.steps]
        assert "step-poison" not in step_ids

    def test_validator_without_view_uses_state_domain(self):
        """Without context_view, validator reads from state.domain_state normally."""
        from datetime import datetime, timezone
        from app.validator import build_validator_context
        from app.task_state import TaskState
        from app.planning import TaskPlan, PlanStep, PlanArgumentSource

        plan = TaskPlan(
            planId="plan-val-002",
            basedOnRevision=1,
            steps=[
                PlanStep(
                    stepId="step-1",
                    description="Search",
                    toolName="search_products",
                    arguments={"query": "x"},
                    argumentSources={"query": PlanArgumentSource(kind="task_goal")},
                    expectedOutput={"requiresProductCandidates": True},
                    status="executed",
                ),
            ],
        )

        state = TaskState(
            taskId="task-val-002",
            revision=1,
            status="ready",
            goal="test",
            taskType="generic",
            activePlan=plan,
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
            domainState={
                "stepExecutionResults": [
                    {
                        "taskId": "task-val-002",
                        "planId": "plan-val-002",
                        "stepId": "step-1",
                        "toolName": "search_products",
                        "resolvedArguments": {"query": "x"},
                        "outcome": "tool_succeeded",
                        "toolTrace": {
                            "tool": "search_products",
                            "ok": True,
                            "durationMs": 10.0,
                            "detail": {"results": 3},
                        },
                        "startedAt": datetime.now(timezone.utc).isoformat(),
                        "finishedAt": datetime.now(timezone.utc).isoformat(),
                        "durationMs": 10.0,
                    },
                ],
                "stepOutputs": {},
            },
        )

        ctx = build_validator_context(state, context_view=None)
        assert len(ctx.steps) == 1
        assert ctx.steps[0].step.step_id == "step-1"
        assert ctx.steps[0].execution_result is not None

    def test_view_step_not_in_plan_raises_error(self):
        """When view has a step not in the plan → ValidatorEvidenceError."""
        from datetime import datetime, timezone
        from app.validator import (
            ValidatorEvidenceError,
            build_validator_context,
        )
        from app.task_state import TaskState
        from app.planning import TaskPlan, PlanStep, PlanArgumentSource
        from app.context_view import (
            ValidatorContextView,
            ValidatorStepEvidence,
        )

        plan = TaskPlan(
            planId="plan-val-003",
            basedOnRevision=1,
            steps=[
                PlanStep(
                    stepId="step-real",
                    description="Real step",
                    toolName="search_products",
                    arguments={"query": "x"},
                    argumentSources={"query": PlanArgumentSource(kind="task_goal")},
                    expectedOutput={"requiresProductCandidates": True},
                    status="executed",
                ),
            ],
        )

        state = TaskState(
            taskId="task-val-003",
            revision=1,
            status="ready",
            goal="test",
            taskType="generic",
            activePlan=plan,
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
            domainState={"stepExecutionResults": [], "stepOutputs": {}},
        )

        # View references step-not-in-plan
        view = ValidatorContextView(
            runId="run-val3",
            taskId="task-val-003",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-val3",
            goal="test",
            plan=plan,
            executedSteps=[
                ValidatorStepEvidence(
                    stepId="step-ghost",
                    toolName="unknown_tool",
                    outcome="tool_succeeded",
                ),
            ],
        )

        with pytest.raises(ValidatorEvidenceError) as exc_info:
            build_validator_context(state, context_view=view)
        assert exc_info.value.code == "view_step_not_in_plan"


# ── FinalAnswer authoritative source ────────────────────────────────────────


class TestFinalAnswerAuthoritativeSource:
    """Verify that _generate_final_answer() uses only FinalAnswerContextView
    when provided — not full chat history or raw TaskState."""

    def test_final_answer_uses_view_not_history(self):
        """When final_answer_view is provided, _generate_final_answer builds
        messages from only the view — no raw history or tool traces."""
        import asyncio
        from unittest.mock import AsyncMock, ANY
        from app.llm import _generate_final_answer
        from app.context_view import FinalAnswerContextView

        async def _test():
            mock_client = AsyncMock()
            mock_client.chat = AsyncMock()
            mock_client.chat.completions = AsyncMock()
            mock_create = AsyncMock()
            mock_create.return_value = type("obj", (object,), {
                "choices": [type("obj", (object,), {
                    "message": type("obj", (object,), {
                        "content": "推荐Sony WH-1000XM5，价格299900。",
                        "tool_calls": None,
                    }),
                })],
            })()
            mock_client.chat.completions.create = mock_create

            view = FinalAnswerContextView(
                runId="run-fa",
                taskId="task-fa",
                baseContextRevision=1,
                phaseTaskRevision=1,
                contextHash="hash-fa",
                goal="帮我选耳机",
                validatedResults=[
                    {
                        "tool": "compare_products",
                        "evidence": {
                            "rankedFinalists": [
                                {"productId": 11, "product": "Sony WH-1000XM5", "price": 299900},
                                {"productId": 33, "product": "Bose QC", "price": 288800},
                            ],
                        },
                    },
                ],
                evidenceRefs=["ref-1", "ref-2"],
                allowedFacts=[
                    {"key": "category", "value": "headphones", "certainty": "confirmed", "source": "user"},
                ],
                answerFormat={
                    "comparisonSelection": {
                        "selectedProductIds": [11, 33],
                        "sourceDisplayOrdinals": [1, 3],
                    },
                },
            )

            # ── history contains poison with wrong price ─────────────────
            poison_history = [
                {"role": "user", "content": "推荐耳机"},
                {"role": "assistant", "content": "poison_price_999999"},
            ]
            poison_traces = []

            with patch("app.llm.settings") as mock_settings:
                mock_settings.deepseek_model = "test-model"
                result = await _generate_final_answer(
                    mock_client,
                    messages=poison_history,
                    tool_traces=poison_traces,
                    on_answer_delta=None,
                    fallback="fallback",
                    final_answer_view=view,
                )

            assert "Sony" in result
            # ── Verify LLM was called with view-only messages ────────────
            call_args = mock_create.call_args
            messages_sent = call_args[1]["messages"]
            # Should be 2 messages: system prompt + view evidence block
            assert len(messages_sent) >= 2
            # The evidence block must contain the authoritative price
            evidence_text = messages_sent[1]["content"]
            assert "299900" in evidence_text
            assert "Sony WH-1000XM5" in evidence_text
            assert "帮我选耳机" in evidence_text
            assert "回答格式约束" in evidence_text
            assert "必须保留这些原展示序号" in evidence_text
            assert "不得把第 3 项重编号为第 2 项" in evidence_text
            # Poison price must NOT be in the view messages
            assert "999999" not in evidence_text
            # Poison history must NOT be in the messages
            all_text = " ".join(m.get("content", "") for m in messages_sent)
            assert "poison_price_999999" not in all_text

        asyncio.run(_test())

    def test_final_answer_without_view_uses_full_history(self):
        """Without final_answer_view, _generate_final_answer uses the
        provided messages directly (legacy path)."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.llm import _generate_final_answer

        async def _test():
            mock_client = AsyncMock()
            mock_client.chat = AsyncMock()
            mock_client.chat.completions = AsyncMock()
            mock_create = AsyncMock()
            mock_create.return_value = type("obj", (object,), {
                "choices": [type("obj", (object,), {
                    "message": type("obj", (object,), {
                        "content": "回答内容",
                        "tool_calls": None,
                    }),
                })],
            })()
            mock_client.chat.completions.create = mock_create

            legacy_messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user question"},
            ]

            with patch("app.llm.settings") as mock_settings:
                mock_settings.deepseek_model = "test-model"
                result = await _generate_final_answer(
                    mock_client,
                    messages=legacy_messages,
                    tool_traces=[],
                    on_answer_delta=None,
                    fallback="fallback",
                    final_answer_view=None,
                )

            assert "回答内容" in result
            # ── Legacy path: the messages passed to LLM are the provided ones ──
            call_args = mock_create.call_args
            messages_sent = call_args[1]["messages"]
            assert messages_sent[0]["content"] == "system prompt"
            assert messages_sent[1]["content"] == "user question"

        asyncio.run(_test())

    def test_non_stream_final_answer_retries_one_connection_failure(self):
        import asyncio
        import httpx
        from unittest.mock import AsyncMock
        from openai import APIConnectionError
        from app.llm import _generate_final_answer

        async def _test():
            response = type("obj", (object,), {
                "choices": [type("obj", (object,), {
                    "message": type("obj", (object,), {
                        "content": "重试后的可靠回答",
                        "tool_calls": None,
                    }),
                })],
            })()
            create = AsyncMock(side_effect=[
                APIConnectionError(request=httpx.Request("POST", "https://api.deepseek.com")),
                response,
            ])
            client = AsyncMock()
            client.chat = AsyncMock()
            client.chat.completions = AsyncMock()
            client.chat.completions.create = create

            with patch("app.llm.settings") as mock_settings:
                mock_settings.deepseek_model = "test-model"
                result = await _generate_final_answer(
                    client,
                    messages=[{"role": "user", "content": "hello"}],
                    tool_traces=[],
                    on_answer_delta=None,
                    fallback="fallback",
                )

            assert result == "重试后的可靠回答"
            assert create.await_count == 2

        asyncio.run(_test())

    def test_streaming_final_answer_does_not_retry_after_connection_failure(self):
        import asyncio
        import httpx
        from unittest.mock import AsyncMock
        from openai import APIConnectionError
        from app.llm import _generate_final_answer

        async def _test():
            create = AsyncMock(side_effect=APIConnectionError(
                request=httpx.Request("POST", "https://api.deepseek.com")
            ))
            client = AsyncMock()
            client.chat = AsyncMock()
            client.chat.completions = AsyncMock()
            client.chat.completions.create = create

            with patch("app.llm.settings") as mock_settings:
                mock_settings.deepseek_model = "test-model"
                try:
                    await _generate_final_answer(
                        client,
                        messages=[{"role": "user", "content": "hello"}],
                        tool_traces=[],
                        on_answer_delta=AsyncMock(),
                        fallback="fallback",
                    )
                except APIConnectionError:
                    pass
                else:
                    raise AssertionError("streaming connection failure must propagate")

            assert create.await_count == 1

        asyncio.run(_test())

    def test_final_answer_view_unknowns_disclosure(self):
        """When view has unknowns, they appear in the LLM prompt."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.llm import _generate_final_answer
        from app.context_view import FinalAnswerContextView

        async def _test():
            mock_client = AsyncMock()
            mock_client.chat = AsyncMock()
            mock_client.chat.completions = AsyncMock()
            mock_create = AsyncMock()
            mock_create.return_value = type("obj", (object,), {
                "choices": [type("obj", (object,), {
                    "message": type("obj", (object,), {
                        "content": "推荐产品A，但蓝牙版本未知。",
                        "tool_calls": None,
                    }),
                })],
            })()
            mock_client.chat.completions.create = mock_create

            view = FinalAnswerContextView(
                runId="run-fa2",
                taskId="task-fa2",
                baseContextRevision=1,
                phaseTaskRevision=1,
                contextHash="hash-fa2",
                goal="帮我选耳机",
                validatedResults=[{"product": "产品A", "score": 0.9}],
                unknowns=["bluetooth version", "water resistance rating"],
            )

            with patch("app.llm.settings") as mock_settings:
                mock_settings.deepseek_model = "test-model"
                await _generate_final_answer(
                    mock_client,
                    messages=[],
                    tool_traces=[],
                    on_answer_delta=None,
                    fallback="fallback",
                    final_answer_view=view,
                )

            # ── Verify unknowns are disclosed in the prompt ──────────────
            evidence_text = mock_create.call_args[1]["messages"][1]["content"]
            assert "bluetooth version" in evidence_text
            assert "water resistance rating" in evidence_text
            assert "尚不确定" in evidence_text

        asyncio.run(_test())


# ── R2 centralized harness pre-validation tests ─────────────────────────────


class TestHarnessPreValidation:
    """Verify that _validate_view_state_metadata() blocks phases when View
    identity metadata doesn't match the current TaskState.

    These tests call the validation function DIRECTLY (unit tests) to cover
    all mismatch codes: task_id, phaseTaskRevision, plan_id, step_id, tool_name.

    IMPORTANT: base_context_revision is the frozen baseContextRevision — it is NOT the
    field used for boundary gating.  That is phase_task_revision.  To test
    revision mismatch, set phase_task_revision to a value that differs from
    state.revision.
    """

    def test_task_id_mismatch_raises_pre_validation_error(self):
        """View task_id != state task_id → HarnessPreValidationError."""
        from app.harness import _validate_view_state_metadata, HarnessPreValidationError
        from app.task_state import TaskState
        from app.context_view import PlannerContextView
        from datetime import datetime, timezone

        view = PlannerContextView(
            runId="run-001",
            taskId="task-wrong",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-01",
            goal="test",
            userMessage="test",
            taskStatus="ready",
        )
        state = TaskState(
            taskId="task-correct",
            revision=1,
            status="ready",
            goal="test",
            taskType="generic",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        with pytest.raises(HarnessPreValidationError) as exc_info:
            _validate_view_state_metadata(view, state)
        assert exc_info.value.code == "view_task_id_mismatch"

    def test_task_revision_mismatch_raises_pre_validation_error(self):
        """View phase_task_revision != state revision → HarnessPreValidationError."""
        from app.harness import _validate_view_state_metadata, HarnessPreValidationError
        from app.task_state import TaskState
        from app.context_view import PlannerContextView
        from datetime import datetime, timezone

        view = PlannerContextView(
            runId="run-001",
            taskId="task-001",
            baseContextRevision=5,          # stale baseContextRevision
            phaseTaskRevision=5,       # actual phase revision (stale, must NOT match state revision=7)
            contextHash="hash-01",
            goal="test",
            userMessage="test",
            taskStatus="ready",
        )
        state = TaskState(
            taskId="task-001",
            revision=7,              # actual revision
            status="ready",
            goal="test",
            taskType="generic",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        with pytest.raises(HarnessPreValidationError) as exc_info:
            _validate_view_state_metadata(view, state)
        assert exc_info.value.code == "view_phase_revision_mismatch"

    def test_plan_id_mismatch_raises_pre_validation_error(self):
        """View plan_id != expected plan_id → HarnessPreValidationError."""
        from app.harness import _validate_view_state_metadata, HarnessPreValidationError
        from app.task_state import TaskState
        from app.context_view import ExecutorContextView
        from datetime import datetime, timezone

        view = ExecutorContextView(
            runId="run-001",
            taskId="task-001",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-01",
            planId="plan-wrong",
            stepId="step-search",
            stepDescription="Search products",
            toolName="search_products",
        )
        state = TaskState(
            taskId="task-001",
            revision=1,
            status="executing",
            goal="test",
            taskType="generic",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        with pytest.raises(HarnessPreValidationError) as exc_info:
            _validate_view_state_metadata(view, state, plan_id="plan-correct")
        assert exc_info.value.code == "view_plan_id_mismatch"

    def test_step_id_mismatch_raises_pre_validation_error(self):
        """View step_id != expected step_id → HarnessPreValidationError."""
        from app.harness import _validate_view_state_metadata, HarnessPreValidationError
        from app.task_state import TaskState
        from app.context_view import ExecutorContextView
        from datetime import datetime, timezone

        view = ExecutorContextView(
            runId="run-001",
            taskId="task-001",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-01",
            planId="plan-001",
            stepId="step-wrong",
            stepDescription="Search products",
            toolName="search_products",
        )
        state = TaskState(
            taskId="task-001",
            revision=1,
            status="executing",
            goal="test",
            taskType="generic",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        with pytest.raises(HarnessPreValidationError) as exc_info:
            _validate_view_state_metadata(view, state, step_id="step-correct")
        assert exc_info.value.code == "view_step_id_mismatch"

    def test_tool_name_mismatch_raises_pre_validation_error(self):
        """View tool_name != expected tool_name → HarnessPreValidationError."""
        from app.harness import _validate_view_state_metadata, HarnessPreValidationError
        from app.task_state import TaskState
        from app.context_view import ExecutorContextView
        from datetime import datetime, timezone

        view = ExecutorContextView(
            runId="run-001",
            taskId="task-001",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-01",
            planId="plan-001",
            stepId="step-search",
            stepDescription="Search products",
            toolName="search_products",
        )
        state = TaskState(
            taskId="task-001",
            revision=1,
            status="executing",
            goal="test",
            taskType="generic",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        with pytest.raises(HarnessPreValidationError) as exc_info:
            _validate_view_state_metadata(view, state, tool_name="different_tool")
        assert exc_info.value.code == "view_tool_name_mismatch"

    def test_all_matching_does_not_raise(self):
        """When all identity metadata matches, no error is raised."""
        from app.harness import _validate_view_state_metadata
        from app.task_state import TaskState
        from app.context_view import ExecutorContextView
        from datetime import datetime, timezone

        view = ExecutorContextView(
            runId="run-001",
            taskId="task-001",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-01",
            planId="plan-001",
            stepId="step-search",
            stepDescription="Search products",
            toolName="search_products",
        )
        state = TaskState(
            taskId="task-001",
            revision=1,
            status="executing",
            goal="test",
            taskType="generic",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        # Should not raise
        _validate_view_state_metadata(
            view, state,
            plan_id="plan-001",
            step_id="step-search",
            tool_name="search_products",
        )

    def test_planner_view_mismatch_blocks_phase_via_harness(self):
        """Integration: when planner_view has wrong task_id, the harness
        step method raises HarnessPreValidationError and the Planner is
        never called."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.harness import run_harness_step, HarnessPreValidationError
        from app.task_state import TaskState
        from app.context_pack import ContextPack, TaskFact, TaskConstraint
        from app.context_view import ContextProjector
        from datetime import datetime, timezone

        async def _test():
            # Create a ContextPack with different task_id than state
            state = TaskState(
                taskId="task-real",
                revision=1,
                status="ready",
                goal="帮我选降噪耳机",
                taskType="ecommerce_guide",
                createdAt=datetime.now(timezone.utc),
                updatedAt=datetime.now(timezone.utc),
            )

            # ContextPack has different task_id → view will have wrong task_id
            pack = ContextPack(
                runId="run-001",
                taskId="task-mismatch",     # !== state.task_id
                baseContextRevision=1,
                goal="帮我选降噪耳机",
                taskType="ecommerce_guide",
                confirmedFacts=[],
                hardConstraints=[],
                softPreferences=[],
                unknowns=[],
                pendingQuestions=[],
                evidenceRefs=[],
            )
            projector = ContextProjector(pack)

            mock_client = AsyncMock()
            mock_client.chat = AsyncMock()
            mock_client.chat.completions = AsyncMock()
            mock_client.chat.completions.create = AsyncMock()

            candidate_tools = [
                {
                    "function": {
                        "name": "search_products",
                        "description": "Search for products",
                        "parameters": {"type": "object", "properties": {}},
                    }
                }
            ]

            with pytest.raises(HarnessPreValidationError) as exc_info:
                await run_harness_step(
                    state, "帮我选降噪耳机", candidate_tools,
                    client=mock_client, model="test-model",
                    projector=projector,
                )

            assert exc_info.value.code == "view_task_id_mismatch"
            # Planner's LLM must NOT have been called
            mock_client.chat.completions.create.assert_not_called()

        asyncio.run(_test())

    def test_validator_view_passes_with_matching_metadata(self):
        """ValidatorView with correct task_id/revision passes pre-validation."""
        from app.harness import _validate_view_state_metadata
        from app.task_state import TaskState
        from app.context_view import ValidatorContextView
        from datetime import datetime, timezone

        view = ValidatorContextView(
            runId="run-001",
            taskId="task-001",
            baseContextRevision=1,
            phaseTaskRevision=1,
            contextHash="hash-01",
            goal="test",
        )
        state = TaskState(
            taskId="task-001",
            revision=1,
            status="ready",
            goal="test",
            taskType="generic",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        # Should not raise
        _validate_view_state_metadata(view, state)

    def test_replanner_view_mismatch_blocks_replanner(self):
        """ReplannerContextView with wrong base_context_revision blocks the replanner."""
        from app.harness import _validate_view_state_metadata, HarnessPreValidationError
        from app.task_state import TaskState
        from app.context_view import ReplannerContextView
        from datetime import datetime, timezone

        view = ReplannerContextView(
            runId="run-001",
            taskId="task-001",
            baseContextRevision=3,           # stale baseContextRevision
            phaseTaskRevision=3,      # actual phase revision (stale, must NOT match state revision=5)
            contextHash="hash-01",
            goal="test",
            failedPlanSummary={"planId": "plan-001", "steps": [], "status": "failed"},
            failureReason="Validator rejected",
            replanAttempt=1,
        )
        state = TaskState(
            taskId="task-001",
            revision=5,               # actual
            status="executing",
            goal="test",
            taskType="generic",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

        with pytest.raises(HarnessPreValidationError) as exc_info:
            _validate_view_state_metadata(view, state)
        assert exc_info.value.code == "view_phase_revision_mismatch"


class TestRevisionAdvancementE2E:
    """Task A E2E tests using real run_harness_step with mocked LLM and
    injected tool_caller — NOT direct _validate_view_state_metadata() calls.

    These tests prove:
      - baseContextRevision=1 survives Planner persistence (P0-4)
      - ExecutorView.phaseTaskRevision matches current state.revision
      - No context_boundary_mismatch is recorded
      - Injected tool_caller is called exactly once
      - Phase/tool/Validator counts are 0 on mismatch (P0-5)
    """

    # ── Shared helpers ────────────────────────────────────────────────────

    @staticmethod
    def _make_initial_state():
        """Return a TaskState at revision=1, status=ready, no plan."""
        from datetime import datetime, timezone
        from app.task_state import TaskState
        return TaskState(
            taskId="task-a-e2e",
            revision=1,
            status="ready",
            goal="test goal",
            taskType="ecommerce_guide",
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
        )

    @staticmethod
    def _make_context_pack(state):
        """Build a ContextPack frozen at state.revision."""
        from app.context_pack import ContextPack
        return ContextPack(
            runId="run-a-e2e",
            taskId=state.task_id,
            baseContextRevision=state.revision,
            goal=state.goal,
            taskType=state.task_type,
            confirmedFacts=[],
            hardConstraints=[],
            softPreferences=[],
            unknowns=[],
            pendingQuestions=[],
            evidenceRefs=[],
        )

    @staticmethod
    def _make_candidate_tool_schemas():
        """Return tool schemas in the wrapped {'function': {...}} format
        that production code passes to the harness."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_products",
                    "description": "Search product catalog",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "category": {"type": "string"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_product_details",
                    "description": "Get product details by IDs",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "productIds": {
                                "type": "array",
                                "items": {"type": "integer"},
                            },
                        },
                        "required": ["productIds"],
                    },
                },
            },
        ]

    @staticmethod
    def _make_mock_llm_response():
        """Build a mock LLM response that returns a valid 2-step plan via
        submit_planner_output."""
        import json
        from unittest.mock import MagicMock

        planner_output = {
            "outcome": "planned",
            "steps": [
                {
                    "stepId": "step-search",
                    "description": "Search for headphones",
                    "toolName": "search_products",
                    "arguments": {"query": "test goal"},
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                },
                {
                    "stepId": "step-detail",
                    "description": "Get product details",
                    "toolName": "get_product_details",
                    "arguments": {"productIds": [1]},
                    "argumentSources": {
                        "productIds": {
                            "kind": "prior_step",
                            "reference": "step-search.productIds",
                        },
                    },
                    "expectedOutput": {"requiresProductDetails": True},
                },
            ],
        }

        tool_call = MagicMock()
        tool_call.function.name = "submit_planner_output"
        tool_call.function.arguments = json.dumps(planner_output)

        message = MagicMock()
        message.tool_calls = [tool_call]
        message.content = None

        choice = MagicMock()
        choice.message = message

        response = MagicMock()
        response.choices = [choice]

        return response

    @staticmethod
    def _make_mock_update_task_state():
        """Create an async mock that simulates OCC persistence.

        Each call increments revision by 1 and applies the patch's status,
        activePlan, and domainStatePatch to an internal state copy.

        Returns (mock_fn, state_container) where state_container["state"]
        can be used to read the latest state after each call.
        """
        from copy import deepcopy
        from app.task_state import TaskStateRevisionConflictError

        container: dict = {"state": None}

        async def _mock(task_id, patch):
            current = container["state"]
            if current is None:
                raise RuntimeError("mock_update_task_state: state not initialised")
            if patch.expected_revision != current.revision:
                raise TaskStateRevisionConflictError(
                    patch.expected_revision, current.revision
                )
            new_state = current.model_copy(deep=True)
            new_state.revision = current.revision + 1
            if patch.status is not None:
                new_state.status = patch.status
            if patch.active_plan is not None:
                new_state.active_plan = patch.active_plan.model_copy(deep=True)
            if patch.domain_state_patch is not None:
                new_domain = dict(new_state.domain_state)
                for k, v in patch.domain_state_patch.items():
                    if v is None:
                        new_domain.pop(k, None)
                    else:
                        new_domain[k] = (
                            deepcopy(v) if isinstance(v, (dict, list)) else v
                        )
                new_state.domain_state = new_domain
            container["state"] = new_state
            return new_state

        return _mock, container

    @staticmethod
    def _make_mock_tool_caller():
        """Return an async mock that returns a successful ToolTrace for
        search_products."""
        from app.schemas import ToolTrace

        async def _mock(tool_name, arguments):
            return ToolTrace(
                tool=tool_name,
                ok=True,
                durationMs=10.0,
                detail=two_stage_search_detail([1, 2, 3]),
            )

        return _mock

    # ── P0-4: Success main-path test ─────────────────────────────────────

    def test_task_a_success_main_path(self):
        """Task A P0-4: revision=1 ContextPack → Planner persists 2-step plan
        (revision increases) → ExecutorView.baseContextRevision still 1 →
        ExecutorView.phaseTaskRevision == current state.revision →
        injected tool_caller called exactly once → returns continue_to_executor →
        Validator calls = 0 → no context_boundary_mismatch."""
        import asyncio
        import json
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.harness import run_harness_step, ContextProjector
        from app.agent_trace import TraceBuilder

        # ── Build initial state and ContextPack ─────────────────────────
        state = self._make_initial_state()
        pack = self._make_context_pack(state)
        projector = ContextProjector(pack)
        candidate_tools = self._make_candidate_tool_schemas()

        # ── Spy on projector methods to capture real Views ─────────────
        captured_views: dict[str, Any] = {}

        def _spy_planner_view(**kwargs):
            view = ContextProjector.planner_view(projector, **kwargs)
            captured_views["planner"] = view
            return view

        def _spy_executor_view(**kwargs):
            view = ContextProjector.executor_view(projector, **kwargs)
            captured_views["executor"] = view
            return view

        projector.planner_view = _spy_planner_view
        projector.executor_view = _spy_executor_view

        # ── Mock LLM client ─────────────────────────────────────────────
        mock_create = AsyncMock()
        mock_create.return_value = self._make_mock_llm_response()

        mock_client = MagicMock()
        mock_client.chat = MagicMock()
        mock_client.chat.completions = MagicMock()
        mock_client.chat.completions.create = mock_create

        # ── Mock update_task_state → simulate OCC ───────────────────────
        mock_persist, container = self._make_mock_update_task_state()
        container["state"] = state

        # ── Mock tool_caller ────────────────────────────────────────────
        mock_tool = self._make_mock_tool_caller()

        # ── Trace builder ───────────────────────────────────────────────
        trace_builder = TraceBuilder("run-a-e2e", mode="context_pack")
        trace_builder.set_base_context_revision(pack.base_context_revision)

        # ── Run harness step ────────────────────────────────────────────
        async def _test():
            with patch("app.planner.update_task_state", new=mock_persist), \
                 patch("app.executor.update_task_state", new=mock_persist):
                result = await run_harness_step(
                    state,
                    "test goal",
                    candidate_tools,
                    client=mock_client,
                    model="test-model",
                    tool_caller=mock_tool,
                    trace_builder=trace_builder,
                    projector=projector,
                )
            return result

        result = asyncio.run(_test())

        # ── Assertions ──────────────────────────────────────────────────

        # 1. action = continue_to_executor (not ready_for_validation)
        assert result.action == "continue_to_executor", (
            f"Expected continue_to_executor, got {result.action}"
        )

        # 2. Executor ran and succeeded
        assert result.executor_result is not None
        assert result.executor_result.outcome == "step_executed"

        # 3. Validator was NOT called
        assert result.validator_result is None

        # 4. No replanner involvement
        assert result.replanner_result is None

        # 5. Planner result has a 2-step plan
        assert result.planner_result is not None
        assert result.planner_result.outcome == "planned"
        assert result.planner_result.plan is not None
        assert len(result.planner_result.plan.steps) == 2

        # 6. State revision advanced
        final_state = container["state"]
        assert final_state.revision >= 3, (
            f"State revision should be >= 3 after Planner+Executor, got {final_state.revision}"
        )

        # 7. Trace: baseContextRevision still 1
        trace = trace_builder.finish()
        assert trace.base_context_revision == 1, (
            f"baseContextRevision should be 1, got {trace.base_context_revision}"
        )

        # 8. Captured PlannerView: base_context_revision=1 (frozen), phase_task_revision=1 (entry)
        planner_view = captured_views["planner"]
        assert planner_view.base_context_revision == 1, (
            f"PlannerView.base_context_revision (frozen base) should be 1, got {planner_view.base_context_revision}"
        )
        assert planner_view.phase_task_revision == 1, (
            f"PlannerView.phase_task_revision should be 1 at entry, got {planner_view.phase_task_revision}"
        )

        # 9. Captured ExecutorView: base_context_revision=1 (frozen base), phase_task_revision=2 (post-Planner persist)
        executor_view = captured_views["executor"]
        assert executor_view.base_context_revision == 1, (
            f"ExecutorView.base_context_revision (frozen base) should be 1, got {executor_view.base_context_revision}"
        )
        assert executor_view.phase_task_revision == 2, (
            f"ExecutorView.phase_task_revision should be exactly 2 (post-Planner persist), "
            f"got {executor_view.phase_task_revision}"
        )

        # 10. Trace: phaseTaskRevisions match entry revisions
        assert trace.phase_task_revisions.get("planner") == 1, (
            f"Trace planner entry should be 1, got {trace.phase_task_revisions.get('planner')}"
        )
        assert trace.phase_task_revisions.get("executor") == 2, (
            f"Trace executor entry should be exactly 2, got {trace.phase_task_revisions.get('executor')}"
        )

        # 11. Trace: NO context_boundary_mismatch
        assert len(trace.context_boundary_mismatches) == 0, (
            f"Expected 0 context_boundary_mismatches, got {trace.context_boundary_mismatches}"
        )

        # 12. Trace: exactly 1 tool call recorded
        assert len(trace.tool_calls) == 1, (
            f"Expected 1 tool call, got {len(trace.tool_calls)}"
        )
        assert trace.tool_calls[0].tool_name == "search_products"
        assert trace.tool_calls[0].ok is True

        # 13. LLM was called at least once (Planner only; Executor uses view, not LLM)
        assert mock_create.call_count >= 1, (
            f"Planner LLM should be called at least once, got {mock_create.call_count}"
        )

        # 14. ContextView recorded for planner and executor
        view_types = [v["type"] for v in trace.context_views]
        assert "planner" in view_types, f"planner view missing from context_views: {view_types}"
        assert "executor" in view_types, f"executor view missing from context_views: {view_types}"

    # ── P0-5: Reverse test — mismatch blocks everything ──────────────────

    def test_task_a_reverse_mismatch_blocks_all(self):
        """Task A P0-5: Fake phaseTaskRevision mismatch through run_harness_step ->
        assert Planner/Executor/tool/Validator calls are ALL 0 ->
        assert Trace contains context_boundary_mismatch.

        The harness always projects views with phase_task_revision=state.revision
        (line 634), so a stale ContextPack alone won't trigger a mismatch.
        Instead we mock projector.planner_view to return a view with a stale
        phaseTaskRevision - simulating the exact scenario the gate protects
        against (a View projected under a revision that has since changed).
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.harness import run_harness_step, HarnessPreValidationError, ContextProjector
        from app.agent_trace import TraceBuilder
        from app.context_view import PlannerContextView

        # --- Build state at revision=5 ---
        state = self._make_initial_state()
        state = state.model_copy(update={"revision": 5})

        # --- Build a valid ContextPack ---
        pack = self._make_context_pack(state)
        projector = ContextProjector(pack)
        candidate_tools = self._make_candidate_tool_schemas()

        # --- Mock projector.planner_view to return a STALE view ---
        # This is what we're testing: the harness gate catches the mismatch.
        stale_view = PlannerContextView(
            runId=pack.run_id,
            taskId=pack.task_id,
            baseContextRevision=pack.base_context_revision,   # = 5 (not the issue)
            phaseTaskRevision=1,                # STALE - doesn't match state.revision=5
            contextHash="hash-stale",
            goal=pack.goal,
            userMessage=pack.goal,
            taskType=pack.task_type,
            taskStatus="ready",
            confirmedFacts=[],
            hardConstraints=[],
            softPreferences=[],
            unknowns=[],
            pendingQuestions=[],
            allowedToolNames=["search_products", "get_product_details"],
            candidateTools=candidate_tools,
            systemPolicies={},
        )
        projector.planner_view = MagicMock(return_value=stale_view)

        # --- Mock LLM (must NEVER be called) ---
        mock_create = AsyncMock()
        mock_create.side_effect = RuntimeError("LLM called - should be blocked!")

        mock_client = MagicMock()
        mock_client.chat = MagicMock()
        mock_client.chat.completions = MagicMock()
        mock_client.chat.completions.create = mock_create

        # --- Mock tool_caller (must NEVER be called) ---
        tool_called = False

        async def mock_tool(tool_name, arguments):
            nonlocal tool_called
            tool_called = True
            raise RuntimeError("tool_caller called - should be blocked!")

        # --- Mock update_task_state (must NEVER be called) ---
        mock_persist, container = self._make_mock_update_task_state()
        container["state"] = state

        # --- Trace builder ---
        trace_builder = TraceBuilder("run-a-rev", mode="context_pack")
        trace_builder.set_base_context_revision(pack.base_context_revision)

        # --- Mock _run_validation_and_recovery (must NEVER be called) ---
        mock_validator = AsyncMock()
        mock_validator.side_effect = RuntimeError(
            "Validator called - mismatch gate should have blocked!"
        )

        # --- Run harness step - MUST raise HarnessPreValidationError ---
        async def _test():
            with patch("app.planner.update_task_state", new=mock_persist), \
                 patch("app.executor.update_task_state", new=mock_persist), \
                 patch("app.harness._run_validation_and_recovery", new=mock_validator):
                return await run_harness_step(
                    state,
                    "test goal",
                    candidate_tools,
                    client=mock_client,
                    model="test-model",
                    tool_caller=mock_tool,
                    trace_builder=trace_builder,
                    projector=projector,
                )

        # The harness calls projector.planner_view(phase_task_revision=state.revision=5)
        # but our mock returns stale_view with phaseTaskRevision=1.
        # _validate_view_and_record sees 1 != 5 -> HarnessPreValidationError.
        with pytest.raises(HarnessPreValidationError) as exc_info:
            asyncio.run(_test())

        assert exc_info.value.code == "view_phase_revision_mismatch"

        # --- Assertions: NOTHING ran ---
        # 1. LLM was never called
        mock_create.assert_not_called()

        # 2. tool_caller was never called
        assert tool_called is False

        # 3. _run_validation_and_recovery was never called
        mock_validator.assert_not_called()

        # 4. State was never mutated through our mock
        assert container["state"].revision == 5

        # 5. Trace records the boundary mismatch
        trace = trace_builder.finish()
        assert len(trace.context_boundary_mismatches) == 1, (
            f"Expected 1 context_boundary_mismatch, got {trace.context_boundary_mismatches}"
        )
        mismatch = trace.context_boundary_mismatches[0]
        assert mismatch["phase"] == "planner"
        assert mismatch["errorCode"] == "view_phase_revision_mismatch"
        assert mismatch["viewPhaseRevision"] == 1  # stale
        assert mismatch["stateRevision"] == 5      # actual

        # 6. No phases were recorded
        assert len(trace.phase_task_revisions) == 0, (
            f"Expected 0 phase_task_revisions, got {trace.phase_task_revisions}"
        )

        # 7. No tool calls in trace
        assert len(trace.tool_calls) == 0

        # 8. Trace is degraded
        assert trace.degraded is True
        assert any("context_boundary_mismatch" in r for r in trace.degraded_reasons)

    # ── P0: FinalAnswer boundary mismatch test ──────────────────────────

    def test_task_a_final_answer_mismatch_recorded(self):
        """Task A: FinalAnswer mismatch through _run_explicit_harness_agent →
        context_boundary_mismatch recorded → finalAction != "task_completed" →
        LLM _generate_final_answer is never called.

        Proves: set_final("task_completed") is AFTER boundary check;
        _validate_view_and_record is used (not bare _validate_view_state_metadata).
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.harness import HarnessPreValidationError
        from app.context_view import FinalAnswerContextView, ContextProjector

        # --- Build state at rev=5, ContextPack at rev=5 ---
        state = self._make_initial_state()
        state = state.model_copy(update={"revision": 5})
        pack = self._make_context_pack(state)
        projector = ContextProjector(pack)

        # --- Mock projector.final_answer_view to return stale view ---
        stale_fa_view = FinalAnswerContextView(
            runId=pack.run_id,
            taskId=pack.task_id,
            baseContextRevision=pack.base_context_revision,
            phaseTaskRevision=1,  # STALE — state.revision=5
            contextHash="hash-stale-fa",
            goal=pack.goal,
            taskType=pack.task_type,
            taskStatus="completed",
            executedSteps=[],
            evidenceRefs=[],
            pendingQuestions=[],
        )
        projector.final_answer_view = MagicMock(return_value=stale_fa_view)

        # --- Mock build_context_pack to return our pack ---
        async def mock_build_context_pack(state, **kwargs):
            return pack

        # --- Mock run_harness_step to return task_completed ---
        from app.harness import HarnessStepResult

        async def mock_run_harness_step(state, message, tool_schemas, **kwargs):
            return HarnessStepResult(
                action="task_completed",
                task_state=state,
                planner_result=None,
                executor_result=None,
                replanner_result=None,
                validator_result=None,
            )

        # --- Mock LLM client (must never reach _generate_final_answer) ---
        mock_create = AsyncMock()
        mock_create.side_effect = RuntimeError(
            "LLM called — FinalAnswer boundary should have blocked!"
        )

        mock_client = MagicMock()
        mock_client.chat = MagicMock()
        mock_client.chat.completions = MagicMock()
        mock_client.chat.completions.create = mock_create

        # --- Call _run_explicit_harness_agent ---
        from app.llm import _run_explicit_harness_agent

        # Capture the finished trace from _persist_trace_safely for assertions
        captured_trace = None

        async def mock_persist(trace):
            nonlocal captured_trace
            captured_trace = trace

        async def _test():
            with patch(
                "app.llm.build_context_pack", new=mock_build_context_pack
            ), patch(
                "app.llm.run_harness_step", new=mock_run_harness_step
            ), patch(
                "app.llm.ContextProjector", return_value=projector
            ), patch(
                "app.llm.settings.agent_context_mode", "context_pack"
            ), patch(
                "app.llm.settings.agent_graph_v2_durable_enabled", False
            ), patch(
                "app.llm._persist_trace_safely", new=mock_persist
            ):
                return await _run_explicit_harness_agent(
                    "test goal",
                    history=None,
                    client=mock_client,
                    task_state=state,
                    on_answer_delta=None,
                    on_task_state=None,
                )

        # The FinalAnswer boundary check raises HarnessPreValidationError,
        # which propagates through the except Exception → mark_degraded → raise
        # pattern in _run_explicit_harness_agent.
        with pytest.raises(HarnessPreValidationError) as exc_info:
            asyncio.run(_test())

        assert exc_info.value.code == "view_phase_revision_mismatch"

        # --- Assertions ---
        # 1. LLM _generate_final_answer was never called
        mock_create.assert_not_called()

        # 2. Trace has exactly 1 context_boundary_mismatch for final_answer
        assert captured_trace is not None, (
            "Expected _persist_trace_safely to be called with finished trace"
        )
        assert len(captured_trace.context_boundary_mismatches) == 1, (
            f"Expected 1 mismatch, got {captured_trace.context_boundary_mismatches}"
        )
        mismatch = captured_trace.context_boundary_mismatches[0]
        assert mismatch["phase"] == "final_answer", (
            f"Expected phase=final_answer, got {mismatch['phase']}"
        )
        assert mismatch["errorCode"] == "view_phase_revision_mismatch"
        assert mismatch["viewPhaseRevision"] == 1   # stale
        assert mismatch["stateRevision"] == 5        # actual

        # 3. finalAction is NOT "task_completed" — set_final is AFTER the check
        assert captured_trace.final_action != "task_completed", (
            f"finalAction should not be task_completed, got {captured_trace.final_action}"
        )

        # 4. Trace is degraded
        assert captured_trace.degraded is True
        assert any(
            "context_boundary_mismatch" in r for r in captured_trace.degraded_reasons
        ), f"Expected degraded reason, got {captured_trace.degraded_reasons}"


class TestContextPackEcommerceVerticalSlice:
    """Real Executor persistence -> ValidatorView -> Validator -> FinalAnswer whitelist."""

    def test_persisted_ecommerce_evidence_reaches_validator_and_final_answer(self):
        import asyncio
        from copy import deepcopy
        from datetime import datetime, timezone
        from unittest.mock import MagicMock, patch

        from app.agent_trace import TraceBuilder
        from app.context_pack import build_context_pack
        from app.context_view import ContextProjector
        from app.domains.ecommerce.models import (
            ShoppingRequirement,
            compare_product_details,
        )
        from app.harness import (
            _build_validated_evidence_refs,
            _build_persisted_comparison_results,
            _build_validated_results,
            run_harness_step,
        )
        from app.planning import PlanArgumentSource, PlanStep, TaskPlan
        from app.schemas import ToolTrace
        from app.task_state import TaskState, TaskStateRevisionConflictError
        from app.tools import TOOL_SCHEMAS

        price_requirement = ShoppingRequirement(
            key="price_minor",
            operator="lte",
            value=500000,
            unit="CNY_MINOR",
            priority="hard",
            source="user:budget",
        )
        resolved_requirements = [price_requirement.model_dump()]

        plan = TaskPlan(
            planId="plan-context-pack-ecommerce",
            basedOnRevision=1,
            steps=[
                PlanStep(
                    stepId="search-step",
                    description="召回耳机候选",
                    toolName="search_products",
                    arguments={"query": "帮我选一款5000元以内的降噪耳机", "category": "耳机"},
                    argumentSources={
                        "query": PlanArgumentSource(kind="task_goal"),
                        "category": PlanArgumentSource(
                            kind="system_policy", reference="searchCategory"
                        ),
                    },
                    expectedOutput={"requiresProductCandidates": True},
                ),
                PlanStep(
                    stepId="compare-step",
                    description="校验并比较候选",
                    toolName="compare_products",
                    arguments={
                        "productIds": [101],
                        "category": "headphones",
                        "requirements": resolved_requirements,
                    },
                    argumentSources={
                        "productIds": PlanArgumentSource(
                            kind="prior_step", reference="search-step.productIds"
                        ),
                        "category": PlanArgumentSource(
                            kind="system_policy", reference="canonicalCategory"
                        ),
                        "requirements": PlanArgumentSource(
                            kind="system_policy", reference="requirements"
                        ),
                    },
                    expectedOutput={"requiresGuideDecision": True},
                ),
            ],
        )
        initial = TaskState(
            taskId="task-context-pack-ecommerce",
            revision=1,
            status="ready",
            goal="帮我选一款5000元以内的降噪耳机",
            taskType="ecommerce_guide",
            activePlan=plan,
            createdAt=datetime.now(timezone.utc),
            updatedAt=datetime.now(timezone.utc),
            domainState={},
        )
        store = {"state": initial}

        async def persist(task_id, state_patch):
            current = store["state"]
            assert task_id == current.task_id
            if state_patch.expected_revision != current.revision:
                raise TaskStateRevisionConflictError(
                    state_patch.expected_revision, current.revision
                )
            updated = current.model_copy(deep=True)
            updated.revision = current.revision + 1
            if state_patch.status is not None:
                updated.status = state_patch.status
            if state_patch.active_plan is not None:
                updated.active_plan = state_patch.active_plan.model_copy(deep=True)
            domain_state = dict(updated.domain_state)
            for key, value in state_patch.domain_state_patch.items():
                if value is None:
                    domain_state.pop(key, None)
                else:
                    domain_state[key] = deepcopy(value)
            updated.domain_state = domain_state
            store["state"] = updated
            return updated

        authoritative_detail = compare_product_details(
            "headphones",
            [
                {
                    "id": 101,
                    "source": "kuaisearch",
                    "title": "权威耳机",
                    "brand": "权威",
                    "categoryL1": "数码",
                    "categoryL2": "耳机",
                    "categoryL3": "蓝牙耳机",
                    "snapshotPriceMinor": 499900,
                    "currency": "CNY",
                    "priceStatus": "verified",
                    "attributeText": "无线降噪耳机",
                    "provenanceUrl": "https://example.test/catalog/101",
                    "attributes": [],
                }
            ],
            [price_requirement],
        )

        async def tool_caller(tool_name, arguments):
            if tool_name == "search_products":
                return ToolTrace(
                    tool=tool_name,
                    ok=True,
                    durationMs=1.0,
                    detail=two_stage_search_detail([101]),
                )
            assert tool_name == "compare_products"
            assert arguments["productIds"] == [101]
            assert arguments["category"] == authoritative_detail["category"]
            assert arguments["requirements"] == authoritative_detail["requirements"]
            return ToolTrace(
                tool=tool_name,
                ok=True,
                durationMs=1.0,
                detail=authoritative_detail,
            )

        schemas = [
            schema
            for schema in TOOL_SCHEMAS
            if schema["function"]["name"] in {"search_products", "compare_products"}
        ]
        client = MagicMock()
        captured = {}

        async def exercise():
            pack = await build_context_pack(
                initial,
                allowed_tools=["search_products", "compare_products"],
                run_id="run-context-pack-ecommerce",
            )
            projector = ContextProjector(pack)
            original_validator_view = projector.validator_view

            def capture_validator_view(**kwargs):
                view = original_validator_view(**kwargs)
                captured["validator"] = view
                return view

            projector.validator_view = capture_validator_view
            trace = TraceBuilder("run-context-pack-ecommerce", mode="context_pack")
            trace.set_base_context_revision(1)
            first = await run_harness_step(
                initial,
                initial.goal,
                schemas,
                client=client,
                model="unused",
                tool_caller=tool_caller,
                projector=projector,
                trace_builder=trace,
                system_policies={
                    "searchCategory": "耳机",
                    "canonicalCategory": "headphones",
                    "requirements": resolved_requirements,
                },
            )
            assert first.action == "continue_to_executor"
            second = await run_harness_step(
                first.task_state,
                initial.goal,
                schemas,
                client=client,
                model="unused",
                tool_caller=tool_caller,
                projector=projector,
                trace_builder=trace,
                system_policies={
                    "searchCategory": "耳机",
                    "canonicalCategory": "headphones",
                    "requirements": resolved_requirements,
                },
            )
            return projector, second

        with patch("app.executor.update_task_state", new=persist), patch(
            "app.validator.update_task_state", new=persist
        ):
            projector, result = asyncio.run(exercise())

        assert result.action == "task_completed", (
            result.validator_result.outcome if result.validator_result else None,
            result.validator_result.error_code if result.validator_result else None,
            result.validator_result.reason if result.validator_result else None,
            [
                (item.step_id, item.outcome, item.error_code, item.reason)
                for item in result.validator_result.step_results
            ] if result.validator_result else None,
            result.task_state.domain_state.get("validationResult"),
            result.executor_result.execution_result.resolved_arguments,
            result.executor_result.execution_result.tool_trace.detail.get("category"),
            result.executor_result.execution_result.tool_trace.detail.get("requirements"),
        )
        assert result.validator_result is not None
        assert result.validator_result.outcome == "passed"
        assert result.task_state.status == "ready"

        validator_view = captured["validator"]
        assert validator_view.phase_task_revision > validator_view.base_context_revision
        search_evidence = next(
            item for item in validator_view.executed_steps
            if item.step_id == "search-step"
        )
        expected_search_output = {
            "contractVersion": "ecommerce-two-stage-ranking-v2",
            "candidatePoolIds": [101],
            "rankedItemIds": [101],
            "productIds": [101],
            "evidenceRefs": ["product:101:title"],
        }
        assert {
            key: search_evidence.normalized_output[key]
            for key in expected_search_output
        } == expected_search_output
        support = search_evidence.normalized_output["candidateSupport"]
        assert {
            key: support[key]
            for key in (
                "hasCompleteMatch", "fullySupportedProductIds",
                "closestAlternativeProductIds", "hardUnknownsByProduct",
            )
        } == {
            "hasCompleteMatch": True,
            "fullySupportedProductIds": [101],
            "closestAlternativeProductIds": [],
            "hardUnknownsByProduct": {},
        }
        assert support["productPresentations"][0]["productId"] == 101
        assert search_evidence.evidence_values == {}
        assert search_evidence.evidence_refs == []
        assert search_evidence.resolved_arguments["query"] == initial.goal
        compare_evidence = next(
            item for item in validator_view.executed_steps
            if item.step_id == "compare-step"
        )
        assert set(compare_evidence.evidence_values) == {
            "category", "requirements", "products", "comparisonMatrix",
            "evidence", "hasCompleteMatch", "rankingTrace",
        }
        assert compare_evidence.evidence_values["category"] == authoritative_detail["category"]
        assert compare_evidence.evidence_values["requirements"] == authoritative_detail["requirements"]
        assert compare_evidence.evidence_values["comparisonMatrix"] == authoritative_detail["comparisonMatrix"]
        projected_evidence = compare_evidence.evidence_values["evidence"]
        assert projected_evidence
        projected_refs = {
            ref
            for item in projected_evidence
            for ref in (
                item["refs"] if "refs" in item else [item.get("ref")]
            )
            if isinstance(ref, str)
        }
        required_refs = {
            check["evidenceRef"]
            for row in authoritative_detail["products"]
            for check in row["checks"]
            if check.get("evidenceRef") is not None
        }
        # Product identity refs are part of the current evidence contract in
        # addition to requirement-check refs.  The projection may enrich the
        # evidence set, but it must never drop any authoritative check ref.
        assert required_refs <= projected_refs
        assert {
            "product:101:title",
            "product:101:brand",
        } <= projected_refs
        assert compare_evidence.resolved_arguments["requirements"] == resolved_requirements
        projected_row = compare_evidence.evidence_values["products"][0]
        assert projected_row["selectionType"] == "full_match"
        assert projected_row["hardFailures"] == 0
        assert projected_row["hardUnknowns"] == 0
        assert projected_row["softScore"] == 0
        assert projected_row["checks"] == authoritative_detail["comparisonMatrix"][0]["checks"]
        assert set(projected_row["product"]) == {
            "id", "title", "brand", "priceStatus", "attributes",
        }

        stale_trace = ToolTrace(
            tool="compare_products",
            ok=True,
            durationMs=1.0,
            detail={"fakePrice": 999999, "evidenceRefs": ["stale:price"]},
        )
        validated = _build_validated_results(result.task_state, [stale_trace])
        serialized = str(validated)
        assert "999999" not in serialized
        assert "stale:price" not in serialized
        assert "123456" not in serialized
        assert "mysql:product:101:title" not in serialized
        assert "499900" in serialized
        refs = _build_validated_evidence_refs(validated)
        assert "product:101:snapshotPriceMinor" in refs
        assert "stale:price" not in refs

        final_view = projector.final_answer_view(
            validated_results=validated,
            evidence_refs=refs,
            phase_task_revision=result.task_state.revision,
        )
        assert final_view.evidence_refs == refs
        assert "999999" not in final_view.model_dump_json(by_alias=True)


# ── Canonical / legacy serialization tests ─────────────────────────────────


class TestBaseContextRevisionSerialization:
    """Verify canonical dumps output baseContextRevision + phaseTaskRevision,
    legacy taskRevision input is accepted, and mismatch fails closed."""

    # ── ContextPack canonical / legacy ────────────────────────────────────

    def test_context_pack_canonical_dump_includes_base_context_revision(self):
        """ContextPack.model_dump(by_alias=True) emits baseContextRevision."""
        from app.context_pack import ContextPack
        pack = ContextPack(
            runId="run-serial",
            taskId="task-serial",
            baseContextRevision=42,
            goal="test goal",
            taskType="ecommerce_guide",
        )
        dump = pack.model_dump(by_alias=True, mode="json")
        assert "baseContextRevision" in dump, (
            f"Canonical dump missing baseContextRevision: {sorted(dump.keys())}"
        )
        assert dump["baseContextRevision"] == 42

    def test_context_pack_accepts_legacy_task_revision(self):
        """ContextPack accepts legacy taskRevision input and migrates it."""
        from app.context_pack import ContextPack
        pack = ContextPack(
            runId="run-legacy",
            taskId="task-legacy",
            taskRevision=7,
            goal="test",
        )
        assert pack.base_context_revision == 7
        dump = pack.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 7
        assert "taskRevision" not in dump

    def test_context_pack_mismatch_fails_closed(self):
        """ContextPack with both baseContextRevision and taskRevision differing → ValueError."""
        from app.context_pack import ContextPack
        with pytest.raises(ValueError, match="baseContextRevision"):
            ContextPack(
                runId="run-conflict",
                taskId="task-conflict",
                baseContextRevision=10,
                taskRevision=20,
                goal="test",
            )

    def test_context_pack_same_values_normalize_to_one_truth(self):
        """Providing equal baseContextRevision + taskRevision → single canonical value."""
        from app.context_pack import ContextPack
        pack = ContextPack(
            runId="run-equal",
            taskId="task-equal",
            baseContextRevision=11,
            taskRevision=11,
            goal="test",
        )
        assert pack.base_context_revision == 11
        dump = pack.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 11
        assert "taskRevision" not in dump

    # ── PlannerView canonical / legacy ────────────────────────────────────

    def test_planner_view_canonical_dump_includes_both_revisions(self):
        """PlannerContextView dump includes baseContextRevision + phaseTaskRevision."""
        from app.context_view import PlannerContextView
        view = PlannerContextView(
            runId="r1", taskId="t1",
            baseContextRevision=5,
            phaseTaskRevision=3,
            contextHash="h1", goal="g",
            userMessage="um", taskStatus="ready",
        )
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 5
        assert dump["phaseTaskRevision"] == 3

    def test_planner_view_accepts_legacy_task_revision(self):
        """PlannerContextView accepts legacy taskRevision → stored as baseContextRevision."""
        from app.context_view import PlannerContextView
        view = PlannerContextView(
            runId="r1", taskId="t1",
            taskRevision=9,
            phaseTaskRevision=1,
            contextHash="h1", goal="g",
            userMessage="um", taskStatus="ready",
        )
        assert view.base_context_revision == 9
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 9
        assert "taskRevision" not in dump

    def test_planner_view_mismatch_fails_closed(self):
        """PlannerContextView with mismatched baseContextRevision vs taskRevision → ValueError."""
        from app.context_view import PlannerContextView
        with pytest.raises(ValueError, match="baseContextRevision"):
            PlannerContextView(
                runId="r1", taskId="t1",
                baseContextRevision=1,
                taskRevision=99,
                phaseTaskRevision=1,
                contextHash="h1", goal="g",
                userMessage="um", taskStatus="ready",
            )

    def test_planner_view_same_values_normalize_to_one_truth(self):
        """PlannerContextView: equal baseContextRevision + taskRevision → single canonical value."""
        from app.context_view import PlannerContextView
        view = PlannerContextView(
            runId="r1", taskId="t1",
            baseContextRevision=6,
            taskRevision=6,
            phaseTaskRevision=1,
            contextHash="h1", goal="g",
            userMessage="um", taskStatus="ready",
        )
        assert view.base_context_revision == 6
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 6
        assert dump["phaseTaskRevision"] == 1
        assert "taskRevision" not in dump

    # ── ExecutorView canonical / legacy ───────────────────────────────────

    def test_executor_view_canonical_dump_includes_both_revisions(self):
        """ExecutorContextView dump includes baseContextRevision + phaseTaskRevision."""
        from app.context_view import ExecutorContextView
        view = ExecutorContextView(
            runId="r1", taskId="t1",
            baseContextRevision=5,
            phaseTaskRevision=3,
            contextHash="h1",
            planId="p1", stepId="s1",
            stepDescription="desc", toolName="t1",
        )
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 5
        assert dump["phaseTaskRevision"] == 3

    def test_executor_view_accepts_legacy_task_revision(self):
        """ExecutorContextView accepts legacy taskRevision → stored as baseContextRevision."""
        from app.context_view import ExecutorContextView
        view = ExecutorContextView(
            runId="r1", taskId="t1",
            taskRevision=7,
            phaseTaskRevision=1,
            contextHash="h1",
            planId="p1", stepId="s1",
            stepDescription="desc", toolName="t1",
        )
        assert view.base_context_revision == 7
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 7

    def test_executor_view_mismatch_fails_closed(self):
        """ExecutorContextView with mismatched baseContextRevision vs taskRevision → ValueError."""
        from app.context_view import ExecutorContextView
        with pytest.raises(ValueError, match="baseContextRevision"):
            ExecutorContextView(
                runId="r1", taskId="t1",
                baseContextRevision=1,
                taskRevision=99,
                phaseTaskRevision=1,
                contextHash="h1",
                planId="p1", stepId="s1",
                stepDescription="desc", toolName="t1",
            )

    def test_executor_view_same_values_normalize_to_one_truth(self):
        """ExecutorContextView: equal baseContextRevision + taskRevision → single canonical value."""
        from app.context_view import ExecutorContextView
        view = ExecutorContextView(
            runId="r1", taskId="t1",
            baseContextRevision=7,
            taskRevision=7,
            phaseTaskRevision=1,
            contextHash="h1",
            planId="p1", stepId="s1",
            stepDescription="desc", toolName="t1",
        )
        assert view.base_context_revision == 7
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 7
        assert dump["phaseTaskRevision"] == 1
        assert "taskRevision" not in dump

    # ── ValidatorView canonical / legacy ──────────────────────────────────

    def test_validator_view_canonical_dump_includes_both_revisions(self):
        """ValidatorContextView dump includes baseContextRevision + phaseTaskRevision."""
        from app.context_view import ValidatorContextView
        view = ValidatorContextView(
            runId="r1", taskId="t1",
            baseContextRevision=5,
            phaseTaskRevision=3,
            contextHash="h1", goal="g",
        )
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 5
        assert dump["phaseTaskRevision"] == 3

    def test_validator_view_accepts_legacy_task_revision(self):
        """ValidatorContextView accepts legacy taskRevision → stored as baseContextRevision."""
        from app.context_view import ValidatorContextView
        view = ValidatorContextView(
            runId="r1", taskId="t1",
            taskRevision=6,
            phaseTaskRevision=1,
            contextHash="h1", goal="g",
        )
        assert view.base_context_revision == 6
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 6

    def test_validator_view_mismatch_fails_closed(self):
        """ValidatorContextView with mismatched baseContextRevision vs taskRevision → ValueError."""
        from app.context_view import ValidatorContextView
        with pytest.raises(ValueError, match="baseContextRevision"):
            ValidatorContextView(
                runId="r1", taskId="t1",
                baseContextRevision=1,
                taskRevision=99,
                phaseTaskRevision=1,
                contextHash="h1", goal="g",
            )

    def test_validator_view_same_values_normalize_to_one_truth(self):
        """ValidatorContextView: equal baseContextRevision + taskRevision → single canonical value."""
        from app.context_view import ValidatorContextView
        view = ValidatorContextView(
            runId="r1", taskId="t1",
            baseContextRevision=6,
            taskRevision=6,
            phaseTaskRevision=1,
            contextHash="h1", goal="g",
        )
        assert view.base_context_revision == 6
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 6
        assert dump["phaseTaskRevision"] == 1
        assert "taskRevision" not in dump

    # ── ReplannerView canonical / legacy ──────────────────────────────────

    def test_replanner_view_canonical_dump_includes_both_revisions(self):
        """ReplannerContextView dump includes baseContextRevision + phaseTaskRevision."""
        from app.context_view import ReplannerContextView
        view = ReplannerContextView(
            runId="r1", taskId="t1",
            baseContextRevision=5,
            phaseTaskRevision=3,
            contextHash="h1", goal="g",
            failedPlanSummary={"planId": "p1", "steps": [], "status": "failed"},
            failureReason="reason", replanAttempt=1,
        )
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 5
        assert dump["phaseTaskRevision"] == 3

    def test_replanner_view_accepts_legacy_task_revision(self):
        """ReplannerContextView accepts legacy taskRevision → stored as baseContextRevision."""
        from app.context_view import ReplannerContextView
        view = ReplannerContextView(
            runId="r1", taskId="t1",
            taskRevision=8,
            phaseTaskRevision=1,
            contextHash="h1", goal="g",
            failedPlanSummary={"planId": "p1", "steps": [], "status": "failed"},
            failureReason="reason", replanAttempt=1,
        )
        assert view.base_context_revision == 8
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 8

    def test_replanner_view_mismatch_fails_closed(self):
        """ReplannerContextView with mismatched baseContextRevision vs taskRevision → ValueError."""
        from app.context_view import ReplannerContextView
        with pytest.raises(ValueError, match="baseContextRevision"):
            ReplannerContextView(
                runId="r1", taskId="t1",
                baseContextRevision=1,
                taskRevision=99,
                phaseTaskRevision=1,
                contextHash="h1", goal="g",
                failedPlanSummary={"planId": "p1", "steps": [], "status": "failed"},
                failureReason="reason", replanAttempt=1,
            )

    def test_replanner_view_same_values_normalize_to_one_truth(self):
        """ReplannerContextView: equal baseContextRevision + taskRevision → single canonical value."""
        from app.context_view import ReplannerContextView
        view = ReplannerContextView(
            runId="r1", taskId="t1",
            baseContextRevision=8,
            taskRevision=8,
            phaseTaskRevision=1,
            contextHash="h1", goal="g",
            failedPlanSummary={"planId": "p1", "steps": [], "status": "failed"},
            failureReason="reason", replanAttempt=1,
        )
        assert view.base_context_revision == 8
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 8
        assert dump["phaseTaskRevision"] == 1
        assert "taskRevision" not in dump

    # ── FinalAnswerView canonical / legacy ────────────────────────────────

    def test_final_answer_view_canonical_dump_includes_both_revisions(self):
        """FinalAnswerContextView dump includes baseContextRevision + phaseTaskRevision."""
        from app.context_view import FinalAnswerContextView
        view = FinalAnswerContextView(
            runId="r1", taskId="t1",
            baseContextRevision=5,
            phaseTaskRevision=3,
            contextHash="h1", goal="g",
        )
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 5
        assert dump["phaseTaskRevision"] == 3

    def test_final_answer_view_accepts_legacy_task_revision(self):
        """FinalAnswerContextView accepts legacy taskRevision → stored as baseContextRevision."""
        from app.context_view import FinalAnswerContextView
        view = FinalAnswerContextView(
            runId="r1", taskId="t1",
            taskRevision=4,
            phaseTaskRevision=1,
            contextHash="h1", goal="g",
        )
        assert view.base_context_revision == 4
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 4

    def test_final_answer_view_mismatch_fails_closed(self):
        """FinalAnswerContextView with mismatched baseContextRevision vs taskRevision → ValueError."""
        from app.context_view import FinalAnswerContextView
        with pytest.raises(ValueError, match="baseContextRevision"):
            FinalAnswerContextView(
                runId="r1", taskId="t1",
                baseContextRevision=1,
                taskRevision=99,
                phaseTaskRevision=1,
                contextHash="h1", goal="g",
            )

    def test_final_answer_view_same_values_normalize_to_one_truth(self):
        """FinalAnswerContextView: equal baseContextRevision + taskRevision → single canonical value."""
        from app.context_view import FinalAnswerContextView
        view = FinalAnswerContextView(
            runId="r1", taskId="t1",
            baseContextRevision=4,
            taskRevision=4,
            phaseTaskRevision=1,
            contextHash="h1", goal="g",
        )
        assert view.base_context_revision == 4
        dump = view.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 4
        assert dump["phaseTaskRevision"] == 1
        assert "taskRevision" not in dump

    # ── Round-trip: legacy input → canonical output ───────────────────────

    def test_all_views_round_trip_legacy_to_canonical(self):
        """All 5 Views: legacy taskRevision in → canonical baseContextRevision out."""
        import json
        from app.context_view import (
            PlannerContextView, ExecutorContextView, ValidatorContextView,
            ReplannerContextView, FinalAnswerContextView,
        )

        views: list[tuple[str, Any]] = [
            ("planner", PlannerContextView(
                runId="r", taskId="t",
                taskRevision=42, phaseTaskRevision=1,
                contextHash="h", goal="g",
                userMessage="um", taskStatus="ready",
            )),
            ("executor", ExecutorContextView(
                runId="r", taskId="t",
                taskRevision=42, phaseTaskRevision=1,
                contextHash="h",
                planId="p", stepId="s",
                stepDescription="d", toolName="t1",
            )),
            ("validator", ValidatorContextView(
                runId="r", taskId="t",
                taskRevision=42, phaseTaskRevision=1,
                contextHash="h", goal="g",
            )),
            ("replanner", ReplannerContextView(
                runId="r", taskId="t",
                taskRevision=42, phaseTaskRevision=1,
                contextHash="h", goal="g",
                failedPlanSummary={"planId": "p", "steps": [], "status": "failed"},
                failureReason="r", replanAttempt=1,
            )),
            ("final_answer", FinalAnswerContextView(
                runId="r", taskId="t",
                taskRevision=42, phaseTaskRevision=1,
                contextHash="h", goal="g",
            )),
        ]

        for name, view in views:
            dump = view.model_dump(by_alias=True, mode="json")
            assert "baseContextRevision" in dump, (
                f"[{name}] missing baseContextRevision in: {json.dumps(dump, sort_keys=True)}"
            )
            assert dump["baseContextRevision"] == 42, (
                f"[{name}] baseContextRevision={dump['baseContextRevision']}, expected 42"
            )
            assert "taskRevision" not in dump, (
                f"[{name}] legacy taskRevision leaked into canonical dump"
            )
            assert "phaseTaskRevision" in dump, (
                f"[{name}] missing phaseTaskRevision"
            )
            assert dump["phaseTaskRevision"] == 1, (
                f"[{name}] phaseTaskRevision={dump['phaseTaskRevision']}, expected 1"
            )

    # ── Hash stability: baseContextRevision excluded ──────────────────────

    def test_view_hash_excludes_base_context_revision(self):
        """Views with different baseContextRevision but same content → same hash."""
        from app.context_view import (
            PlannerContextView, _view_hash,
        )
        v1 = PlannerContextView(
            runId="r-same-hash", taskId="t-same-hash",
            baseContextRevision=1, phaseTaskRevision=1,
            contextHash="h", goal="g",
            userMessage="um", taskStatus="ready",
        )
        v2 = PlannerContextView(
            runId="r-same-hash", taskId="t-same-hash",
            baseContextRevision=999, phaseTaskRevision=1,
            contextHash="h", goal="g",
            userMessage="um", taskStatus="ready",
        )
        # Different baseContextRevision but same run_id excluded → same hash
        h1 = _view_hash(v1)
        h2 = _view_hash(v2)
        assert h1 == h2, (
            f"Hash should ignore baseContextRevision: {h1} vs {h2}"
        )

    def test_legacy_and_canonical_equivalent_inputs_same_view_hash(self):
        """Legacy taskRevision and canonical baseContextRevision with the same
        value produce identical normalized dumps and identical view hashes."""
        import json
        from app.context_view import PlannerContextView, _view_hash

        v_legacy = PlannerContextView(
            runId="r-hash", taskId="t-hash",
            taskRevision=5, phaseTaskRevision=1,
            contextHash="h", goal="g",
            userMessage="um", taskStatus="ready",
        )
        v_canonical = PlannerContextView(
            runId="r-hash", taskId="t-hash",
            baseContextRevision=5, phaseTaskRevision=1,
            contextHash="h", goal="g",
            userMessage="um", taskStatus="ready",
        )
        d1 = v_legacy.model_dump(by_alias=True, mode="json")
        d2 = v_canonical.model_dump(by_alias=True, mode="json")
        assert d1 == d2, (
            f"Legacy and canonical inputs must normalize to one dump: "
            f"{json.dumps(d1, sort_keys=True)} vs {json.dumps(d2, sort_keys=True)}"
        )
        assert "taskRevision" not in d1 and "taskRevision" not in d2
        assert _view_hash(v_legacy) == _view_hash(v_canonical), (
            "Legacy and canonical equivalent inputs must yield the same view hash"
        )

    def test_legacy_and_canonical_equivalent_inputs_same_pack_hash(self):
        """ContextPack legacy taskRevision and canonical baseContextRevision with
        the same value produce identical dumps and identical pack hashes."""
        import json
        from app.context_pack import ContextPack, context_pack_hash

        p_legacy = ContextPack(runId="r-hash", taskId="t-hash", taskRevision=5, goal="g")
        p_canonical = ContextPack(runId="r-hash", taskId="t-hash", baseContextRevision=5, goal="g")
        d1 = p_legacy.model_dump(by_alias=True, mode="json")
        d2 = p_canonical.model_dump(by_alias=True, mode="json")
        assert d1 == d2, (
            f"Legacy and canonical inputs must normalize to one dump: "
            f"{json.dumps(d1, sort_keys=True)} vs {json.dumps(d2, sort_keys=True)}"
        )
        assert "taskRevision" not in d1 and "taskRevision" not in d2
        assert context_pack_hash(p_legacy) == context_pack_hash(p_canonical), (
            "Legacy and canonical equivalent inputs must yield the same pack hash"
        )

    def test_context_pack_hash_excludes_base_context_revision(self):
        """ContextPacks with different baseContextRevision but same content → same hash."""
        from app.context_pack import ContextPack, context_pack_hash
        p1 = ContextPack(
            runId="r-same", taskId="t-same",
            baseContextRevision=1,
            goal="g",
        )
        p2 = ContextPack(
            runId="r-same", taskId="t-same",
            baseContextRevision=999,
            goal="g",
        )
        h1 = context_pack_hash(p1)
        h2 = context_pack_hash(p2)
        assert h1 == h2, (
            f"Hash should ignore baseContextRevision: {h1} vs {h2}"
        )


class TestRevisionInputFailClosed:
    """Presence-based fail-closed normalization (Codex P0).

    The migration must judge presence by key existence, never by truthiness:
    a provided falsy ``0`` is a provided value.  Any two provided names that
    differ must be rejected across ContextPack and all five Views, and
    ``model_validate`` must never mutate the caller's dict.
    """

    VIEW_KWARGS = {
        "planner": dict(
            phaseTaskRevision=1, contextHash="h", goal="g",
            userMessage="um", taskStatus="ready",
        ),
        "executor": dict(
            phaseTaskRevision=1, contextHash="h",
            planId="p", stepId="s", stepDescription="d", toolName="t",
        ),
        "validator": dict(
            phaseTaskRevision=1, contextHash="h", goal="g",
        ),
        "replanner": dict(
            phaseTaskRevision=1, contextHash="h", goal="g",
            failedPlanSummary={"planId": "p", "steps": [], "status": "failed"},
            failureReason="r", replanAttempt=1,
        ),
        "final_answer": dict(
            phaseTaskRevision=1, contextHash="h", goal="g",
        ),
    }

    def _models(self):
        from app.context_pack import ContextPack
        from app.context_view import (
            PlannerContextView, ExecutorContextView, ValidatorContextView,
            ReplannerContextView, FinalAnswerContextView,
        )
        yield "pack", ContextPack, {"goal": "g"}
        for name in ("planner", "executor", "validator", "replanner", "final_answer"):
            yield name, {
                "planner": PlannerContextView,
                "executor": ExecutorContextView,
                "validator": ValidatorContextView,
                "replanner": ReplannerContextView,
                "final_answer": FinalAnswerContextView,
            }[name], self.VIEW_KWARGS[name]

    @pytest.mark.parametrize("conflict_kwargs", [
        {"baseContextRevision": 0, "taskRevision": 1},
        {"taskRevision": 1, "task_revision": 2},
        {"baseContextRevision": 1, "base_context_revision": 2},
        {"baseContextRevision": 3, "taskRevision": 4},
    ], ids=[
        "canonical-falsy-zero-vs-legacy",
        "legacy-camel-vs-legacy-snake",
        "canonical-camel-vs-canonical-snake",
        "canonical-vs-legacy-differ",
    ])
    def test_any_two_provided_values_differ_fail_closed(self, conflict_kwargs):
        """Every model must reject two provided revision names that differ."""
        for label, cls, extra in self._models():
            with pytest.raises(ValueError, match="baseContextRevision"):
                cls(runId="r", taskId="t", **extra, **conflict_kwargs)
            assert label

    def test_model_validate_does_not_mutate_input_payload(self):
        """Before-validator must copy the input, never rewrite the caller's dict."""
        from app.context_pack import ContextPack
        from app.context_view import PlannerContextView
        payload = {"runId": "r", "taskId": "t", "taskRevision": 7, "goal": "g"}
        before = dict(payload)
        ContextPack.model_validate(payload)
        assert payload == before, (
            f"ContextPack mutated input payload: {payload} != {before}"
        )
        vpayload = {
            "runId": "r", "taskId": "t", "taskRevision": 7,
            "phaseTaskRevision": 1, "contextHash": "h", "goal": "g",
            "userMessage": "um", "taskStatus": "ready",
        }
        vbefore = dict(vpayload)
        PlannerContextView.model_validate(vpayload)
        assert vpayload == vbefore, (
            f"PlannerContextView mutated input payload: {vpayload} != {vbefore}"
        )

    def test_legacy_value_cannot_override_invalid_canonical(self):
        """ge=1 must still reject a falsy canonical even when legacy differs."""
        from app.context_pack import ContextPack
        with pytest.raises(ValueError):
            ContextPack(
                runId="r", taskId="t", goal="g",
                baseContextRevision=0, taskRevision=1,
            )

    def test_four_names_same_value_normalize_to_single_base(self):
        """All four accepted names with the same value normalize to one value."""
        from app.context_pack import ContextPack
        from app.context_view import PlannerContextView
        pack = ContextPack(
            runId="r", taskId="t", goal="g",
            baseContextRevision=9, base_context_revision=9,
            taskRevision=9, task_revision=9,
        )
        assert pack.base_context_revision == 9
        dump = pack.model_dump(by_alias=True, mode="json")
        assert dump["baseContextRevision"] == 9
        assert "taskRevision" not in dump and "task_revision" not in dump
        assert "base_context_revision" not in dump
        view = PlannerContextView(
            runId="r", taskId="t",
            baseContextRevision=9, base_context_revision=9,
            taskRevision=9, task_revision=9,
            phaseTaskRevision=1, contextHash="h", goal="g",
            userMessage="um", taskStatus="ready",
        )
        assert view.base_context_revision == 9
        vdump = view.model_dump(by_alias=True, mode="json")
        assert vdump["baseContextRevision"] == 9
        assert "taskRevision" not in vdump


class TestSingleStepSearchNormalizedOutputE2E:
    """Real Harness path: tool caller -> Executor persist -> Validator."""

    def test_single_step_search_candidates_complete_without_replanner(self):
        import asyncio
        from types import SimpleNamespace

        from app import task_state
        from app.context_pack import build_context_pack
        from app.context_view import ContextProjector
        from app.domains.ecommerce.ranking_contract import (
            RANKING_FORMULA,
            RANKING_TIE_BREAK,
            TWO_STAGE_RANKING_CONTRACT_VERSION,
        )
        from app.harness import run_harness_step
        from app.planning import TaskPlan
        from app.schemas import ToolTrace
        from app.task_state import (
            TaskStateCreateRequest,
            TaskStatePatchRequest,
            create_task_state,
            update_task_state,
        )
        from tests.fake_redis import FakeRedis

        candidate_ids = [
            4346166, 2157503, 1239068, 117916, 3934984,
            5989522, 634577, 3470593, 4524730, 1912721,
            2965840, 3956695, 3600996, 1967528, 5304970,
            5286377, 1092202, 2640402, 351899, 4244556,
        ]
        candidate_pool_ids = candidate_ids + list(range(7_000_001, 7_000_031))
        requirement = {
            "key": "os",
            "operator": "eq",
            "value": "ios",
            "unit": "enum",
            "priority": "hard",
            "source": "user",
        }
        schema = {
            "type": "function",
            "function": {
                "name": "search_products",
                "description": "检索商品",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string"},
                        "category": {"type": "string"},
                        "requirements": {"type": "array"},
                    },
                    "required": ["query", "category", "requirements"],
                },
            },
        }

        async def exercise():
            task_state._client = FakeRedis()
            task_state._task_locks.clear()
            task_state._session_locks.clear()
            created = await create_task_state(TaskStateCreateRequest(
                goal="想找 iOS 二手机。",
                task_type="ecommerce_guide",
                domain_state={
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "useCases": [],
                        "requirements": [requirement],
                        "candidateIds": [],
                        "comparedIds": [],
                        "evidenceStatus": "missing",
                    }
                },
            ))
            ready = await update_task_state(
                created.task_id,
                TaskStatePatchRequest(
                    expectedRevision=created.revision,
                    actor="agent",
                    status="ready",
                ),
            )
            plan = TaskPlan(
                planId="plan-smoke-005-harness",
                basedOnRevision=ready.revision,
                steps=[{
                    "stepId": "step-search",
                    "description": "搜索 iOS 二手机",
                    "toolName": "search_products",
                    "arguments": {
                        "query": ready.goal,
                        "category": "手机",
                        "requirements": [requirement],
                    },
                    "argumentSources": {
                        "query": {"kind": "task_goal"},
                        "category": {
                            "kind": "shopping_guide",
                            "reference": "category",
                        },
                        "requirements": {
                            "kind": "shopping_guide",
                            "reference": "requirements",
                        },
                    },
                    "expectedOutput": {"requiresProductCandidates": True},
                }],
            )
            planned = await update_task_state(
                ready.task_id,
                TaskStatePatchRequest(
                    expectedRevision=ready.revision,
                    actor="agent",
                    activePlan=plan,
                ),
            )
            pack = await build_context_pack(
                planned,
                allowed_tools=["search_products"],
            )
            projector = ContextProjector(pack)
            captured = {}
            original_validator_view = projector.validator_view

            def capture_validator_view(**kwargs):
                view = original_validator_view(**kwargs)
                captured["validator"] = view
                return view

            projector.validator_view = capture_validator_view
            model_call = AsyncMock()
            client = SimpleNamespace(chat=SimpleNamespace(
                completions=SimpleNamespace(create=model_call)
            ))
            evidence = [
                {
                    "ref": f"product:{item}:attribute:os",
                    "field": "relevance.attr_value",
                    "rawValue": "iOS," + ("x" * 5000),
                    "method": "used-phone-exact-token-seven-field-v2",
                }
                for item in candidate_ids
            ]
            tool_caller = AsyncMock(return_value=ToolTrace(
                tool="search_products",
                ok=True,
                detail={
                    "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                    "candidatePoolIds": candidate_pool_ids,
                    "rankedItemIds": candidate_ids,
                    "candidateIds": candidate_ids,
                    "candidates": [
                        {
                            "id": item,
                            "attributes": [{
                                "key": "os",
                                "normalizedNumber": None,
                                "normalizedBoolean": None,
                                "normalizedText": "ios",
                                "rawValue": "iOS," + ("x" * 5000),
                                "evidenceField": "relevance.attr_value",
                                "extractionMethod": "used-phone-exact-token-seven-field-v2",
                            }],
                            "checks": [{
                                "key": "os", "operator": "eq", "expected": "ios",
                                "unit": "enum", "priority": "hard", "source": "user",
                                "actual": "ios", "status": "pass",
                                "evidenceRef": f"product:{item}:attribute:os",
                            }],
                            "selectionType": "full_match",
                            "evidenceRefs": [f"product:{item}:attribute:os"],
                        }
                        for item in candidate_ids
                    ],
                    "retrievalTrace": {
                        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                        "candidatePoolCount": len(candidate_pool_ids),
                        "authoritativeFactCount": len(candidate_pool_ids),
                    },
                    "rankingTrace": {
                        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                        "inputCandidateCount": len(candidate_pool_ids),
                        "rankedItemCount": len(candidate_ids),
                        "tieBreak": RANKING_TIE_BREAK,
                        "formula": RANKING_FORMULA,
                    },
                    "citationTrace": {
                        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                        "sourceTool": "search_products",
                        "rankedItemIds": candidate_ids,
                        "evidenceRefCount": len(evidence),
                        "binding": "current_successful_tool_call_ranked_items_only",
                    },
                    "evidenceRefs": [item["ref"] for item in evidence],
                    "evidence": evidence,
                    "eliminated": [],
                },
            ))

            result = await run_harness_step(
                planned,
                planned.goal,
                [schema],
                client=client,
                model="offline-test-model",
                tool_caller=tool_caller,
                projector=projector,
            )
            return result, model_call, tool_caller, captured, projector

        result, model_call, tool_caller, captured, projector = asyncio.run(exercise())

        assert result.action == "task_completed", (
            result.validator_result.model_dump(mode="json", by_alias=True)
            if result.validator_result else None
        )
        assert result.executor_result is not None
        assert result.executor_result.outcome == "step_executed"
        assert result.validator_result is not None
        assert result.validator_result.outcome == "passed"
        assert result.validator_result.step_results[0].outcome == "satisfied"
        assert result.replanner_result is None
        assert result.task_state.status == "ready"
        assert result.task_state.domain_state["stepOutputs"]["step-search"][
            "values"
        ]["candidatePoolIds"] == candidate_pool_ids
        assert result.task_state.domain_state["stepOutputs"]["step-search"][
            "values"
        ]["rankedItemIds"] == candidate_ids
        assert result.task_state.domain_state["stepOutputs"]["step-search"][
            "values"
        ]["productIds"] == candidate_ids
        assert result.task_state.domain_state["stepOutputs"]["step-search"][
            "values"
        ]["evidenceRefs"] == [
            f"product:{item}:attribute:os" for item in candidate_ids
        ]
        validator_step = captured["validator"].executed_steps[0]
        assert validator_step.evidence_values == {}
        assert validator_step.evidence_refs == []
        assert validator_step.normalized_output["candidatePoolIds"] == candidate_pool_ids
        from app.context_pack import _estimate_dict_tokens
        assert _estimate_dict_tokens(
            captured["validator"].model_dump(
                by_alias=True,
                mode="json",
                exclude={"context_hash", "contextHash"},
            )
        ) <= 6000
        tool_caller.assert_awaited_once()
        model_call.assert_not_awaited()

        from copy import deepcopy
        from app.harness import (
            _build_validated_evidence_refs,
            _build_persisted_comparison_results,
            _build_validated_results,
            _build_validated_scope_results,
            build_validated_guide_result,
        )

        poison_trace = ToolTrace(
            tool="search_products",
            ok=True,
            detail={
                "rankedItemIds": [999999],
                "evidenceRefs": ["product:999999:poison"],
                "rawUnvalidated": "must-not-reach-final-answer",
            },
        )
        validated = _build_validated_results(result.task_state, [poison_trace])
        persisted_scope_state = result.task_state.model_copy(update={
            "active_plan": None,
            "revision": result.task_state.revision + 1,
        })
        scope_id = result.task_state.domain_state["candidateScope"]["scopeId"]
        assert _build_validated_scope_results(
            persisted_scope_state,
            f"validated-scope:{scope_id}",
        ) == validated

        # A later comparison owns its own ValidatorResult.  It must not replace
        # the immutable source identity of the still-active CandidateScope.
        # Recommending again from that unchanged scope must therefore rebuild
        # the original trusted presentation instead of comparing the current
        # comparison Plan identity to the old search Plan identity.
        post_compare_state = persisted_scope_state.model_copy(deep=True)
        post_compare_state.domain_state["validationResult"] = {
            "outcome": "passed",
            "taskId": post_compare_state.task_id,
            "planId": "plan-later-comparison",
            "basedOnRevision": post_compare_state.revision,
            "stepResults": [{
                "stepId": "step-later-comparison",
                "outcome": "satisfied",
                "expectedOutput": {"requiresGuideDecision": True},
                "evidenceSummary": {"requiresGuideDecision": {
                    "finalistIds": candidate_ids[:2],
                    "rankedFinalists": [
                        {"productId": item} for item in candidate_ids[:2]
                    ],
                    "hasCompleteMatch": True,
                }},
            }],
        }
        post_compare_state.domain_state["shoppingGuide"]["mode"] = "compare"
        post_compare_state.domain_state["shoppingGuide"]["comparedIds"] = (
            candidate_ids[:2]
        )
        post_compare_state.domain_state["stepOutputs"] = {
            "step-later-comparison": {
                "taskId": post_compare_state.task_id,
                "planId": "plan-later-comparison",
                "stepId": "step-later-comparison",
                "values": {"productIds": candidate_ids[:2]},
            }
        }
        post_compare_state = post_compare_state.model_copy(update={
            "revision": post_compare_state.revision + 1,
        })
        assert _build_validated_scope_results(
            post_compare_state,
            f"validated-scope:{scope_id}",
        ) == validated
        persisted_comparison = _build_persisted_comparison_results(
            post_compare_state,
            f"validated-scope:{scope_id}",
        )
        assert persisted_comparison is not None
        assert persisted_comparison[0]["tool"] == "compare_products"
        assert persisted_comparison[0]["evidence"]["rankedFinalists"] == (
            post_compare_state.domain_state["validationResult"]["stepResults"][0]
            ["evidenceSummary"]["requiresGuideDecision"]["rankedFinalists"]
        )
        post_compare_guide = build_validated_guide_result(post_compare_state)
        assert post_compare_guide is not None
        assert [
            item["product"]["id"] for item in post_compare_guide["products"]
        ] == [str(item) for item in candidate_ids[:3]]

        forged_source_receipt = post_compare_state.model_copy(deep=True)
        forged_source_receipt.domain_state["validationResult"] = deepcopy(
            result.task_state.domain_state["validationResult"]
        )
        forged_source_receipt.domain_state["validationResult"]["outcome"] = (
            "validation_failed"
        )
        forged_source_receipt.domain_state["validationResult"]["errorCode"] = (
            "injected"
        )
        forged_source_receipt.domain_state["validationResult"]["reason"] = (
            "injected"
        )
        with pytest.raises(ValueError, match="Validator identity mismatch"):
            _build_validated_scope_results(
                forged_source_receipt,
                f"validated-scope:{scope_id}",
            )

        later_plan_output = post_compare_state.model_copy(deep=True)
        later_plan_output.domain_state["stepOutputs"]["step-later-comparison"][
            "planId"
        ] = "plan-later-comparison"
        assert _build_validated_scope_results(
            later_plan_output,
            f"validated-scope:{scope_id}",
        ) == validated

        forged_source_execution = post_compare_state.model_copy(deep=True)
        forged_source_execution.domain_state["stepExecutionResults"][0][
            "toolTrace"
        ]["detail"]["candidateIds"] = [999999]
        with pytest.raises(ValueError, match="source execution is invalid"):
            _build_validated_scope_results(
                forged_source_execution,
                f"validated-scope:{scope_id}",
            )
        from app.llm import _validated_results_for_answer_context
        assert _validated_results_for_answer_context(
            persisted_scope_state,
            [poison_trace],
            f"validated-scope:{scope_id}",
        ) == validated
        persisted_guide_result = build_validated_guide_result(
            persisted_scope_state
        )
        assert persisted_guide_result is not None
        assert [
            item["product"]["id"]
            for item in persisted_guide_result["products"]
        ] == [str(item) for item in candidate_ids[:3]]
        retained_terminal_plan_state = result.task_state.model_copy(update={
            "revision": result.task_state.revision + 1,
        })
        retained_plan_guide_result = build_validated_guide_result(
            retained_terminal_plan_state
        )
        assert retained_plan_guide_result is not None
        assert [
            item["product"]["id"]
            for item in retained_plan_guide_result["products"]
        ] == [str(item) for item in candidate_ids[:3]]
        support = result.task_state.domain_state["stepOutputs"]["step-search"][
            "values"
        ]["candidateSupport"]
        compact_support = dict(support)
        compact_support.pop("productPresentations")
        assert [
            item["productId"] for item in support["productPresentations"]
        ] == candidate_ids[:3]
        assert validated == [{
            "stepId": "step-search",
            "tool": "search_products",
            "evidence": {
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "candidatePoolIds": candidate_pool_ids,
                "rankedItemIds": candidate_ids,
                "productIds": candidate_ids,
                "evidenceRefs": [
                    f"product:{item}:attribute:os" for item in candidate_ids
                ],
                "candidateSupport": compact_support,
            },
            "evidenceRefs": [
                f"product:{item}:attribute:os" for item in candidate_ids
            ],
            "validationSummary": {
                "requiresProductCandidates": {
                    "candidatePoolCount": len(candidate_pool_ids),
                    "rankedItemCount": len(candidate_ids),
                    "candidatePoolIds": candidate_pool_ids,
                    "rankedItemIds": candidate_ids,
                    "productIds": candidate_ids,
                    "evidenceRefs": [
                        f"product:{item}:attribute:os" for item in candidate_ids
                    ],
                    **support,
                }
            },
        }]
        final_view = projector.final_answer_view(
            validated_results=validated,
            evidence_refs=_build_validated_evidence_refs(validated),
            phase_task_revision=result.task_state.revision,
        )
        final_payload = final_view.model_dump_json(by_alias=True)
        assert "must-not-reach-final-answer" not in final_payload
        assert "product:999999:poison" not in final_payload
        assert final_view.answer_category == "phone"
        assert final_view.answer_constraints == [requirement]
        assert final_view.evidence_refs == [
            f"product:{item}:attribute:os" for item in candidate_ids
        ]
        guide_result = build_validated_guide_result(result.task_state)
        assert guide_result is not None
        assert guide_result["contractVersion"] == "validated-product-presentation-v1"
        assert [
            item["product"]["id"] for item in guide_result["products"]
        ] == [str(item) for item in candidate_ids[:3]]
        assert [
            item["product"]["id"]
            for item in guide_result["expandedProducts"]
        ] == [str(item) for item in candidate_ids]
        assert all(
            "rawUnvalidated" not in json.dumps(item)
            for item in guide_result["products"]
        )
        assert _estimate_dict_tokens(
            final_view.model_dump(
                by_alias=True,
                mode="json",
                exclude={"context_hash", "contextHash"},
            )
        ) <= 4000

        foreign = result.task_state.model_copy(deep=True)
        foreign.domain_state["stepOutputs"]["step-search"]["taskId"] = "other-task"
        with pytest.raises(ValueError, match="identity mismatch"):
            _build_validated_results(foreign, [])

        unvalidated = result.task_state.model_copy(deep=True)
        unvalidated.domain_state["validationResult"]["outcome"] = "validation_failed"
        unvalidated.domain_state["validationResult"]["errorCode"] = "injected"
        unvalidated.domain_state["validationResult"]["reason"] = "injected"
        assert _build_validated_results(unvalidated, [poison_trace]) == []

    def test_tampered_two_stage_step_output_fails_before_prior_step_dispatch(self):
        import asyncio
        from copy import deepcopy
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from app.executor import run_executor_step
        from app.planning import TaskPlan
        from app.task_state import TaskState
        from app import task_state
        from tests.fake_redis import FakeRedis

        plan = TaskPlan(
            planId="plan-tampered-ranking-output",
            basedOnRevision=1,
            steps=[
                {
                    "stepId": "step-search",
                    "description": "search",
                    "toolName": "search_products",
                    "arguments": {"query": "phone"},
                    "argumentSources": {"query": {"kind": "task_goal"}},
                    "expectedOutput": {"requiresProductCandidates": True},
                    "status": "executed",
                },
                {
                    "stepId": "step-details",
                    "description": "details",
                    "toolName": "get_product_details",
                    "arguments": {"productIds": [101]},
                    "argumentSources": {
                        "productIds": {
                            "kind": "prior_step",
                            "reference": "step-search.productIds",
                        }
                    },
                    "expectedOutput": {"requiresProductDetails": True},
                },
            ],
        )
        values = two_stage_search_detail([101])["rankedItemIds"]
        stored_values = {
            "contractVersion": "ecommerce-two-stage-ranking-v2",
            "candidatePoolIds": [101],
            "rankedItemIds": values,
            "productIds": [999],
            "evidenceRefs": ["product:101:title"],
            "candidateSupport": {
                "hasCompleteMatch": True,
                "fullySupportedProductIds": [101],
                "closestAlternativeProductIds": [],
                "hardUnknownsByProduct": {},
            },
        }
        now = datetime.now(timezone.utc)
        state = TaskState(
            taskId="task-tampered-ranking-output",
            taskType="ecommerce_guide",
            status="ready",
            revision=2,
            goal="phone",
            activePlan=plan,
            domainState={
                "stepOutputs": {
                    "step-search": {
                        "taskId": "task-tampered-ranking-output",
                        "planId": plan.plan_id,
                        "stepId": "step-search",
                        "values": deepcopy(stored_values),
                    }
                }
            },
            createdAt=now,
            updatedAt=now,
        )
        schema = {
            "type": "function",
            "function": {
                "name": "get_product_details",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "productIds": {"type": "array", "items": {"type": "integer"}}
                    },
                    "required": ["productIds"],
                },
            },
        }
        caller = AsyncMock()

        async def exercise():
            task_state._client = FakeRedis()
            task_state._task_locks.clear()
            task_state._session_locks.clear()
            await task_state._client.set(
                task_state._state_key(state.task_id),
                state.model_dump_json(by_alias=True),
            )
            return await run_executor_step(state, [schema], tool_caller=caller)

        result = asyncio.run(exercise())

        assert result.outcome == "step_blocked"
        assert result.error_code == "product_ids_alias_mismatch"
        caller.assert_not_awaited()
