import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.ecommerce.strategy_routing import (
    BoundedToolRequest,
    BudgetLimits,
    BudgetState,
    ErrorCode,
    ReceiptIssuer,
    RunnerRouteAuthority,
    Strategy,
    UtcClock,
)
from app.graph.strategy_dispatch import DispatchTerminal, StrategyDispatchAdapter
from app.graph.strategy_receipt_ledger import StrategyLedgerSlot, StrategyReceiptLedger
from tests.test_strategy_receipt_ledger import AtomicLedgerRedis, Clock


class DispatchClock(Clock):
    def now_ms(self):
        return int(self.value.timestamp() * 1000)


def _route(*, simple=False, policy=False):
    authority = RunnerRouteAuthority()
    observation = authority.observe_server({
        "taskKind": "simple" if simple else "structured",
        "planSteps": 1 if simple else 2,
        "needsObservation": not simple and not policy,
        "dynamicRevision": False,
        "toolUncertainty": False,
        "policyDenied": policy,
    })
    return authority.route(observation)


def _parts(*, clock=None, redis=None, limits=None, task_id="task-dispatch"):
    clock = clock or DispatchClock()
    request = BoundedToolRequest.create(
        tool_name="search_products", arguments={"query": "phone", "limit": 2},
        state_revision=3, step_id="step-1",
    )
    slot = StrategyLedgerSlot.create(
        task_id=task_id, run_id="run-1", thread_id="thread-1", plan_id="plan-1",
        step_id="step-1", state_revision=3, tool_name=request.tool_name,
        canonical_args_digest=request.args_digest,
    )
    budget = BudgetState.start(limits or BudgetLimits.from_plain({
        "maxTransitions": 4, "maxToolCalls": 4, "maxReplans": 2,
        "maxTokens": 100, "maxElapsedMs": 10000,
    }), clock=clock)
    redis = redis or AtomicLedgerRedis()
    return clock, request, slot, budget, StrategyReceiptLedger(redis, clock=clock)


def _run(coro):
    return asyncio.run(coro)


def test_success_is_attested_without_returning_raw_tool_output():
    clock, request, slot, budget, ledger = _parts()
    calls = []

    async def caller(received):
        calls.append(received)
        return {"authorization": "Bearer secret", "raw": "private"}

    result = _run(StrategyDispatchAdapter(enabled=True).dispatch(
        route_decision=_route(), budget=budget, request=request, ledger=ledger,
        receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller,
    ))
    assert result.terminal is DispatchTerminal.COMPLETED
    assert len(calls) == 1
    assert result.receipt_snapshot and b"Bearer" not in result.receipt_snapshot
    assert result.receipt_digest and result.budget.tool_calls_used == 1


def test_tool_error_is_committed_as_tool_failed_without_exception_text():
    clock, request, slot, budget, ledger = _parts()

    async def caller(_):
        raise RuntimeError("password=never-return-this")

    result = _run(StrategyDispatchAdapter(enabled=True).dispatch(
        route_decision=_route(), budget=budget, request=request, ledger=ledger,
        receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller,
    ))
    assert result.terminal is DispatchTerminal.TOOL_FAILED
    assert result.receipt_snapshot and b"password" not in result.receipt_snapshot
    assert result.error_code is None


def test_replay_and_feature_off_have_zero_tool_calls():
    clock, request, slot, budget, ledger = _parts()
    calls = 0

    async def caller(_):
        nonlocal calls
        calls += 1
        return "ignored"

    adapter = StrategyDispatchAdapter(enabled=True)
    first = _run(adapter.dispatch(route_decision=_route(), budget=budget, request=request, ledger=ledger,
                                  receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller))
    assert first.terminal is DispatchTerminal.COMPLETED
    off_clock, off_request, off_slot, off_budget, off_ledger = _parts(task_id="task-off")
    off = _run(StrategyDispatchAdapter(enabled=False).dispatch(route_decision=_route(), budget=off_budget, request=off_request, ledger=off_ledger,
                                                               receipt_issuer=ReceiptIssuer(clock=off_clock), slot=off_slot, tool_caller=caller))
    assert off.terminal is DispatchTerminal.DISABLED
    assert not off_ledger._client.slots
    replay = _run(adapter.dispatch(route_decision=_route(), budget=off_budget, request=request, ledger=ledger,
                                   receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller))
    assert replay.terminal is DispatchTerminal.REPLAY
    assert calls == 1


