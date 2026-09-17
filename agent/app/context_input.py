"""Default-off typed seam for isolated model-context experiments.

No HTTP field, environment flag or model output can issue this scope. Ordinary
production messages and ContextPack construction remain byte-for-byte unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_scope: ContextVar[bool] = ContextVar("context_history_input_scope", default=False)
_pack_budget: ContextVar[int | None] = ContextVar("context_history_pack_budget", default=None)
BOUNDARY = "context-history-input-v1"


@contextmanager
def experimental_context_input(*, pack_budget_tokens: int | None = None):
    if pack_budget_tokens is not None and (type(pack_budget_tokens) is not int or not 1000 <= pack_budget_tokens <= 128000):
        raise ValueError("experimental_pack_budget_out_of_bounds")
    token = _scope.set(True)
    budget_token = _pack_budget.set(pack_budget_tokens)
    try:
        yield
    finally:
        _pack_budget.reset(budget_token)
        _scope.reset(token)


def is_experimental_context_input():
    return _scope.get()


def experimental_pack_budget():
    return _pack_budget.get() if _scope.get() else None


def phase_context(phase, payload):
    if not _scope.get():
        return payload
    if phase not in {"extraction", "planner", "replanner", "decision", "final_answer"}:
        raise ValueError("unknown_context_model_phase")
    return {"boundary": BOUNDARY, "phase": phase, "payload": deepcopy(payload)}
