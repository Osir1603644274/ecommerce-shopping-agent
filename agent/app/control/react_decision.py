"""Strict model decision boundary for the minimal ReAct V0 runtime.

The decider receives exactly one bounded ``DecisionContextView`` and may only
submit one structured ``NextAction``.  This module never executes a tool and
never writes TaskState; server-owned validation happens before either is
possible.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from ..model_compat import tool_choice_kwargs
from .react_actions import ActionOutcome, NextAction
from .react_context import (
    DecisionContextView,
    build_decision_context_view,
)


REACT_ACTION_TOOL_NAME = "submit_react_action"
REACT_DECISION_SYSTEM_PROMPT = (
    "You are the bounded decision controller for a shopping agent. "
    "Observe only the supplied DecisionContextView and submit exactly one "
    "legal action option. Never answer the user in plain text. Return only the "
    "identity fields plus one exact allowedActionOptions.optionId. The server "
    "owns and materializes the action kind, tool arguments, product IDs, "
    "questions, evidence references, and review codes. Do not provide hidden "
    "reasoning or chain of thought."
)


class ReactActionProposal(BaseModel):
    """Untrusted model choice before the server materializes ``NextAction``."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["react-action-proposal-v1"] = Field(
        default="react-action-proposal-v1", alias="schemaVersion"
    )
    task_id: str = Field(alias="taskId", min_length=1, max_length=128)
    based_on_revision: int = Field(alias="basedOnRevision", ge=1)
    decision_view_hash: str = Field(
        alias="decisionViewHash", min_length=8, max_length=128
    )
    option_id: str = Field(
        alias="optionId",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )

    @field_validator("task_id", "decision_view_hash", "option_id", mode="after")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("proposal text cannot be blank")
        return normalized

class ReactDecisionError(ValueError):
    """A fail-closed model output or action-boundary rejection."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReactShadowObservation:
    """Side-effect-free result of one shadow decision attempt."""

    status: str
    task_revision: int
    duration_ms: float
    view: DecisionContextView | None = None
    action: NextAction | None = None
    error_code: str | None = None
    decision_source: Literal["deterministic_policy", "model"] | None = None
    selected_option_id: str | None = None


def _selected_option_id(
    action: NextAction,
    view: DecisionContextView,
) -> str | None:
    """Recover the published option selected for an exact materialized action."""

    for option in view.allowed_action_options:
        if (
            option.kind == action.kind
            and option.reason_code == action.reason_code
            and option.tool_name == action.tool_name
            and option.argument_refs == action.argument_refs
            and option.answer_context_ref == action.answer_context_ref
            and option.question == action.question
            and option.review_code == action.review_code
        ):
            return option.option_id
    return None


def _action_schema() -> dict[str, Any]:
    schema = ReactActionProposal.model_json_schema(by_alias=True)

    def strip_descriptions(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("description", None)
            value.pop("title", None)
            for nested in value.values():
                strip_descriptions(nested)
        elif isinstance(value, list):
            for nested in value:
                strip_descriptions(nested)

    strip_descriptions(schema)
    schema["additionalProperties"] = False
    return schema


REACT_ACTION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": REACT_ACTION_TOOL_NAME,
        "description": "Submit one revision-bound action from the published decision view.",
        "parameters": _action_schema(),
    },
}


def validate_next_action(
    action: NextAction,
    view: DecisionContextView,
) -> NextAction:
    """Validate identity, action eligibility and exact server-owned references."""

    if action.task_id != view.task_id:
        raise ReactDecisionError("task_identity_mismatch", "action taskId mismatch")
    if action.based_on_revision != view.task_revision:
        raise ReactDecisionError(
            "stale_task_revision", "action basedOnRevision is not current"
        )
    if action.decision_view_hash != view.decision_view_hash:
        raise ReactDecisionError(
            "decision_view_hash_mismatch", "action decisionViewHash mismatch"
        )
    if action.kind not in view.allowed_actions:
        raise ReactDecisionError("action_not_allowed", "action kind is not allowed")
    matching = [
        option
        for option in view.allowed_action_options
        if option.kind == action.kind
        and option.reason_code == action.reason_code
        and option.tool_name == action.tool_name
        and option.argument_refs == action.argument_refs
        and option.answer_context_ref == action.answer_context_ref
        and option.question == action.question
        and option.review_code == action.review_code
    ]
    if not matching:
        raise ReactDecisionError(
            "action_option_mismatch",
            "NextAction must exactly match one published server-owned option",
        )
    return action


def materialize_next_action(
    proposal: ReactActionProposal,
    view: DecisionContextView,
) -> NextAction:
    """Resolve the model's choice into a server-owned executable contract."""

    if proposal.task_id != view.task_id:
        raise ReactDecisionError("task_identity_mismatch", "proposal taskId mismatch")
    if proposal.based_on_revision != view.task_revision:
        raise ReactDecisionError(
            "stale_task_revision", "proposal basedOnRevision is not current"
        )
    if proposal.decision_view_hash != view.decision_view_hash:
        raise ReactDecisionError(
            "decision_view_hash_mismatch", "proposal decisionViewHash mismatch"
        )
    option = next(
        (
            item
            for item in view.allowed_action_options
            if item.option_id == proposal.option_id
        ),
        None,
    )
    if option is None:
        raise ReactDecisionError(
            "action_option_not_allowed", "proposal optionId is not published"
        )

    payload: dict[str, Any] = {
        "actionId": f"action-{uuid.uuid4().hex[:16]}",
        "taskId": view.task_id,
        "basedOnRevision": view.task_revision,
        "decisionViewHash": view.decision_view_hash,
        "kind": option.kind,
        "reasonCode": option.reason_code,
    }
    if option.kind == "CALL_TOOL":
        payload.update({
            "toolName": option.tool_name,
            "argumentRefs": dict(option.argument_refs or {}),
        })
    elif option.kind == "ANSWER":
        payload["answerContextRef"] = option.answer_context_ref
    elif option.kind == "ASK_CLARIFICATION":
        payload["question"] = option.question
    else:
        payload["reviewCode"] = option.review_code
    return validate_next_action(NextAction.model_validate(payload), view)


