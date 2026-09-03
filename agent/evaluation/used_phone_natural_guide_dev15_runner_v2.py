"""Versioned Dev15 post-TaskState-fix runner identity for attempt-002."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

from . import used_phone_public_agent_runner_v1 as _kernel
from . import used_phone_natural_guide_dev15_runner_v1 as _v1


PROTOCOL_VERSION = "used-phone-natural-guide-dev15-production-agent-v2-taskstate-fix"
PRODUCTION_CODE_SCOPE_SHA256 = "aa72d4bbcaa0f6c7dad7fa39437d003111fcd214c4b0d3d6397113f63e716204"
PublicRunnerError = _kernel.PublicRunnerError

_FIELDS = dict(_v1._IDENTITY_FIELDS)
_FIELDS.update({
    "PROTOCOL_VERSION": PROTOCOL_VERSION,
    "PRODUCTION_CODE_SCOPE_SHA256": PRODUCTION_CODE_SCOPE_SHA256,
})
_V1 = {name: getattr(_kernel, name) for name in _FIELDS}
_LOCK = threading.Lock()


def _allowed() -> frozenset[Path]:
    root = _kernel.REPO_ROOT
    return frozenset({
        Path(_kernel.__file__).resolve(),
        Path(_v1.__file__).resolve(),
        Path(__file__).resolve(),
        _v1.SCHEMA_PATH.resolve(),
        (root / "agent" / "scripts" / "run_used_phone_natural_guide_dev15_v2.py").resolve(),
        (root / "agent" / "scripts" / "__init__.py").resolve(),
        (root / "agent" / "__init__.py").resolve(),
        (root / "agent" / "evaluation" / "__init__.py").resolve(),
    })


def _hashes() -> dict[str, str]:
    root = _kernel.REPO_ROOT
    paths = {
        "agent/evaluation/used_phone_public_agent_runner_v1.py": Path(_kernel.__file__).resolve(),
        "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": Path(_v1.__file__).resolve(),
        "agent/evaluation/used_phone_natural_guide_dev15_runner_v2.py": Path(__file__).resolve(),
        "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": _v1.SCHEMA_PATH.resolve(),
        "agent/scripts/run_used_phone_natural_guide_dev15_v2.py": (
            root / "agent" / "scripts" / "run_used_phone_natural_guide_dev15_v2.py"
        ).resolve(),
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


@contextmanager
def _identity():
    if not _LOCK.acquire(blocking=False):
        raise PublicRunnerError("Dev15 v2 identity is already active")
    original = {name: getattr(_kernel, name) for name in _FIELDS}
    original_allowed = _kernel._allowed_runtime_repo_files
    original_hashes = _kernel._runner_code_hashes
    try:
        for name, value in _FIELDS.items():
            setattr(_kernel, name, value)
        _kernel._allowed_runtime_repo_files = _allowed
        _kernel._runner_code_hashes = _hashes
        _kernel._allowed_repo_module_path.cache_clear()
        yield
    finally:
        _kernel._allowed_runtime_repo_files = original_allowed
        _kernel._runner_code_hashes = original_hashes
        for name, value in original.items():
            setattr(_kernel, name, value)
        _kernel._allowed_repo_module_path.cache_clear()
        _LOCK.release()


def _assert_restored() -> None:
    if any(getattr(_kernel, name) != value for name, value in _V1.items()):
        raise PublicRunnerError("v1 kernel identity leaked after Dev15 v2 call")


def _call(function, /, *args, **kwargs):
    with _identity():
        result = function(*args, **kwargs)
    _assert_restored()
    return result


def production_scope_sha256() -> str:
    return _kernel.production_scope_sha256()


def verify_production_scope() -> dict[str, Any]:
    return _call(_kernel.verify_production_scope)


def audit_public_inputs(
    *, public_cases_path: Path, public_catalog_path: Path, verify_java: bool = True,
) -> dict[str, Any]:
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
    if allow_sealed_test or resume:
        raise PublicRunnerError("Dev15 v2 is public-only and requires a fresh run")
    selected_splits = list(splits or ["dev"])
    selected_ids = list(case_ids) if case_ids is not None else list(_v1.EXPECTED_CASE_IDS)
    if selected_splits != ["dev"] or selected_ids != list(_v1.EXPECTED_CASE_IDS):
        raise PublicRunnerError("Dev15 v2 requires the complete ordered public dev set")
    with _identity():
        result = await _kernel.run_public_agent(
            public_cases_path=public_cases_path,
            public_catalog_path=public_catalog_path,
            run_dir=run_dir,
            splits=selected_splits,
            case_ids=selected_ids,
            allow_sealed_test=False,
            resume=False,
            timeout_seconds=timeout_seconds,
        )
    _assert_restored()
    return result


def validate_prediction_row(row: Mapping[str, Any]) -> None:
    _call(_kernel.validate_prediction_row, row)


__all__ = [
    "PublicRunnerError", "audit_public_inputs", "production_scope_sha256",
    "run_public_agent", "validate_prediction_row", "verify_production_scope",
]
