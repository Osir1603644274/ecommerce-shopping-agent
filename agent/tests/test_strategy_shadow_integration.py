from datetime import UTC, datetime

import pytest

from app.settings import settings
from app.task_state import TaskState
from app.control.planning import PlanStep, TaskPlan
from app.graph.strategy_shadow import (
    build_strategy_shadow_report,
    derive_server_observation,
    strategy_shadow_event,
)


def _state(*, steps=1, tool="search_products", task_type="shopping", **extra):
    now = datetime.now(UTC)
    plan_steps = [
        PlanStep(
            stepId=f"s{i}",
            description="server plan step",
            toolName=tool,
            arguments={"query": "phone"},
            argumentSources={"query": {"kind": "task_goal"}},
            expectedOutput={"items": "bounded"},
        )
        for i in range(steps)
    ]
    return TaskState(
        taskId="task-shadow-1",
        taskType=task_type,
        sessionId="session-1",
        status="ready",
        revision=3,
        goal="ignored by adapter",
        pendingQuestions=extra.pop("pendingQuestions", []),
        unknowns=extra.pop("unknowns", []),
        domainState=extra.pop("domainState", {}),
        activePlan=TaskPlan(
            planId="plan-1",
            basedOnRevision=3,
            steps=plan_steps,
        ),
        createdAt=now,
        updatedAt=now,
    )


def test_flag_off_is_default_and_adapter_has_no_execution_side_effects():
    assert settings.agent_strategy_shadow_enabled is False
    state = _state()
    before = state.model_dump(mode="json")
    report = build_strategy_shadow_report(state)
    assert report["strategy"] == "FAST"
    assert report["candidateOnly"] is True
    assert state.model_dump(mode="json") == before


def test_server_facts_map_fast_pae_and_bounded_react_candidates():
    assert build_strategy_shadow_report(_state())["strategy"] == "FAST"
    assert build_strategy_shadow_report(_state(steps=2))["strategy"] == "PAE"
    assert build_strategy_shadow_report(_state(), checkpoint_revision=2)["strategy"] == "BOUNDED_REACT"
    assert build_strategy_shadow_report(_state(pendingQuestions=["q"]))["strategy"] == "BOUNDED_REACT"


def test_raw_strategy_reason_and_signals_are_ignored():
    state = _state(domainState={"strategy": "BOUNDED_REACT", "reason": "secret"})
    report = build_strategy_shadow_report(state)
    assert report["strategy"] == "FAST"
    assert "secret" not in repr(report)


def test_receipt_is_the_only_tool_uncertainty_source_and_is_revision_bound():
    state = _state(domainState={"v2RunMarker": {"runId": "run-1"}})
    assert derive_server_observation(state)["toolUncertainty"] is False
    receipt = {
        "runId": "run-1", "planId": "plan-1", "stepId": "s0", "revision": 3,
        "at": "2026-08-22T00:00:00Z", "status": "UNKNOWN", "queryRequired": True,
    }
    assert derive_server_observation(
        state,
        runner_receipt=receipt,
    )["toolUncertainty"] is True
    assert build_strategy_shadow_report(state, runner_receipt={"status": "UNKNOWN"})["errorCode"] == "receipt_ignored"


@pytest.mark.parametrize("field,value", [("runId", "run-other"), ("planId", "plan-other"), ("stepId", "s9"), ("revision", 4)])
def test_foreign_receipt_binding_is_ignored_not_uncertainty(field, value):
    state = _state(domainState={"v2RunMarker": {"runId": "run-1"}})
    receipt = {
        "runId": "run-1", "planId": "plan-1", "stepId": "s0", "revision": 3,
        "at": "2026-08-22T00:00:00Z", "status": "UNKNOWN", "queryRequired": True,
    }
    receipt[field] = value
    report = build_strategy_shadow_report(state, runner_receipt=receipt)
    assert report["strategy"] == "FAST"
    assert report["errorCode"] == "receipt_ignored"
    assert report["excluded"] is False


def test_caller_plan_step_run_overrides_are_not_public_trust_inputs():
    state = _state(domainState={"v2RunMarker": {"runId": "run-1"}})
    receipt = {
        "runId": "run-1", "planId": "plan-1", "stepId": "s0", "revision": 3,
        "at": "2026-08-22T00:00:00Z", "status": "UNKNOWN", "queryRequired": True,
    }
    assert build_strategy_shadow_report(state, runner_receipt=receipt)["strategy"] == "BOUNDED_REACT"
    for kwargs in (
        {"runner_plan_id": "plan-foreign"},
        {"expected_step_id": "s-foreign"},
        {"runner_run_id": "run-foreign"},
    ):
        report = build_strategy_shadow_report(
            state, runner_receipt=receipt, **kwargs
        )
        assert report["strategy"] == "FAST"
        assert report["errorCode"] == "receipt_ignored"
        assert report["excluded"] is False


def test_transaction_and_write_adjacent_tools_are_excluded():
    for task_type in ("transaction", "order", "payment", "checkout", "unknown"):
        assert build_strategy_shadow_report(_state(task_type=task_type))["excluded"] is True
    assert build_strategy_shadow_report(_state(tool="create_order"))["reasonCode"] == "POLICY_DENIED"
    assert build_strategy_shadow_report(_state(tool="create_order"))["strategy"] == "BOUNDED_REACT"


def test_invalid_validated_plan_and_sts_fail_open_with_redaction():
    report = build_strategy_shadow_report(_state(domainState={"shoppingTaskStateV2": {"bad": "x"}}))
    assert report["strategy"] is None
    assert report["errorCode"] == "shadow_contract_error"
    assert "bad" not in repr(report)


def test_event_is_redacted_and_does_not_contain_receipt_or_text():
    event = strategy_shadow_event(
        build_strategy_shadow_report(_state(domainState={"strategy": "FAST"})),
        task_id="task-shadow-1",
        revision=3,
    )
    assert event["routeDecision"] == "FAST"
    assert not {"goal", "strategy", "receipt", "prompt"} & set(event)
    assert all(not isinstance(value, (dict, list)) for value in event.values())
