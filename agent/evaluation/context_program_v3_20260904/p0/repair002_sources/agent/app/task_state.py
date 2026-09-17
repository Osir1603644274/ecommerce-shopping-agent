import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .planning import PlanningFailure, TaskPlan, validate_existing_plan_update
from .settings import settings

logger = logging.getLogger(__name__)

TASK_STATE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TASK_EVENTS = 200
MAX_SESSION_TASKS = 10

_CAS_STATE_SCRIPT = """
local raw = redis.call('get', KEYS[1])
if not raw then
    return {-1, -1}
end
local current = cjson.decode(raw)
local actual = tonumber(current.revision)
local expected = tonumber(ARGV[1])
if actual ~= expected then
    return {0, actual}
end
redis.call('set', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
return {1, expected + 1}
"""

_CAS_STATE_AND_SIDE_RECORD_SCRIPT = """
local raw = redis.call('get', KEYS[1])
if not raw then
    return {-1, -1}
end
local current = cjson.decode(raw)
local actual = tonumber(current.revision)
local expected = tonumber(ARGV[1])
if actual ~= expected then
    return {0, actual}
end
local existing = redis.call('get', KEYS[2])
if existing and existing ~= ARGV[4] then
    return {-2, actual}
end
redis.call('set', KEYS[2], ARGV[4], 'EX', tonumber(ARGV[3]))
redis.call('set', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
return {1, expected + 1}
"""

TaskStatus = Literal[
    "collecting_information",
    "ready",
    "executing",
    "paused",
    "completed",
    "cancelled",
]
TaskActor = Literal["user", "agent", "tool", "system"]
FactCertainty = Literal["confirmed", "predicted", "inferred"]
ConstraintOperator = Literal["eq", "lte", "gte", "in", "not_in"]
TaskRelation = Literal[
    "continue_current",
    "start_new",
    "resume_previous",
    "cancel_current",
    "ambiguous",
]
TaskStateTraceCallback = Callable[[dict[str, Any]], Awaitable[None]]

_ALLOWED_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "collecting_information": {"ready", "paused", "cancelled"},
    "ready": {
        "collecting_information",
        "executing",
        "paused",
        "completed",
        "cancelled",
    },
    "executing": {
        "collecting_information",
        "ready",
        "paused",
        "completed",
        "cancelled",
    },
    "paused": {
        "collecting_information",
        "ready",
        "executing",
        "cancelled",
    },
    "completed": set(),
    "cancelled": set(),
}

_client: redis.Redis | None = None
_task_locks: dict[str, asyncio.Lock] = {}
_session_locks: dict[str, asyncio.Lock] = {}

# Coroutine-local client override for the V2 shadow.  Only the task that sets
# it (and its descendants, which inherit the asyncio context) reads the
# isolated store; every concurrent authoritative request keeps reading the
# real ``_client``.  This is the isolation boundary — the shadow must never
# swap a process-global, or it would route other in-flight requests' TaskState
# reads/writes to the scratch store.
_TASK_STATE_CLIENT_OVERRIDE: ContextVar[Any | None] = ContextVar(
    "task_state_client_override", default=None
)


@asynccontextmanager
async def override_task_state_client(client: Any):
    """Scope ``_get_client()`` to ``client`` for the current coroutine only.

    ``ContextVar.set`` returns a token and ``reset`` restores the previous
    value, so nested and exception-ridden use always unwinds cleanly and never
    leaks the override into a later task.  Concurrent requests (separate asyncio
    tasks, contexts captured before this override) are unaffected.
    """
    token = _TASK_STATE_CLIENT_OVERRIDE.set(client)
    try:
        yield
    finally:
        _TASK_STATE_CLIENT_OVERRIDE.reset(token)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_client() -> redis.Redis:
    override = _TASK_STATE_CLIENT_OVERRIDE.get()
    if override is not None:
        return override
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _state_key(task_id: str) -> str:
    return f"task-state:{task_id}"


def _events_key(task_id: str) -> str:
    return f"task-state:{task_id}:events"


def _session_task_key(session_id: str) -> str:
    return f"task-state-session:{session_id}"


def _session_tasks_key(session_id: str) -> str:
    return f"task-state-session:{session_id}:tasks"


