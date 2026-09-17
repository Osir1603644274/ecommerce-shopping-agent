import asyncio
import os
import uuid

import pytest

from app.domains.ecommerce.strategy_routing import ReceiptIssuer, RunnerRouteAuthority
from app.graph.strategy_dispatch import DispatchTerminal, StrategyDispatchAdapter
from tests.test_strategy_dispatch import DispatchClock, _parts


def _redis_test(function):
    def wrapped():
        return asyncio.run(function())
    return wrapped


def _route():
    authority = RunnerRouteAuthority()
    return authority.route(authority.observe_server({
        "taskKind": "structured", "planSteps": 2, "needsObservation": True,
        "dynamicRevision": False, "toolUncertainty": False, "policyDenied": False,
    }))


@_redis_test
async def test_real_redis_dispatch_claim_commit_replay_and_concurrent_winner():
    url = os.getenv("STRATEGY_LEDGER_REDIS_URL")
    if not url:
        pytest.skip("set STRATEGY_LEDGER_REDIS_URL to run real Redis integration")
    redis = pytest.importorskip("redis.asyncio").from_url(url, decode_responses=False)
    try:
        task_id = f"task-dispatch-real-{uuid.uuid4().hex}"
        clock = DispatchClock()
        clock, request, slot, budget, ledger = _parts(clock=clock, redis=redis, task_id=task_id)
        calls = 0

        async def caller(_):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"raw": "discarded"}

        adapter = StrategyDispatchAdapter(enabled=True)
        results = await asyncio.gather(*[
            adapter.dispatch(route_decision=_route(), budget=budget, request=request, ledger=ledger,
                             receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller)
            for _ in range(2)
        ])
        assert sum(result.terminal is DispatchTerminal.COMPLETED for result in results) == 1
        assert sum(result.terminal is DispatchTerminal.IN_PROGRESS for result in results) == 1
        assert calls == 1
        replay = await adapter.dispatch(route_decision=_route(), budget=budget, request=request, ledger=ledger,
                                         receipt_issuer=ReceiptIssuer(clock=clock), slot=slot, tool_caller=caller)
        assert replay.terminal is DispatchTerminal.REPLAY
        assert calls == 1
    finally:
        await redis.aclose()
