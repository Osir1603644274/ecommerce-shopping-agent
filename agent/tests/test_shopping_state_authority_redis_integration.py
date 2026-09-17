from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from app import task_state
from app.context_pack import build_context_pack
from app.domains.ecommerce.models import ShoppingGuideState
from app.domains.ecommerce.shopping_state_authority import (
    ShoppingStateAuthorityError,
    select_shopping_state_authority,
)
from app.domains.ecommerce.shopping_state_update import build_shopping_state_transition_patch
from app.planner import build_planner_context
from app.task_state import (
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    TaskStateRevisionConflictError,
    create_task_state,
    get_task_state,
    update_task_state,
)


def test_real_redis_cross_process_recovery_degradation_and_occ() -> None:
    async def run() -> None:
        url = os.getenv("TASK_STATE_ATOMIC_REDIS_URL")
        if not url:
            pytest.skip("set TASK_STATE_ATOMIC_REDIS_URL for real Redis")
        redis_module = pytest.importorskip("redis.asyncio")
        first = redis_module.from_url(url, decode_responses=True)
        previous = task_state._client
        task_state._client = first
        task_state._task_locks.clear()
        state = None
        second = None
        try:
            guide_raw = {
                "mode": "recommend",
                "category": "phone",
                "useCases": ["gaming_title_claim"],
                "requirements": [{
                    "key": "price_minor", "operator": "lte", "value": 300000,
                    "unit": "CNY_MINOR", "priority": "hard", "source": "user",
                }],
                "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
            }
            state = await create_task_state(TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="预算3000以内的游戏手机",
                sessionId=f"session-{uuid.uuid4().hex}",
                domainState={"shoppingGuide": guide_raw},
            ))
            v2_patch = build_shopping_state_transition_patch(
                state,
                ShoppingGuideState.model_validate(guide_raw),
                {"status": "collecting_information"},
                constraints_changed=False,
            )
            committed = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision,
                    actor="agent",
                    domainStatePatch=v2_patch,
                ),
            )
            stale_revision = state.revision

            await first.aclose()
            second = redis_module.from_url(url, decode_responses=True)
            task_state._client = second
            task_state._task_locks.clear()
            recovered = await get_task_state(state.task_id)
            assert recovered is not None
            selected = select_shopping_state_authority(
                domain_state=recovered.domain_state,
                task_id=recovered.task_id,
                task_revision=recovered.revision,
                goal=recovered.goal,
                unknowns=recovered.unknowns,
                pending_questions=recovered.pending_questions,
            )
            assert selected.source == "v2"

            raw = json.loads(await second.get(f"task-state:{state.task_id}"))
            raw["domainState"].pop("shoppingTaskStateV2")
            await second.set(f"task-state:{state.task_id}", json.dumps(raw, ensure_ascii=False))
            degraded = await get_task_state(state.task_id)
            assert degraded is not None
            selected = select_shopping_state_authority(
                domain_state=degraded.domain_state,
                task_id=degraded.task_id,
                task_revision=degraded.revision,
                goal=degraded.goal,
                unknowns=degraded.unknowns,
                pending_questions=degraded.pending_questions,
            )
            assert selected.source == "legacy_degraded"
            assert selected.degraded_reason == "v2_missing_legacy_semantic_equivalent"

            divergent = dict(degraded.domain_state)
            divergent["shoppingGuide"] = {
                **divergent["shoppingGuide"],
                "requirements": [{
                    **divergent["shoppingGuide"]["requirements"][0],
                    "value": 200000,
                }],
            }
            with pytest.raises(ShoppingStateAuthorityError, match="binding_mismatch"):
                select_shopping_state_authority(
                    domain_state=divergent,
                    task_id=degraded.task_id,
                    task_revision=degraded.revision,
                    goal=degraded.goal,
                    unknowns=degraded.unknowns,
                    pending_questions=degraded.pending_questions,
                )

            with pytest.raises(TaskStateRevisionConflictError):
                await update_task_state(
                    state.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=stale_revision,
                        actor="agent",
                        domainStatePatch={"staleWriter": True},
                    ),
                )
        finally:
            client = second or first
            if state is not None:
                await client.delete(
                    f"task-state:{state.task_id}",
                    f"task-state:{state.task_id}:events",
                )
            if second is not None:
                await second.aclose()
            task_state._client = previous
            task_state._task_locks.clear()

    asyncio.run(run())