def _task_lock(task_id: str) -> asyncio.Lock:
    return _task_locks.setdefault(task_id, asyncio.Lock())


def _session_lock(session_id: str) -> asyncio.Lock:
    return _session_locks.setdefault(session_id, asyncio.Lock())


async def _emit_trace(
    trace: TaskStateTraceCallback | None,
    step: str,
    label: str,
    data: dict[str, Any] | None = None,
) -> None:
    if trace is None:
        return
    event: dict[str, Any] = {
        "step": step,
        "label": label,
        "at": _utc_now().isoformat(),
    }
    if data is not None:
        event["data"] = data
    await trace(event)


def _clean_unique_strings(values: list[str], field_name: str) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} 不能包含空字符串")
        if normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned


def _ensure_unique_keys(items: list[Any], field_name: str) -> list[Any]:
    keys = [item.key for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{field_name} 中的 key 不能重复")
    return items


class TaskFact(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(min_length=1, max_length=64)
    value: Any
    certainty: FactCertainty = "confirmed"
    source: TaskActor
    observed_at: datetime = Field(default_factory=_utc_now, alias="observedAt")

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return value.strip()


class TaskConstraint(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(min_length=1, max_length=64)
    operator: ConstraintOperator = "eq"
    value: Any
    source: TaskActor

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return value.strip()


class TaskStateCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_type: str = Field(
        default="generic",
        alias="taskType",
        min_length=1,
        max_length=64,
    )
    goal: str = Field(min_length=1, max_length=500)
    session_id: str | None = Field(
        default=None,
        alias="sessionId",
        min_length=1,
        max_length=64,
    )
    facts: list[TaskFact] = Field(default_factory=list)
    constraints: list[TaskConstraint] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list, max_length=50)
    pending_questions: list[str] = Field(
        default_factory=list,
        alias="pendingQuestions",
        max_length=20,
    )
    domain_state: dict[str, Any] = Field(
        default_factory=dict,
        alias="domainState",
    )

    @field_validator("task_type", "goal")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("facts")
    @classmethod
    def unique_fact_keys(cls, value: list[TaskFact]) -> list[TaskFact]:
        return _ensure_unique_keys(value, "facts")

    @field_validator("constraints")
    @classmethod
    def unique_constraint_keys(
        cls,
        value: list[TaskConstraint],
    ) -> list[TaskConstraint]:
        return _ensure_unique_keys(value, "constraints")

    @field_validator("unknowns")
    @classmethod
    def normalize_unknowns(cls, value: list[str]) -> list[str]:
        return _clean_unique_strings(value, "unknowns")

    @field_validator("pending_questions")
    @classmethod
    def normalize_questions(cls, value: list[str]) -> list[str]:
        return _clean_unique_strings(value, "pendingQuestions")


class TaskStatePatchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    expected_revision: int = Field(alias="expectedRevision", ge=1)
    actor: TaskActor = "user"
    status: TaskStatus | None = None
    goal: str | None = Field(default=None, min_length=1, max_length=500)
    upsert_facts: list[TaskFact] = Field(
        default_factory=list,
        alias="upsertFacts",
    )
    remove_fact_keys: list[str] = Field(
        default_factory=list,
        alias="removeFactKeys",
    )
    upsert_constraints: list[TaskConstraint] = Field(
        default_factory=list,
        alias="upsertConstraints",
    )
    remove_constraint_keys: list[str] = Field(
        default_factory=list,
        alias="removeConstraintKeys",
    )
    add_unknowns: list[str] = Field(default_factory=list, alias="addUnknowns")
    resolve_unknowns: list[str] = Field(
        default_factory=list,
        alias="resolveUnknowns",
    )
    pending_questions: list[str] | None = Field(
        default=None,
        alias="pendingQuestions",
        max_length=20,
    )
    domain_state_patch: dict[str, Any] = Field(
        default_factory=dict,
        alias="domainStatePatch",
        description="浅层合并到domainState；值为null时删除对应字段。",
    )
    active_plan: TaskPlan | None = Field(default=None, alias="activePlan")
    planning_failure: PlanningFailure | None = Field(
        default=None,
        alias="planningFailure",
    )

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("upsert_facts")
    @classmethod
    def unique_upsert_fact_keys(cls, value: list[TaskFact]) -> list[TaskFact]:
        return _ensure_unique_keys(value, "upsertFacts")

    @field_validator("upsert_constraints")
    @classmethod
    def unique_upsert_constraint_keys(
        cls,
        value: list[TaskConstraint],
    ) -> list[TaskConstraint]:
        return _ensure_unique_keys(value, "upsertConstraints")

    @field_validator(
        "remove_fact_keys",
        "remove_constraint_keys",
        "add_unknowns",
        "resolve_unknowns",
    )
    @classmethod
    def normalize_string_lists(
        cls,
        value: list[str],
        info,
    ) -> list[str]:
        return _clean_unique_strings(value, info.field_name)

    @field_validator("pending_questions")
    @classmethod
    def normalize_pending_questions(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is None:
            return None
        return _clean_unique_strings(value, "pendingQuestions")

    @model_validator(mode="after")
    def validate_patch(self):
        has_change = any(
            (
                self.status is not None,
                self.goal is not None,
                self.upsert_facts,
                self.remove_fact_keys,
                self.upsert_constraints,
                self.remove_constraint_keys,
                self.add_unknowns,
                self.resolve_unknowns,
                self.pending_questions is not None,
                self.domain_state_patch,
                "active_plan" in self.model_fields_set,
                "planning_failure" in self.model_fields_set,
            )
        )
        if not has_change:
            raise ValueError("TaskState patch 至少需要包含一项状态变更")

        upsert_fact_keys = {item.key for item in self.upsert_facts}
        if upsert_fact_keys.intersection(self.remove_fact_keys):
            raise ValueError("同一个 fact 不能同时 upsert 和 remove")
        upsert_constraint_keys = {item.key for item in self.upsert_constraints}
        if upsert_constraint_keys.intersection(self.remove_constraint_keys):
            raise ValueError("同一个 constraint 不能同时 upsert 和 remove")
        if set(self.add_unknowns).intersection(self.resolve_unknowns):
            raise ValueError("同一个 unknown 不能同时 add 和 resolve")
        planner_fields_changed = any(
            field_name in self.model_fields_set
            for field_name in ("active_plan", "planning_failure")
        )
        if planner_fields_changed and self.actor not in {"agent", "system"}:
            raise ValueError("Planner状态只能由 agent 或 system 更新")
        return self


class TaskState(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias="taskId")
    task_type: str = Field(alias="taskType")
    session_id: str | None = Field(default=None, alias="sessionId")
    status: TaskStatus
    revision: int = Field(ge=1)
    goal: str
    facts: list[TaskFact] = Field(default_factory=list)
    constraints: list[TaskConstraint] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(
        default_factory=list,
        alias="pendingQuestions",
    )
    domain_state: dict[str, Any] = Field(
        default_factory=dict,
        alias="domainState",
    )
    active_plan: TaskPlan | None = Field(default=None, alias="activePlan")
    planning_failure: PlanningFailure | None = Field(
        default=None,
        alias="planningFailure",
    )
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class TaskRelationDecision(BaseModel):
    """A model proposal for selecting the task that should receive a message."""

    model_config = ConfigDict(populate_by_name=True)

    relation: TaskRelation
    target_task_id: str | None = Field(default=None, alias="targetTaskId")
    reason: str = Field(min_length=1, max_length=500)
    clarification_question: str | None = Field(
        default=None,
        alias="clarificationQuestion",
        max_length=300,
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("target_task_id", "clarification_question")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_target(self) -> "TaskRelationDecision":
        if self.relation == "resume_previous" and not self.target_task_id:
            raise ValueError("resume_previous 必须提供 targetTaskId")
        return self


SessionTaskTransitionCallback = Callable[[TaskState, str], Awaitable[None]]


class TaskStateEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(alias="eventId")
    task_id: str = Field(alias="taskId")
    event_type: Literal["created", "updated"] = Field(alias="eventType")
    revision: int = Field(ge=1)
    actor: TaskActor
    changes: dict[str, Any]
    created_at: datetime = Field(alias="createdAt")


class TaskStateNotFoundError(LookupError):
    pass


class TaskStateRevisionConflictError(RuntimeError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"TaskState revision 冲突：expected={expected}, actual={actual}"
        )


class TaskStateSideRecordConflictError(RuntimeError):
    pass


class TaskStateTransitionError(ValueError):
    pass


def _new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:16]}"


def _new_event_id() -> str:
    return f"task-event-{uuid.uuid4().hex[:16]}"


async def _append_event(
    event: TaskStateEvent,
    *,
    trace: TaskStateTraceCallback | None = None,
) -> None:
    client = _get_client()
    key = _events_key(event.task_id)
    await client.rpush(key, event.model_dump_json(by_alias=True))
    await _emit_trace(
        trace,
        "event_appended",
        "事件已追加到 Redis List",
        {
            "redisKey": key,
            "eventType": event.event_type,
            "revision": event.revision,
        },
    )
    await client.ltrim(key, -MAX_TASK_EVENTS, -1)
    await _emit_trace(
        trace,
        "events_trimmed",
        "事件列表已裁剪",
        {"maxEvents": MAX_TASK_EVENTS},
    )
    await client.expire(key, TASK_STATE_TTL_SECONDS)
    await _emit_trace(
        trace,
        "event_ttl_refreshed",
        "事件列表 TTL 已刷新",
        {"ttlSeconds": TASK_STATE_TTL_SECONDS},
    )


async def create_task_state(
    request: TaskStateCreateRequest,
    *,
    trace: TaskStateTraceCallback | None = None,
) -> TaskState:
    await _emit_trace(
        trace,
        "request_validated",
        "创建请求已通过 Pydantic 校验",
        request.model_dump(by_alias=True, mode="json"),
    )
    now = _utc_now()
    state = TaskState(
        task_id=_new_task_id(),
        task_type=request.task_type,
        session_id=request.session_id,
        status="collecting_information",
        revision=1,
        goal=request.goal,
        facts=request.facts,
        constraints=request.constraints,
        unknowns=request.unknowns,
        pending_questions=request.pending_questions,
        domain_state=request.domain_state,
        created_at=now,
        updated_at=now,
    )
    if state.task_type == "ecommerce_guide":
        # Fresh ecommerce tasks are an explicit server-owned migration point:
        # valid compatibility input is dual-written into the complete V2.1
        # contract before the first snapshot becomes visible.
        from .domains.ecommerce.models import ShoppingGuideState
        from .domains.ecommerce.shopping_state_authority import bind_authoritative_write
        from .domains.ecommerce.shopping_state_update import (
            build_shopping_state_transition_patch,
        )
        from .settings import settings

        try:
            guide = ShoppingGuideState.model_validate(
                state.domain_state.get("shoppingGuide")
            )
        except (TypeError, ValueError):
            guide = None
        if guide is not None:
            domain_state = dict(state.domain_state)
            domain_state.update(build_shopping_state_transition_patch(
                state,
                guide,
                {"status": state.status},
                constraints_changed=False,
            ))
            domain_state = bind_authoritative_write(
                domain_state,
                task_id=state.task_id,
                task_revision=state.revision,
                goal=state.goal,
                unknowns=state.unknowns,
                pending_questions=state.pending_questions,
                mode=settings.shopping_state_authority,
            )
            state = state.model_copy(update={"domain_state": domain_state})
    await _emit_trace(
        trace,
        "state_assembled",
        "第一份 TaskState 快照已组装",
        state.model_dump(by_alias=True, mode="json"),
    )
    client = _get_client()
    key = _state_key(state.task_id)
    await client.set(key, state.model_dump_json(by_alias=True))
    await _emit_trace(
        trace,
        "snapshot_saved",
        "快照已写入 Redis",
        {"redisKey": key, "revision": state.revision},
    )
    await client.expire(key, TASK_STATE_TTL_SECONDS)
    await _emit_trace(
        trace,
        "snapshot_ttl_refreshed",
        "快照 TTL 已设置",
        {"ttlSeconds": TASK_STATE_TTL_SECONDS},
    )
    await _append_event(
        TaskStateEvent(
            event_id=_new_event_id(),
            task_id=state.task_id,
            event_type="created",
            revision=state.revision,
            actor="user",
            changes=request.model_dump(by_alias=True, mode="json"),
            created_at=now,
        ),
        trace=trace,
    )
    await _emit_trace(
        trace,
        "operation_completed",
        "创建操作完成",
        {"taskId": state.task_id, "revision": state.revision},
    )
    return state


async def get_task_state(task_id: str) -> TaskState | None:
    raw = await _get_client().get(_state_key(task_id))
    if raw is None:
        return None
    return TaskState.model_validate_json(raw)


async def get_session_task_state(session_id: str) -> TaskState | None:
    """Return the active TaskState bound to a chat session, if it still exists."""
    client = _get_client()
    binding_key = _session_task_key(session_id)
    task_id = await client.get(binding_key)
    if task_id is None:
        return None
    state = await get_task_state(task_id)
    if state is None:
        # The snapshot may have expired before the small session -> task pointer.
        await client.delete(binding_key)
        return None
    await client.expire(binding_key, TASK_STATE_TTL_SECONDS)
    return state


async def _register_session_task(
    client: redis.Redis,
    session_id: str,
    task_id: str,
) -> None:
    """Keep a bounded, recency-ordered registry without changing the active task."""
    key = _session_tasks_key(session_id)
    await client.zadd(key, {task_id: time.time_ns()})
    task_ids = await client.zrange(key, 0, -1)
    overflow = len(task_ids) - MAX_SESSION_TASKS
    if overflow > 0:
        await client.zrem(key, *task_ids[:overflow])
    await client.expire(key, TASK_STATE_TTL_SECONDS)


async def list_session_task_states(session_id: str) -> list[TaskState]:
    """Return recent session tasks from newest to oldest, cleaning stale ids."""
    client = _get_client()
    registry_key = _session_tasks_key(session_id)
    active_task_id = await client.get(_session_task_key(session_id))
    task_ids = await client.zrange(registry_key, 0, -1)
    if active_task_id is not None and active_task_id not in task_ids:
        await _register_session_task(client, session_id, active_task_id)
        task_ids.append(active_task_id)

    states: list[TaskState] = []
    stale_ids: list[str] = []
    for task_id in reversed(task_ids):
        state = await get_task_state(task_id)
        if state is None or state.session_id != session_id:
            stale_ids.append(task_id)
            continue
        states.append(state)
    if stale_ids:
        await client.zrem(registry_key, *stale_ids)
    if active_task_id is not None:
        states.sort(key=lambda state: state.task_id != active_task_id)
    if states:
        await client.expire(registry_key, TASK_STATE_TTL_SECONDS)
    return states


async def get_or_create_session_task_state(
    session_id: str,
    message: str,
    task_type: str = "local_life",
    domain_state: dict[str, Any] | None = None,
) -> tuple[TaskState, bool]:
    """Reuse one active task per session; terminal tasks start a fresh task."""
    async with _session_lock(session_id):
        current = await get_session_task_state(session_id)
        if current is not None and current.status not in {"completed", "cancelled"}:
            await _register_session_task(
                _get_client(),
                session_id,
                current.task_id,
            )
            return current, False

        state = await create_task_state(
            TaskStateCreateRequest(
                task_type=task_type,
                goal=message,
                session_id=session_id,
                domain_state=(
                    deepcopy(domain_state)
                    if domain_state is not None
                    else {"origin": "chat", "turnCount": 0}
                ),
            )
        )
        client = _get_client()
        binding_key = _session_task_key(session_id)
        await client.set(binding_key, state.task_id)
        await client.expire(binding_key, TASK_STATE_TTL_SECONDS)
        await _register_session_task(client, session_id, state.task_id)
        return state, True


async def apply_session_task_relation(
    session_id: str,
    message: str,
    decision: TaskRelationDecision,
    *,
    on_transition: SessionTaskTransitionCallback | None = None,
    task_type: str = "local_life",
    domain_state: dict[str, Any] | None = None,
) -> TaskState:
    """Apply a validated relation decision while preserving paused tasks."""

    async def notify(state: TaskState, phase: str) -> None:
        if on_transition is not None:
            await on_transition(state, phase)

    async with _session_lock(session_id):
        client = _get_client()
        binding_key = _session_task_key(session_id)
        current = await get_session_task_state(session_id)

        if decision.relation in {"continue_current", "ambiguous"}:
            if current is None:
                raise TaskStateNotFoundError(session_id)
            return current

        if decision.relation == "cancel_current":
            if current is None:
                raise TaskStateNotFoundError(session_id)
            cancelled = current
            if current.status not in {"completed", "cancelled"}:
                cancelled = await update_task_state(
                    current.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=current.revision,
                        actor="user",
                        status="cancelled",
                        domainStatePatch={
                            "taskRelationReason": decision.reason,
                        },
                    ),
                )
            await client.delete(binding_key)
            await notify(cancelled, "task_cancelled")
            return cancelled

        if decision.relation == "start_new":
            if (
                current is not None
                and current.status not in {"paused", "completed", "cancelled"}
            ):
                current = await update_task_state(
                    current.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=current.revision,
                        actor="system",
                        status="paused",
                        domainStatePatch={
                            "pausedFromStatus": current.status,
                            "taskRelationReason": decision.reason,
                        },
                    ),
                )
                await notify(current, "task_paused")

            created = await create_task_state(
                TaskStateCreateRequest(
                    task_type=task_type,
                    goal=message,
                    session_id=session_id,
                    domain_state={
                        **(
                            deepcopy(domain_state)
                            if domain_state is not None
                            else {"origin": "chat", "turnCount": 0}
                        ),
                        "taskRelationReason": decision.reason,
                    },
                )
            )
            await client.set(binding_key, created.task_id)
            await client.expire(binding_key, TASK_STATE_TTL_SECONDS)
            await _register_session_task(client, session_id, created.task_id)
            await notify(created, "task_started")
            return created

        recent_states = await list_session_task_states(session_id)
        target = next(
            (
                state
                for state in recent_states
                if state.task_id == decision.target_task_id
            ),
            None,
        )
        if target is None:
            raise TaskStateNotFoundError(decision.target_task_id or session_id)
        if current is not None and current.task_id == target.task_id:
            return current
        if target.status != "paused":
            raise TaskStateTransitionError(
                f"只能恢复 paused 任务，当前状态为 {target.status}"
            )

        if (
            current is not None
            and current.status not in {"paused", "completed", "cancelled"}
        ):
            current = await update_task_state(
                current.task_id,
                TaskStatePatchRequest(
                    expectedRevision=current.revision,
                    actor="system",
                    status="paused",
                    domainStatePatch={
                        "pausedFromStatus": current.status,
                        "taskRelationReason": decision.reason,
                    },
                ),
            )
            await notify(current, "task_paused")

        resume_status = target.domain_state.get("pausedFromStatus")
        if resume_status == "executing":
            resume_status = "ready"
        if resume_status not in {"collecting_information", "ready"}:
            resume_status = (
                "collecting_information"
                if target.pending_questions
                else "ready"
            )
        resumed = await update_task_state(
            target.task_id,
            TaskStatePatchRequest(
                expectedRevision=target.revision,
                actor="system",
                status=resume_status,
                domainStatePatch={
                    "pausedFromStatus": None,
                    "taskRelationReason": decision.reason,
                    "resumedAt": _utc_now().isoformat(),
                },
            ),
        )
        await client.set(binding_key, resumed.task_id)
        await client.expire(binding_key, TASK_STATE_TTL_SECONDS)
        await _register_session_task(client, session_id, resumed.task_id)
        await notify(resumed, "task_resumed")
        return resumed


