"""SUT-only runner identity for the frozen used-phone ranking benchmark v2."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

from . import used_phone_public_agent_runner_v1 as _kernel


PROTOCOL_VERSION = "used-phone-public-production-agent-runner-v2-ranking"
PREDICTION_SCHEMA_VERSION = "used-phone-ranking-prediction-v2"
PUBLIC_CASES_SHA256 = "2c05fcfcad843bc97a22b4d22a58c894f6574ef408cbcfca05cf6783fee2917c"
PUBLIC_CATALOG_SHA256 = _kernel.PUBLIC_CATALOG_SHA256
PUBLIC_CASE_COUNT = 30
PUBLIC_CATALOG_COUNT = _kernel.PUBLIC_CATALOG_COUNT
EXPECTED_SPLIT_COUNTS = {"dev": 10, "validation": 10, "test": 10}
EXPECTED_FAMILY_COUNTS = {"hard_constraint": 15, "hard_soft_ranking": 9, "multi_turn_update": 3, "negation": 3}
EXPECTED_CASE_IDS = tuple(f"UPV2-RK-{prefix}{index:02d}" for prefix in ("D", "V", "T") for index in range(1, 11))
PRODUCTION_CODE_SCOPE_SHA256 = "7708d786415a426816315a8488524c8b3468fe521904935e40de9daf9de18372"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "used_phone_ranking_prediction_v2.schema.json"

PublicRunnerError = _kernel.PublicRunnerError
_FIELDS = {
    "PROTOCOL_VERSION": PROTOCOL_VERSION, "PREDICTION_SCHEMA_VERSION": PREDICTION_SCHEMA_VERSION,
    "PUBLIC_CASES_SHA256": PUBLIC_CASES_SHA256, "PUBLIC_CATALOG_SHA256": PUBLIC_CATALOG_SHA256,
    "PUBLIC_CASE_COUNT": PUBLIC_CASE_COUNT, "PUBLIC_CATALOG_COUNT": PUBLIC_CATALOG_COUNT,
    "EXPECTED_SPLIT_COUNTS": EXPECTED_SPLIT_COUNTS, "EXPECTED_FAMILY_COUNTS": EXPECTED_FAMILY_COUNTS,
    "EXPECTED_CASE_IDS": EXPECTED_CASE_IDS, "PRODUCTION_CODE_SCOPE_SHA256": PRODUCTION_CODE_SCOPE_SHA256,
    "SCHEMA_PATH": SCHEMA_PATH,
}
_V1 = {name: getattr(_kernel, name) for name in _FIELDS}
_lock = threading.Lock()


def _allowed() -> frozenset[Path]:
    return frozenset({
        Path(_kernel.__file__).resolve(), Path(__file__).resolve(), SCHEMA_PATH.resolve(),
        (_kernel.REPO_ROOT / "agent" / "scripts" / "run_used_phone_ranking_v2.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "scripts" / "__init__.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "__init__.py").resolve(),
        (_kernel.REPO_ROOT / "agent" / "evaluation" / "__init__.py").resolve(),
    })


def _hashes() -> dict[str, str]:
    paths = {
        "agent/evaluation/used_phone_public_agent_runner_v1.py": Path(_kernel.__file__).resolve(),
        "agent/evaluation/used_phone_ranking_runner_v2.py": Path(__file__).resolve(),
        "agent/evaluation/schemas/used_phone_ranking_prediction_v2.schema.json": SCHEMA_PATH.resolve(),
        "agent/scripts/run_used_phone_ranking_v2.py": (_kernel.REPO_ROOT / "agent" / "scripts" / "run_used_phone_ranking_v2.py").resolve(),
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


@contextmanager
def _identity():
    if not _lock.acquire(blocking=False):
        raise PublicRunnerError("ranking identity is already active; use a separate process")
    original = {name: getattr(_kernel, name) for name in _FIELDS}
    allowed, hashes = _kernel._allowed_runtime_repo_files, _kernel._runner_code_hashes
    try:
        for name, value in _FIELDS.items():
            setattr(_kernel, name, value)
        _kernel._allowed_runtime_repo_files, _kernel._runner_code_hashes = _allowed, _hashes
        _kernel._allowed_repo_module_path.cache_clear()
        yield
    finally:
        _kernel._allowed_runtime_repo_files, _kernel._runner_code_hashes = allowed, hashes
        for name, value in original.items():
            setattr(_kernel, name, value)
        _kernel._allowed_repo_module_path.cache_clear()
        _lock.release()


def _assert_restored() -> None:
    if any(getattr(_kernel, name) != value for name, value in _V1.items()):
        raise PublicRunnerError("v1 identity leaked after ranking runner call")


def _call(function, /, *args, **kwargs):
    with _identity():
        result = function(*args, **kwargs)
    _assert_restored()
    return result


def production_scope_sha256() -> str:
    return _kernel.production_scope_sha256()


def verify_production_scope() -> dict[str, Any]:
    return _call(_kernel.verify_production_scope)


def audit_public_inputs(*, public_cases_path: Path, public_catalog_path: Path, verify_java: bool = True) -> dict[str, Any]:
    return _call(_kernel.audit_public_inputs, public_cases_path=public_cases_path, public_catalog_path=public_catalog_path, verify_java=verify_java)


async def run_public_agent(
    *, public_cases_path: Path, public_catalog_path: Path, run_dir: Path,
    splits: Iterable[str] | None = None, case_ids: Iterable[str] | None = None,
    allow_sealed_test: bool = False, resume: bool = False, timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    with _identity():
        result = await _kernel.run_public_agent(
            public_cases_path=public_cases_path, public_catalog_path=public_catalog_path,
            run_dir=run_dir, splits=splits, case_ids=case_ids,
            allow_sealed_test=allow_sealed_test, resume=resume, timeout_seconds=timeout_seconds,
        )
    _assert_restored()
    return result


def validate_prediction_row(row: Mapping[str, Any]) -> None:
    _call(_kernel.validate_prediction_row, row)


__all__ = ["PublicRunnerError", "audit_public_inputs", "production_scope_sha256", "run_public_agent", "validate_prediction_row", "verify_production_scope"]
