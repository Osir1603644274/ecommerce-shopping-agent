"""Persisted, phase-level debugger state for one Agent turn.

The normal chat runtime still runs to a user-facing boundary.  This module
stores a separate debug-turn control record so the learning UI can advance one
server phase per explicit request without treating a post-hoc trace as a live
pause.  ContextPack bytes are stored under a private sibling key and are never
included in the public response model.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .settings import settings
from .transport_resolver import (
    ToolTransportConfigurationError,
    validate_tool_transport_identity,
)


DEBUG_TURN_TTL_SECONDS = 60 * 60
DebugTurnStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
DebugTurnStage = Literal[
    "task_manager",
    "task_state",
    "context_pack",
    "planner",
    "executor",
    "validator",
    "replanner",
    "final_answer",
    "done",
]

_CONTEXT_DEBUG_STAGES = frozenset({
    "context_pack",
    "planner",
    "executor",
    "validator",
    "replanner",
    "final_answer",
})


class DebugTurnCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    message: str = Field(min_length=1, max_length=500)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=64)
    domain_hint: Literal["ecommerce"] = Field(default="ecommerce", alias="domainHint")

    @field_validator("message", "session_id")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class DebugTurnStepRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    expected_revision: int = Field(alias="expectedRevision", ge=1)


class DebugStepRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    index: int = Field(ge=1)
    stage: DebugTurnStage
    label: str
    code_file: str = Field(alias="codeFile")
    code_function: str = Field(alias="codeFunction")
    outcome: Literal["passed", "failed", "cancelled"]
    duration_ms: float = Field(alias="durationMs", ge=0)
    key_state: dict[str, Any] = Field(default_factory=dict, alias="keyState")
    message: str | None = None
    error_code: str | None = Field(default=None, alias="errorCode")
    finished_at: str = Field(alias="finishedAt")


class DebugTurn(BaseModel):
    """Public debugger checkpoint; contains only deliberately projected state."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    debug_turn_id: str = Field(alias="debugTurnId")
    request_id: str = Field(alias="requestId")
    revision: int = Field(ge=1)
    session_id: str = Field(alias="sessionId")
    message: str
    domain_hint: Literal["ecommerce"] = Field(alias="domainHint")
    status: DebugTurnStatus = "queued"
    next_stage: DebugTurnStage = Field(default="task_manager", alias="nextStage")
    run_id: str = Field(alias="runId")
    task_id: str | None = Field(default=None, alias="taskId")
    task_revision: int | None = Field(default=None, alias="taskRevision")
    relation: dict[str, Any] | None = None
    context_pack_hash: str | None = Field(default=None, alias="contextPackHash")
    context_token_count: int | None = Field(default=None, alias="contextTokenCount")
    allowed_tool_names: list[str] = Field(default_factory=list, alias="allowedToolNames")
    steps: list[DebugStepRecord] = Field(default_factory=list)
    final_answer: str | None = Field(default=None, alias="finalAnswer")
    guide_result: dict[str, Any] | None = Field(default=None, alias="guideResult")
    failure_code: str | None = Field(default=None, alias="failureCode")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    @classmethod
    def create(cls, request: DebugTurnCreateRequest) -> "DebugTurn":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            debugTurnId=f"debug-turn-{uuid.uuid4().hex[:12]}",
            requestId=f"req-{uuid.uuid4().hex[:12]}",
            revision=1,
            sessionId=request.session_id,
            message=request.message,
            domainHint=request.domain_hint,
            status="queued",
            nextStage="task_manager",
            runId=f"run-debug-{uuid.uuid4().hex[:12]}",
            createdAt=now,
            updatedAt=now,
        )


class DebugTurnNotFoundError(KeyError):
    pass


class DebugTurnRevisionConflictError(RuntimeError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"debug turn revision conflict: expected {expected}, actual {actual}"
        )


