"""Identity adapter for the public-only used-phone v2 contract-dev runner.

The previously accepted v1 runner remains byte-stable.  This module reuses its
isolation/execution kernel under a process-local, non-reentrant identity lease,
then restores every patched v1 constant even when the run fails.  Separate
processes are required for distinct benchmark identities.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping, Sequence

from . import used_phone_public_agent_runner_v1 as _kernel


PROTOCOL_VERSION = "used-phone-public-production-agent-runner-v2-contract-dev"
PREDICTION_SCHEMA_VERSION = "used-phone-public-agent-prediction-v2"
PUBLIC_CASES_SHA256 = "a68480f9448f4570d2d24314e777c385d326baebd0cd00d6e0f25343a076f083"
PUBLIC_CATALOG_SHA256 = _kernel.PUBLIC_CATALOG_SHA256
PUBLIC_CASE_COUNT = 30
PUBLIC_CATALOG_COUNT = _kernel.PUBLIC_CATALOG_COUNT
EXPECTED_SPLIT_COUNTS = {"dev": 30}
EXPECTED_FAMILY_COUNTS = {
    "clause_scope": 12,
    "multi_turn_delta": 12,
    "negation_scope": 6,
}
EXPECTED_CASE_IDS = tuple(f"UPV2-CD-{index:02d}" for index in range(1, 31))
PRODUCTION_CODE_SCOPE_SHA256 = "4163cccfbea21c0f0fdc9c3d6d957c8d35818c192034cd92f43151df26b6576c"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "used_phone_public_agent_prediction_v2.schema.json"

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
_V1_IDENTITY = {
    name: getattr(_kernel, name)
    for name in _IDENTITY_FIELDS
}
_identity_lock = threading.Lock()


def _v2_allowed_runtime_repo_files() -> frozenset[Path]:
    return frozenset({
        Path(_kernel.__file__).resolve(),
        Path(__file__).resolve(),
        SCHEMA_PATH.resolve(),
        (_kernel.REPO_ROOT / "agent" / "scripts" / "run_used_phone_public_agent_v2.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "scripts" / "__init__.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "__init__.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "evaluation" / "__init__.py").resolve(),
    })


def _v2_runner_code_hashes() -> dict[str, str]:
    paths = {
        "agent/evaluation/used_phone_public_agent_runner_v1.py": Path(_kernel.__file__).resolve(),
        "agent/evaluation/used_phone_public_agent_runner_v2.py": Path(__file__).resolve(),
        "agent/evaluation/schemas/used_phone_public_agent_prediction_v2.schema.json": SCHEMA_PATH.resolve(),
        "agent/scripts/run_used_phone_public_agent_v2.py": (
            _kernel.REPO_ROOT / "agent" / "scripts" / "run_used_phone_public_agent_v2.py"
        ).resolve(),
    }
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in paths.items()
    }


@contextmanager
def _v2_identity():
    if not _identity_lock.acquire(blocking=False):
        raise PublicRunnerError(
            "v2 benchmark identity is already active in this process; use a separate process"
        )
    original = {name: getattr(_kernel, name) for name in _IDENTITY_FIELDS}
    original_allowed_files = _kernel._allowed_runtime_repo_files
    original_runner_hashes = _kernel._runner_code_hashes
    try:
        for name, value in _IDENTITY_FIELDS.items():
            setattr(_kernel, name, value)
        _kernel._allowed_runtime_repo_files = _v2_allowed_runtime_repo_files
        _kernel._runner_code_hashes = _v2_runner_code_hashes
        _kernel._allowed_repo_module_path.cache_clear()
        yield
    finally:
        _kernel._allowed_runtime_repo_files = original_allowed_files
        _kernel._runner_code_hashes = original_runner_hashes
        for name, value in original.items():
            setattr(_kernel, name, value)
        _kernel._allowed_repo_module_path.cache_clear()
        _identity_lock.release()


def _assert_v1_restored() -> None:
    for name, value in _V1_IDENTITY.items():
        if getattr(_kernel, name) != value:
            raise PublicRunnerError(f"v1 kernel identity leaked after v2 call: {name}")


def load_public_bundle(public_cases_path: Path, public_catalog_path: Path) -> PublicBundle:
    with _v2_identity():
        result = _kernel.load_public_bundle(public_cases_path, public_catalog_path)
    _assert_v1_restored()
    return result


def production_scope_sha256() -> str:
    return _kernel.production_scope_sha256()


def verify_production_scope() -> dict[str, Any]:
    with _v2_identity():
        result = _kernel.verify_production_scope()
    _assert_v1_restored()
    return result


def audit_public_inputs(
    *, public_cases_path: Path, public_catalog_path: Path, verify_java: bool = True,
) -> dict[str, Any]:
    with _v2_identity():
        result = _kernel.audit_public_inputs(
            public_cases_path=public_cases_path,
            public_catalog_path=public_catalog_path,
            verify_java=verify_java,
        )
    _assert_v1_restored()
    return result


async def run_public_agent(
    *,
    public_cases_path: Path,
    public_catalog_path: Path,
    run_dir: Path,
    splits: Iterable[str] | None = None,
    case_ids: Iterable[str] | None = None,
    allow_sealed_test: bool = False,
    resume: bool = False,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    if allow_sealed_test:
        raise PublicRunnerError("contract-dev v2 has no sealed test split")
    with _v2_identity():
        result = await _kernel.run_public_agent(
            public_cases_path=public_cases_path,
            public_catalog_path=public_catalog_path,
            run_dir=run_dir,
            splits=splits,
            case_ids=case_ids,
            allow_sealed_test=False,
            resume=resume,
            timeout_seconds=timeout_seconds,
        )
    _assert_v1_restored()
    return result


def validate_prediction_row(row: Mapping[str, Any]) -> None:
    with _v2_identity():
        _kernel.validate_prediction_row(row)
    _assert_v1_restored()


def materialize_prediction(**kwargs: Any) -> dict[str, Any]:
    with _v2_identity():
        result = _kernel.materialize_prediction(**kwargs)
    _assert_v1_restored()
    return result


__all__ = [
    "CaseResult", "PublicBundle", "PublicCase", "PublicIsolationError",
    "PublicRunnerError", "RUN_LOCAL_TEMP_DIR_NAME", "RecordingClient",
    "audit_public_inputs", "load_public_bundle", "materialize_prediction",
    "production_scope_sha256", "run_public_agent", "validate_prediction_row",
    "verify_production_scope",
]
