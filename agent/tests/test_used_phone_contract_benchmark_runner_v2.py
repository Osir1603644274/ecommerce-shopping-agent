from copy import deepcopy

import pytest

from agent.evaluation import used_phone_contract_benchmark_runner_v2 as benchmark
from agent.evaluation import used_phone_public_agent_runner_v1 as v1


def _identity():
    return {name: deepcopy(getattr(v1, name)) for name in benchmark._IDENTITY_FIELDS}


def test_benchmark_identity_is_frozen_and_v1_is_restored():
    before = _identity()
    assert benchmark.PRODUCTION_CODE_SCOPE_SHA256 == (
        "c40406f56fe6672f9e858555816f66da132cf0620364c1356867d9958a1c326c"
    )
    assert benchmark.production_scope_sha256() != benchmark.PRODUCTION_CODE_SCOPE_SHA256
    with pytest.raises(v1.PublicRunnerError, match="production code scope SHA mismatch"):
        benchmark.verify_production_scope()
    assert len(benchmark.EXPECTED_CASE_IDS) == 60
    assert _identity() == before


def test_prediction_schema_accepts_benchmark_id_and_rejects_public_dev_id():
    before = _identity()
    benchmark.validate_prediction_row({"caseId": "UPV2-CB-V20", "rankedItemIds": []})
    with pytest.raises(Exception):
        benchmark.validate_prediction_row({"caseId": "UPV2-CD-01", "rankedItemIds": []})
    assert _identity() == before


def test_benchmark_identity_is_non_reentrant():
    with benchmark._benchmark_identity():
        with pytest.raises(benchmark.PublicRunnerError, match="already active"):
            with benchmark._benchmark_identity():
                pass
