"""Runtime-switch defaults and validation for ReAct V0."""

import pytest
from pydantic import ValidationError

from app.settings import Settings


def test_control_runtime_defaults_to_durable_react_v1() -> None:
    configured = Settings(_env_file=None)
    assert configured.agent_control_runtime == "react_v1"
    assert configured.agent_experimental_control_runtimes_enabled is False
    assert configured.agent_react_live_enabled is True
    assert configured.agent_graph_v2_durable_enabled is True
    assert configured.agent_react_max_iterations == 4
    assert configured.agent_react_v1_max_model_decisions == 2
    assert configured.agent_react_decision_timeout_seconds == 15.0
    assert configured.agent_react_final_answer_timeout_seconds == 30.0


@pytest.mark.parametrize(
    "runtime", ["fixed_v1", "react_v1"]
)
def test_control_runtime_accepts_production_and_rollback_values(runtime: str) -> None:
    assert Settings(
        _env_file=None, agent_control_runtime=runtime
    ).agent_control_runtime == runtime


@pytest.mark.parametrize(
    "runtime", ["react_v0_shadow", "react_v0", "adaptive_hybrid_v1"]
)
def test_historical_control_runtime_requires_explicit_experiment_gate(
    runtime: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_control_runtime=runtime)

    configured = Settings(
        _env_file=None,
        agent_experimental_control_runtimes_enabled=True,
        agent_control_runtime=runtime,
    )
    assert configured.agent_control_runtime == runtime


def test_control_runtime_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_control_runtime="react_magic")


def test_react_decision_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_react_decision_timeout_seconds=0)


def test_react_final_answer_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_react_final_answer_timeout_seconds=0)


@pytest.mark.parametrize("value", [0, 9])
def test_react_iteration_budget_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_react_max_iterations=value)


@pytest.mark.parametrize("value", [0, 1, 3])
def test_react_v1_model_decision_budget_is_exact(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_react_v1_max_model_decisions=value)