async def clear_session_task_state(
    session_id: str,
    *,
    delete_task: bool = True,
) -> None:
    """Remove a session binding and, by default, its snapshot and event history."""
    async with _session_lock(session_id):
        client = _get_client()
        binding_key = _session_task_key(session_id)
        registry_key = _session_tasks_key(session_id)
        task_ids = await client.zrange(registry_key, 0, -1)
        active_task_id = await client.get(binding_key)
        if active_task_id is not None and active_task_id not in task_ids:
            task_ids.append(active_task_id)
        keys = [binding_key, registry_key]
        if delete_task:
            for task_id in task_ids:
                keys.extend((_state_key(task_id), _events_key(task_id)))
        await client.delete(*keys)


def _upsert_items(existing: list[Any], incoming: list[Any]) -> list[Any]:
    incoming_by_key = {item.key: item for item in incoming}
    merged = [
        incoming_by_key.pop(item.key, item)
        for item in existing
    ]
    merged.extend(incoming_by_key.values())
    return merged


def _remove_items(existing: list[Any], keys: list[str]) -> list[Any]:
    removed = set(keys)
    return [item for item in existing if item.key not in removed]


def _validate_status_transition(current: TaskStatus, target: TaskStatus) -> None:
    if current == target:
        return
    if target not in _ALLOWED_STATUS_TRANSITIONS[current]:
        raise TaskStateTransitionError(
            f"不允许从 {current} 转换到 {target}"
        )


