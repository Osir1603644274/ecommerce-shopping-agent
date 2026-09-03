"""Prediction-first evaluator for held-out natural-guide Validation20-v2."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any

from . import used_phone_natural_guide_validation20_evaluator_v1 as _base


SCHEMA_VERSION="used-phone-natural-guide-validation20-v2-score-v1"
PROTOCOL_VERSION="used-phone-natural-guide-validation20-v2-production-agent-v1"
PUBLIC_CASES_SHA256="d2d7f656a788aec0b21a64c5616a2ceda64c36b8b850c28594396b03bb6879b8"
PUBLIC_CATALOG_SHA256=_base.PUBLIC_CATALOG_SHA256
PREREGISTRATION_SHA256="d401fa296a6932259641fbd156188c897023538d74ddd1275c78472d5fc0b937"
PRODUCTION_CODE_SCOPE_SHA256="37797e162819ba53a35816cc3e566fc347ca07da90d533270c566b1e9832a293"
CASE_IDS=tuple(f"UPNG-W{index:02d}" for index in range(1,21))
AUTHORIZED_RUN_DIRECTORY="used-phone-natural-guide-validation20-v2-e2e-20260814-attempt-001"
SCHEMA_PATH=Path(__file__).resolve().parent/"schemas"/"used_phone_natural_guide_validation20_v2_prediction_v1.schema.json"
RUNNER_HASHES={
 "agent/evaluation/used_phone_public_agent_runner_v1.py":"7c552a5b2f7dc0b42d61a7d37ce35688ce6b3be88f2517660885e383be9e7c09",
 "agent/evaluation/used_phone_natural_guide_validation20_v2_runner_v1.py":"bd9490f49a8cea15fa6fa959bc67c97ca134bb44d9832edbe7292f1797251b85",
 "agent/evaluation/schemas/used_phone_natural_guide_validation20_v2_prediction_v1.schema.json":"337881d83665bc3c976777cc2c2b0f591f1dcd2881c67326387e342fe392fa82",
 "agent/scripts/run_used_phone_natural_guide_validation20_v2_v1.py":"a107f3123d456bd8d248ee4f9c8b02acc528711047757c11b3d1dc082410e863",
}
canonical_bytes=_base.canonical_bytes
sha256_file=_base.sha256_file


@contextmanager
def _identity():
    names=("SCHEMA_VERSION","PROTOCOL_VERSION","PUBLIC_CASES_SHA256","PUBLIC_CATALOG_SHA256","PREREGISTRATION_SHA256","PRODUCTION_CODE_SCOPE_SHA256","CASE_IDS","AUTHORIZED_RUN_DIRECTORY","SCHEMA_PATH","RUNNER_HASHES")
    original={name:getattr(_base,name) for name in names}
    try:
        for name in names: setattr(_base,name,globals()[name])
        yield
    finally:
        for name,value in original.items(): setattr(_base,name,value)


def _load_preregistration(path:Path)->dict[str,Any]:
    resolved=path.resolve()
    if resolved.name!="validation20_v2_preregistration_v1.json" or sha256_file(resolved)!=PREREGISTRATION_SHA256: raise ValueError("Validation20-v2 preregistration identity mismatch")
    value=json.loads(resolved.read_text(encoding="utf-8"))
    if value.get("schemaVersion")!="used-phone-natural-guide-validation20-v2-preregistration-v1" or value.get("status")!="FROZEN_HELD_OUT_VALIDATION_AI_DESIGNED_NOT_HUMAN_GOLD" or value.get("orderedCaseIds")!=list(CASE_IDS) or value.get("caseCount")!=20 or value.get("inputIdentity")!={"publicCasesSha256":PUBLIC_CASES_SHA256,"publicCatalogSha256":PUBLIC_CATALOG_SHA256,"preRepairProductionScopeSha256":"a1ddc4bb58198a62dc87cf191edada842bfbcd1c973822b2a05b418f541d9381"} or [row.get("caseId") for row in value.get("cases",[])]!=list(CASE_IDS): raise ValueError("Validation20-v2 preregistration semantic mismatch")
    return value


def score_validation20_v2(*,predictions_path:Path,manifest_path:Path,public_catalog_path:Path,preregistration_path:Path)->dict[str,Any]:
    with _identity():
        original_loader=_base._load_preregistration
        _base._load_preregistration=_load_preregistration
        try:
            result=_base.score_validation20(predictions_path=predictions_path,manifest_path=manifest_path,public_catalog_path=public_catalog_path,preregistration_path=preregistration_path)
        finally:
            _base._load_preregistration=original_loader
    result["schemaVersion"]=SCHEMA_VERSION
    result["labelBoundary"]="AI-designed held-out validation contracts, not human gold"
    return result


__all__=["canonical_bytes","score_validation20_v2"]
