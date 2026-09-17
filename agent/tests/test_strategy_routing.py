from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import pickle

import pytest

from app.domains.ecommerce.strategy_routing import (
    BoundedReactTerminal, BoundedToolRequest, BudgetLimits, BudgetState,
    ErrorCode, ReceiptIssuer, ReplayLedger, RunnerRouteAuthority, StateDelta,
    StepCommit, Strategy, TerminalReason, serialize_receipt,
    serialize_route_decision, serialize_step_commit, serialize_terminal,
)


class FakeClock:
    def __init__(self, tick=100, now=None):
        self.tick = tick
        self.now = now or datetime.now(UTC)
    def now_ms(self): return self.tick
    def now_utc(self): return self.now


def observation(**changes):
    raw = {"taskKind": "simple", "planSteps": 1, "needsObservation": False, "dynamicRevision": False, "toolUncertainty": False, "policyDenied": False}
    raw.update(changes)
    return raw


def limits():
    return {"maxTransitions": 2, "maxToolCalls": 2, "maxReplans": 1, "maxTokens": 100, "maxElapsedMs": 1000}


def test_runner_owned_observation_derives_route_and_decision_is_not_forgeable():
    authority = RunnerRouteAuthority()
    decision = authority.route(authority.observe_server(observation()))
    assert decision.strategy is Strategy.FAST
    assert b'"reasonCode":"SIMPLE_TASK"' in serialize_route_decision(decision)
    dynamic = authority.route(authority.observe_server(observation(taskKind="structured", planSteps=2, needsObservation=True)))
    assert dynamic.strategy is Strategy.BOUNDED_REACT
    with pytest.raises(ValueError, match="conflicting"):
        authority.observe_server(observation(planSteps=2))
    with pytest.raises(PermissionError, match="runner-issued"):
        authority.route({"taskKind": "simple"})
    forged = replace(decision, strategy=Strategy.BOUNDED_REACT)
    with pytest.raises(PermissionError, match="runner-issued"):
        serialize_route_decision(forged)
    assert not hasattr(decision, "model_copy") and not hasattr(decision, "model_construct")


def test_budget_revalidates_plain_limits_rejects_bool_and_uses_authoritative_clock():
    clock = FakeClock()
    budget = BudgetState.start(limits(), clock=clock)
    clock.tick = 140
    after = budget.consume(transitions=1, tool_calls=1, tokens=10)
    assert after.elapsed_ms_used == 40
    with pytest.raises(ValueError, match="exhausted"):
        after.consume(transitions=2)
    with pytest.raises(ValueError, match="strict"):
        BudgetState.start({**limits(), "maxTokens": True}, clock=clock)
    with pytest.raises(ValueError, match="positive"):
        after.consume()
    clock.tick = 1
    with pytest.raises(ValueError, match="backwards"):
        after.consume(tokens=1)
    assert not hasattr(after, "model_copy")
    with pytest.raises(PermissionError, match="not issued"):
        replace(after, tokens_used=0).consume(tokens=1)


def test_budget_binds_one_authority_clock_and_rejects_substitution_or_reset():
    clock = FakeClock(tick=100)
    budget = BudgetState.start(limits(), clock=clock)
    with pytest.raises(TypeError):
        budget.consume(clock=clock, tokens=1)
    replacement = FakeClock(tick=200)
    object.__setattr__(budget, "_authority_clock", replacement)
    with pytest.raises(ValueError, match="mutated"):
        budget.consume(tokens=1)

    clock.tick = 120
    after = BudgetState.start(limits(), clock=clock).consume(tokens=1)
    clock.tick = 1
    with pytest.raises(ValueError, match="backwards"):
        after.consume(tokens=1)


def test_allowlisted_tool_schemas_are_closed_canonical_and_reject_writes_nested_secrets_unicode():
    first = BoundedToolRequest.create(tool_name="search_products", arguments={"limit": 3, "query": "phone"}, state_revision=7, step_id="step-1")
    second = BoundedToolRequest.create(tool_name="search_products", arguments={"query": "phone", "limit": 3}, state_revision=7, step_id="step-1")
    assert first.args_digest == second.args_digest and first.canonical_args_text() == '{"limit":3,"query":"phone"}'
    for args in ({"query": {"authorization": "x"}}, {"query": "p", "paymentToken": "x"}, {"quｅry": "p", "limit": 1}):
        with pytest.raises((ValueError, PermissionError)):
            BoundedToolRequest.create(tool_name="search_products", arguments=args, state_revision=1, step_id="step-1")
    with pytest.raises(PermissionError):
        BoundedToolRequest.create(tool_name="create_order", arguments={}, state_revision=1, step_id="step-1")
    with pytest.raises(ValueError):
        BoundedToolRequest.create(tool_name="get_product_details", arguments={"productId": True}, state_revision=1, step_id="step-1")


