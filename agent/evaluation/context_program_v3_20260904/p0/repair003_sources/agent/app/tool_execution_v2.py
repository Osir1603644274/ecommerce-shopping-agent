"""Explicit durable tool-boundary contract.

The adapter in this module owns the *external* read-only-tool protocol.  The
TaskState receipt is only a later OCC projection; it is never used to decide
whether a call may cross the tool boundary again.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Any
from .schemas import ToolTrace
from .graph.tool_inbox_v2 import (
    InboxResponse,
    InboxStatus,
    ToolInbox,
    ToolInboxSlot,
    sha256,
)


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 160:
        raise ValueError(f"{field} must be a bounded server identifier")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise ValueError(f"{field} must be SHA-256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be SHA-256") from exc
    return value


def _owner_hash(value: object) -> str:
    if type(value) is not str or len(value) != 16:
        raise ValueError("session_owner_hash must be the server owner digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("session_owner_hash must be the server owner digest") from exc
    return value

@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    task_id: str
    run_id: str
    thread_id: str
    plan_id: str
    step_id: str
    state_revision: int
    session_owner_hash: str
    execution_id: str
    logical_slot_key: str
    fence: int
    canonical_args_sha256: str

    def __post_init__(self) -> None:
        for field in ("task_id", "run_id", "thread_id", "plan_id", "step_id"):
            _identifier(getattr(self, field), field)
        _owner_hash(self.session_owner_hash)
        _digest(self.execution_id, "execution_id")
        _digest(self.logical_slot_key, "logical_slot_key")
        if type(self.state_revision) is not int or self.state_revision < 1:
            raise ValueError("state_revision must be positive")
        if type(self.fence) is not int or self.fence < 1:
            raise ValueError("fence must be positive")
        _digest(self.canonical_args_sha256, "canonical_args_sha256")


@dataclass(frozen=True, slots=True)
class ToolInboxExecution:
    """Frozen runner-owned outcome returned by a V2 tool boundary."""

    context: ToolExecutionContext
    trace: ToolTrace
    receipt: dict[str, object]
    replayed: bool

class ToolCallerV2(Protocol):
    async def __call__(self, tool_name: str, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolTrace: ...

class LegacyToolCallerForbidden(RuntimeError):
    """Raised when durable code tries to treat a two-argument caller as V2."""


class ToolInboxExecutionRejected(RuntimeError):
    """The runner-owned inbox did not authorize a tool-boundary crossing."""

    def __init__(self, response: InboxResponse) -> None:
        self.response = response
        super().__init__(response.error_code or response.status.value)


class ToolInboxCallerV2:
    """Claim -> IN_FLIGHT -> V2 caller -> frozen ledger receipt -> complete.

    ``UNKNOWN`` is intentionally terminal: a caller which died after business
    success and before ``complete`` cannot prove non-execution, so this adapter
    never creates a replacement call for that logical slot.
    """

    def __init__(self, *, inbox: ToolInbox, caller: ToolCallerV2) -> None:
        self._inbox = inbox
        self._caller = caller

    @property
    def inbox(self) -> ToolInbox:
        """Recovery probe; it grants no caller capability by itself."""
        return self._inbox

    @staticmethod
    def _context(
        slot: ToolInboxSlot,
        *,
        run_id: str,
        thread_id: str,
        session_owner_hash: str,
        response: InboxResponse,
    ) -> ToolExecutionContext:
        if response.execution_id is None or response.fence is None:
            raise ToolInboxExecutionRejected(response)
        return ToolExecutionContext(
            task_id=slot.task_id,
            run_id=run_id,
            thread_id=thread_id,
            plan_id=slot.plan_id,
            step_id=slot.step_id,
            state_revision=slot.state_revision,
            session_owner_hash=session_owner_hash,
            execution_id=response.execution_id,
            logical_slot_key=slot.logical_slot_key(),
            fence=response.fence,
            canonical_args_sha256=slot.canonical_args_sha256,
        )

    @staticmethod
    def _receipt(context: ToolExecutionContext, *, tool_name: str, trace: ToolTrace) -> dict[str, object]:
        result_hash = sha256(trace.model_dump(by_alias=True, mode="json"))
        tool_outcome = "tool_succeeded" if trace.ok else "tool_failed"
        return {
            "taskId": context.task_id,
            "runId": context.run_id,
            "threadId": context.thread_id,
            "sessionOwnerHash": context.session_owner_hash,
            "planId": context.plan_id,
            "stepId": context.step_id,
            "toolName": tool_name,
            "stateRevision": context.state_revision,
            "inputHash": context.canonical_args_sha256,
            "resultHash": result_hash,
            "executionId": context.execution_id,
            "logicalSlotKey": context.logical_slot_key,
            "fence": context.fence,
            "inboxStatus": InboxStatus.SUCCEEDED.value,
            "toolOutcome": tool_outcome,
        }

    async def execute(
        self,
        *,
        slot: ToolInboxSlot,
        run_id: str,
        thread_id: str,
        session_owner_hash: str,
        tool_name: str,
        arguments: dict[str, Any],
        on_in_flight: Callable[[ToolExecutionContext], Awaitable[None]] | None = None,
    ) -> ToolInboxExecution:
        """Execute exactly the slot described by already-validated arguments."""
        if tool_name != slot.tool_name:
            raise ValueError("tool name does not match ToolInbox slot")
        if sha256(arguments) != slot.canonical_args_sha256:
            raise ValueError("arguments do not match canonicalArgsSha256")
        claimed = await self._inbox.claim(slot, run_id=run_id, thread_id=thread_id)
        context = self._context(
            slot,
            run_id=run_id,
            thread_id=thread_id,
            session_owner_hash=session_owner_hash,
            response=claimed,
        )
        if claimed.status is InboxStatus.SUCCEEDED:
            if claimed.trace is None or claimed.receipt is None:
                raise ToolInboxExecutionRejected(claimed)
            if claimed.receipt != self._receipt(context, tool_name=tool_name, trace=claimed.trace):
                raise ToolInboxExecutionRejected(InboxResponse(InboxStatus.CONFLICT))
            return ToolInboxExecution(context, claimed.trace, claimed.receipt, True)
        if claimed.status is not InboxStatus.CLAIMED:
            raise ToolInboxExecutionRejected(claimed)
        inflight = await self._inbox.enter_in_flight(
            slot, execution_id=context.execution_id, fence=context.fence
        )
        if inflight.status is not InboxStatus.IN_FLIGHT:
            raise ToolInboxExecutionRejected(inflight)
        if on_in_flight is not None:
            await on_in_flight(context)
        trace = await self._caller(tool_name, dict(arguments), context)
        if not isinstance(trace, ToolTrace) or trace.tool != tool_name:
            raise RuntimeError("ToolCallerV2 returned an invalid ToolTrace")
        receipt = self._receipt(context, tool_name=tool_name, trace=trace)
        completed = await self._inbox.complete(
            slot,
            execution_id=context.execution_id,
            fence=context.fence,
            trace=trace,
            receipt=receipt,
        )
        if completed.status is not InboxStatus.SUCCEEDED or completed.trace is None or completed.receipt is None:
            raise ToolInboxExecutionRejected(completed)
        if completed.receipt != receipt or completed.trace != trace:
            raise ToolInboxExecutionRejected(InboxResponse(InboxStatus.CONFLICT))
        return ToolInboxExecution(context, completed.trace, completed.receipt, False)


__all__ = [
    "LegacyToolCallerForbidden",
    "ToolCallerV2",
    "ToolExecutionContext",
    "ToolInboxCallerV2",
    "ToolInboxExecution",
    "ToolInboxExecutionRejected",
]
