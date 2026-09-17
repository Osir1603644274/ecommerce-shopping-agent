from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.control.react_actions import (
    ActionOutcome,
    AnswerPayload,
    AskClarificationPayload,
    CallToolPayload,
    NeedsReviewPayload,
    NextAction,
)


def _identity() -> dict[str, object]:
    return {
        "actionId": "act-001",
        "taskId": "task-001",
        "basedOnRevision": 18,
        "decisionViewHash": "0123456789abcdef",
        "reasonCode": "candidate_scope_missing",
    }


def test_call_tool_action_is_discriminated_frozen_and_reference_only():
    refs = {"requirements": "taskState.shoppingGuide.requirements"}
    action = NextAction.model_validate({
        **_identity(),
        "kind": "CALL_TOOL",
        "toolName": " search_products ",
        "argumentRefs": refs,
    })
    refs["requirements"] = "attacker.changed"

    assert action.kind == "CALL_TOOL"
    assert action.tool_name == "search_products"
    assert action.argument_refs == {
        "requirements": "taskState.shoppingGuide.requirements"
    }
    assert action.typed_payload() == CallToolPayload(
        toolName="search_products",
        argumentRefs={
            "requirements": "taskState.shoppingGuide.requirements",
        },
    )
    with pytest.raises(ValidationError):
        action.kind = "ANSWER"


@pytest.mark.parametrize(
    ("kind", "payload", "payload_type"),
    [
        (
            "ANSWER",
            {"answerContextRef": "finalAnswerView:sha256:abc"},
            AnswerPayload,
        ),
        (
            "ASK_CLARIFICATION",
            {"question": "你的预算上限是多少？"},
            AskClarificationPayload,
        ),
        (
            "NEEDS_REVIEW",
            {"reviewCode": "transition_budget_exhausted"},
            NeedsReviewPayload,
        ),
    ],
)
def test_non_tool_action_has_one_typed_payload(kind, payload, payload_type):
    action = NextAction.model_validate({**_identity(), "kind": kind, **payload})

    assert isinstance(action.typed_payload(), payload_type)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "CALL_TOOL", "toolName": "search_products"},
        {
            "kind": "CALL_TOOL",
            "argumentRefs": {"requirements": "taskState.requirements"},
        },
        {"kind": "CALL_TOOL", "toolName": "search_products", "argumentRefs": {}},
    ],
)
def test_call_tool_requires_tool_name_and_nonempty_argument_refs(payload):
    with pytest.raises(ValidationError):
        NextAction.model_validate({**_identity(), **payload})


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "ANSWER",
            "answerContextRef": "final-view:1",
            "toolName": "search_products",
            "argumentRefs": {"query": "taskState.goal"},
        },
        {
            "kind": "ASK_CLARIFICATION",
            "question": "预算是多少？",
            "reviewCode": "manual_review",
        },
        {
            "kind": "NEEDS_REVIEW",
            "reviewCode": "manual_review",
            "answerContextRef": "final-view:1",
        },
    ],
)
def test_action_payload_fields_are_mutually_exclusive(payload):
    with pytest.raises(ValidationError, match="payload mismatch"):
        NextAction.model_validate({**_identity(), **payload})


@pytest.mark.parametrize("field", ["thought", "reasoning", "chainOfThought"])
def test_free_form_reasoning_fields_are_forbidden(field):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NextAction.model_validate({
            **_identity(),
            "kind": "ANSWER",
            "answerContextRef": "final-view:1",
            field: "hidden reasoning",
        })


def test_reason_code_is_bounded_machine_code_not_free_form_reasoning():
    with pytest.raises(ValidationError):
        NextAction.model_validate({
            **_identity(),
            "kind": "ANSWER",
            "answerContextRef": "final-view:1",
            "reasonCode": "I think this answer is probably good",
        })


def test_success_outcome_contains_explicit_audit_fields_and_is_frozen():
    outcome = ActionOutcome.model_validate({
        "actionId": "act-001",
        "status": "SUCCEEDED",
        "observationRef": "scope-123",
        "validatorOutcome": "PASSED",
        "stateRevisionAfter": 19,
        "retryable": False,
        "errorCode": None,
    })

    assert outcome.model_dump(by_alias=True) == {
        "schemaVersion": "react-action-outcome-v0",
        "actionId": "act-001",
        "status": "SUCCEEDED",
        "observationRef": "scope-123",
        "validatorOutcome": "PASSED",
        "stateRevisionAfter": 19,
        "retryable": False,
        "errorCode": None,
    }
    with pytest.raises(ValidationError):
        outcome.retryable = True


def test_failed_outcome_requires_machine_error_code():
    outcome = ActionOutcome.model_validate({
        "actionId": "act-001",
        "status": "FAILED",
        "observationRef": None,
        "validatorOutcome": "FAILED",
        "stateRevisionAfter": 18,
        "retryable": True,
        "errorCode": "tool_timeout",
    })

    assert outcome.error_code == "tool_timeout"
    with pytest.raises(ValidationError, match="requires errorCode"):
        ActionOutcome.model_validate({
            **outcome.model_dump(by_alias=True),
            "errorCode": None,
        })


@pytest.mark.parametrize(
    "updates",
    [
        {"errorCode": "impossible_success"},
        {"retryable": True},
        {"stateRevisionAfter": None},
        {"validatorOutcome": "REJECTED"},
    ],
)
def test_success_outcome_fails_closed_on_inconsistent_fields(updates):
    payload = {
        "actionId": "act-001",
        "status": "SUCCEEDED",
        "observationRef": "scope-123",
        "validatorOutcome": "PASSED",
        "stateRevisionAfter": 19,
        "retryable": False,
        "errorCode": None,
    }
    payload.update(updates)

    with pytest.raises(ValidationError):
        ActionOutcome.model_validate(payload)
