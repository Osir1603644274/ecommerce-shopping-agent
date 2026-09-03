"""One-shot sealed Test10 runner for the frozen two-stage ranking v3 scope."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading
from typing import Any, Iterable
from unittest.mock import patch

from . import used_phone_public_agent_runner_v1 as _kernel
from . import used_phone_two_stage_ranking_runner_v3 as _dev


PROTOCOL_VERSION = "used-phone-public-production-agent-runner-v3-two-stage-ranking-test10"
PREDICTION_SCHEMA_VERSION = _dev.PREDICTION_SCHEMA_VERSION
NO_MODEL_NAME = _dev.NO_MODEL_NAME
NO_MODEL_ENDPOINT = _dev.NO_MODEL_ENDPOINT
PUBLIC_CASES_SHA256 = _dev.PUBLIC_CASES_SHA256
PUBLIC_CATALOG_SHA256 = _dev.PUBLIC_CATALOG_SHA256
PUBLIC_CASE_COUNT = _dev.PUBLIC_CASE_COUNT
PUBLIC_CATALOG_COUNT = _dev.PUBLIC_CATALOG_COUNT
EXPECTED_SPLIT_COUNTS = _dev.EXPECTED_SPLIT_COUNTS
EXPECTED_FAMILY_COUNTS = _dev.EXPECTED_FAMILY_COUNTS
EXPECTED_CASE_IDS = _dev.EXPECTED_CASE_IDS
TEST_CASE_IDS = EXPECTED_CASE_IDS[20:30]
PRODUCTION_CODE_SCOPE_SHA256 = _dev.PRODUCTION_CODE_SCOPE_SHA256
SCHEMA_PATH = _dev.SCHEMA_PATH
_V2_SCHEMA_PATH = _dev._V2_SCHEMA_PATH
PublicRunnerError = _kernel.PublicRunnerError
_lock = threading.Lock()


def _allowed() -> frozenset[Path]:
    root = _kernel.REPO_ROOT
    return frozenset({
        Path(_kernel.__file__).resolve(), Path(_dev.__file__).resolve(),
        Path(__file__).resolve(), SCHEMA_PATH.resolve(), _V2_SCHEMA_PATH.resolve(),
        (root / "agent" / "scripts" / "run_used_phone_two_stage_test_v1.py").resolve(),
        (root / "agent" / "scripts" / "__init__.py").resolve(),
        (root / "agent" / "__init__.py").resolve(),
        (root / "agent" / "evaluation" / "__init__.py").resolve(),
    })


def _hashes() -> dict[str, str]:
    root = _kernel.REPO_ROOT
    paths = {
        "agent/evaluation/used_phone_public_agent_runner_v1.py": Path(_kernel.__file__).resolve(),
        "agent/evaluation/used_phone_two_stage_ranking_runner_v3.py": Path(_dev.__file__).resolve(),
        "agent/evaluation/used_phone_two_stage_test_runner_v1.py": Path(__file__).resolve(),
        "agent/evaluation/schemas/used_phone_two_stage_ranking_prediction_v3.schema.json": SCHEMA_PATH.resolve(),
        "agent/evaluation/schemas/used_phone_ranking_prediction_v2.schema.json": _V2_SCHEMA_PATH.resolve(),
        "agent/scripts/run_used_phone_two_stage_test_v1.py": (
            root / "agent" / "scripts" / "run_used_phone_two_stage_test_v1.py"
        ).resolve(),
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


_FIELDS = {
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
    "DEEPSEEK_ENDPOINT": NO_MODEL_ENDPOINT,
}


@contextmanager
def _identity():
    if not _lock.acquire(blocking=False):
        raise PublicRunnerError("Test10 runner identity is already active")
    try:
        with (
            patch.object(_dev, "_FIELDS", _FIELDS),
            patch.object(_dev, "_allowed", _allowed),
            patch.object(_dev, "_hashes", _hashes),
            _dev._identity(),
        ):
            yield
    finally:
        _lock.release()


def production_scope_sha256() -> str:
    return _kernel.production_scope_sha256()


def verify_production_scope() -> dict[str, Any]:
    with _identity():
        return _kernel.verify_production_scope()


def audit_public_inputs(*, public_cases_path: Path, public_catalog_path: Path,
                        verify_java: bool = True) -> dict[str, Any]:
    try:
        with _identity():
            return _kernel.audit_public_inputs(
                public_cases_path=public_cases_path,
                public_catalog_path=public_catalog_path,
                verify_java=verify_java,
            )
    except OSError as exc:
        raise PublicRunnerError(
            f"Test10 Java read-only audit unavailable: {type(exc).__name__}: {exc}"
        ) from exc


def _validate_selection(splits: Iterable[str] | None,
                        case_ids: Iterable[str] | None,
                        allow_sealed_test: bool) -> tuple[list[str], list[str]]:
    if allow_sealed_test is not True:
        raise PublicRunnerError("Test10 runner requires explicit sealed-test authority")
    selected_splits = list(splits or ["test"])
    if selected_splits != ["test"]:
        raise PublicRunnerError("Test10 runner requires exactly the test split")
    selected_ids = list(case_ids) if case_ids is not None else list(TEST_CASE_IDS)
    if selected_ids != list(TEST_CASE_IDS):
        raise PublicRunnerError("Test10 runner requires the complete ordered T01-T10 case set")
    return selected_splits, selected_ids


async def run_public_agent(
    *, public_cases_path: Path, public_catalog_path: Path, run_dir: Path,
    splits: Iterable[str] | None = None, case_ids: Iterable[str] | None = None,
    allow_sealed_test: bool = False, resume: bool = False,
    timeout_seconds: float = 90.0, client=None, model_name: str | None = None,
) -> dict[str, Any]:
    if resume:
        raise PublicRunnerError("Test10 runner requires a fresh directory; resume is forbidden")
    if client is not None:
        raise PublicRunnerError("Test10 no-model identity rejects external clients")
    if model_name is not None and model_name != NO_MODEL_NAME:
        raise PublicRunnerError("Test10 no-model identity rejects model overrides")
    selected_splits, selected_ids = _validate_selection(
        splits, case_ids, allow_sealed_test
    )
    try:
        with _identity():
            manifest = await _kernel.run_public_agent(
                public_cases_path=public_cases_path,
                public_catalog_path=public_catalog_path,
                run_dir=run_dir,
                splits=selected_splits,
                case_ids=selected_ids,
                allow_sealed_test=True,
                resume=False,
                timeout_seconds=timeout_seconds,
                client=_dev._NoModelClient(),
                model_name=NO_MODEL_NAME,
            )
            output = Path(run_dir).resolve()
            prediction_path = output / "predictions.jsonl"
            rows = _kernel._read_jsonl(prediction_path)
            normalized_rows = []
            for row in rows:
                if "candidatePoolIds" not in row:
                    row = {
                        **row,
                        "rankingContractVersion": _dev.TWO_STAGE_RANKING_CONTRACT_VERSION,
                        "candidatePoolIds": [],
                    }
                _dev.validate_prediction_row(row)
                normalized_rows.append(row)
            payload = b"".join(_kernel.canonical_json_bytes(row) for row in normalized_rows)
            prediction_sha = _kernel._atomic_write(prediction_path, payload)
            manifest["prediction"] = {
                "path": "predictions.jsonl", "rowCount": len(normalized_rows),
                "sha256": prediction_sha,
            }
            manifest["twoStagePredictionSchemaVersion"] = PREDICTION_SCHEMA_VERSION
            _kernel._atomic_write(
                output / "manifest.json", _kernel.canonical_json_bytes(manifest)
            )
    except OSError as exc:
        raise PublicRunnerError(
            f"Test10 Java read-only preflight unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    return manifest


def validate_prediction_row(row: dict[str, Any]) -> None:
    _dev.validate_prediction_row(row)


__all__ = [
    "PublicRunnerError", "audit_public_inputs", "production_scope_sha256",
    "run_public_agent", "validate_prediction_row", "verify_production_scope",
]
