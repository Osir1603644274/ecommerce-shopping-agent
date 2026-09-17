from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.shopping_memory_v13_governance_v1 import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    materialize,
    run_governance_evaluation,
)


def test_governance_runner_has_paired_coverage_and_all_cases_pass():
    report = run_governance_evaluation()
    assert report["schemaVersion"] == SCHEMA_VERSION
    assert report["contractVersion"] == CONTRACT_VERSION
    assert report["familyCount"] >= 15
    assert report["caseCount"] == report["familyCount"] * 2
    assert set(report["families"].values()) == {2}
    assert report["failedCaseCount"] == 0
    assert report["passedCaseCount"] == report["caseCount"]
    assert report["boundedDecision"] == "BOUNDED_GOVERNANCE_ACCEPT"


def test_runner_never_claims_sealed_java_or_default_switch():
    report = run_governance_evaluation()
    assert report["sealedExecuted"] is False
    assert report["javaConsentServiceExecuted"] is False
    assert report["defaultFlagsChanged"] is False
    assert report["productionDefaultDecision"] == "HOLD_UNCHANGED"
    consent = [case for case in report["cases"] if case["family"] == "consent_boundary"]
    assert len(consent) == 2
    assert {case["execution_layer"] for case in consent} == {"contract_fixture_only"}


def test_required_governance_families_are_present():
    report = run_governance_evaluation()
    assert {
        "consent_boundary", "owner_isolation", "recipient_scope",
        "category_scope", "catalog_revision", "expiry", "revocation",
        "version_chain", "supersede", "current_turn_override",
        "feature_flags", "authority_failure", "projection_budget",
        "context_leakage", "task_state_immutability",
        "soft_rerank_no_filter",
    } <= set(report["families"])


def test_soft_rerank_pair_preserves_cardinality_and_never_filters():
    report = run_governance_evaluation()
    cases = [case for case in report["cases"] if case["family"] == "soft_rerank_no_filter"]
    assert len(cases) == 2
    assert all(case["observed"]["count"] == 20 for case in cases)
    assert all(case["observed"]["filtered"] == 0 for case in cases)


def test_context_pair_has_allowlisted_model_channel_and_no_secret_channel():
    report = run_governance_evaluation()
    cases = {case["polarity"]: case for case in report["cases"] if case["family"] == "context_leakage"}
    assert cases["positive"]["observed"] == {
        "visible": 1,
        "idLeaked": False,
        "ownerLeaked": False,
        "revisionLeaked": False,
    }
    assert cases["negative"]["observed"] == {"executorVisible": False}


def test_materialize_refuses_to_overwrite(tmp_path: Path):
    target = tmp_path / "governance-v1"
    report_path = materialize(target)
    assert report_path.is_file()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["boundedDecision"] == "BOUNDED_GOVERNANCE_ACCEPT"
    assert (target / "SHA256SUMS.txt").is_file()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        materialize(target)
