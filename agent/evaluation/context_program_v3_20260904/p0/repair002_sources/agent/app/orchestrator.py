"""Central routing policy for the Agent runtime.

The orchestrator does not execute business tools itself.  Its job is to make
the choice between the persisted Harness and the temporary legacy rollback
path explicit, testable, and observable.
"""

from dataclasses import dataclass
from typing import Literal


OrchestratorRoute = Literal[
    "unified_harness",
    "legacy_rollback",
    "compatibility_fallback",
    "unsupported",
]


@dataclass(frozen=True)
class OrchestratorDecision:
    route: OrchestratorRoute
    reason: str


class AgentOrchestrator:
    """Choose exactly one runtime path for a user turn."""

    def __init__(
        self,
        *,
        mode: str = "unified",
        legacy_fallback_enabled: bool = False,
    ) -> None:
        if mode not in {"unified", "legacy"}:
            raise ValueError("mode must be unified or legacy")
        self._mode = mode
        self._legacy_fallback_enabled = legacy_fallback_enabled

    def decide(
        self,
        *,
        has_task_state: bool,
        harness_contract_covered: bool,
    ) -> OrchestratorDecision:
        if self._mode == "legacy":
            return OrchestratorDecision(
                route="legacy_rollback",
                reason="agent_orchestrator_mode=legacy",
            )
        if has_task_state and harness_contract_covered:
            return OrchestratorDecision(
                route="unified_harness",
                reason="task_state_present_and_tool_contract_covered",
            )
        if self._legacy_fallback_enabled:
            reason = (
                "task_state_missing"
                if not has_task_state
                else "tool_contract_not_migrated"
            )
            return OrchestratorDecision(
                route="compatibility_fallback",
                reason=reason,
            )
        return OrchestratorDecision(
            route="unsupported",
            reason=(
                "task_state_missing"
                if not has_task_state
                else "tool_contract_not_migrated"
            ),
        )
