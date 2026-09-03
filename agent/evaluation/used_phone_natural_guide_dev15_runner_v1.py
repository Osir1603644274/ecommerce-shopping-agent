"""Public Dev15 identity over the accepted production Agent runner kernel.

The runner sees only natural-language public cases and the frozen public
catalog.  Expectations and scoring code are deliberately outside its runtime
read/import surface.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

from . import used_phone_public_agent_runner_v1 as _kernel


PROTOCOL_VERSION = "used-phone-natural-guide-dev15-production-agent-v1"
PREDICTION_SCHEMA_VERSION = "used-phone-natural-guide-dev15-prediction-v1"
PUBLIC_CASES_SHA256 = "f26a94f9034ec0da56710b78060b45c62b33ad72e9b46cebfa0fe7723fa8b6f4"
PUBLIC_CATALOG_SHA256 = _kernel.PUBLIC_CATALOG_SHA256
PUBLIC_CASE_COUNT = 15
PUBLIC_CATALOG_COUNT = _kernel.PUBLIC_CATALOG_COUNT
EXPECTED_SPLIT_COUNTS = {"dev": 15}
EXPECTED_FAMILY_COUNTS = {
    "clarification": 2,
    "comparison": 2,
    "multi_turn_delta": 3,
    "natural_constraints": 5,
    "substitute": 1,
    "unsupported_or_no_match": 2,
}
EXPECTED_CASE_IDS = tuple(f"UPNG-D{index:02d}" for index in range(1, 16))
PRODUCTION_CODE_SCOPE_SHA256 = "8aedc5739c01c59b751c773212f90ef5fdb8eb1e4a55f52639bf52b03d9b6fc2"
SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "used_phone_natural_guide_dev15_prediction_v1.schema.json"
)

PublicRunnerError = _kernel.PublicRunnerError
PublicIsolationError = _kernel.PublicIsolationError
PublicCase = _kernel.PublicCase
PublicBundle = _kernel.PublicBundle
RecordingClient = _kernel.RecordingClient

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
_IDENTITY_LOCK = threading.Lock()


def _allowed_runtime_repo_files() -> frozenset[Path]:
    root = _kernel.REPO_ROOT
    return frozenset({
        Path(_kernel.__file__).resolve(),
        Path(__file__).resolve(),
        SCHEMA_PATH.resolve(),
        (root / "agent" / "scripts" / "run_used_phone_natural_guide_dev15_v1.py").resolve(),
        (root / "agent" / "scripts" / "__init__.py").resolve(),
        (root / "agent" / "__init__.py").resolve(),
        (root / "agent" / "evaluation" / "__init__.py").resolve(),
    })


def _runner_code_hashes() -> dict[str, str]:
    root = _kernel.REPO_ROOT
    paths = {
        "agent/evaluation/used_phone_public_agent_runner_v1.py": Path(_kernel.__file__).resolve(),
        "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": Path(__file__).resolve(),
        "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": SCHEMA_PATH.resolve(),
        "agent/scripts/run_used_phone_natural_guide_dev15_v1.py": (
            root / "agent" / "scripts" / "run_used_phone_natural_guide_dev15_v1.py"
        ).resolve(),
    }
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in paths.items()
    }


@contextmanager
def _identity():
    if not _IDENTITY_LOCK.acquire(blocking=False):
        raise PublicRunnerError("Dev15 runner identity is already active; use a separate process")
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
        _IDENTITY_LOCK.release()


def _assert_restored() -> None:
    if any(getattr(_kernel, name) != value for name, value in _V1_IDENTITY.items()):
        raise PublicRunnerError("v1 runner identity leaked after Dev15 call")


def _call(function, /, *args, **kwargs):
    with _identity():
        result = function(*args, **kwargs)
    _assert_restored()
    return result


def load_public_bundle(public_cases_path: Path, public_catalog_path: Path) -> PublicBundle:
    return _call(_kernel.load_public_bundle, public_cases_path, public_catalog_path)


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
    if allow_sealed_test:
        raise PublicRunnerError("Dev15 is public-only and has no sealed split")
    if resume:
        raise PublicRunnerError("Dev15 requires a fresh immutable run directory")
    selected_splits = list(splits or ["dev"])
    selected_ids = list(case_ids) if case_ids is not None else list(EXPECTED_CASE_IDS)
    if selected_splits != ["dev"] or selected_ids != list(EXPECTED_CASE_IDS):
        raise PublicRunnerError("Dev15 requires the complete ordered public dev set")
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
    "PublicIsolationError", "PublicRunnerError", "audit_public_inputs",
    "load_public_bundle", "production_scope_sha256", "run_public_agent",
    "validate_prediction_row", "verify_production_scope",
]