def test_real_redis_task_state_cas_synchronizes_top_level_v2_semantics() -> None:
    async def run() -> None:
        url = os.getenv("TASK_STATE_ATOMIC_REDIS_URL")
        if not url:
            pytest.skip("set TASK_STATE_ATOMIC_REDIS_URL for real Redis")
        redis_module = pytest.importorskip("redis.asyncio")
        first = redis_module.from_url(url, decode_responses=True)
        previous = task_state._client
        task_state._client = first
        task_state._task_locks.clear()
        state = None
        second = None
        try:
            state = await create_task_state(TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="initial goal",
                sessionId=f"session-{uuid.uuid4().hex}",
                domainState={"shoppingGuide": {
                    "mode": "recommend",
                    "category": "phone",
                    "requirements": [],
                    "candidateIds": [],
                    "comparedIds": [],
                    "evidenceStatus": "missing",
                }},
            ))

            state = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision,
                    actor="agent",
                    goal="NEW GOAL",
                ),
            )
            state = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision,
                    actor="agent",
                    addUnknowns=["new unknown"],
                ),
            )
            state = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision,
                    actor="agent",
                    pendingQuestions=["independent question"],
                ),
            )
            state = await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision,
                    actor="agent",
                    resolveUnknowns=["new unknown"],
                ),
            )

            await first.aclose()
            second = redis_module.from_url(url, decode_responses=True)
            task_state._client = second
            task_state._task_locks.clear()
            reloaded = await get_task_state(state.task_id)
            assert reloaded is not None
            selected = select_shopping_state_authority(
                domain_state=reloaded.domain_state,
                task_id=reloaded.task_id,
                task_revision=reloaded.revision,
                goal=reloaded.goal,
                unknowns=reloaded.unknowns,
                pending_questions=reloaded.pending_questions,
            )
            assert selected.source == "v2"
            assert selected.goal == "NEW GOAL"
            assert selected.unknowns == ()
            assert selected.pending_questions == ("independent question",)
            assert reloaded.domain_state["shoppingTaskStateV2"]["pendingQuestions"] == [
                "independent question"
            ]

            pack = await build_context_pack(reloaded, history=[], run_id="redis-v2-reload")
            planner = build_planner_context(reloaded, "continue", [])
            assert pack.goal == planner.goal == "NEW GOAL"
            assert pack.unknowns == []
            assert planner.unknowns == ()
            assert pack.pending_questions == ["independent question"]
            assert planner.pending_questions == ("independent question",)

            # A damaged V2.1 cannot be silently unbound and committed with a
            # newer top-level TaskState. The failed synchronization must leave
            # the Redis CAS record byte-for-byte at the current revision.
            raw_key = f"task-state:{state.task_id}"
            damaged = json.loads(await second.get(raw_key))
            damaged["domainState"]["shoppingTaskStateV2"]["pendingQuestions"] = [""]
            await second.set(raw_key, json.dumps(damaged, ensure_ascii=False))
            before_failed_write = await second.get(raw_key)
            with pytest.raises(ShoppingStateAuthorityError):
                await update_task_state(
                    state.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=state.revision,
                        actor="agent",
                        goal="MUST NOT COMMIT",
                    ),
                )
            assert await second.get(raw_key) == before_failed_write
            unchanged = await get_task_state(state.task_id)
            assert unchanged is not None
            assert unchanged.revision == state.revision
            assert unchanged.goal == "NEW GOAL"
        finally:
            client = second or first
            if state is not None:
                await client.delete(
                    f"task-state:{state.task_id}",
                    f"task-state:{state.task_id}:events",
                )
            if second is not None:
                await second.aclose()
            task_state._client = previous
            task_state._task_locks.clear()

    asyncio.run(run())