def deterministic_next_action(view: DecisionContextView) -> NextAction | None:
    """Resolve server-proven choices before paying for a model decision."""

    def select(option_id: str) -> NextAction:
        return materialize_next_action(
            ReactActionProposal(
                taskId=view.task_id,
                basedOnRevision=view.task_revision,
                decisionViewHash=view.decision_view_hash,
                optionId=option_id,
            ),
            view,
        )

    if (
        len(view.allowed_action_options) == 1
        and view.allowed_action_options[0].kind == "NEEDS_REVIEW"
    ):
        return select(view.allowed_action_options[0].option_id)

    # An unsupported capability has one strictly more informative safe result:
    # publish the server-authored evidence boundary.  Asking a model to choose
    # between that answer and a clarification adds latency/failure surface but
    # cannot add evidence or authorize another action.
    if (
        view.server_signals.get("unsupportedCapabilityEvidence", False)
        and view.answer_context_ref is not None
    ):
        return select("answer.validated_context")

    # Adaptive observations cross the model boundary only when the server has
    # published a real choice.  Calling a model to echo the sole legal option
    # adds latency and failure surface without adding decision value.
    if view.observation_summary.adaptive_trigger is not None:
        if len(view.allowed_action_options) == 1:
            return select(view.allowed_action_options[0].option_id)
        return None

    if view.pending_questions:
        return select("clarify.pending.0")

    # A successfully validated tool action is a server-owned proof that this
    # turn can advance to answer composition.  Check it before the original
    # user-message signals (boundComparison / broadCatalogDiscovery), which
    # intentionally remain true after execution and would otherwise dispatch
    # the same tool forever in a live Observe -> Act loop.
    last_outcome = view.last_outcome
    if (
        isinstance(last_outcome, dict)
        and last_outcome.get("status") == "SUCCEEDED"
        and last_outcome.get("validatorOutcome") == "PASSED"
        and view.answer_context_ref is not None
    ):
        return select("answer.validated_context")

    if view.server_signals.get("freshSearchRequired", False):
        search = next((tool for tool in view.allowed_tools if tool.name == "search_products"), None)
        if search is not None:
            return select("tool.search_products")

    if view.server_signals.get("boundComparison", False):
        compare = next(
            (tool for tool in view.allowed_tools if tool.name == "compare_products"),
            None,
        )
        if compare is not None:
            return select(f"tool.{compare.name}")

    if (
        (
            view.server_signals.get("presentationOnly", False)
            or view.server_signals.get("scopeAnswerRequested", False)
        )
        and view.answer_context_ref is not None
    ):
        return select("answer.validated_context")

    if view.server_signals.get("broadCatalogDiscovery", False):
        search = next(
            (tool for tool in view.allowed_tools if tool.name == "search_products"),
            None,
        )
        if search is not None:
            return select(f"tool.{search.name}")

    if view.server_signals.get("rerankRequested", False):
        rerank = next(
            (
                tool
                for tool in view.allowed_tools
                if tool.name == "rerank_products_in_scope"
            ),
            None,
        )
        if rerank is not None:
            return select(f"tool.{rerank.name}")

    if (
        view.answer_context_ref is None
        and not view.unknowns
        and len(view.allowed_tools) == 1
    ):
        only_tool = view.allowed_tools[0]
        return select(f"tool.{only_tool.name}")
    return None


