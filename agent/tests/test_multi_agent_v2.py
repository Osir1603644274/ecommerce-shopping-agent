from __future__ import annotations

import copy

import pytest

from app.context_compiler_v1 import sha256_json
from app.multi_agent_v2 import (
    MultiAgentEnvelopeV2,
    bind_envelope_v2,
    candidate_decision_support,
    validate_reply_v2,
)


def envelope(message_type="RESEARCH_REQUEST", sequence=1, sender="SHOPPING_COORDINATOR", recipient="EVIDENCE_RESEARCH_AGENT"):
    return bind_envelope_v2(
        task_id="task-1", task_revision=7,
        candidate_scope_id="scope-1", candidate_scope_source_revision=6,
        candidate_scope_hash="a" * 64, parent_run_id="run-parent",
        agent_run_id="run-child", sequence=sequence, sender=sender,
        recipient=recipient, message_type=message_type,
        payload={"candidateIds": [1, 2]},
    )


def test_envelope_round_trip_and_reply_binding():
    request = envelope()
    reply = envelope("RESEARCH_REPORT", 2, "EVIDENCE_RESEARCH_AGENT", "SHOPPING_COORDINATOR")
    validate_reply_v2(request, reply)


@pytest.mark.parametrize("field,value", [
    ("taskRevision", 8), ("candidateScopeId", "scope-other"),
    ("candidateScopeSourceRevision", 5), ("parentRunId", "other"),
])
def test_reply_identity_drift_fails_closed(field, value):
    request = envelope()
    payload = envelope("RESEARCH_REPORT", 2, "EVIDENCE_RESEARCH_AGENT", "SHOPPING_COORDINATOR").model_dump(by_alias=True, mode="json")
    payload[field] = value
    payload["bindingHash"] = sha256_json({k: v for k, v in payload.items() if k != "bindingHash"})
    reply = MultiAgentEnvelopeV2.model_validate(payload)
    with pytest.raises(ValueError, match="stale or cross-scope"):
        validate_reply_v2(request, reply)


def test_payload_tamper_is_rejected():
    payload = envelope().model_dump(by_alias=True, mode="json")
    payload["payload"]["candidateIds"].append(3)
    with pytest.raises(ValueError, match="payload hash mismatch"):
        MultiAgentEnvelopeV2.model_validate(payload)


def test_decision_support_uses_only_verified_attributes():
    result = candidate_decision_support([
        {"id": 1, "attributes": [
            {"key": "battery_health", "status": "known", "value": "90_plus", "evidenceRef": "e:1"},
            {"key": "screen_originality", "status": "unknown", "value": "original", "evidenceRef": None},
        ]},
        {"id": 2, "attributes": [
            {"key": "battery_health", "status": "known", "value": "80_90", "evidenceRef": "e:2"},
            {"key": "screen_originality", "status": "known", "value": "original", "evidenceRef": "e:3"},
        ]},
    ])
    assert result["bestVerifiedCandidateIds"] == [2]
    assert "screen_originality" not in result["candidates"][0]["verifiedAttributes"]
