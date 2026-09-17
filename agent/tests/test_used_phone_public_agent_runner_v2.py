import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest

from agent.evaluation import used_phone_public_agent_runner_v1 as v1
from agent.evaluation import used_phone_public_agent_runner_v2 as v2


def _v1_identity():
    return {
        name: deepcopy(getattr(v1, name))
        for name in v2._IDENTITY_FIELDS
    }


def test_v2_scope_is_independent_and_v1_pin_is_unchanged():
    before = _v1_identity()
    assert v2.PRODUCTION_CODE_SCOPE_SHA256 == (
        "4163cccfbea21c0f0fdc9c3d6d957c8d35818c192034cd92f43151df26b6576c"
    )
    assert v2.production_scope_sha256() != v2.PRODUCTION_CODE_SCOPE_SHA256
    with pytest.raises(v1.PublicRunnerError, match="production code scope SHA mismatch"):
        v2.verify_production_scope()
    assert _v1_identity() == before
    assert v1.PRODUCTION_CODE_SCOPE_SHA256 != v2.PRODUCTION_CODE_SCOPE_SHA256


def test_v2_runner_provenance_includes_adapter_schema_and_cli():
    hashes = v2._v2_runner_code_hashes()
    assert set(hashes) == {
        "agent/evaluation/used_phone_public_agent_runner_v1.py",
        "agent/evaluation/used_phone_public_agent_runner_v2.py",
        "agent/evaluation/schemas/used_phone_public_agent_prediction_v2.schema.json",
        "agent/scripts/run_used_phone_public_agent_v2.py",
    }


def test_v2_prediction_schema_rejects_v1_identity_and_restores_kernel():
    before = _v1_identity()
    v2.validate_prediction_row({"caseId": "UPV2-CD-01", "rankedItemIds": []})
    with pytest.raises(Exception):
        v2.validate_prediction_row({"caseId": "UPV1-HC-01", "rankedItemIds": []})
    assert _v1_identity() == before


def test_v2_identity_restores_kernel_after_exception():
    before = _v1_identity()
    with patch.object(v1, "verify_production_scope", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            v2.verify_production_scope()
    assert _v1_identity() == before


def test_v2_identity_rejects_reentrant_use():
    with v2._v2_identity():
        with pytest.raises(v2.PublicRunnerError, match="already active"):
            with v2._v2_identity():
                pass


def test_contract_dev_rejects_sealed_test_flag(tmp_path):
    with pytest.raises(v2.PublicRunnerError, match="no sealed test"):
        asyncio.run(v2.run_public_agent(
            public_cases_path=tmp_path / "cases_public.jsonl",
            public_catalog_path=tmp_path / "catalog.jsonl",
            run_dir=tmp_path / "run",
            allow_sealed_test=True,
        ))
