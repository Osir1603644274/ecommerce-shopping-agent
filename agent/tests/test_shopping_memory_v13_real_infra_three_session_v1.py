from pathlib import Path

import pytest

from evaluation import shopping_memory_v13_real_infra_three_session_v1 as runner


def test_catalog_fixture_is_hash_pinned_deterministic_and_paired():
    first, first_receipt = runner.load_candidate_fixture()
    second, second_receipt = runner.load_candidate_fixture()
    assert first == second
    assert first_receipt == second_receipt
    assert len(first) == 20
    assert len({str(item["productId"]) for item in first}) == 20
    assert str(first[0]["productId"]) == first_receipt["baselineFirstProductId"]
    assert str(first[1]["productId"]) == first_receipt["preferredSecondProductId"]

    key = runner.PREFERENCE["attributeKey"]
    value = runner.PREFERENCE["normalizedValue"]
    first_attrs = {item["key"]: item["value"] for item in first[0]["attributes"] if item["status"] == "known"}
    second_attrs = {item["key"]: item["value"] for item in first[1]["attributes"] if item["status"] == "known"}
    assert first_attrs.get(key) != value
    assert second_attrs.get(key) == value


def test_attempt_directory_is_never_reused(tmp_path: Path):
    attempt = tmp_path / "attempt001"
    runner.create_attempt_dir(attempt)
    with pytest.raises(FileExistsError):
        runner.create_attempt_dir(attempt)


def test_verdict_requires_every_check():
    assert runner.verdict_for_checks({"a": True, "b": True}) == runner.VERDICT
    assert runner.verdict_for_checks({"a": True, "b": False}) == "HOLD_REAL_INFRA_E2E_FAILED"
    assert runner.verdict_for_checks({}) == "HOLD_REAL_INFRA_E2E_FAILED"


@pytest.mark.parametrize("secret", sorted(runner.SECRET_KEYS))
def test_attempt_artifacts_reject_secret_fields(secret: str):
    with pytest.raises(runner.RunFailure, match="secret field"):
        runner.assert_artifact_safe({secret: "opaque"})


def test_scope_is_explicitly_no_model_and_default_preserving():
    assert runner.RERANK_WEIGHT == 0.08
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "NO_LLM_EXTRACTION" in source
    assert '"productionDefaultChanged": False' in source
    assert "validation.jsonl" not in source
    assert "sealed-test.jsonl" not in source