def test_budget_and_policy_denial_prevent_ledger_or_tool_dispatch():
    limits = BudgetLimits.from_plain({"maxTransitions": 1, "maxToolCalls": 1, "maxReplans": 2, "maxTokens": 100, "maxElapsedMs": 10000})
    clock, request, slot, budget, ledger = _parts(limits=limits)
    exhausted = budget.consume(transitions=1, tool_calls=1)
    calls = []

    async def caller(_):
        calls.append(True)

    result = _run(StrategyDispatchAdapter(enabled=True).dispatch(route_decision=_route(), budget=exhausted, request=request, ledger=ledger,
                                                                  receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller))
    assert result.terminal is DispatchTerminal.BUDGET_EXHAUSTED and not calls and not ledger._client.slots
    policy = _run(StrategyDispatchAdapter(enabled=True).dispatch(route_decision=_route(policy=True), budget=budget, request=request, ledger=ledger,
                                                                 receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller))
    assert policy.terminal is DispatchTerminal.POLICY_DENIED and not calls and not ledger._client.slots
    fast = _run(StrategyDispatchAdapter(enabled=True).dispatch(route_decision=_route(simple=True), budget=budget, request=request, ledger=ledger,
                                                               receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller))
    assert fast.terminal is DispatchTerminal.POLICY_DENIED and not calls and not ledger._client.slots


def test_concurrent_claim_has_one_tool_winner_and_commit_failure_needs_review():
    clock, request, slot, budget, ledger = _parts()
    calls = 0

    async def caller(_):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "ignored"

    async def both():
        adapter = StrategyDispatchAdapter(enabled=True)
        return await asyncio.gather(*[
            adapter.dispatch(route_decision=_route(), budget=budget, request=request, ledger=ledger,
                             receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller)
            for _ in range(2)
        ])

    results = _run(both())
    assert sum(result.terminal is DispatchTerminal.COMPLETED for result in results) == 1
    assert sum(result.terminal is DispatchTerminal.IN_PROGRESS for result in results) == 1
    assert calls == 1

    class CommitFailRedis(AtomicLedgerRedis):
        async def strategy_ledger_commit(self, *args, **kwargs):
            raise ConnectionError("redis unavailable")

    fail_clock, fail_request, fail_slot, fail_budget, fail_ledger = _parts(redis=CommitFailRedis(), task_id="task-commit-fail")
    fail_calls = []

    async def fail_caller(_):
        fail_calls.append(True)
        return "ignored"

    failed = _run(StrategyDispatchAdapter(enabled=True).dispatch(route_decision=_route(), budget=fail_budget, request=fail_request, ledger=fail_ledger,
                                                                 receipt_issuer=ReceiptIssuer(clock=fail_clock), slot=fail_slot, tool_caller=fail_caller))
    assert failed.terminal is DispatchTerminal.NEEDS_REVIEW
    assert len(fail_calls) == 1


def test_expired_lease_recovery_cap_is_no_tool_unknown():
    clock, request, slot, budget, _ = _parts(task_id="task-recovery")
    redis = AtomicLedgerRedis()
    ledger = StrategyReceiptLedger(redis, clock=clock, lease_seconds=1, max_recovery_count=0)
    assert _run(ledger.claim(slot)).status.value == "CLAIMED"
    clock.advance(2)
    calls = []

    async def caller(_):
        calls.append(True)

    result = _run(StrategyDispatchAdapter(enabled=True).dispatch(route_decision=_route(), budget=budget, request=request, ledger=ledger,
                                                                  receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller))
    assert result.terminal is DispatchTerminal.UNKNOWN
    assert not calls


def test_write_request_injection_is_rejected_before_adapter_dispatch():
    with pytest.raises((PermissionError, ValueError)):
        BoundedToolRequest.create(tool_name="create_order", arguments={"productId": 1}, state_revision=3, step_id="step-1")
