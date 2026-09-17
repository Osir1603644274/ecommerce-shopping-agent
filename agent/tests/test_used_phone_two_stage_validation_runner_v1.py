import asyncio
from copy import deepcopy

import pytest

from agent.evaluation import used_phone_public_agent_runner_v1 as kernel
from agent.evaluation import used_phone_two_stage_ranking_runner_v3 as dev
from agent.evaluation import used_phone_two_stage_validation_runner_v1 as validation


def test_validation_runner_requires_full_ordered_validation10_and_no_test_authority():
    assert validation._validate_selection(["validation"], None, False) == (
        ["validation"], list(validation.VALIDATION_CASE_IDS)
    )
    for splits, case_ids, sealed in (
        (["dev"], None, False),
        (["test"], None, False),
        (["validation", "validation"], None, False),
        (["validation"], [validation.VALIDATION_CASE_IDS[0]], False),
        (["validation"], None, True),
    ):
        with pytest.raises(validation.PublicRunnerError):
            validation._validate_selection(splits, case_ids, sealed)


@pytest.mark.parametrize(
    "kwargs,pattern",
    [
        ({"resume": True}, "resume is forbidden"),
        ({"client": object()}, "rejects external clients"),
        ({"model_name": "external-model"}, "rejects model overrides"),
        ({"splits": ["dev"]}, "validation split"),
        ({"case_ids": ["UPV2-RK-V01"]}, "complete ordered"),
        ({"allow_sealed_test": True}, "sealed/test"),
    ],
)
def test_validation_runner_rejects_extra_authority_before_dispatch(kwargs, pattern):
    with pytest.raises(validation.PublicRunnerError, match=pattern):
        asyncio.run(validation.run_public_agent(
            public_cases_path=validation.Path("cases_public.jsonl"),
            public_catalog_path=validation.Path("catalog.jsonl"),
            run_dir=validation.Path("run"),
            **kwargs,
        ))


def test_validation_identity_is_distinct_and_restores_dev_and_kernel():
    before = {name: deepcopy(getattr(kernel, name)) for name in validation._FIELDS}
    dev_protocol = dev.PROTOCOL_VERSION
    with validation._identity():
        assert kernel.PROTOCOL_VERSION == validation.PROTOCOL_VERSION
        assert kernel.EXPECTED_CASE_IDS == validation.EXPECTED_CASE_IDS
    assert {name: getattr(kernel, name) for name in validation._FIELDS} == before
    assert dev.PROTOCOL_VERSION == dev_protocol
    hashes = validation._hashes()
    assert "agent/evaluation/used_phone_two_stage_validation_runner_v1.py" in hashes
    assert "agent/scripts/run_used_phone_two_stage_validation_v1.py" in hashes


def test_validation_runner_has_no_scorer_or_judgment_dependency():
    source = validation.Path(validation.__file__).read_text(encoding="utf-8")
    assert "validation_scorer" not in source
    assert "judgments_hidden" not in source
    assert "oracle" not in source.casefold()
    assert "gold" not in source.casefold()