def test_delta_explicit_patch_and_step_commit_occ_bind_request_receipt_and_next_revision():
    request = BoundedToolRequest.create(tool_name="search_products", arguments={"query": "phone", "limit": 2}, state_revision=3, step_id="step-3")
    issuer = ReceiptIssuer(clock=FakeClock(now=datetime.now(UTC)))
    receipt = issuer.issue(request, receipt_id="receipt-3", outcome="succeeded")
    delta = StateDelta.create(base_revision=3, operations=[{"op": "set", "field": "candidateScope", "value": {"scopeId": "scope-1", "candidateIds": [1, 2]}}])
    commit = StepCommit.commit(current_server_revision=3, delta=delta, receipt=receipt)
    assert b'"nextRevision":4' in serialize_step_commit(commit)
    with pytest.raises(ValueError, match="OCC"):
        StepCommit.commit(current_server_revision=4, delta=delta, receipt=receipt)
    for operations in ([{"op": "replace", "field": "fullState", "value": {}}], [{"op": "set", "field": "unknowns", "value": [], "entireState": {}}], [{"op": "set", "field": "unknowns", "value": []}, {"op": "remove", "field": "unknowns"}]):
        with pytest.raises(ValueError):
            StateDelta.create(base_revision=3, operations=operations)


def test_receipt_is_runner_issued_clock_bound_opaque_and_snapshot_validated():
    request = BoundedToolRequest.create(tool_name="compare_products", arguments={"productIds": [1, 2]}, state_revision=1, step_id="step-1")
    issuer = ReceiptIssuer(clock=FakeClock(now=datetime.now(UTC)))
    receipt = issuer.issue(request, receipt_id="receipt-1", outcome="failed", error_code=ErrorCode.TOOL_FAILED)
    assert serialize_receipt(receipt)
    with pytest.raises(ValueError, match="outcome"):
        issuer.issue(request, receipt_id="receipt-2", outcome="failed")
    with pytest.raises(ValueError, match="forbidden"):
        issuer.issue(request, receipt_id="diagnosis-secret", outcome="succeeded")
    future = ReceiptIssuer(clock=FakeClock(now=datetime.now(UTC) + timedelta(minutes=1)))
    with pytest.raises(ValueError, match="future skew"):
        future.issue(request, receipt_id="receipt-3", outcome="succeeded")
    object.__setattr__(receipt, "receipt_id", "receipt-mutated")
    with pytest.raises((ValueError, PermissionError), match="mutated|runner-issued"):
        serialize_receipt(receipt)


def test_replay_slots_bind_step_and_revision_not_args_and_return_independent_snapshots():
    request = BoundedToolRequest.create(tool_name="get_product_details", arguments={"productId": 1}, state_revision=4, step_id="step-4")
    receipt = ReceiptIssuer(clock=FakeClock(now=datetime.now(UTC))).issue(request, receipt_id="receipt-4", outcome="succeeded")
    ledger = ReplayLedger().record(receipt)
    replay = ledger.exact_replay(step_id="step-4", args_digest=receipt.args_digest, state_revision=4)
    assert replay is not receipt and serialize_receipt(replay) == serialize_receipt(receipt)
    other = BoundedToolRequest.create(tool_name="get_product_details", arguments={"productId": 2}, state_revision=4, step_id="step-4")
    conflicting = ReceiptIssuer(clock=FakeClock(now=datetime.now(UTC))).issue(other, receipt_id="receipt-5", outcome="succeeded")
    with pytest.raises(ValueError, match="conflict"):
        ledger.record(conflicting)
    object.__setattr__(replay, "receipt_id", "receipt-tampered")
    assert serialize_receipt(receipt)
    again = ledger.exact_replay(step_id="step-4", args_digest=receipt.args_digest, state_revision=4)
    assert again.receipt_id == "receipt-4"


def test_terminal_reasons_are_closed_and_public_surface_has_no_fulfillment_action():
    assert BoundedReactTerminal.create(reason=TerminalReason.BUDGET_EXHAUSTED, state_revision=8).reason is TerminalReason.BUDGET_EXHAUSTED
    import app.domains.ecommerce.strategy_routing as module
    assert "FulfillmentAction" not in module.__all__


