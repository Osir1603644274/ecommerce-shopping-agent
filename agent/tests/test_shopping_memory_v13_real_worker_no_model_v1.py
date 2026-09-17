from pathlib import Path
import json

import pytest

from evaluation import shopping_memory_v13_real_worker_no_model_v1 as runner


def test_candidate_id_matches_production_formula_and_is_stable():
    proposal = dict(next(iter(runner.FIXTURES.values())))
    first = runner.candidate_id_for("job-1", proposal)
    second = runner.candidate_id_for("job-1", dict(reversed(list(proposal.items()))))
    assert first == second
    assert len(first) == 43
    assert runner.candidate_id_for("job-2", proposal) != first


def test_fixtures_are_distinct_single_catalog_tuples():
    values = list(runner.FIXTURES.values())
    assert len(values) == 2
    assert {item["attributeKey"] for item in values} == {
        "screen_originality", "battery_originality",
    }
    assert all(item["catalogRevision"] == runner.CATALOG_REVISION for item in values)
    assert all(item["recipientScope"] == "self" for item in values)


def test_verdict_requires_every_mechanical_check():
    assert runner.verdict_for_checks({"a": True, "b": True}) == runner.VERDICT
    assert runner.verdict_for_checks({"a": True, "b": False}) == runner.HOLD
    assert runner.verdict_for_checks({}) == runner.HOLD


def test_receipt_summary_binds_message_identity_and_full_proposal():
    message = next(iter(runner.FIXTURES))
    envelope = {
        "jobId": "job-1",
        "sessionBinding": "a" * 64,
        "categoryId": "phone",
        "recipientScope": "self",
        "messageDigest": runner.e2e.base.digest_text(message),
        "proposals": [runner.FIXTURES[message]],
    }
    summary = runner._receipt_summary(json.dumps(envelope), "job-1", message)
    assert summary["proposal"] == {
        key: value for key, value in runner.FIXTURES[message].items()
        if key != "displayLabel"
    }
    envelope["messageDigest"] = "b" * 64
    with pytest.raises(runner.RunFailure):
        runner._receipt_summary(json.dumps(envelope), "job-1", message)


def test_attempt_directory_is_immutable(tmp_path: Path):
    output = tmp_path / "worker-attempt001"
    runner.e2e.base.create_attempt_dir(output)
    with pytest.raises(FileExistsError):
        runner.e2e.base.create_attempt_dir(output)


def test_scope_does_not_claim_real_llm_extraction():
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert '"real_llm_extraction"' in source
    assert '"NO_REAL_LLM_EXTRACTION"' in source
    assert '"modelCalls": measured_model_calls' in source


def test_runner_binds_both_inherited_e2e_modules():
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "Path(e2e.__file__).resolve()" in source
    assert "Path(e2e.base.__file__).resolve()" in source
    assert '"sourceSha256Current": source_current' in source
