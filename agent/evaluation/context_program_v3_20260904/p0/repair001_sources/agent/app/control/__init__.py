"""Stable, lazily loaded control-plane exports.

Lazy loading keeps compatibility facades such as :mod:`app.planning` free from
import cycles while callers migrate to the explicit control package.
"""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ExecutorRunResult": ("..executor", "ExecutorRunResult"),
    "HarnessStepResult": ("..harness", "HarnessStepResult"),
    "PlannerContext": ("..planner", "PlannerContext"),
    "PlannerResult": (".planning", "PlannerResult"),
    "ReplannerContext": ("..replanner", "ReplannerContext"),
    "ReplannerResult": ("..replanner", "ReplannerResult"),
    "TaskState": ("..task_state", "TaskState"),
    "ValidatorContext": ("..validator", "ValidatorContext"),
    "ValidatorResult": ("..validator", "ValidatorResult"),
    "run_executor_step": ("..executor", "run_executor_step"),
    "run_harness_step": ("..harness", "run_harness_step"),
    "run_planner_phase": ("..planner", "run_planner_phase"),
    "run_replanner_phase": ("..replanner", "run_replanner_phase"),
    "run_validator_phase": ("..validator", "run_validator_phase"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
