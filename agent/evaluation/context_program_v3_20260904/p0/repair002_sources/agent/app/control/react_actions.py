"""Fail-closed, storage-free action contracts for the minimal ReAct V0 loop.

The model is allowed to select one bounded action and emit a short machine
reason code.  It cannot attach free-form reasoning, raw prompts, or executable
tool arguments: tool inputs are server-owned references into a validated view.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ActionKind = Literal[
    "CALL_TOOL",
    "ANSWER",
    "ASK_CLARIFICATION",
    "NEEDS_REVIEW",
]
ActionStatus = Literal["SUCCEEDED", "FAILED", "REJECTED", "INTERRUPTED"]
ActionValidatorOutcome = Literal["NOT_RUN", "PASSED", "REJECTED", "FAILED"]


def react_plan_contract_sha256(plan: Any) -> str:
    """Hash immutable Plan content while ignoring execution lifecycle fields."""
    raw = (
        plan.model_dump(by_alias=True, mode="json")
        if hasattr(plan, "model_dump")
        else dict(plan)
    )
    steps = []
    for item in raw.get("steps", []):
        step = dict(item)
        step.pop("status", None)
        steps.append(step)
    contract = {
        "planId": raw.get("planId"),
        "basedOnRevision": raw.get("basedOnRevision"),
        "decision": raw.get("decision"),
        "steps": steps,
    }
    return hashlib.sha256(
        json.dumps(
            contract,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def react_action_anchor_key(
    task_id: str,
    run_id: str,
    thread_id: str,
    action_id: str,
) -> str:
    identity = "\x00".join((task_id, run_id, thread_id)).encode("utf-8")
    prefix = f"graph-v2:react-policy:{hashlib.sha256(identity).hexdigest()}"
    return f"{prefix}:action:{action_id}"


class _ReactContract(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)


class CallToolPayload(_ReactContract):
    tool_name: str = Field(alias="toolName", min_length=1, max_length=128)
    argument_refs: dict[str, str] = Field(alias="argumentRefs", min_length=1)

    @field_validator("tool_name")
    @classmethod
    def _normalize_tool_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("toolName cannot be blank")
        return normalized

    @field_validator("argument_refs")
    @classmethod
    def _validate_argument_refs(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, reference in value.items():
            if type(key) is not str or type(reference) is not str:
                raise ValueError("argumentRefs must map strings to strings")
            key = key.strip()
            reference = reference.strip()
            if not key or not reference:
                raise ValueError("argumentRefs cannot contain blank keys or references")
            if len(key) > 128 or len(reference) > 512:
                raise ValueError("argumentRefs entry exceeds its bounded size")
            if key in normalized:
                raise ValueError("argumentRefs cannot contain duplicate normalized keys")
            normalized[key] = reference
        if not normalized:
            raise ValueError("CALL_TOOL requires at least one argumentRef")
        return normalized


class AnswerPayload(_ReactContract):
    answer_context_ref: str = Field(
        alias="answerContextRef", min_length=1, max_length=512
    )


class AskClarificationPayload(_ReactContract):
    question: str = Field(min_length=1, max_length=512)


class NeedsReviewPayload(_ReactContract):
    review_code: str = Field(
        alias="reviewCode",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )


ActionPayload: TypeAlias = (
    CallToolPayload | AnswerPayload | AskClarificationPayload | NeedsReviewPayload
)


class NextAction(_ReactContract):
    """One revision-bound decision emitted by the ReAct V0 decider.

    Payload fields are intentionally flat to keep the wire shape small and
    compatible with tool-schema constrained model output.  ``kind`` is the
    discriminator, and the after-validator guarantees exactly one legal
    payload shape.
    """

    schema_version: Literal["react-action-v0"] = Field(
        default="react-action-v0", alias="schemaVersion"
    )
    action_id: str = Field(alias="actionId", min_length=1, max_length=128)
    task_id: str = Field(alias="taskId", min_length=1, max_length=128)
    based_on_revision: int = Field(alias="basedOnRevision", ge=1)
    decision_view_hash: str = Field(
        alias="decisionViewHash", min_length=8, max_length=128
    )
    kind: ActionKind
    reason_code: str = Field(
        alias="reasonCode",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    tool_name: str | None = Field(
        default=None, alias="toolName", min_length=1, max_length=128
    )
    argument_refs: dict[str, str] | None = Field(
        default=None, alias="argumentRefs", min_length=1
    )
    answer_context_ref: str | None = Field(
        default=None, alias="answerContextRef", min_length=1, max_length=512
    )
    question: str | None = Field(default=None, min_length=1, max_length=512)
    review_code: str | None = Field(
        default=None,
        alias="reviewCode",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @field_validator("action_id", "task_id", "decision_view_hash")
    @classmethod
    def _normalize_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("action identity fields cannot be blank")
        return normalized

    @field_validator("answer_context_ref", "question", mode="after")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("action payload text cannot be blank")
        return normalized

    @model_validator(mode="after")
    def _validate_discriminated_payload(self) -> "NextAction":
        fields = {
            "toolName": self.tool_name,
            "argumentRefs": self.argument_refs,
            "answerContextRef": self.answer_context_ref,
            "question": self.question,
            "reviewCode": self.review_code,
        }
        required_by_kind = {
            "CALL_TOOL": {"toolName", "argumentRefs"},
            "ANSWER": {"answerContextRef"},
            "ASK_CLARIFICATION": {"question"},
            "NEEDS_REVIEW": {"reviewCode"},
        }
        required = required_by_kind[self.kind]
        present = {name for name, value in fields.items() if value is not None}
        if present != required:
            missing = sorted(required - present)
            forbidden = sorted(present - required)
            details: list[str] = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if forbidden:
                details.append(f"forbidden {', '.join(forbidden)}")
            raise ValueError(
                f"{self.kind} payload mismatch: " + "; ".join(details)
            )
        if self.kind == "CALL_TOOL":
            # Reuse the standalone payload's strict normalization and bounds.
            payload = CallToolPayload(
                toolName=self.tool_name,
                argumentRefs=self.argument_refs,
            )
            object.__setattr__(self, "tool_name", payload.tool_name)
            object.__setattr__(self, "argument_refs", payload.argument_refs)
        return self

    def typed_payload(self) -> ActionPayload:
        """Return the one payload type selected by ``kind``."""

        if self.kind == "CALL_TOOL":
            return CallToolPayload(
                toolName=self.tool_name,
                argumentRefs=self.argument_refs,
            )
        if self.kind == "ANSWER":
            return AnswerPayload(answerContextRef=self.answer_context_ref)
        if self.kind == "ASK_CLARIFICATION":
            return AskClarificationPayload(question=self.question)
        return NeedsReviewPayload(reviewCode=self.review_code)


class ActionOutcome(_ReactContract):
    """Auditable result of attempting one revision-bound ``NextAction``."""

    schema_version: Literal["react-action-outcome-v0"] = Field(
        default="react-action-outcome-v0", alias="schemaVersion"
    )
    action_id: str = Field(alias="actionId", min_length=1, max_length=128)
    status: ActionStatus
    observation_ref: str | None = Field(
        alias="observationRef", min_length=1, max_length=512
    )
    validator_outcome: ActionValidatorOutcome = Field(alias="validatorOutcome")
    state_revision_after: int | None = Field(alias="stateRevisionAfter", ge=1)
    retryable: bool
    error_code: str | None = Field(
        alias="errorCode",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @field_validator("action_id", "observation_ref")
    @classmethod
    def _normalize_outcome_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("outcome identity/reference fields cannot be blank")
        return normalized

    @model_validator(mode="after")
    def _validate_outcome(self) -> "ActionOutcome":
        if self.status == "SUCCEEDED":
            if self.error_code is not None:
                raise ValueError("SUCCEEDED outcome cannot contain errorCode")
            if self.retryable:
                raise ValueError("SUCCEEDED outcome cannot be retryable")
            if self.state_revision_after is None:
                raise ValueError("SUCCEEDED outcome requires stateRevisionAfter")
            if self.validator_outcome in {"REJECTED", "FAILED"}:
                raise ValueError("SUCCEEDED outcome cannot have failed validation")
        elif self.status in {"FAILED", "REJECTED"}:
            if self.error_code is None:
                raise ValueError(f"{self.status} outcome requires errorCode")
            if self.status == "REJECTED" and self.retryable:
                raise ValueError("REJECTED outcome cannot be retryable")
        elif self.status == "INTERRUPTED":
            if self.error_code is not None:
                raise ValueError("INTERRUPTED outcome cannot contain errorCode")
            if self.retryable:
                raise ValueError("INTERRUPTED outcome cannot be retryable")
        return self


__all__ = [
    "ActionKind",
    "ActionOutcome",
    "ActionPayload",
    "ActionStatus",
    "ActionValidatorOutcome",
    "AnswerPayload",
    "AskClarificationPayload",
    "CallToolPayload",
    "NeedsReviewPayload",
    "NextAction",
]
