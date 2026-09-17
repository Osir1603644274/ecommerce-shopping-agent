import json
import types
from pathlib import Path

import pytest

from evaluation import shopping_memory_v13_real_infra_three_session_v2 as runner


def test_verdict_requires_every_measured_check():
    assert runner.verdict_for_checks({"a": True, "b": True}) == runner.VERDICT
    assert runner.verdict_for_checks({"a": True, "b": False}) == runner.HOLD
    assert runner.verdict_for_checks({}) == runner.HOLD


def test_database_transition_contract_is_exact():
    before = {
        "accountRows": 1, "memoryRows": 0, "maxMemoryVersion": 0,
        "activeRows": 0, "revokedRows": 0,
        "projectionHeadRows": 0, "projectionRevision": 0,
        "consentGrantRows": 0, "consumedConsentRows": 0,
        "commandResultRows": 0, "appliedCommandRows": 0,
    }
    confirmed = {
        "accountRows": 1, "memoryRows": 1, "maxMemoryVersion": 1,
        "activeRows": 1, "revokedRows": 0,
        "projectionHeadRows": 1, "projectionRevision": 1,
        "consentGrantRows": 1, "consumedConsentRows": 1,
        "commandResultRows": 1, "appliedCommandRows": 1,
    }
    revoked = {
        "accountRows": 1, "memoryRows": 2, "maxMemoryVersion": 2,
        "activeRows": 1, "revokedRows": 1,
        "projectionHeadRows": 1, "projectionRevision": 2,
        "consentGrantRows": 2, "consumedConsentRows": 2,
        "commandResultRows": 2, "appliedCommandRows": 2,
    }
    assert all(runner.database_transition_checks(before, confirmed, revoked).values())
    revoked["projectionRevision"] = 3
    assert runner.database_transition_checks(before, confirmed, revoked)["revocationCommitted"] is False


def test_artifacts_reject_secret_bearing_keys():
    runner.assert_artifact_safe({"cookieSha256": "a" * 64})
    with pytest.raises(runner.RunFailure):
        runner.assert_artifact_safe({"accessToken": "forbidden"})
    with pytest.raises(runner.RunFailure):
        runner.assert_artifact_safe({"ownerBinding": "forbidden"})
    with pytest.raises(runner.RunFailure):
        runner.assert_artifact_safe({"memoryHandle": "forbidden"})


def test_runtime_secret_values_are_scanned():
    runner.assert_no_secret_values({"cookieSha256": "a" * 64}, {"raw-cookie-value"})
    with pytest.raises(runner.RunFailure):
        runner.assert_no_secret_values({"note": "raw-cookie-value"}, {"raw-cookie-value"})


def test_frozen_settings_source_has_both_memory_defaults_off():
    path = runner.AGENT_ROOT / "app/settings.py"
    report = json.loads((
        runner.AGENT_ROOT
        / "evaluation/results/shopping_memory_v13_real_infra_three_session_v2_attempt004_20260830/report.json"
    ).read_text(encoding="utf-8"))
    assert report["checks"]["settingsSourceHashFrozen"] is True
    assert report["checks"]["settingsSourceHashStable"] is True
    assert runner.settings_defaults_from_source(path) == {
        "memory_bff_enabled": False,
        "memory_projection_client_enabled": False,
    }


def test_attempt_directory_is_immutable(tmp_path: Path):
    output = tmp_path / "attempt002"
    runner.base.create_attempt_dir(output)
    with pytest.raises(FileExistsError):
        runner.base.create_attempt_dir(output)


def test_model_tripwire_blocks_alias_and_counts(monkeypatch):
    from app import llm

    dummy = types.ModuleType("app.tripwire_test_dummy")
    dummy.get_client = llm.get_client
    monkeypatch.setitem(runner.sys.modules, dummy.__name__, dummy)
    counters, restore = runner.install_model_tripwire()
    try:
        with pytest.raises(runner.RunFailure):
            dummy.get_client()
        assert counters["getClientAttempts"] == 1
        assert counters["patchedGetClientAliases"] >= 1
    finally:
        restore()
    assert dummy.get_client is llm.get_client
