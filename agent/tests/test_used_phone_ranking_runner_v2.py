from copy import deepcopy

import pytest

from agent.evaluation import used_phone_public_agent_runner_v1 as v1
from agent.evaluation import used_phone_ranking_runner_v2 as ranking


def _v1_identity():
    return {name: deepcopy(getattr(v1, name)) for name in ranking._FIELDS}


def test_ranking_scope_identity_and_v1_restore():
    before = _v1_identity()
    assert ranking.PRODUCTION_CODE_SCOPE_SHA256 == (
        "7708d786415a426816315a8488524c8b3468fe521904935e40de9daf9de18372"
    )
    assert ranking.production_scope_sha256() != ranking.PRODUCTION_CODE_SCOPE_SHA256
    with pytest.raises(v1.PublicRunnerError, match="production code scope SHA mismatch"):
        ranking.verify_production_scope()
    assert len(ranking.EXPECTED_CASE_IDS) == 30
    assert _v1_identity() == before


def test_schema_rejects_contract_benchmark_identity():
    ranking.validate_prediction_row({"caseId": "UPV2-RK-D01", "rankedItemIds": []})
    with pytest.raises(Exception):
        ranking.validate_prediction_row({"caseId": "UPV2-CB-D01", "rankedItemIds": []})


def test_identity_is_non_reentrant():
    with ranking._identity():
        with pytest.raises(ranking.PublicRunnerError, match="already active"):
            with ranking._identity():
                pass
