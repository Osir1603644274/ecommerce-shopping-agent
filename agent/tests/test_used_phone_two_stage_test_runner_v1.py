import asyncio

import pytest

from agent.evaluation import used_phone_two_stage_test_runner_v1 as test_runner


def test_test_runner_requires_explicit_authority_and_complete_ordered_test10():
    assert test_runner._validate_selection(["test"], None, True) == (
        ["test"], list(test_runner.TEST_CASE_IDS)
    )
    for splits, ids, sealed in (
        (["test"], None, False), (["dev"], None, True),
        (["validation"], None, True), (["test"], [test_runner.TEST_CASE_IDS[0]], True),
    ):
        with pytest.raises(test_runner.PublicRunnerError):
            test_runner._validate_selection(splits, ids, sealed)


@pytest.mark.parametrize("kwargs", [
    {"resume": True, "allow_sealed_test": True},
    {"client": object(), "allow_sealed_test": True},
    {"model_name": "external", "allow_sealed_test": True},
    {"splits": ["validation"], "allow_sealed_test": True},
    {"case_ids": ["UPV2-RK-T01"], "allow_sealed_test": True},
])
def test_test_runner_rejects_extra_authority_before_dispatch(kwargs):
    with pytest.raises(test_runner.PublicRunnerError):
        asyncio.run(test_runner.run_public_agent(
            public_cases_path=test_runner.Path("cases_public.jsonl"),
            public_catalog_path=test_runner.Path("catalog.jsonl"),
            run_dir=test_runner.Path("run"), **kwargs,
        ))


def test_test_runner_has_no_scorer_or_judgment_dependency():
    source = test_runner.Path(test_runner.__file__).read_text(encoding="utf-8")
    assert "test_scorer" not in source
    assert "judgments_hidden" not in source
    assert "oracle" not in source.casefold()
    assert "gold" not in source.casefold()