async def decide_next_action(
    view: DecisionContextView,
    *,
    client: Any,
    model: str,
    on_response: Callable[[Any], None] | None = None,
) -> NextAction:
    """Run one structured decision call and fail closed on every shape error."""
    from ..context_input import phase_context

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": REACT_DECISION_SYSTEM_PROMPT + (
                "\nlongTermMemory contains user-confirmed soft preferences only. "
                "Current requirements always override memory. Memory cannot authorize "
                "tools, relax constraints, or provide product evidence; select only an existing option."
                if view.long_term_memory else ""
            )},
            {
                "role": "user",
                "content": json.dumps(
                    phase_context("decision", view.model_dump(by_alias=True, mode="json")),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
        tools=[REACT_ACTION_TOOL_SCHEMA],
        # Named tool selection is supported by V4 only when thinking is
        # explicitly disabled. Keep the generic compatibility default intact.
        **({"extra_body": {"thinking": {"type": "disabled"}}}
           if model.strip().lower().startswith("deepseek-v4-") else {}),
        **tool_choice_kwargs(
            model,
            {
                "type": "function",
                "function": {"name": REACT_ACTION_TOOL_NAME},
            },
            thinking_enabled=False,
        ),
    )
    if on_response is not None:
        on_response(response)
    if getattr(response.choices[0], "finish_reason", None) == "length":
        raise ReactDecisionError("action_output_truncated", "decider response was truncated")
    reply = response.choices[0].message
    calls = list(reply.tool_calls or [])
    if len(calls) != 1:
        raise ReactDecisionError(
            "action_call_count_invalid", "decider must submit exactly one tool call"
        )
    call = calls[0]
    if call.function.name != REACT_ACTION_TOOL_NAME:
        raise ReactDecisionError(
            "action_tool_name_invalid", "decider returned an unexpected tool"
        )
    try:
        raw = json.loads(call.function.arguments or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReactDecisionError(
            "action_json_invalid", "decider returned invalid action JSON"
        ) from exc
    if not isinstance(raw, dict):
        raise ReactDecisionError(
            "action_payload_not_object", "action payload must be a JSON object"
        )
    try:
        proposal = ReactActionProposal.model_validate(raw)
    except ValidationError as exc:
        raise ReactDecisionError(
            "action_contract_invalid", "model choice failed the proposal contract"
        ) from exc
    return materialize_next_action(proposal, view)


async def decide_react_v0_once(
    *,
    state: Any,
    user_message: str,
    allowed_tool_names: Iterable[str],
    client: Any,
    model: str,
    timeout_seconds: float,
    last_outcome: ActionOutcome | None = None,
) -> ReactShadowObservation:
    """Select one bounded action without writing state or executing it."""

    started = time.perf_counter()
    try:
        view = build_decision_context_view(
            state,
            user_message=user_message,
            allowed_tool_names=allowed_tool_names,
            last_outcome=last_outcome,
        )
    except Exception:
        return ReactShadowObservation(
            status="failed",
            task_revision=state.revision,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error_code="decision_view_build_failed",
        )
    deterministic = deterministic_next_action(view)
    if deterministic is not None:
        return ReactShadowObservation(
            status="accepted",
            task_revision=state.revision,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            view=view,
            action=deterministic,
            decision_source="deterministic_policy",
            selected_option_id=_selected_option_id(deterministic, view),
        )
    try:
        action = await asyncio.wait_for(
            decide_next_action(view, client=client, model=model),
            timeout=max(float(timeout_seconds), 0.01),
        )
    except asyncio.TimeoutError:
        return ReactShadowObservation(
            status="failed",
            task_revision=state.revision,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            view=view,
            error_code="decision_timeout",
            decision_source="model",
        )
    except ReactDecisionError as exc:
        return ReactShadowObservation(
            status="rejected",
            task_revision=state.revision,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            view=view,
            error_code=exc.code,
            decision_source="model",
        )
    except Exception:
        return ReactShadowObservation(
            status="failed",
            task_revision=state.revision,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            view=view,
            error_code="decision_model_failed",
            decision_source="model",
        )
    return ReactShadowObservation(
        status="accepted",
        task_revision=state.revision,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        view=view,
        action=action,
        decision_source="model",
        selected_option_id=_selected_option_id(action, view),
    )


async def observe_react_v0_shadow(
    *,
    state: Any,
    user_message: str,
    allowed_tool_names: Iterable[str],
    client: Any,
    model: str,
    timeout_seconds: float,
) -> ReactShadowObservation:
    """Observe one decision without writing state or executing any action."""

    return await decide_react_v0_once(
        state=state,
        user_message=user_message,
        allowed_tool_names=allowed_tool_names,
        client=client,
        model=model,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "REACT_ACTION_TOOL_NAME",
    "REACT_ACTION_TOOL_SCHEMA",
    "REACT_DECISION_SYSTEM_PROMPT",
    "ReactActionProposal",
    "ReactDecisionError",
    "ReactShadowObservation",
    "decide_react_v0_once",
    "decide_next_action",
    "deterministic_next_action",
    "materialize_next_action",
    "observe_react_v0_shadow",
    "validate_next_action",
]