def test_p0_all_formal_objects_require_real_identity_and_signed_bytes():
    authority = RunnerRouteAuthority()
    decision = authority.route(authority.observe_server(observation()))
    with pytest.raises(TypeError): copy.copy(decision)
    with pytest.raises(TypeError): copy.deepcopy(decision)
    with pytest.raises(PermissionError, match="runner-issued"):
        serialize_route_decision(replace(decision, strategy=Strategy.PAE))
    object.__setattr__(decision, "strategy", Strategy.PAE)
    with pytest.raises(ValueError, match="mutated"):
        serialize_route_decision(decision)

    clock = FakeClock()
    budget = BudgetState.start(limits(), clock=clock)
    with pytest.raises(PermissionError, match="not issued"):
        replace(budget, limits=BudgetLimits(1, 1, 1, 1, 1)).consume(tokens=1)
    object.__setattr__(budget, "tokens_used", 1)
    with pytest.raises(ValueError, match="mutated"):
        budget.consume(tokens=1)

    request = BoundedToolRequest.create(tool_name="search_products", arguments={"query": "phone", "limit": 2}, state_revision=1, step_id="step-1")
    issuer = ReceiptIssuer(clock=FakeClock(now=datetime.now(UTC)))
    with pytest.raises(TypeError): copy.copy(request)
    object.__setattr__(request, "state_revision", 2)
    with pytest.raises(ValueError, match="mutated"):
        issuer.issue(request, receipt_id="receipt-mutated", outcome="succeeded")

    delta = StateDelta.create(base_revision=1, operations=[{"op": "set", "field": "unknowns", "value": ["later"]}])
    receipt = issuer.issue(BoundedToolRequest.create(tool_name="search_products", arguments={"query": "phone", "limit": 2}, state_revision=1, step_id="step-2"), receipt_id="receipt-2", outcome="succeeded")
    with pytest.raises(TypeError): copy.copy(delta)
    object.__setattr__(delta, "base_revision", 2)
    with pytest.raises(ValueError, match="mutated"):
        StepCommit.commit(current_server_revision=1, delta=delta, receipt=receipt)

    with pytest.raises(TypeError):
        pickle.dumps(receipt)
    with pytest.raises(TypeError):
        copy.deepcopy(receipt)
    object.__setattr__(receipt, "receipt_id", "receipt-tampered")
    with pytest.raises(ValueError, match="mutated"):
        serialize_receipt(receipt)


def test_p0_replay_ledger_is_runner_private_and_terminal_is_strict():
    with pytest.raises(TypeError):
        ReplayLedger({("step-1", 1): b"{}"})
    ledger = ReplayLedger()
    request = BoundedToolRequest.create(tool_name="get_product_details", arguments={"productId": 1}, state_revision=1, step_id="step-1")
    receipt = ReceiptIssuer(clock=FakeClock(now=datetime.now(UTC))).issue(request, receipt_id="receipt-1", outcome="succeeded")
    ledger = ledger.record(receipt)
    ledger._snapshots[("step-1", 1)] = b"{}"
    with pytest.raises(ValueError, match="mutated"):
        ledger.exact_replay(step_id="step-1", args_digest=receipt.args_digest, state_revision=1)
    ledger = ReplayLedger().record(receipt)
    inner_mismatch = ReceiptIssuer(clock=FakeClock(now=datetime.now(UTC))).issue(
        BoundedToolRequest.create(
            tool_name="get_product_details",
            arguments={"productId": 2},
            state_revision=1,
            step_id="step-1",
        ),
        receipt_id="receipt-inner-mismatch",
        outcome="succeeded",
    )
    ledger._snapshots[("step-1", 1)] = serialize_receipt(inner_mismatch)
    with pytest.raises(ValueError, match="mutated"):
        ledger.exact_replay(step_id="step-1", args_digest=receipt.args_digest, state_revision=1)

    ledger = ReplayLedger().record(receipt)
    ledger._snapshots[("outer-mismatch", 1)] = serialize_receipt(receipt)
    with pytest.raises(ValueError, match="mutated"):
        ledger.exact_replay(step_id="step-1", args_digest=receipt.args_digest, state_revision=1)
    terminal = BoundedReactTerminal.create(reason=TerminalReason.COMPLETED, state_revision=1)
    assert serialize_terminal(terminal)
    with pytest.raises(ValueError):
        BoundedReactTerminal.create(reason="completed", state_revision=1)
    forged = replace(terminal, reason=TerminalReason.POLICY_DENIED)
    with pytest.raises(PermissionError):
        serialize_terminal(forged)
    object.__setattr__(terminal, "state_revision", 2)
    with pytest.raises(ValueError, match="mutated"):
        serialize_terminal(terminal)


def test_p1_structured_text_and_digest_boundaries_are_closed():
    for query in ("Bearer secret", "password", "diagnosis", "anaphylaxis"):
        with pytest.raises(ValueError):
            BoundedToolRequest.create(tool_name="search_products", arguments={"query": query, "limit": 1}, state_revision=1, step_id="step-1")
    with pytest.raises(ValueError):
        StateDelta.create(base_revision=1, operations=[{"op": "set", "field": "unknowns", "value": ["diagnosis"]}])
    with pytest.raises(ValueError):
        StateDelta.create(base_revision=1, operations=[{"op": "set", "field": "currentAction", "value": {"kind": "search", "reasonCode": "diagnosis"}}])
