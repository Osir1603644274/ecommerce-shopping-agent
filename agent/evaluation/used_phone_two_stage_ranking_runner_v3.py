"""Public-dev-only runner identity for auditable two-stage ranking v3."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent.app.domains.ecommerce.ranking_contract import (
    TWO_STAGE_RANKING_CONTRACT_VERSION,
    TwoStageRankingContractError,
    normalize_persisted_ranking_values,
    normalize_search_products_detail,
)

from . import used_phone_public_agent_runner_v1 as _kernel


PROTOCOL_VERSION = "used-phone-public-production-agent-runner-v3-two-stage-ranking-dev"
PREDICTION_SCHEMA_VERSION = "used-phone-two-stage-ranking-prediction-v3"
NO_MODEL_NAME = "none-deterministic-production-control"
NO_MODEL_ENDPOINT = "disabled://no-model"
PUBLIC_CASES_SHA256 = "2c05fcfcad843bc97a22b4d22a58c894f6574ef408cbcfca05cf6783fee2917c"
PUBLIC_CATALOG_SHA256 = _kernel.PUBLIC_CATALOG_SHA256
PUBLIC_CASE_COUNT = 30
PUBLIC_CATALOG_COUNT = _kernel.PUBLIC_CATALOG_COUNT
EXPECTED_SPLIT_COUNTS = {"dev": 10, "validation": 10, "test": 10}
EXPECTED_FAMILY_COUNTS = {
    "hard_constraint": 15,
    "hard_soft_ranking": 9,
    "multi_turn_update": 3,
    "negation": 3,
}
EXPECTED_CASE_IDS = tuple(
    f"UPV2-RK-{prefix}{index:02d}"
    for prefix in ("D", "V", "T")
    for index in range(1, 11)
)
PRODUCTION_CODE_SCOPE_SHA256 = "192c9313927c4cc01e648426bbdc2c7a5d9e0ffc6444f00ce46725c2011a32ff"
SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "used_phone_two_stage_ranking_prediction_v3.schema.json"
)
_V2_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "used_phone_ranking_prediction_v2.schema.json"
)

PublicRunnerError = _kernel.PublicRunnerError
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
_V1 = {name: getattr(_kernel, name) for name in _FIELDS}
_V1_MATERIALIZE_PREDICTION = _kernel.materialize_prediction
_V1_NETWORK_GUARD = _kernel.production_network_guard
_lock = threading.Lock()


class _NoModelCompletions:
    async def create(self, *_args: Any, **_kwargs: Any) -> Any:
        raise PublicRunnerError(
            "v3 public-dev identity forbids model calls; deterministic production control did not close the turn"
        )


class _NoModelClient:
    """Kernel-compatible client that turns any model attempt into a closed failure."""

    def __init__(self) -> None:
        self.ledger: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=_NoModelCompletions())

    def fork(self) -> "_NoModelClient":
        return _NoModelClient()


@contextmanager
def _no_model_network_guard(
    network_ledger: list[dict[str, Any]],
    tool_invocation_ledger: list[dict[str, Any]] | None = None,
):
    """Retain the v1 Java read boundary while denying model HTTP at send time."""

    import httpx

    with _V1_NETWORK_GUARD(network_ledger, tool_invocation_ledger):
        guarded_send = httpx.AsyncClient.send

        async def no_model_send(client, request, *args, **kwargs):  # noqa: ANN001
            host = (request.url.host or "").casefold()
            if host == "api.deepseek.com":
                raise PublicRunnerError(
                    "v3 no-model identity denied model network before send"
                )
            return await guarded_send(client, request, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "send", no_model_send):
            yield


def _allowed() -> frozenset[Path]:
    root = _kernel.REPO_ROOT
    return frozenset({
        Path(_kernel.__file__).resolve(),
        Path(__file__).resolve(),
        SCHEMA_PATH.resolve(),
        _V2_SCHEMA_PATH.resolve(),
        (root / "agent" / "scripts" / "run_used_phone_two_stage_ranking_v3.py").resolve(),
        (root / "agent" / "scripts" / "__init__.py").resolve(),
        (root / "agent" / "__init__.py").resolve(),
        (root / "agent" / "evaluation" / "__init__.py").resolve(),
    })


def _hashes() -> dict[str, str]:
    paths = {
        "agent/evaluation/used_phone_public_agent_runner_v1.py": Path(_kernel.__file__).resolve(),
        "agent/evaluation/used_phone_two_stage_ranking_runner_v3.py": Path(__file__).resolve(),
        "agent/evaluation/schemas/used_phone_two_stage_ranking_prediction_v3.schema.json": SCHEMA_PATH.resolve(),
        "agent/evaluation/schemas/used_phone_ranking_prediction_v2.schema.json": _V2_SCHEMA_PATH.resolve(),
        "agent/scripts/run_used_phone_two_stage_ranking_v3.py": (
            _kernel.REPO_ROOT / "agent" / "scripts" / "run_used_phone_two_stage_ranking_v3.py"
        ).resolve(),
    }
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in paths.items()
    }


def _strict_search_output(
    tool_traces: Sequence[Mapping[str, Any]],
    final_turn_index: int | None = None,
):
    trace = next(
        (
            item for item in reversed(tool_traces)
            if item.get("tool") == "search_products"
            and (
                final_turn_index is None
                or item.get("publicTurnIndex") == final_turn_index
            )
        ),
        None,
    )
    if trace is None:
        return None
    if trace.get("ok") is not True:
        raise PublicRunnerError(
            "the final search_products attempt did not succeed; older results cannot be reused"
        )
    try:
        return normalize_search_products_detail(trace.get("detail"))
    except TwoStageRankingContractError as exc:
        raise PublicRunnerError(
            f"untrusted two-stage search result: {exc.code}: {exc}"
        ) from exc


def _trusted_persisted_search_output(
    *,
    tool_traces: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    final_turn_index: int,
):
    raw_output = _strict_search_output(tool_traces, final_turn_index)
    if raw_output is None:
        return None
    state = next(
        (
            item.get("state") for item in reversed(snapshots)
            if isinstance(item, Mapping) and isinstance(item.get("state"), Mapping)
        ),
        None,
    )
    if not isinstance(state, Mapping):
        raise PublicRunnerError("trusted ranking TaskState snapshot is missing")
    active_plan = state.get("activePlan")
    domain_state = state.get("domainState")
    if not isinstance(active_plan, Mapping) or not isinstance(domain_state, Mapping):
        raise PublicRunnerError("trusted ranking Plan or domainState is missing")
    search_steps = [
        step for step in active_plan.get("steps", [])
        if isinstance(step, Mapping)
        and step.get("toolName") == "search_products"
        and step.get("status") == "executed"
    ] if isinstance(active_plan.get("steps"), list) else []
    if len(search_steps) != 1:
        raise PublicRunnerError("final Plan must contain one executed search_products step")
    step_id = search_steps[0].get("stepId")
    outputs = domain_state.get("stepOutputs")
    stored = outputs.get(step_id) if isinstance(outputs, Mapping) else None
    if (
        not isinstance(stored, Mapping)
        or stored.get("taskId") != state.get("taskId")
        or stored.get("planId") != active_plan.get("planId")
        or stored.get("stepId") != step_id
    ):
        raise PublicRunnerError("persisted ranking output identity mismatch")
    try:
        persisted = normalize_persisted_ranking_values(stored.get("values"))
    except TwoStageRankingContractError as exc:
        raise PublicRunnerError(
            f"untrusted persisted ranking output: {exc.code}: {exc}"
        ) from exc
    if persisted != raw_output:
        raise PublicRunnerError("raw and persisted two-stage ranking outputs differ")
    return persisted


def _strict_actual_ranked_ids(
    tool_traces: Sequence[Mapping[str, Any]],
    final_turn_index: int | None = None,
) -> list[str]:
    output = _strict_search_output(tool_traces, final_turn_index)
    return [str(item) for item in output.ranked_item_ids] if output else []


def _materialize_two_stage_prediction(**kwargs: Any) -> dict[str, Any]:
    case = kwargs["case"]
    final_turn_index = len(case.turns) - 1
    output = _trusted_persisted_search_output(
        tool_traces=kwargs["tool_traces"],
        snapshots=kwargs["snapshots"],
        final_turn_index=final_turn_index,
    )
    with patch.object(_kernel, "SCHEMA_PATH", _V2_SCHEMA_PATH):
        prediction = _V1_MATERIALIZE_PREDICTION(**kwargs)
    prediction["rankingContractVersion"] = TWO_STAGE_RANKING_CONTRACT_VERSION
    prediction["candidatePoolIds"] = (
        [str(item) for item in output.candidate_pool_ids]
        if output is not None
        else []
    )
    ranked = prediction.get("rankedItemIds", [])
    if not set(ranked).issubset(prediction["candidatePoolIds"]):
        raise PublicRunnerError("ranked prediction is outside the trusted candidate pool")
    if any(
        citation.get("itemId") not in ranked
        for citation in prediction.get("evidenceCitations", [])
        if isinstance(citation, Mapping)
    ):
        raise PublicRunnerError("prediction citation is outside rankedItemIds")
    validate_prediction_row(prediction)
    return prediction


@contextmanager
def _identity():
    if not _lock.acquire(blocking=False):
        raise PublicRunnerError("v3 two-stage ranking identity is already active")
    original = {name: getattr(_kernel, name) for name in _FIELDS}
    allowed = _kernel._allowed_runtime_repo_files
    hashes = _kernel._runner_code_hashes
    materializer = _kernel.materialize_prediction
    ranked_extractor = _kernel._actual_ranked_ids
    network_guard = _kernel.production_network_guard
    try:
        for name, value in _FIELDS.items():
            setattr(_kernel, name, value)
        _kernel._allowed_runtime_repo_files = _allowed
        _kernel._runner_code_hashes = _hashes
        _kernel.materialize_prediction = _materialize_two_stage_prediction
        _kernel._actual_ranked_ids = _strict_actual_ranked_ids
        _kernel.production_network_guard = _no_model_network_guard
        _kernel._allowed_repo_module_path.cache_clear()
        yield
    finally:
        _kernel._allowed_runtime_repo_files = allowed
        _kernel._runner_code_hashes = hashes
        _kernel.materialize_prediction = materializer
        _kernel._actual_ranked_ids = ranked_extractor
        _kernel.production_network_guard = network_guard
        for name, value in original.items():
            setattr(_kernel, name, value)
        _kernel._allowed_repo_module_path.cache_clear()
        _lock.release()


def _assert_restored() -> None:
    if any(getattr(_kernel, name) != value for name, value in _V1.items()):
        raise PublicRunnerError("v1 identity leaked after v3 runner call")


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
    try:
        return _call(
            _kernel.audit_public_inputs,
            public_cases_path=public_cases_path,
            public_catalog_path=public_catalog_path,
            verify_java=verify_java,
        )
    except OSError as exc:
        raise PublicRunnerError(
            f"v3 Java read-only audit unavailable: {type(exc).__name__}: {exc}"
        ) from exc


def _validate_dev_selection(
    splits: Iterable[str] | None,
    case_ids: Iterable[str] | None,
    allow_sealed_test: bool,
) -> tuple[list[str], list[str] | None]:
    if allow_sealed_test:
        raise PublicRunnerError("v3 identity is public-dev-only; sealed test is forbidden")
    selected_splits = list(splits or ["dev"])
    if selected_splits != ["dev"]:
        raise PublicRunnerError(
            "v3 identity requires exactly one ordered public dev split"
        )
    selected_ids = list(case_ids) if case_ids is not None else None
    if selected_ids is not None and selected_ids != list(EXPECTED_CASE_IDS[:10]):
        raise PublicRunnerError(
            "v3 scored identity requires the complete ordered public dev case set"
        )
    return selected_splits, list(EXPECTED_CASE_IDS[:10])


async def run_public_agent(
    *, public_cases_path: Path, public_catalog_path: Path, run_dir: Path,
    splits: Iterable[str] | None = None, case_ids: Iterable[str] | None = None,
    allow_sealed_test: bool = False, resume: bool = False,
    timeout_seconds: float = 90.0, client=None, model_name: str | None = None,
) -> dict[str, Any]:
    if resume:
        raise PublicRunnerError(
            "v3 public-dev identity requires a fresh run directory; resume is forbidden"
        )
    if client is not None:
        raise PublicRunnerError(
            "v3 no-model identity does not accept an external model client"
        )
    if model_name is not None and model_name != NO_MODEL_NAME:
        raise PublicRunnerError("v3 no-model identity forbids model overrides")
    selected_splits, selected_ids = _validate_dev_selection(
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
                allow_sealed_test=False,
                resume=resume,
                timeout_seconds=timeout_seconds,
                client=_NoModelClient(),
                model_name=NO_MODEL_NAME,
            )
    except OSError as exc:
        raise PublicRunnerError(
            f"v3 Java read-only preflight unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    with _identity():
        output = Path(run_dir).resolve()
        prediction_path = output / "predictions.jsonl"
        rows = _kernel._read_jsonl(prediction_path)
        normalized_rows = []
        for row in rows:
            if "candidatePoolIds" not in row:
                row = {
                    **row,
                    "rankingContractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                    "candidatePoolIds": [],
                }
            validate_prediction_row(row)
            normalized_rows.append(row)
        payload = b"".join(_kernel.canonical_json_bytes(row) for row in normalized_rows)
        prediction_sha = _kernel._atomic_write(prediction_path, payload)
        manifest["prediction"] = {
            "path": "predictions.jsonl",
            "rowCount": len(normalized_rows),
            "sha256": prediction_sha,
        }
        manifest["twoStagePredictionSchemaVersion"] = PREDICTION_SCHEMA_VERSION
        _kernel._atomic_write(
            output / "manifest.json", _kernel.canonical_json_bytes(manifest)
        )
    _assert_restored()
    return manifest


def validate_prediction_row(row: Mapping[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(dict(row))
    pool = row.get("candidatePoolIds")
    ranked = row.get("rankedItemIds")
    if isinstance(pool, list) and isinstance(ranked, list) and not set(ranked).issubset(pool):
        raise PublicRunnerError("rankedItemIds must be a subset of candidatePoolIds")
    if any(
        citation.get("itemId") not in ranked
        for citation in row.get("evidenceCitations", [])
        if isinstance(citation, Mapping)
    ):
        raise PublicRunnerError("evidence citation item must be ranked")


__all__ = [
    "PublicRunnerError", "audit_public_inputs", "production_scope_sha256",
    "run_public_agent", "validate_prediction_row", "verify_production_scope",
]
