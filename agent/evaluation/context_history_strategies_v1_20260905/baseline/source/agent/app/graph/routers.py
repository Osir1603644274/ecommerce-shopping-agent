"""Explicit V2 conditional routers.

Each router is a pure function of graph state: it checks the bounded transition
budget FIRST (``transition_limit_reached`` always routes to END), then maps the
node's ``action`` onto the next node.  A terminal boundary (``ask_user``,
``stop_turn``, ``task_completed``) routes to ``END``; the surrounding harness
boundary block in ``_run_explicit_harness_agent`` consumes ``transitions[-1]``
exactly like the V1 graph, so no controller code needs to change.

Day-2 adds the durable variants: the durable graph owns a real ``clarification``
node, so ``ask_user`` routes to the clarification interrupt instead of END, and
``route_after_clarification`` returns the resolved run to the Planner.
"""

from __future__ import annotations

from langgraph.graph import END

from .state import GraphV2State

__all__ = [
    "route_after_entry",
    "route_after_planner",
    "route_after_executor",
    "route_after_validator",
    "route_after_replanner",
    "route_after_entry_durable",
    "route_after_planner_durable",
    "route_after_executor_durable",
    "route_after_validator_durable",
    "route_after_replanner_durable",
    "route_after_clarification",
    "route_after_react_policy_durable",
]


def route_after_entry(state: GraphV2State) -> str:
    """Pick the entry business node chosen by the ``entry`` node."""
    if state.get("transition_limit_reached"):
        return END
    route = state.get("last_route_decision")
    if route == "planner":
        return "planner"
    if route == "replanner":
        return "replanner"
    # The entry node only ever writes these two values; anything else is a
    # programming error and must not loop the graph.
    return END


def route_after_planner(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    return {
        "continue_to_executor": "executor",
        "ready_for_validation": "validator",
    }.get(state.get("action"), END)


def route_after_executor(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    return {
        "continue_to_executor": "executor",
        "ready_for_validation": "validator",
    }.get(state.get("action"), END)


def route_after_validator(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    return {
        "ready_for_replanning": "replanner",
        "task_completed": END,
    }.get(state.get("action"), END)


def route_after_replanner(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    return {
        "continue_to_executor": "executor",
    }.get(state.get("action"), END)


# ── Day-2 durable variants ───────────────────────────────────────────────────


def route_after_entry_durable(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    route = state.get("last_route_decision")
    if route == "planner":
        return "planner"
    if route == "replanner":
        return "replanner"
    if route == "clarification":
        return "clarification"
    if route == "react_policy":
        return "react_policy"
    return END


def route_after_planner_durable(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    return {
        "continue_to_executor": "executor",
        "ready_for_validation": "validator",
        "ask_user": "clarification",
    }.get(state.get("action"), END)


def route_after_executor_durable(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    return {
        "continue_to_executor": "executor",
        "ready_for_validation": "validator",
        "ask_user": "clarification",
    }.get(state.get("action"), END)


def route_after_validator_durable(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    if state.get("control_policy") == "react_v1":
        if state.get("action") in {
            "ready_for_replanning",
            "task_completed",
        }:
            return "react_policy"
        last_outcome = state.get("react_last_outcome")
        if (
            state.get("action") == "stop_turn"
            and isinstance(last_outcome, dict)
            and last_outcome.get("errorCode") == "product_candidates_missing"
        ):
            # Fixed V1 deliberately stops after a ranked zero-result.  ReAct V1
            # gets one more bounded Decide pass so it can choose only among the
            # safe clarification/boundary options published by ContextView.
            return "react_policy"
    return {
        "ready_for_replanning": "replanner",
        "task_completed": END,
    }.get(state.get("action"), END)


def route_after_replanner_durable(state: GraphV2State) -> str:
    if state.get("transition_limit_reached"):
        return END
    return {
        "continue_to_executor": "executor",
        "ask_user": "clarification",
    }.get(state.get("action"), END)


def route_after_clarification(state: GraphV2State) -> str:
    """After a resolved clarification, return to the Planner with new info."""
    if state.get("transition_limit_reached"):
        return END
    if state.get("action") != "continue_to_executor":
        return END
    if state.get("control_policy") == "react_v1":
        return "react_policy"
    return "planner"


def route_after_react_policy_durable(state: GraphV2State) -> str:
    """Route one bounded policy choice into shared durable business nodes."""
    if state.get("transition_limit_reached"):
        return END
    return {
        "continue_to_executor": "executor",
        "ask_user": "clarification",
    }.get(state.get("action"), END)
