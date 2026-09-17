"""LangGraph control plane for the project's bounded ReAct runtime.

The existing Harness remains the domain-safe implementation of one complete
Reason -> Act -> Observe transition:

* Planner/Replanner: Reason
* Executor tool call: Act
* Validator evidence check: Observe

LangGraph owns only transition routing and the maximum-step boundary. Redis
TaskState remains the single durable source of truth, so this graph is compiled
without a checkpointer and does not introduce a second persistence model.
"""

from __future__ import annotations

import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from openai import AsyncOpenAI

from .agent_trace import TraceBuilder
from .context_view import ContextProjector
from .executor import ToolCaller
from .harness import HarnessStepResult
from .task_state import TaskState


HarnessStepRunner = Callable[..., Awaitable[HarnessStepResult]]
ToolSchemaResolver = Callable[[TaskState], list[dict[str, Any]]]
TransitionCallback = Callable[[HarnessStepResult], Awaitable[None]]


class ReActGraphState(TypedDict):
    """Mutable state for one LangGraph invocation."""

    task_state: TaskState
    action: str
    transition_count: int
    transition_limit_reached: bool
    transitions: Annotated[list[HarnessStepResult], operator.add]


@dataclass(frozen=True)
class ReActGraphRuntime:
    """Static dependencies injected into graph nodes for one request."""

    user_message: str
    client: AsyncOpenAI
    model: str
    resolve_tool_schemas: ToolSchemaResolver
    step_runner: HarnessStepRunner
    tool_caller: ToolCaller
    trace_builder: TraceBuilder
    projector: ContextProjector | None
    max_transitions: int
    system_policies: dict[str, Any] | None = None
    on_transition: TransitionCallback | None = None


async def _react_cycle(
    state: ReActGraphState,
    runtime: Runtime[ReActGraphRuntime],
) -> dict[str, Any]:
    """Execute exactly one bounded Reason -> Act -> Observe transition."""

    dependencies = runtime.context
    current = state["task_state"]
    schemas = dependencies.resolve_tool_schemas(current)
    dependencies.trace_builder.start_phase("harness_step")
    try:
        result = await dependencies.step_runner(
            current,
            dependencies.user_message,
            schemas,
            client=dependencies.client,
            model=dependencies.model,
            system_policies=dependencies.system_policies,
            tool_caller=dependencies.tool_caller,
            trace_builder=dependencies.trace_builder,
            projector=dependencies.projector,
        )
    except Exception:
        dependencies.trace_builder.end_phase("exception")
        raise

    dependencies.trace_builder.end_phase(
        result.action,
        {"action": result.action},
    )
    if dependencies.on_transition is not None:
        await dependencies.on_transition(result)

    next_count = state["transition_count"] + 1
    return {
        "task_state": result.task_state,
        "action": result.action,
        "transition_count": next_count,
        "transition_limit_reached": (
            result.action == "continue_to_executor"
            and next_count >= dependencies.max_transitions
        ),
        "transitions": [result],
    }


def _route_after_cycle(state: ReActGraphState) -> str:
    if (
        state["action"] == "continue_to_executor"
        and not state["transition_limit_reached"]
    ):
        return "continue"
    return "end"


def _build_graph():
    builder = StateGraph(ReActGraphState, context_schema=ReActGraphRuntime)
    builder.add_node("react_cycle", _react_cycle)
    builder.add_edge(START, "react_cycle")
    builder.add_conditional_edges(
        "react_cycle",
        _route_after_cycle,
        {"continue": "react_cycle", "end": END},
    )
    # The V2 control plane (agent/app/graph/builder.py) compiles the same
    # contract with explicit planner/executor/validator/replanner nodes under
    # the name "ecommerce-guide-v2-control-plane".  V1 keeps its own stable
    # name so a compiled graph can be identified unambiguously in a dump.
    return builder.compile(name="ecommerce-guide-v1-controlled-react")


CONTROLLED_REACT_GRAPH = _build_graph()


async def run_controlled_react_graph(
    state: TaskState,
    runtime: ReActGraphRuntime,
) -> ReActGraphState:
    """Run the compiled graph to one user-facing boundary."""

    initial: ReActGraphState = {
        "task_state": state,
        "action": "continue_to_executor",
        "transition_count": 0,
        "transition_limit_reached": False,
        "transitions": [],
    }
    return await CONTROLLED_REACT_GRAPH.ainvoke(
        initial,
        context=runtime,
        config={"recursion_limit": max(runtime.max_transitions + 2, 3)},
    )