async def update_task_state(
    task_id: str,
    patch: TaskStatePatchRequest,
    *,
    trace: TaskStateTraceCallback | None = None,
    immutable_side_record: tuple[str, str] | None = None,
) -> TaskState:
    # The local lock avoids duplicate work inside one event loop. Redis Lua CAS
    # remains the cross-process source of truth when Agent instances scale out.
    await _emit_trace(
        trace,
        "waiting_for_lock",
        "正在等待当前 taskId 的异步锁",
        {"taskId": task_id},
    )
    async with _task_lock(task_id):
        await _emit_trace(
            trace,
            "lock_acquired",
            "已获得异步锁",
            {"taskId": task_id},
        )
        current = await get_task_state(task_id)
        if current is None:
            raise TaskStateNotFoundError(task_id)
        await _emit_trace(
            trace,
            "snapshot_loaded",
            "已读取当前快照",
            {
                "taskId": task_id,
                "revision": current.revision,
                "status": current.status,
            },
        )
        if patch.expected_revision != current.revision:
            await _emit_trace(
                trace,
                "revision_conflict",
                "revision 校验失败",
                {
                    "expectedRevision": patch.expected_revision,
                    "actualRevision": current.revision,
                },
            )
            raise TaskStateRevisionConflictError(
                patch.expected_revision,
                current.revision,
            )
        await _emit_trace(
            trace,
            "revision_checked",
            "revision 校验通过",
            {"revision": current.revision},
        )

        next_status = patch.status or current.status
        _validate_status_transition(current.status, next_status)

        facts = _remove_items(current.facts, patch.remove_fact_keys)
        facts = _upsert_items(facts, patch.upsert_facts)
        constraints = _remove_items(
            current.constraints,
            patch.remove_constraint_keys,
        )
        constraints = _upsert_items(
            constraints,
            patch.upsert_constraints,
        )

        unknowns = [
            item for item in current.unknowns
            if item not in set(patch.resolve_unknowns)
        ]
        for item in patch.add_unknowns:
            if item not in unknowns:
                unknowns.append(item)

        domain_state = dict(current.domain_state)
        for key, value in patch.domain_state_patch.items():
            if value is None:
                domain_state.pop(key, None)
            else:
                domain_state[key] = value

        if current.task_type == "ecommerce_guide":
            # Import locally to keep the generic TaskState contract independent
            # from ecommerce model initialization.
            from .domains.ecommerce.shopping_state_authority import bind_authoritative_write
            from .settings import settings

            domain_state = bind_authoritative_write(
                domain_state,
                task_id=current.task_id,
                task_revision=current.revision + 1,
                goal=patch.goal or current.goal,
                unknowns=unknowns,
                pending_questions=(
                    list(patch.pending_questions)
                    if patch.pending_questions is not None
                    else list(current.pending_questions)
                ),
                mode=settings.shopping_state_authority,
                compatibility_projection_changed=(
                    "shoppingGuide" in patch.domain_state_patch
                    and "shoppingTaskStateV2" not in patch.domain_state_patch
                ),
            )

        active_plan = current.active_plan
        if "active_plan" in patch.model_fields_set:
            proposed_plan = patch.active_plan
            if proposed_plan is not None and (
                current.active_plan is None
                or proposed_plan.plan_id != current.active_plan.plan_id
            ):
                if proposed_plan.based_on_revision != current.revision:
                    raise TaskStateTransitionError(
                        "新Plan的basedOnRevision必须等于当前TaskState revision"
                    )
                if proposed_plan.status != "active" or any(
                    step.status != "pending" for step in proposed_plan.steps
                ):
                    raise TaskStateTransitionError(
                        "新Plan必须是active，且所有PlanStep必须是pending"
                    )
            elif proposed_plan is not None and current.active_plan is not None:
                try:
                    validate_existing_plan_update(current.active_plan, proposed_plan)
                except ValueError as exc:
                    raise TaskStateTransitionError(str(exc)) from exc
            active_plan = proposed_plan

        if next_status == "completed" and (
            active_plan is None or active_plan.status != "completed"
        ):
            raise TaskStateTransitionError(
                "TaskState只能与completed Plan在同一次Patch中完成"
            )
        # A completed Plan records one finished Agent action.  The durable task
        # may remain ready for a follow-up turn and receive a fresh Plan.

        planning_failure = current.planning_failure
        if "planning_failure" in patch.model_fields_set:
            proposed_failure = patch.planning_failure
            if (
                proposed_failure is not None
                and proposed_failure.based_on_revision != current.revision
            ):
                raise TaskStateTransitionError(
                    "PlanningFailure的basedOnRevision必须等于当前TaskState revision"
                )
            planning_failure = proposed_failure

        now = _utc_now()
        updated = current.model_copy(
            update={
                "status": next_status,
                "revision": current.revision + 1,
                "goal": patch.goal or current.goal,
                "facts": facts,
                "constraints": constraints,
                "unknowns": unknowns,
                "pending_questions": (
                    patch.pending_questions
                    if patch.pending_questions is not None
                    else current.pending_questions
                ),
                "domain_state": domain_state,
                "active_plan": active_plan,
                "planning_failure": planning_failure,
                "updated_at": now,
            }
        )
        await _emit_trace(
            trace,
            "patch_merged",
            "Patch 已合并为新快照",
            updated.model_dump(by_alias=True, mode="json"),
        )
        client = _get_client()
        key = _state_key(task_id)
        if immutable_side_record is None:
            cas_result = await client.eval(
                _CAS_STATE_SCRIPT,
                1,
                key,
                current.revision,
                updated.model_dump_json(by_alias=True),
                TASK_STATE_TTL_SECONDS,
            )
        else:
            side_key, side_payload = immutable_side_record
            if not side_key or not side_payload:
                raise ValueError("immutable side record must be non-empty")
            cas_result = await client.eval(
                _CAS_STATE_AND_SIDE_RECORD_SCRIPT,
                2,
                key,
                side_key,
                current.revision,
                updated.model_dump_json(by_alias=True),
                TASK_STATE_TTL_SECONDS,
                side_payload,
            )
        cas_status = int(cas_result[0])
        actual_revision = int(cas_result[1])
        if cas_status == -1:
            raise TaskStateNotFoundError(task_id)
        if cas_status == -2:
            raise TaskStateSideRecordConflictError(
                f"immutable side record conflict for task {task_id}"
            )
        if cas_status != 1:
            await _emit_trace(
                trace,
                "revision_conflict",
                "Redis CAS revision 校验失败",
                {
                    "expectedRevision": current.revision,
                    "actualRevision": actual_revision,
                },
            )
            raise TaskStateRevisionConflictError(
                current.revision,
                actual_revision,
            )
        await _emit_trace(
            trace,
            "snapshot_saved",
            "新快照已覆盖写入 Redis",
            {"redisKey": key, "revision": updated.revision},
        )
        await _emit_trace(
            trace,
            "snapshot_ttl_refreshed",
            "快照 TTL 已刷新",
            {"ttlSeconds": TASK_STATE_TTL_SECONDS},
        )
        event = TaskStateEvent(
            event_id=_new_event_id(),
            task_id=task_id,
            event_type="updated",
            revision=updated.revision,
            actor=patch.actor,
            changes=patch.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            created_at=now,
        )
        try:
            await _append_event(event, trace=trace)
        except Exception:
            if immutable_side_record is None:
                raise
            logger.warning(
                "atomic TaskState commit succeeded but event append failed",
                exc_info=True,
            )
        await _emit_trace(
            trace,
            "operation_completed",
            "更新操作完成，异步锁即将释放",
            {"taskId": task_id, "revision": updated.revision},
        )
        return updated


async def list_task_events(task_id: str) -> list[TaskStateEvent]:
    if await get_task_state(task_id) is None:
        raise TaskStateNotFoundError(task_id)
    raw_events = await _get_client().lrange(_events_key(task_id), 0, -1)
    return [
        TaskStateEvent.model_validate_json(raw_event)
        for raw_event in raw_events
    ]
