from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app import task_state
from app.task_state import (
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    TaskStateSideRecordConflictError,
    create_task_state,
    get_task_state,
    update_task_state,
)


def test_real_redis_taskstate_and_side_record_commit_atomically() -> None:
    async def run() -> None:
        url = os.getenv("TASK_STATE_ATOMIC_REDIS_URL")
        if not url:
            pytest.skip("set TASK_STATE_ATOMIC_REDIS_URL for real Redis")
        redis = pytest.importorskip("redis.asyncio").from_url(
            url,
            decode_responses=True,
        )
        previous = task_state._client
        task_state._client = redis
        task_state._task_locks.clear()
        state = None
        side_key = f"test:atomic-side:{uuid.uuid4().hex}"
        event_failure_side_key = f"test:atomic-side:{uuid.uuid4().hex}"
        try:
            state = await create_task_state(TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="atomic side record integration",
                sessionId=f"session-{uuid.uuid4().hex}",
            ))
            committed = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision,
                    actor="agent",
                    domainStatePatch={"atomicReceipt": {"version": 1}},
                ),
                immutable_side_record=(side_key, '{"version": 1}'),
            )
            recovered = await get_task_state(state.task_id)
            assert recovered is not None
            assert recovered.revision == committed.revision
            assert recovered.domain_state["atomicReceipt"] == {"version": 1}
            assert await redis.get(side_key) == '{"version": 1}'

            with pytest.raises(TaskStateSideRecordConflictError):
                await update_task_state(
                    state.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=committed.revision,
                        actor="agent",
                        domainStatePatch={"mustNotCommit": True},
                    ),
                    immutable_side_record=(side_key, '{"version": 2}'),
                )
            unchanged = await get_task_state(state.task_id)
            assert unchanged is not None
            assert unchanged.revision == committed.revision
            assert "mustNotCommit" not in unchanged.domain_state
            assert await redis.get(side_key) == '{"version": 1}'

            with patch(
                "app.task_state._append_event",
                new=AsyncMock(side_effect=RuntimeError("event store unavailable")),
            ):
                after_event_failure = await update_task_state(
                    state.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=committed.revision,
                        actor="agent",
                        domainStatePatch={"eventFailureReceipt": True},
                    ),
                    immutable_side_record=(
                        event_failure_side_key,
                        '{"version": 1}',
                    ),
                )
            assert after_event_failure.revision == committed.revision + 1
            assert await redis.get(event_failure_side_key) == '{"version": 1}'
        finally:
            if state is not None:
                await redis.delete(
                    f"task-state:{state.task_id}",
                    f"task-state:{state.task_id}:events",
                )
            await redis.delete(side_key)
            await redis.delete(event_failure_side_key)
            await redis.aclose()
            task_state._client = previous
            task_state._task_locks.clear()

    asyncio.run(run())
