"""V2 control-plane graph builder.

The graph is genuinely LangGraph-visible: five explicit nodes (``entry``,
``planner``, ``executor``, ``validator``, ``replanner``) connected by explicit
conditional routers.  Each business node executes exactly one production phase
function and emits a redacted node-event pair.  No checkpointer and no second
persistence model — TaskState remains the single durable source of truth.

Day-2 adds the enumerable ``clarification`` node/edge and the durable
``build_graph_v2_durable`` variant: same business nodes plus ``clarification``,
durable routers that send ``ask_user`` into the real ``interrupt()`` boundary,
and a LangGraph checkpointer (``GraphV2CheckpointSaver``) passed by the durable
runner.  The checkpoint only ever stores allowlisted routing/identity channels —
TaskState is ``UntrackedValue`` and never enters it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from .checkpoint import GraphV2CheckpointSaver
from .nodes import entry_node
from .nodes.clarification import clarification_node
from .nodes.executor import executor_node
from .nodes.planner import planner_node
from .nodes.replanner import replanner_node
from .nodes.react_policy import react_policy_node
from .nodes.validator import validator_node
from .pause_control import GraphPauseRequested, matching_pause_request
from .routers import (
    route_after_clarification,
    route_after_entry,
    route_after_entry_durable,
    route_after_executor,
    route_after_executor_durable,
    route_after_planner,
    route_after_planner_durable,
    route_after_replanner,
    route_after_replanner_durable,
    route_after_react_policy_durable,
    route_after_validator,
    route_after_validator_durable,
)
from .runtime import GraphV2Runtime
from .state import GraphV2State

__all__ = [
    "GRAPH_V2_NAME",
    "CONTROLLED_GRAPH_V2",
    "build_graph_v2",
    "run_graph_v2",
    "build_graph_v2_durable",
]

# A stable, searchable compile name for the V2 control plane.
GRAPH_V2_NAME = "ecommerce-guide-v2-control-plane"


def _pause_guard(
    node_name: str,
    node: Callable[[GraphV2State, Any], Awaitable[dict[str, Any]]],
):
    """Pause only before the next node effect, leaving its prior checkpoint valid."""

    async def guarded(state: GraphV2State, runtime: Any) -> dict[str, Any]:
        request = await matching_pause_request(
            node_name=node_name,
            runtime=runtime.context,
        )
        if request is not None:
            raise GraphPauseRequested(request)
        return await node(state, runtime)

    guarded.__name__ = f"pause_guarded_{node_name}"
    return guarded


def build_graph_v2():
    builder = StateGraph(GraphV2State, context_schema=GraphV2Runtime)
    builder.add_node("entry", entry_node)
    builder.add_node("planner", planner_node)
    builder.add_node("executor", executor_node)
    builder.add_node("validator", validator_node)
    builder.add_node("replanner", replanner_node)
    builder.add_edge(START, "entry")
    builder.add_conditional_edges(
        "entry",
        route_after_entry,
        {"planner": "planner", "replanner": "replanner", END: END},
    )
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {"executor": "executor", "validator": "validator", END: END},
    )
    builder.add_conditional_edges(
        "executor",
        route_after_executor,
        {"executor": "executor", "validator": "validator", END: END},
    )
    builder.add_conditional_edges(
        "validator",
        route_after_validator,
        {"replanner": "replanner", "task_completed": END, END: END},
    )
    builder.add_conditional_edges(
        "replanner",
        route_after_replanner,
        {"executor": "executor", END: END},
    )
    return builder.compile(name=GRAPH_V2_NAME)


def build_graph_v2_durable(checkpointer: GraphV2CheckpointSaver | None = None):
    """Compile the Day-2 durable graph (adds the clarification node/edge).

    ``checkpointer`` is supplied by the durable runner; when None the compiled
    graph is only used for structure inspection (nodes/edges/routes).
    """
    builder = StateGraph(GraphV2State, context_schema=GraphV2Runtime)
    builder.add_node("entry", _pause_guard("entry", entry_node))
    builder.add_node("planner", _pause_guard("planner", planner_node))
    builder.add_node("executor", _pause_guard("executor", executor_node))
    builder.add_node("validator", _pause_guard("validator", validator_node))
    builder.add_node("replanner", _pause_guard("replanner", replanner_node))
    builder.add_node(
        "clarification", _pause_guard("clarification", clarification_node)
    )
    builder.add_node(
        "react_policy", _pause_guard("react_policy", react_policy_node)
    )
    builder.add_edge(START, "entry")
    builder.add_conditional_edges(
        "entry",
        route_after_entry_durable,
        {"planner": "planner", "replanner": "replanner",
         "react_policy": "react_policy",
         "clarification": "clarification", END: END},
    )
    builder.add_conditional_edges(
        "planner",
        route_after_planner_durable,
        {"executor": "executor", "validator": "validator",
         "clarification": "clarification", END: END},
    )
    builder.add_conditional_edges(
        "executor",
        route_after_executor_durable,
        {"executor": "executor", "validator": "validator",
         "clarification": "clarification", END: END},
    )
    builder.add_conditional_edges(
        "validator",
        route_after_validator_durable,
        {"replanner": "replanner", "react_policy": "react_policy",
         "task_completed": END, END: END},
    )
    builder.add_conditional_edges(
        "replanner",
        route_after_replanner_durable,
        {"executor": "executor", "clarification": "clarification", END: END},
    )
    builder.add_conditional_edges(
        "clarification",
        route_after_clarification,
        {"planner": "planner", "react_policy": "react_policy", END: END},
    )
    builder.add_conditional_edges(
        "react_policy",
        route_after_react_policy_durable,
        {"executor": "executor", "clarification": "clarification", END: END},
    )
    return builder.compile(name=GRAPH_V2_NAME, checkpointer=checkpointer)


CONTROLLED_GRAPH_V2 = build_graph_v2()


async def run_graph_v2(
    task_state: Any,
    runtime: GraphV2Runtime,
) -> GraphV2State:
    """Run the compiled V2 graph to one user-facing boundary.

    ``task_state`` is the durable TaskState from the surrounding request; the
    ``entry`` node may persist a recovered Executor claim before routing.
    """
    initial: GraphV2State = {
        "task_state": task_state,
        "action": "continue_to_executor",
        "transition_count": 0,
        "replan_count": 0,
        "transition_limit_reached": False,
        "terminal_outcome": None,
        "degraded_reason": None,
        "last_route_decision": None,
        "transitions": [],
        "node_events": [],
    }
    return await CONTROLLED_GRAPH_V2.ainvoke(
        initial,
        context=runtime,
        # Each business node counts one superstep; the budget check lives in
        # the nodes, so the recursion cap only needs generous headroom.
        config={"recursion_limit": max(runtime.max_transitions * 4 + 20, 60)},
    )
