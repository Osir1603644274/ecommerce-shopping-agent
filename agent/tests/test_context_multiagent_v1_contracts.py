from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = ROOT / "schemas" / "context-multiagent-v1"
EVALUATION_ROOT = ROOT / "evaluation"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_investigation_set() -> dict:
    return {
        "schemaVersion": "investigation-set-v1",
        "investigationSetId": "is-1",
        "taskId": "task-1",
        "taskRevision": 3,
        "candidateScopeId": "scope-1",
        "candidateScopeSourceRevision": 3,
        "candidateScopeHash": "a" * 64,
        "candidateIds": [10, 20, 30],
        "evidenceGapKeys": ["battery_health", "repair_history"],
        "selectionPolicyVersion": "investigation-selection-policy-v1",
        "expiresAt": "2026-09-01T00:00:00Z",
        "bindingHash": "b" * 64,
    }


def _valid_route_decision() -> dict:
    return {
        "schemaVersion": "route-decision-v1",
        "decisionId": "route-1",
        "runId": "run-1",
        "taskId": "task-1",
        "taskRevision": 3,
        "candidateScopeId": "scope-1",
        "candidateScopeSourceRevision": 3,
        "routeClass": "RESEARCH_REQUIRED",
        "reasons": ["critical evidence gap"],
        "routePolicyVersion": "context-multiagent-route-v1",
        "decisionSource": "DETERMINISTIC_GATE",
        "researchAllowed": True,
        "transactionAllowed": False,
        "bindingHash": "c" * 64,
    }


def _valid_research_report() -> dict:
    return {
        "schemaVersion": "research-report-v1",
        "reportId": "report-1",
        "requestId": "request-1",
        "handoffId": "handoff-1",
        "parentRunId": "parent-1",
        "childRunId": "child-1",
        "taskId": "task-1",
        "taskRevision": 3,
        "candidateScopeId": "scope-1",
        "candidateScopeSourceRevision": 3,
        "candidateScopeHash": "a" * 64,
        "investigationSetId": "is-1",
        "investigationSetBindingHash": "b" * 64,
        "findings": [
            {
                "candidateId": 10,
                "evidenceGapKey": "battery_health",
                "verdict": "UNKNOWN",
                "evidenceRefs": [],
                "summary": "No verified source resolves the gap.",
                "sourceAuthority": "INSUFFICIENT",
            }
        ],
        "rejectedFindings": [],
        "unresolved": [
            {
                "candidateId": 10,
                "evidenceGapKey": "battery_health",
                "reason": "verified source unavailable",
            }
        ],
        "stopReason": "COMPLETE",
        "generatedAt": "2026-09-01T00:00:00Z",
        "reportHash": "d" * 64,
        "bindingHash": "e" * 64,
    }


def test_all_declared_schemas_are_valid_and_present() -> None:
    index = _load(SCHEMA_ROOT / "schema-index-v1.2.json")
    assert index["status"] == "FROZEN_IMPLEMENTATION_AMENDMENT_002"
    assert len(index["schemas"]) == 9
    assert len(index["schemas"]) == len(set(index["schemas"]))
    for name in index["schemas"]:
        schema = _load(SCHEMA_ROOT / name)
        Draft202012Validator.check_schema(schema)


def test_contract_freeze_hashes_match_disk() -> None:
    manifest = _load(EVALUATION_ROOT / "contract-freeze-manifest-v3.json")
    assert manifest["decision"] == "PLAN_ACCEPT_WITH_SCOPE_REDUCTION"
    for entry in manifest["files"]:
        path = ROOT / entry["path"]
        assert path.is_file(), entry["path"]
        assert _sha256(path) == entry["sha256"], entry["path"]


def test_investigation_set_is_bounded_to_eight_unique_candidates() -> None:
    validator = Draft202012Validator(
        _load(SCHEMA_ROOT / "investigation-set.schema.json")
    )
    validator.validate(_valid_investigation_set())

    too_many = _valid_investigation_set()
    too_many["candidateIds"] = list(range(1, 10))
    with pytest.raises(ValidationError):
        validator.validate(too_many)

    duplicate = _valid_investigation_set()
    duplicate["candidateIds"] = [10, 10]
    with pytest.raises(ValidationError):
        validator.validate(duplicate)


def test_route_decision_enforces_five_class_capabilities() -> None:
    validator = Draft202012Validator(_load(SCHEMA_ROOT / "route-decision.schema.json"))
    validator.validate(_valid_route_decision())

    invalid_transaction = _valid_route_decision()
    invalid_transaction.update(
        routeClass="MUST_TRANSACTION",
        researchAllowed=True,
        transactionAllowed=True,
    )
    with pytest.raises(ValidationError):
        validator.validate(invalid_transaction)

    invalid_direct = _valid_route_decision()
    invalid_direct.update(
        routeClass="MUST_DIRECT",
        researchAllowed=True,
        transactionAllowed=False,
    )
    with pytest.raises(ValidationError):
        validator.validate(invalid_direct)


def test_research_report_has_no_recommendation_or_candidate_expansion_field() -> None:
    validator = Draft202012Validator(_load(SCHEMA_ROOT / "research-report.schema.json"))
    validator.validate(_valid_research_report())

    for forbidden in ("recommendation", "proposedCandidates", "finalAnswer"):
        report = copy.deepcopy(_valid_research_report())
        report[forbidden] = []
        with pytest.raises(ValidationError):
            validator.validate(report)


def test_preregistration_freezes_arms_routes_and_decision_levels() -> None:
    text = (EVALUATION_ROOT / "preregistration-context-multiagent-v1.md").read_text(
        encoding="utf-8"
    )
    for arm in ("CTX0", "CTX1a", "CTX1b", "MA1"):
        assert arm in text
    for route in (
        "MUST_CLARIFY",
        "MUST_TRANSACTION",
        "MUST_DIRECT",
        "RESEARCH_ELIGIBLE",
        "RESEARCH_REQUIRED",
    ):
        assert route in text
    for level in (
        "CONTRACT_ACCEPT",
        "ENGINEERING_ACCEPT",
        "EFFECTIVENESS_CONFIRMED",
        "PRODUCTION_DEFAULT_ACCEPT",
    ):
        assert level in text


def test_lineage_does_not_authorize_sealed_reuse() -> None:
    lineage = _load(EVALUATION_ROOT / "data-lineage-v1.json")
    serialized = json.dumps(lineage, ensure_ascii=False).lower()
    assert "consumed_final_hold" in serialized
    assert "sealed" in serialized
    assert "do not" in serialized or "不得" in serialized
