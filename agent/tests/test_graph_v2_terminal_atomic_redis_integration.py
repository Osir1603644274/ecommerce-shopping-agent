from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid

import pytest

from app import task_state
from app.graph.resume import (
    prepare_terminal_response_receipt,
    read_terminal_response_receipt,
    session_owner_hash,
)
from app.task_state import (
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    TaskStateSideRecordConflictError,
    create_task_state,
    get_task_state,
    update_task_state,
)


def test_real_redis_finalization_and_outbox_commit_atomically() -> None:
    async def run() -> None:
        url = os.getenv("TASK_STATE_ATOMIC_REDIS_URL")
        if not url:
            pytest.skip("set TASK_STATE_ATOMIC_REDIS_URL for real Redis")
        redis = pytest.importorskip("redis.asyncio").from_url(url, decode_responses=True)
        previous = task_state._client
        task_state._client = redis
        task_state._task_locks.clear()
        state = None
        side_key = None
        session_id = f"session-{uuid.uuid4().hex}"
        try:
            state = await create_task_state(TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="atomic terminal outbox integration",
                sessionId=session_id,
            ))
            run_id = f"run-{uuid.uuid4().hex}"
            thread_id = f"v2-task:{state.task_id}:{run_id}"
            publication_id = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
            answer = "已经生成且只能发布一次的最终回答。"
            base_revision = state.revision
            prepared = prepare_terminal_response_receipt(
                task_id=state.task_id,
                run_id=run_id,
                thread_id=thread_id,
                proposal_hash=publication_id,
                session_id=session_id,
                answer=answer,
                state_revision=base_revision + 1,
                base_task_revision=base_revision,
                control_policy="react_v1",
            )
            assert prepared is not None
            side_key, side_payload = prepared
            finalization = {
                "version": 1,
                "taskId": state.task_id,
                "runId": run_id,
                "threadId": thread_id,
                "publicationId": publication_id,
                "sessionOwnerHash": session_owner_hash(session_id),
                "controlPolicy": "react_v1",
                "policyRevision": "react-v1-2026-08-27",
                "baseTaskRevision": base_revision,
                "finalizationRevision": base_revision + 1,
                "answerSha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            }
            committed = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=base_revision,
                    actor="agent",
                    domainStatePatch={"v2FinalAnswerReceipt": finalization},
                ),
                immutable_side_record=(
                    side_key,
                    json.dumps(side_payload, ensure_ascii=False, sort_keys=True),
                ),
            )

            # This is the response-loss recovery read: both sides must already
            # exist after the single atomic commit, with no repair write.
            recovered = await get_task_state(state.task_id)
            assert recovered is not None and recovered == committed
            assert await redis.get(side_key) is not None
            replay = await read_terminal_response_receipt(
                task_id=state.task_id,
                run_id=run_id,
                thread_id=thread_id,
                proposal_hash=publication_id,
                session_id=session_id,
                task_state=recovered,
            )
            assert replay is not None and replay["answer"] == answer

            conflicting = dict(side_payload)
            conflicting["answer"] = "冲突回答"
            with pytest.raises(TaskStateSideRecordConflictError):
                await update_task_state(
                    state.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=committed.revision,
                        actor="agent",
                        domainStatePatch={"mustNotCommit": True},
                    ),
                    immutable_side_record=(
                        side_key,
                        json.dumps(conflicting, ensure_ascii=False, sort_keys=True),
                    ),
                )
            unchanged = await get_task_state(state.task_id)
            assert unchanged is not None
            assert unchanged.revision == committed.revision
            assert "mustNotCommit" not in unchanged.domain_state
        finally:
            keys = []
            if state is not None:
                keys.extend([
                    f"task-state:{state.task_id}",
                    f"task-state:{state.task_id}:events",
                    f"task-state-session:{session_id}",
                    f"task-state-session:{session_id}:tasks",
                ])
            if side_key is not None:
                keys.append(side_key)
            if keys:
                await redis.delete(*keys)
            await redis.aclose()
            task_state._client = previous
            task_state._task_locks.clear()

    asyncio.run(run())
