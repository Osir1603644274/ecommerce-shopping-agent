"""Production runner identity for the frozen used-phone contract benchmark v2.

This adapter imports no hidden expectations or scorer.  It exposes the accepted
runner kernel to the frozen 60-case public input while keeping the v1 and public
contract-dev identities unchanged after every call.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

from . import used_phone_public_agent_runner_v1 as _kernel


PROTOCOL_VERSION = "used-phone-public-production-agent-runner-v2-contract-benchmark"
PREDICTION_SCHEMA_VERSION = "used-phone-contract-benchmark-prediction-v2"
PUBLIC_CASES_SHA256 = "62b8a7e5d90e3c2e8d64e4f981eb8be1990dbb0749aad0e55a819f3cc33c905e"
PUBLIC_CATALOG_SHA256 = _kernel.PUBLIC_CATALOG_SHA256
PUBLIC_CASE_COUNT = 60
PUBLIC_CATALOG_COUNT = _kernel.PUBLIC_CATALOG_COUNT
EXPECTED_SPLIT_COUNTS = {"dev": 20, "validation": 20, "test": 20}
EXPECTED_FAMILY_COUNTS = {
    "clause_scope": 24,
    "multi_turn_delta": 24,
    "negation_scope": 12,
}
EXPECTED_CASE_IDS = tuple(
    f"UPV2-CB-{prefix}{index:02d}"
    for prefix in ("D", "V", "T")
    for index in range(1, 21)
)
PRODUCTION_CODE_SCOPE_SHA256 = "c40406f56fe6672f9e858555816f66da132cf0620364c1356867d9958a1c326c"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "used_phone_contract_benchmark_prediction_v2.schema.json"

PublicRunnerError = _kernel.PublicRunnerError
PublicIsolationError = _kernel.PublicIsolationError
PublicCase = _kernel.PublicCase
PublicBundle = _kernel.PublicBundle
CaseResult = _kernel.CaseResult
RecordingClient = _kernel.RecordingClient
RUN_LOCAL_TEMP_DIR_NAME = _kernel.RUN_LOCAL_TEMP_DIR_NAME

_IDENTITY_FIELDS = {
    "PROTOCOL_VERSION": PROTOCOL_VERSION,
    "PREDICTION_SCHEMA_VERSION": PREDICTION_SCHEMA_VERSION,
    "PUBLIC_CASES_SHA256": PUBLIC_CASES_SHA256,
    "PUBLIC_CATALOG_SHA256": PUBLIC_CATALOG_SHA256,
    "PUBLIC_CASE_COUNT": PUBLIC_CASE_COUNT,
    "PUBLIC_CATALOG_COUNT": PUBLIC_CATALOG_COUNT,
    "EXPECTED_SPLIT_COUNTS": EXPECTED_SPLIT_COUNTS,
    "EXPECTED_FAMILY_COUNTS": EXPECTED_FAMILY_COUNTS,
    "EXPECTED_CASE_IDS": EXPECTED_CASE_IDS,
    "PRODUCTION_CODE_SCOPE_SHA256": PRODUCTION_CODE_SCOPE_SHA256,
    "SCHEMA_PATH": SCHEMA_PATH,
}
_V1_IDENTITY = {name: getattr(_kernel, name) for name in _IDENTITY_FIELDS}
_identity_lock = threading.Lock()


def _allowed_runtime_repo_files() -> frozenset[Path]:
    return frozenset({
        Path(_kernel.__file__).resolve(),
        Path(__file__).resolve(),
        SCHEMA_PATH.resolve(),
        (_kernel.REPO_ROOT / "agent" / "scripts" / "run_used_phone_contract_benchmark_v2.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "scripts" / "__init__.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "__init__.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "evaluation" / "__init__.py").resolve(),
    })


def _runner_code_hashes() -> dict[str, str]:
    paths = {
        "agent/evaluation/used_phone_public_agent_runner_v1.py": Path(_kernel.__file__).resolve(),
        "agent/evaluation/used_phone_contract_benchmark_runner_v2.py": Path(__file__).resolve(),
        "agent/evaluation/schemas/used_phone_contract_benchmark_prediction_v2.schema.json": SCHEMA_PATH.resolve(),
        "agent/scripts/run_used_phone_contract_benchmark_v2.py": (
            _kernel.REPO_ROOT / "agent" / "scripts" / "run_used_phone_contract_benchmark_v2.py"
        ).resolve(),
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


@contextmanager
def _benchmark_identity():
    if not _identity_lock.acquire(blocking=False):
        raise PublicRunnerError("contract benchmark identity is already active; use a separate process")
    original = {name: getattr(_kernel, name) for name in _IDENTITY_FIELDS}
    original_allowed = _kernel._allowed_runtime_repo_files
    original_hashes = _kernel._runner_code_hashes
    try:
        for name, value in _IDENTITY_FIELDS.items():
            setattr(_kernel, name, value)
        _kernel._allowed_runtime_repo_files = _allowed_runtime_repo_files
        _kernel._runner_code_hashes = _runner_code_hashes
        _kernel._allowed_repo_module_path.cache_clear()
        yield
    finally:
        _kernel._allowed_runtime_repo_files = original_allowed
        _kernel._runner_code_hashes = original_hashes
        for name, value in original.items():
            setattr(_kernel, name, value)
        _kernel._allowed_repo_module_path.cache_clear()
        _identity_lock.release()


def _assert_v1_restored() -> None:
    for name, value in _V1_IDENTITY.items():
        if getattr(_kernel, name) != value:
            raise PublicRunnerError(f"v1 kernel identity leaked: {name}")


def _call(function, /, *args, **kwargs):
    with _benchmark_identity():
        result = function(*args, **kwargs)
    _assert_v1_restored()
    return result


def load_public_bundle(public_cases_path: Path, public_catalog_path: Path) -> PublicBundle:
    return _call(_kernel.load_public_bundle, public_cases_path, public_catalog_path)


def production_scope_sha256() -> str:
    return _kernel.production_scope_sha256()


def verify_production_scope() -> dict[str, Any]:
    return _call(_kernel.verify_production_scope)


def audit_public_inputs(*, public_cases_path: Path, public_catalog_path: Path, verify_java: bool = True) -> dict[str, Any]:
    return _call(
        _kernel.audit_public_inputs,
        public_cases_path=public_cases_path,
        public_catalog_path=public_catalog_path,
        verify_java=verify_java,
    )


async def run_public_agent(
    *, public_cases_path: Path, public_catalog_path: Path, run_dir: Path,
    splits: Iterable[str] | None = None, case_ids: Iterable[str] | None = None,
    allow_sealed_test: bool = False, resume: bool = False,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    with _benchmark_identity():
        result = await _kernel.run_public_agent(
            public_cases_path=public_cases_path,
            public_catalog_path=public_catalog_path,
            run_dir=run_dir,
            splits=splits,
            case_ids=case_ids,
            allow_sealed_test=allow_sealed_test,
            resume=resume,
            timeout_seconds=timeout_seconds,
        )
    _assert_v1_restored()
    return result


def validate_prediction_row(row: Mapping[str, Any]) -> None:
    _call(_kernel.validate_prediction_row, row)


__all__ = [
    "PublicIsolationError", "PublicRunnerError", "audit_public_inputs",
    "load_public_bundle", "production_scope_sha256", "run_public_agent",
    "validate_prediction_row", "verify_production_scope",
]
