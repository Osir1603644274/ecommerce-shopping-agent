"""One-shot held-out Validation20-v2 identity over the production kernel."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

from . import used_phone_public_agent_runner_v1 as _kernel


PROTOCOL_VERSION = "used-phone-natural-guide-validation20-v2-production-agent-v1"
PREDICTION_SCHEMA_VERSION = "used-phone-natural-guide-validation20-v2-prediction-v1"
PUBLIC_CASES_SHA256 = "d2d7f656a788aec0b21a64c5616a2ceda64c36b8b850c28594396b03bb6879b8"
PUBLIC_CATALOG_SHA256 = _kernel.PUBLIC_CATALOG_SHA256
PUBLIC_CASE_COUNT = 20
PUBLIC_CATALOG_COUNT = _kernel.PUBLIC_CATALOG_COUNT
EXPECTED_SPLIT_COUNTS = {"validation": 20}
EXPECTED_FAMILY_COUNTS = {"clarification":2,"comparison":3,"multi_turn_delta":5,"natural_constraints":7,"substitute":1,"unsupported_or_no_match":2}
EXPECTED_CASE_IDS = tuple(f"UPNG-W{index:02d}" for index in range(1, 21))
PRODUCTION_CODE_SCOPE_SHA256 = "37797e162819ba53a35816cc3e566fc347ca07da90d533270c566b1e9832a293"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "used_phone_natural_guide_validation20_v2_prediction_v1.schema.json"
PublicRunnerError = _kernel.PublicRunnerError
_FIELDS = {"PROTOCOL_VERSION":PROTOCOL_VERSION,"PREDICTION_SCHEMA_VERSION":PREDICTION_SCHEMA_VERSION,"PUBLIC_CASES_SHA256":PUBLIC_CASES_SHA256,"PUBLIC_CATALOG_SHA256":PUBLIC_CATALOG_SHA256,"PUBLIC_CASE_COUNT":PUBLIC_CASE_COUNT,"PUBLIC_CATALOG_COUNT":PUBLIC_CATALOG_COUNT,"EXPECTED_SPLIT_COUNTS":EXPECTED_SPLIT_COUNTS,"EXPECTED_FAMILY_COUNTS":EXPECTED_FAMILY_COUNTS,"EXPECTED_CASE_IDS":EXPECTED_CASE_IDS,"PRODUCTION_CODE_SCOPE_SHA256":PRODUCTION_CODE_SCOPE_SHA256,"SCHEMA_PATH":SCHEMA_PATH}
_LOCK = threading.Lock()


def _allowed() -> frozenset[Path]:
    root = _kernel.REPO_ROOT
    return frozenset({Path(_kernel.__file__).resolve(),Path(__file__).resolve(),SCHEMA_PATH.resolve(),(root/"agent"/"scripts"/"run_used_phone_natural_guide_validation20_v2_v1.py").resolve(),(root/"agent"/"scripts"/"__init__.py").resolve(),(root/"agent"/"__init__.py").resolve(),(root/"agent"/"evaluation"/"__init__.py").resolve()})


def _hashes() -> dict[str, str]:
    root = _kernel.REPO_ROOT
    paths = {"agent/evaluation/used_phone_public_agent_runner_v1.py":Path(_kernel.__file__).resolve(),"agent/evaluation/used_phone_natural_guide_validation20_v2_runner_v1.py":Path(__file__).resolve(),"agent/evaluation/schemas/used_phone_natural_guide_validation20_v2_prediction_v1.schema.json":SCHEMA_PATH.resolve(),"agent/scripts/run_used_phone_natural_guide_validation20_v2_v1.py":(root/"agent"/"scripts"/"run_used_phone_natural_guide_validation20_v2_v1.py").resolve()}
    return {name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in paths.items()}


@contextmanager
def _identity():
    if not _LOCK.acquire(blocking=False):
        raise PublicRunnerError("Validation20-v2 identity is already active")
    original={name:getattr(_kernel,name) for name in _FIELDS}; old_allowed,old_hashes=_kernel._allowed_runtime_repo_files,_kernel._runner_code_hashes
    try:
        for name,value in _FIELDS.items(): setattr(_kernel,name,value)
        _kernel._allowed_runtime_repo_files,_kernel._runner_code_hashes=_allowed,_hashes
        _kernel._allowed_repo_module_path.cache_clear(); yield
    finally:
        _kernel._allowed_runtime_repo_files,_kernel._runner_code_hashes=old_allowed,old_hashes
        for name,value in original.items(): setattr(_kernel,name,value)
        _kernel._allowed_repo_module_path.cache_clear(); _LOCK.release()


def _call(function, /, *args, **kwargs):
    with _identity(): return function(*args, **kwargs)


def production_scope_sha256() -> str: return _kernel.production_scope_sha256()
def verify_production_scope() -> dict[str, Any]: return _call(_kernel.verify_production_scope)
def audit_public_inputs(*,public_cases_path:Path,public_catalog_path:Path,verify_java:bool=True)->dict[str,Any]: return _call(_kernel.audit_public_inputs,public_cases_path=public_cases_path,public_catalog_path=public_catalog_path,verify_java=verify_java)


async def run_public_agent(*,public_cases_path:Path,public_catalog_path:Path,run_dir:Path,splits:Iterable[str]|None=None,case_ids:Iterable[str]|None=None,allow_sealed_test:bool=False,resume:bool=False,timeout_seconds:float=90.0)->dict[str,Any]:
    if allow_sealed_test or resume: raise PublicRunnerError("Validation20-v2 requires a fresh public-input run")
    selected_splits=list(splits or ["validation"]); selected_ids=list(case_ids) if case_ids is not None else list(EXPECTED_CASE_IDS)
    if selected_splits != ["validation"] or selected_ids != list(EXPECTED_CASE_IDS): raise PublicRunnerError("Validation20-v2 requires the complete ordered set")
    with _identity(): return await _kernel.run_public_agent(public_cases_path=public_cases_path,public_catalog_path=public_catalog_path,run_dir=run_dir,splits=selected_splits,case_ids=selected_ids,allow_sealed_test=False,resume=False,timeout_seconds=timeout_seconds)


def validate_prediction_row(row:Mapping[str,Any])->None: _call(_kernel.validate_prediction_row,row)
