"""Bounded Multi-Agent V2 communication and decision-support contracts.

This module does not grant tools or authority.  It binds every handoff to the
server-owned task revision and CandidateScope and carries only a compact,
validated projection back to the coordinator.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_compiler_v1 import canonical_json, sha256_json


AgentRoleV2 = Literal["SHOPPING_COORDINATOR", "EVIDENCE_RESEARCH_AGENT"]
MessageTypeV2 = Literal["RESEARCH_REQUEST", "RESEARCH_REPORT"]


class MultiAgentEnvelopeV2(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["multi-agent-envelope-v2"] = Field(
        default="multi-agent-envelope-v2", alias="schemaVersion"
    )
    task_id: str = Field(alias="taskId", min_length=1)
    task_revision: int = Field(alias="taskRevision", ge=0)
    candidate_scope_id: str = Field(alias="candidateScopeId", min_length=1)
    candidate_scope_source_revision: int = Field(
        alias="candidateScopeSourceRevision", ge=0
    )
    candidate_scope_hash: str = Field(
        alias="candidateScopeHash", pattern=r"^[0-9a-f]{64}$"
    )
    parent_run_id: str = Field(alias="parentRunId", min_length=1)
    agent_run_id: str = Field(alias="agentRunId", min_length=1)
    sequence: int = Field(ge=1)
    sender: AgentRoleV2
    recipient: AgentRoleV2
    message_type: MessageTypeV2 = Field(alias="messageType")
    payload: dict[str, Any]
    payload_hash: str = Field(alias="payloadHash", pattern=r"^[0-9a-f]{64}$")
    binding_hash: str = Field(alias="bindingHash", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_hashes_and_direction(self) -> "MultiAgentEnvelopeV2":
        if sha256_json(self.payload) != self.payload_hash:
            raise ValueError("multi-agent payload hash mismatch")
        direction = (self.sender, self.recipient, self.message_type)
        if direction not in {
            ("SHOPPING_COORDINATOR", "EVIDENCE_RESEARCH_AGENT", "RESEARCH_REQUEST"),
            ("EVIDENCE_RESEARCH_AGENT", "SHOPPING_COORDINATOR", "RESEARCH_REPORT"),
        }:
            raise ValueError("invalid multi-agent message direction")
        expected = sha256_json(self.model_dump(
            by_alias=True, mode="json", exclude={"binding_hash"}
        ))
        if expected != self.binding_hash:
            raise ValueError("multi-agent binding hash mismatch")
        return self


def bind_envelope_v2(**values: Any) -> MultiAgentEnvelopeV2:
    payload = values["payload"]
    draft = {
        "schemaVersion": "multi-agent-envelope-v2",
        "taskId": values["task_id"],
        "taskRevision": values["task_revision"],
        "candidateScopeId": values["candidate_scope_id"],
        "candidateScopeSourceRevision": values["candidate_scope_source_revision"],
        "candidateScopeHash": values["candidate_scope_hash"],
        "parentRunId": values["parent_run_id"],
        "agentRunId": values["agent_run_id"],
        "sequence": values["sequence"],
        "sender": values["sender"],
        "recipient": values["recipient"],
        "messageType": values["message_type"],
        "payload": payload,
        "payloadHash": sha256_json(payload),
    }
    draft["bindingHash"] = sha256_json(draft)
    return MultiAgentEnvelopeV2.model_validate(draft)


def validate_reply_v2(
    request: MultiAgentEnvelopeV2,
    reply: MultiAgentEnvelopeV2,
) -> None:
    fields = (
        "task_id",
        "task_revision",
        "candidate_scope_id",
        "candidate_scope_source_revision",
        "candidate_scope_hash",
        "parent_run_id",
        "agent_run_id",
    )
    if any(getattr(request, field) != getattr(reply, field) for field in fields):
        raise ValueError("stale or cross-scope multi-agent reply")
    if reply.sequence != request.sequence + 1:
        raise ValueError("multi-agent reply sequence mismatch")
    if reply.message_type != "RESEARCH_REPORT":
        raise ValueError("unexpected multi-agent reply type")


def candidate_decision_support(products: list[dict[str, Any]]) -> dict[str, Any]:
    """Project only verified selection-critical attributes for parent merge."""

    weights = {
        "battery_health": {"90_plus": 3, "80_90": 2, "70_80": 1},
        "screen_originality": {"original": 2},
        "motherboard_repair": {"not_repaired": 2},
        "battery_originality": {"original": 1},
        "scratch_level": {"none": 1},
        "shell_condition": {"normal": 1},
    }
    rows = []
    for product in products:
        verified = {
            item["key"]: {
                "value": item.get("value"),
                "evidenceRef": item.get("evidenceRef"),
            }
            for item in product.get("attributes", [])
            if item.get("status") == "known" and item.get("evidenceRef")
        }
        score = sum(
            weights[key].get(value["value"], 0)
            for key, value in verified.items()
            if key in weights
        )
        rows.append({
            "candidateId": int(product["id"]),
            "verifiedRiskScore": score,
            "verifiedAttributes": verified,
        })
    best = max((row["verifiedRiskScore"] for row in rows), default=0)
    return {
        "schemaVersion": "candidate-decision-support-v1",
        "candidates": rows,
        "bestVerifiedCandidateIds": [
            row["candidateId"] for row in rows if row["verifiedRiskScore"] == best
        ],
        "rule": "highest_verified_risk_score_first",
        "bindingHash": sha256_json(rows),
    }


__all__ = [
    "MultiAgentEnvelopeV2",
    "bind_envelope_v2",
    "candidate_decision_support",
    "validate_reply_v2",
]