class DebugTurnStore:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client = client
        self._context_identities: dict[str, dict[str, str]] = {}
        self._context_identity_errors: dict[str, str] = {}

    def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(settings.redis_url, decode_responses=True)
        return self._client

    @staticmethod
    def _turn_key(debug_turn_id: str) -> str:
        return f"agent-debug-turn:{debug_turn_id}"

    @staticmethod
    def _pack_key(debug_turn_id: str) -> str:
        return f"agent-debug-turn:{debug_turn_id}:context-pack"

    async def create(self, request: DebugTurnCreateRequest) -> DebugTurn:
        turn = DebugTurn.create(request)
        await self.save(turn)
        return turn

    async def get(self, debug_turn_id: str) -> DebugTurn:
        raw = await self._get_client().get(self._turn_key(debug_turn_id))
        if raw is None:
            raise DebugTurnNotFoundError(debug_turn_id)
        await self._ensure_context_identity(debug_turn_id)
        return self._decorate_context_identity(
            DebugTurn.model_validate_json(raw)
        )

    async def save(self, turn: DebugTurn) -> None:
        client = self._get_client()
        await self._ensure_context_identity(turn.debug_turn_id)
        turn = self._decorate_context_identity(turn)
        await client.set(
            self._turn_key(turn.debug_turn_id),
            turn.model_dump_json(by_alias=True),
        )
        await client.expire(
            self._turn_key(turn.debug_turn_id),
            DEBUG_TURN_TTL_SECONDS,
        )

    async def save_context_pack(self, debug_turn_id: str, payload: str) -> None:
        identity = self._context_identity_from_payload(payload)
        self._context_identities[debug_turn_id] = identity
        self._context_identity_errors.pop(debug_turn_id, None)
        client = self._get_client()
        await client.set(self._pack_key(debug_turn_id), payload)
        await client.expire(self._pack_key(debug_turn_id), DEBUG_TURN_TTL_SECONDS)

    async def get_context_pack(self, debug_turn_id: str) -> str:
        raw = await self._get_client().get(self._pack_key(debug_turn_id))
        if raw is None:
            raise DebugTurnNotFoundError(f"{debug_turn_id}:context-pack")
        return raw

    @staticmethod
    def _context_identity_from_payload(payload: str) -> dict[str, str]:
        """Extract only the four public contract labels from a real Pack."""

        from .context_pack import ContextPack

        pack = ContextPack.model_validate_json(payload)
        identity = {
            "contextPolicyId": pack.context_policy_id,
            "contextPolicyVersion": pack.context_policy_version,
            "contextSkillId": pack.context_skill_id,
            "contextSkillVersion": pack.context_skill_version,
        }
        if not all(isinstance(value, str) and value.strip() for value in identity.values()):
            raise ValueError("ContextPack context identity is incomplete")
        return identity

    async def _ensure_context_identity(self, debug_turn_id: str) -> None:
        if debug_turn_id in self._context_identities:
            return
        if debug_turn_id in self._context_identity_errors:
            return
        raw = await self._get_client().get(self._pack_key(debug_turn_id))
        if raw is None:
            return
        try:
            self._context_identities[debug_turn_id] = (
                self._context_identity_from_payload(raw)
            )
        except (TypeError, ValueError):
            self._context_identity_errors[debug_turn_id] = (
                "context_identity_invalid"
            )

    def _decorate_context_identity(self, turn: DebugTurn) -> DebugTurn:
        identity = self._context_identities.get(turn.debug_turn_id)
        identity_error = self._context_identity_errors.get(turn.debug_turn_id)
        has_existing_identity = any(
            record.stage in _CONTEXT_DEBUG_STAGES
            and "contextIdentity" in record.key_state
            for record in turn.steps
        )
        has_existing_transport_identity = any(
            "toolTransportIdentity" in record.key_state
            for record in turn.steps
        )
        if (
            identity is None
            and identity_error is None
            and not has_existing_identity
            and not has_existing_transport_identity
        ):
            return turn
        if identity is None and identity_error is None:
            identity_error = "context_identity_unavailable"

        steps: list[DebugStepRecord] = []
        for record in turn.steps:
            key_state = dict(record.key_state)
            transport_identity = key_state.get("toolTransportIdentity")
            if transport_identity is not None and not (
                isinstance(transport_identity, dict)
                and transport_identity.get("status") == "error"
            ):
                try:
                    key_state["toolTransportIdentity"] = (
                        validate_tool_transport_identity(transport_identity)
                    )
                except ToolTransportConfigurationError as exc:
                    key_state["toolTransportIdentity"] = {
                        "status": "error",
                        "code": exc.code,
                    }
            if record.stage not in _CONTEXT_DEBUG_STAGES:
                if "contextIdentity" in key_state:
                    key_state["contextIdentity"] = {
                        "status": "error",
                        "code": "context_identity_mismatch",
                    }
                steps.append(record.model_copy(update={"key_state": key_state}))
                continue

            existing = key_state.get("contextIdentity")
            if identity_error is not None:
                key_state["contextIdentity"] = {
                    "status": "error",
                    "code": identity_error,
                }
            elif existing is not None and existing != identity:
                key_state["contextIdentity"] = {
                    "status": "error",
                    "code": "context_identity_mismatch",
                }
            else:
                key_state["contextIdentity"] = dict(identity)
            steps.append(record.model_copy(update={"key_state": key_state}))
        return turn.model_copy(update={"steps": steps}, deep=True)


_store: DebugTurnStore | None = None
_locks: dict[str, asyncio.Lock] = {}


def get_debug_turn_store() -> DebugTurnStore:
    global _store
    if _store is None:
        _store = DebugTurnStore()
    return _store


def set_debug_turn_store(store: DebugTurnStore) -> None:
    global _store
    _store = store


def debug_turn_lock(debug_turn_id: str) -> asyncio.Lock:
    return _locks.setdefault(debug_turn_id, asyncio.Lock())


def reset_debug_turn_locks() -> None:
    _locks.clear()


def completed_step(
    turn: DebugTurn,
    *,
    stage: DebugTurnStage,
    label: str,
    code_file: str,
    code_function: str,
    outcome: Literal["passed", "failed", "cancelled"],
    duration_ms: float,
    key_state: dict[str, Any],
    message: str | None = None,
    error_code: str | None = None,
) -> DebugStepRecord:
    return DebugStepRecord(
        index=len(turn.steps) + 1,
        stage=stage,
        label=label,
        codeFile=code_file,
        codeFunction=code_function,
        outcome=outcome,
        durationMs=round(duration_ms, 2),
        keyState=key_state,
        message=message,
        errorCode=error_code,
        finishedAt=datetime.now(timezone.utc).isoformat(),
    )


def next_checkpoint(turn: DebugTurn, **updates: Any) -> DebugTurn:
    updated = turn.model_copy(
        update={
            "revision": turn.revision + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **updates,
        },
        deep=True,
    )
    # The step endpoint returns this in-memory checkpoint directly.  Apply the
    # same server-owned identity projection used by the Redis save/read path so
    # API responses cannot lag behind the persisted node detail.
    store = _store
    return store._decorate_context_identity(updated) if store is not None else updated
