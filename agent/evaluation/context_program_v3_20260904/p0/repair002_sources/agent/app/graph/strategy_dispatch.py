"""Single-step, read-only Strategy dispatch seam.

This module is deliberately not imported by ``resume.py`` or any graph entry
point.  It is the Batch 2b production-shaped seam for a later Batch 2c hook.
The durable ledger remains the authority for idempotency; raw tool results never
enter the returned contract or the ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable

from ..domains.ecommerce.strategy_routing import (
    BoundedToolRequest,
    BudgetState,
    ErrorCode,
    ReceiptIssuer,
    RouteDecision,
    Strategy,
    ToolReceipt,
    _request,
    _verify_budget,
    serialize_route_decision,
    serialize_receipt,
)
from .strategy_receipt_ledger import (
    LedgerResponse,
    LedgerStatus,
    StrategyLedgerSlot,
    StrategyReceiptLedger,
)

READ_ONLY_TOOLS = frozenset({"search_products", "get_product_details", "compare_products"})


class DispatchTerminal(StrEnum):
    DISABLED = "disabled"
    COMPLETED = "completed"
    REPLAY = "replay"
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_DENIED = "policy_denied"
    IN_PROGRESS = "in_progress"
    NEEDS_REVIEW = "needs_review"
    REVISION_CONFLICT = "revision_conflict"
    LEDGER_UNAVAILABLE = "ledger_unavailable"
    UNKNOWN = "unknown"
    TOOL_FAILED = "tool_failed"


@dataclass(frozen=True, slots=True)
class StrategyDispatchResult:
    terminal: DispatchTerminal
    state_revision: int
    budget: BudgetState
    receipt_snapshot: bytes | None = None
    receipt_digest: str | None = None
    error_code: str | None = None


ToolCaller = Callable[[BoundedToolRequest], Awaitable[object]]


def _receipt_fields(receipt: ToolReceipt, slot: StrategyLedgerSlot) -> None:
    if (
        receipt.tool_name != slot.tool_name
        or receipt.step_id != slot.step_id
        or receipt.state_revision != slot.state_revision
        or receipt.args_digest != slot.canonical_args_digest
    ):
        raise ValueError("receipt does not match dispatch slot")


def _snapshot(receipt: ToolReceipt | None) -> tuple[bytes | None, str | None]:
    if receipt is None:
        return None, None
    raw = bytes(serialize_receipt(receipt))
    return raw, hashlib.sha256(raw).hexdigest()


def _error_code(response: LedgerResponse) -> str | None:
    return response.error_code if response.error_code in {
        "policy_denied", "ledger_unavailable", "CORRUPT_RECORD",
    } else None


class StrategyDispatchAdapter:
    """Runner-owned one-shot dispatch for BOUNDED_REACT read tools."""

    def __init__(self, *, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise TypeError("dispatch feature flag must be a strict bool")
        self._enabled = enabled

    async def dispatch(
        self,
        *,
        route_decision: RouteDecision,
        budget: BudgetState,
        request: BoundedToolRequest,
        ledger: StrategyReceiptLedger,
        receipt_issuer: ReceiptIssuer,
        slot: StrategyLedgerSlot,
        tool_caller: ToolCaller,
    ) -> StrategyDispatchResult:
        """Run at most one server-authorized read-only tool call.

        ``tool_caller`` receives the validated request but its return value is
        intentionally discarded.  The upper layer can independently consume
        a bounded/redacted tool result; this seam only attests execution.
        """
        try:
            if type(route_decision) is not RouteDecision:
                raise PermissionError("route decision must be runner-issued")
            serialize_route_decision(route_decision)
            if type(budget) is not BudgetState:
                raise PermissionError("budget must be runner-issued")
            _verify_budget(budget)
            if type(request) is not BoundedToolRequest:
                raise PermissionError("request must be runner-issued")
            request = _request(request)
            if type(ledger) is not StrategyReceiptLedger:
                raise PermissionError("ledger must be server-owned")
            if type(receipt_issuer) is not ReceiptIssuer:
                raise PermissionError("receipt issuer must be server-owned")
            if type(slot) is not StrategyLedgerSlot:
                raise PermissionError("slot must be server-owned")
            slot = StrategyLedgerSlot.create(
                task_id=slot.task_id, run_id=slot.run_id, thread_id=slot.thread_id,
                plan_id=slot.plan_id, step_id=slot.step_id,
                state_revision=slot.state_revision, tool_name=slot.tool_name,
                canonical_args_digest=slot.canonical_args_digest,
            )
            if not callable(tool_caller):
                raise PermissionError("tool caller must be callable")
            if (
                request.tool_name not in READ_ONLY_TOOLS
                or route_decision.strategy is not Strategy.BOUNDED_REACT
                or route_decision.reason_code.value == "POLICY_DENIED"
                or slot.tool_name != request.tool_name
                or slot.step_id != request.step_id
                or slot.state_revision != request.state_revision
                or slot.canonical_args_digest != request.args_digest
            ):
                return StrategyDispatchResult(DispatchTerminal.POLICY_DENIED, request.state_revision, budget, error_code="policy_denied")
            if not self._enabled:
                return StrategyDispatchResult(DispatchTerminal.DISABLED, request.state_revision, budget)
        except (PermissionError, TypeError, ValueError):
            revision = request.state_revision if type(request) is BoundedToolRequest else 0
            return StrategyDispatchResult(DispatchTerminal.POLICY_DENIED, revision, budget, error_code="policy_denied")

        try:
            consumed = budget.consume(transitions=1, tool_calls=1)
        except (TypeError, ValueError):
            return StrategyDispatchResult(DispatchTerminal.BUDGET_EXHAUSTED, request.state_revision, budget)

        claimed = await ledger.claim(slot)
        if claimed.status is LedgerStatus.REPLAY:
            digest = hashlib.sha256(claimed.receipt_snapshot).hexdigest() if claimed.receipt_snapshot else None
            return StrategyDispatchResult(DispatchTerminal.REPLAY, request.state_revision, consumed, claimed.receipt_snapshot, digest)
        if claimed.status is not LedgerStatus.CLAIMED:
            terminal = {
                LedgerStatus.IN_PROGRESS: DispatchTerminal.IN_PROGRESS,
                LedgerStatus.NEEDS_REVIEW: DispatchTerminal.NEEDS_REVIEW,
                LedgerStatus.REVISION_CONFLICT: DispatchTerminal.REVISION_CONFLICT,
                LedgerStatus.LEDGER_UNAVAILABLE: DispatchTerminal.LEDGER_UNAVAILABLE,
                LedgerStatus.POLICY_DENIED: DispatchTerminal.POLICY_DENIED,
                LedgerStatus.UNKNOWN: DispatchTerminal.UNKNOWN,
                LedgerStatus.CORRUPT: DispatchTerminal.UNKNOWN,
            }.get(claimed.status, DispatchTerminal.NEEDS_REVIEW)
            return StrategyDispatchResult(terminal, request.state_revision, consumed, error_code=_error_code(claimed))
        if type(claimed.lease_token) is not str or not claimed.lease_token:
            return StrategyDispatchResult(DispatchTerminal.NEEDS_REVIEW, request.state_revision, consumed, error_code="invalid_lease")

        receipt: ToolReceipt
        try:
            await tool_caller(request)
            tool_outcome = "succeeded"
            tool_error = None
        except asyncio.CancelledError:
            raise
        except Exception:
            tool_outcome = "failed"
            tool_error = ErrorCode.TOOL_FAILED
        try:
            receipt = receipt_issuer.issue(
                request, receipt_id="receipt-" + secrets.token_hex(16),
                outcome=tool_outcome, error_code=tool_error,
            )
        except Exception:
            return StrategyDispatchResult(DispatchTerminal.NEEDS_REVIEW, request.state_revision, consumed, error_code="receipt_issue_failed")

        try:
            _receipt_fields(receipt, slot)
            receipt_raw, receipt_digest = _snapshot(receipt)
        except Exception:
            return StrategyDispatchResult(DispatchTerminal.NEEDS_REVIEW, request.state_revision, consumed, error_code="receipt_issue_failed")
        committed = await ledger.commit(slot, lease_token=claimed.lease_token or "", receipt=receipt)
        if committed.status in {LedgerStatus.COMMITTED, LedgerStatus.REPLAY}:
            committed_raw = committed.receipt_snapshot or receipt_raw
            committed_digest = hashlib.sha256(committed_raw).hexdigest() if committed_raw else receipt_digest
            terminal = DispatchTerminal.COMPLETED if receipt.outcome == "succeeded" else DispatchTerminal.TOOL_FAILED
            return StrategyDispatchResult(terminal, request.state_revision, consumed, committed_raw, committed_digest)
        return StrategyDispatchResult(
            DispatchTerminal.NEEDS_REVIEW,
            request.state_revision,
            consumed,
            receipt_raw,
            receipt_digest,
            error_code=committed.error_code or "commit_failed",
        )


__all__ = ["DispatchTerminal", "StrategyDispatchAdapter", "StrategyDispatchResult"]
