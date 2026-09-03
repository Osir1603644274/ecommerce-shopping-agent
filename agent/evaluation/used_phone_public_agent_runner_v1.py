"""Public-only runner for the frozen 100-case used-phone production Agent.

The runner is deliberately label blind.  It pins and reads only the public
case envelope and public 252-item catalog, then calls ``app.llm.run_agent``.
Hidden cases, judgments, scorers, builders, oracle outputs and release
manifests are outside the readable/importable surface while a case executes.
"""

from __future__ import annotations

import asyncio
import builtins
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import sysconfig
import tempfile
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from unittest.mock import patch

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


PROTOCOL_VERSION = "used-phone-public-production-agent-runner-v1"
PREDICTION_SCHEMA_VERSION = "used-phone-public-agent-prediction-v1"
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
PUBLIC_CASES_SHA256 = "dc2ea60041c5db3cd8db8df81afcafe542944b1d961624b3aadf9797511bf111"
PUBLIC_CATALOG_SHA256 = "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50"
PUBLIC_CASE_COUNT = 100
PUBLIC_CATALOG_COUNT = 252
EXPECTED_SPLIT_COUNTS = {"dev": 50, "test": 30, "validation": 20}
EXPECTED_FAMILY_COUNTS = {
    "clarification": 10,
    "comparison": 10,
    "hard_constraint": 10,
    "hard_soft_ranking": 10,
    "multi_turn_update": 10,
    "negation": 10,
    "substitute": 10,
    "tradeoff_explanation": 10,
    "unknown_conflict": 10,
    "unsatisfiable": 10,
}
EXPECTED_FAMILY_PREFIXES = ("CLR", "CMP", "HC", "HS", "MT", "NEG", "NO", "SUB", "TO", "UNK")
EXPECTED_CASE_IDS = tuple(
    f"UPV1-{prefix}-{index:02d}"
    for prefix in EXPECTED_FAMILY_PREFIXES
    for index in range(1, 11)
)
CONTROLLED_GROUPS = (
    "battery_health",
    "battery_originality",
    "motherboard_repair",
    "os",
    "scratch_level",
    "screen_originality",
    "shell_condition",
)
EXPECTED_ATTRIBUTE_STATUS_COUNTS = {
    "battery_health": {"known": 220, "conflict": 0, "unknown": 32},
    "battery_originality": {"known": 224, "conflict": 0, "unknown": 28},
    "motherboard_repair": {"known": 227, "conflict": 0, "unknown": 25},
    "os": {"known": 204, "conflict": 1, "unknown": 47},
    "scratch_level": {"known": 224, "conflict": 0, "unknown": 28},
    "screen_originality": {"known": 224, "conflict": 0, "unknown": 28},
    "shell_condition": {"known": 223, "conflict": 0, "unknown": 29},
}
ATTRIBUTE_RULESET_VERSION = "used-phone-exact-token-seven-field-v2"
ATTRIBUTE_CONTRACT_CODE_SHA256 = "545c4f7d528ed9875dd468d9d9bbf5e4e6034b6cdbcf0860800bfc2b4b18688a"
PRODUCTION_CODE_SCOPE_SHA256 = "f948ee5aae0fd30657d187dcca0d61803ef1e2f488a9462ade64c21e507ec3fe"
JAVA_BASE_URL = "http://127.0.0.1:18081"
JAVA_CATALOG_VERSION = f"used-phone-benchmark-v1-{DATASET_REVISION}"
JAVA_CATALOG_CONTENT_SHA256 = "d60cdf433f035df73b2530a8b28e7352a3f92c7b4db2af5a18ad43fd4b8a1d00"
JAVA_ATTRIBUTE_COUNT = 1547
JAVA_PROJECTION_SHA256 = "566d619fb4ae6ef00d8ca1bd37cf173f196abbbf54cade078c1c5a5a478928ca"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
PRODUCTION_RUNTIME_CONFIG = {
    "agentContextMode": "context_pack",
    "agentOrchestratorMode": "unified",
    "agentLegacyFallbackEnabled": False,
    "agentRequestDeadlineSeconds": 20.0,
    "agentToolTransportMode": "live",
    "ecommerceGuideEnabled": True,
    "agentTransactionEnabled": False,
    "evidenceCriticEnabled": False,
    "productRetrievalMode": "bm25",
    "backendBaseUrl": JAVA_BASE_URL,
}
ALLOWED_PRODUCT_TOOLS = frozenset({"search_products", "get_product_details", "compare_products"})
FORMAL_ACTIONS = (
    "RETRIEVE_FILTER_AND_RANK",
    "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
    "CLARIFY",
    "COMPARE_WITH_FIELD_EVIDENCE",
    "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS",
    "ABSTAIN_OR_EXPLAIN",
    "RETRIEVE_FILTER_AND_RANK_WITH_UNKNOWNS",
    "UPDATE_STATE_THEN_RETRIEVE",
)
RETRIEVAL_ACTIONS = frozenset({
    "RETRIEVE_FILTER_AND_RANK",
    "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
    "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS",
    "RETRIEVE_FILTER_AND_RANK_WITH_UNKNOWNS",
    "UPDATE_STATE_THEN_RETRIEVE",
})
DENIED_MODULE_FRAGMENTS = (
    "used_phone_benchmark_scorer",
    "used_phone_full_catalog_judgments",
    "used_phone_offline_baselines",
    "used_phone_complex_cases_v1",
    "freeze_used_phone_complex_benchmark",
    "summarize_used_phone_baselines",
    "used_phone_complex_metrics",
    "used_phone_complex_pilot",
    "used_phone_model_harness",
    "build_used_phone_",
    "score_used_phone_",
)
DENIED_PATH_FRAGMENTS = (
    "cases_hidden",
    "judgments",
    "qrel",
    "scorer",
    "oracle",
    "rule_floor",
    "baseline_report",
    "evaluation_runs",
    "release_audit",
    "frozen_used_phone",
    "manifest.json",
    "audit.json",
)
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "used_phone_public_agent_prediction_v1.schema.json"
REPO_ROOT = Path(__file__).resolve().parents[2]

# Run-local scratch directory name.  ``public_runtime_isolation`` points the
# runtime's default tempfile at ``<run_dir>/.runner-temp`` for the duration of a
# guarded block so CPython's lazy default-temp-dir writability probe and every
# later default tempfile file stay inside the run directory.
RUN_LOCAL_TEMP_DIR_NAME = ".runner-temp"


class PublicRunnerError(RuntimeError):
    """Fail-closed public runner contract error."""


class PublicIsolationError(PublicRunnerError):
    """A hidden artifact/module or forbidden write/network path was observed."""


@dataclass(frozen=True)
class PublicCase:
    case_id: str
    split: str
    behavior_family: str
    turns: tuple[dict[str, Any], ...]
    visible_candidates: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PublicBundle:
    cases: tuple[PublicCase, ...]
    catalog_by_id: Mapping[str, dict[str, Any]]
    public_cases_path: Path
    public_catalog_path: Path

    def by_id(self) -> dict[str, PublicCase]:
        return {case.case_id: case for case in self.cases}


@dataclass
class CaseResult:
    prediction: dict[str, Any]
    trace: dict[str, Any]


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise PublicRunnerError(f"blank JSONL row: {path.name}:{line_number}")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PublicRunnerError(f"invalid JSONL row: {path.name}:{line_number}") from exc
            if not isinstance(value, dict):
                raise PublicRunnerError(f"non-object JSONL row: {path.name}:{line_number}")
            rows.append(value)
    return rows


def _counter(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def load_public_bundle(public_cases_path: Path, public_catalog_path: Path) -> PublicBundle:
    cases_path = Path(public_cases_path).resolve()
    catalog_path = Path(public_catalog_path).resolve()
    if cases_path.name != "cases_public.jsonl" or catalog_path.name != "catalog.jsonl":
        raise PublicRunnerError("public artifact filenames do not match the pinned contract")
    if sha256_file(cases_path) != PUBLIC_CASES_SHA256:
        raise PublicRunnerError("public cases SHA mismatch")
    if sha256_file(catalog_path) != PUBLIC_CATALOG_SHA256:
        raise PublicRunnerError("public catalog SHA mismatch")
    case_rows = _read_jsonl(cases_path)
    catalog_rows = _read_jsonl(catalog_path)
    if len(case_rows) != PUBLIC_CASE_COUNT or len(catalog_rows) != PUBLIC_CATALOG_COUNT:
        raise PublicRunnerError("public artifact count mismatch")
    if tuple(row.get("caseId") for row in case_rows) != EXPECTED_CASE_IDS:
        raise PublicRunnerError("public case identity/order mismatch")
    if _counter(str(row.get("split")) for row in case_rows) != EXPECTED_SPLIT_COUNTS:
        raise PublicRunnerError("public split count mismatch")
    if _counter(str(row.get("behaviorFamily")) for row in case_rows) != EXPECTED_FAMILY_COUNTS:
        raise PublicRunnerError("public behavior family count mismatch")

    cases: list[PublicCase] = []
    for row in case_rows:
        allowed = {
            "behaviorFamily", "caseId", "lengthClass", "schemaVersion", "split",
            "turns", "userVisibleCandidateContext",
        }
        if not set(row).issubset(allowed) or row.get("schemaVersion") != "used-phone-complex-cases-v1":
            raise PublicRunnerError(f"public case shape mismatch: {row.get('caseId')}")
        turns = row.get("turns")
        if not isinstance(turns, list) or not 1 <= len(turns) <= 2:
            raise PublicRunnerError(f"public turn contract mismatch: {row.get('caseId')}")
        for turn in turns:
            if (
                not isinstance(turn, dict)
                or set(turn) != {"role", "text", "turnId"}
                or turn.get("role") != "user"
                or not isinstance(turn.get("text"), str)
                or not turn["text"].strip()
            ):
                raise PublicRunnerError(f"public turn shape mismatch: {row.get('caseId')}")
        visible = row.get("userVisibleCandidateContext", [])
        if not isinstance(visible, list) or len(visible) not in {0, 1, 2}:
            raise PublicRunnerError(f"public visible candidate mismatch: {row.get('caseId')}")
        for candidate in visible:
            if (
                not isinstance(candidate, dict)
                or set(candidate) != {"candidateLabel", "itemId", "title"}
                or candidate.get("candidateLabel") not in {"A", "B", "当前商品"}
                or not re.fullmatch(r"[1-9][0-9]*", str(candidate.get("itemId", "")))
            ):
                raise PublicRunnerError(f"public candidate shape mismatch: {row.get('caseId')}")
        cases.append(PublicCase(
            case_id=str(row["caseId"]),
            split=str(row["split"]),
            behavior_family=str(row["behaviorFamily"]),
            turns=tuple(deepcopy(turns)),
            visible_candidates=tuple(deepcopy(visible)),
        ))

    catalog_by_id: dict[str, dict[str, Any]] = {}
    observed_counts = {
        group: {"known": 0, "conflict": 0, "unknown": 0}
        for group in CONTROLLED_GROUPS
    }
    for row in catalog_rows:
        item_id = row.get("itemId")
        attributes = row.get("attributes")
        if (
            not isinstance(item_id, str)
            or not re.fullmatch(r"[1-9][0-9]*", item_id)
            or item_id in catalog_by_id
            or row.get("datasetRevision") != DATASET_REVISION
            or row.get("schemaVersion") != "used-phone-attribute-contract-v2"
            or not isinstance(attributes, dict)
            or set(attributes) != set(CONTROLLED_GROUPS)
        ):
            raise PublicRunnerError("public catalog identity/contract mismatch")
        for group in CONTROLLED_GROUPS:
            observation = attributes[group]
            status = observation.get("status") if isinstance(observation, dict) else None
            if status not in {"known", "conflict", "unknown"}:
                raise PublicRunnerError(f"invalid public observation: {item_id}/{group}")
            observed_counts[group][status] += 1
            if observation.get("key") != group:
                raise PublicRunnerError(f"public observation key mismatch: {item_id}/{group}")
        catalog_by_id[item_id] = deepcopy(row)
    if observed_counts != EXPECTED_ATTRIBUTE_STATUS_COUNTS:
        raise PublicRunnerError("public seven-attribute status counts mismatch")
    visible_ids = {
        str(candidate["itemId"])
        for case in cases
        for candidate in case.visible_candidates
    }
    if not visible_ids.issubset(catalog_by_id):
        raise PublicRunnerError("public visible candidate is outside catalog")
    return PublicBundle(tuple(cases), catalog_by_id, cases_path, catalog_path)


def _production_scope_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted((REPO_ROOT / "agent" / "app").rglob("*.py")):
        rows.append({
            "path": path.relative_to(REPO_ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return rows


def production_scope_sha256() -> str:
    return hashlib.sha256(canonical_json_bytes(_production_scope_rows())).hexdigest()


def verify_production_scope() -> dict[str, Any]:
    rows = _production_scope_rows()
    actual = hashlib.sha256(canonical_json_bytes(rows)).hexdigest()
    if actual != PRODUCTION_CODE_SCOPE_SHA256:
        raise PublicRunnerError(f"production code scope SHA mismatch: {actual}")
    attribute_path = REPO_ROOT / "agent" / "app" / "domains" / "ecommerce" / "used_phone_attributes.py"
    if sha256_file(attribute_path) != ATTRIBUTE_CONTRACT_CODE_SHA256:
        raise PublicRunnerError("seven-attribute production contract SHA mismatch")
    return {"fileCount": len(rows), "sha256": actual, "files": rows}


def _normalized_path(value: Any) -> Path | None:
    if isinstance(value, int):
        return None
    try:
        return Path(os.path.realpath(os.fspath(value)))
    except (TypeError, ValueError, OSError):
        return None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _denied_text(value: str) -> bool:
    lowered = value.replace("\\", "/").casefold()
    return any(fragment in lowered for fragment in DENIED_MODULE_FRAGMENTS + DENIED_PATH_FRAGMENTS)


@lru_cache(maxsize=1)
def _allowed_runtime_repo_files() -> frozenset[Path]:
    return frozenset({
        Path(__file__).resolve(),
        SCHEMA_PATH.resolve(),
        (REPO_ROOT / "agent" / "scripts" / "run_used_phone_public_agent_v1.py").resolve(),
        (REPO_ROOT / "agent" / "scripts" / "__init__.py").resolve(),
        (REPO_ROOT / "agent" / "__init__.py").resolve(),
        (REPO_ROOT / "agent" / "evaluation" / "__init__.py").resolve(),
    })


# Origin strings are bounded by the interpreter's stable module set (thousands,
# not more), but a full pytest process imports far more than 2048 of them.  A
# small cache thrashes and re-runs ``os.path.realpath`` on every isolation
# check, turning each entry into seconds.  Sized to hold the whole module set.
@lru_cache(maxsize=65536)
def _normalized_module_origin(value: str) -> Path | None:
    return _normalized_path(value)


@lru_cache(maxsize=1)
def _production_runtime_root() -> Path:
    return (REPO_ROOT / "agent" / "app").resolve()


@lru_cache(maxsize=1)
def _runtime_dependency_roots() -> tuple[Path, ...]:
    values = {
        value for key, value in sysconfig.get_paths().items()
        if key in {"stdlib", "platstdlib", "purelib", "platlib"} and value
    }
    if os.name == "nt":
        # Windows CPython keeps binary extension modules (``_ssl.pyd``, ``_bz2.pyd``,
        # ...) in a sibling ``DLLs`` directory outside every ``sysconfig`` path.
        # They are interpreter runtime dependencies, not repository scripts.
        for prefix in {sys.base_prefix, sys.prefix}:
            values.add(str(Path(prefix).resolve() / "DLLs"))
    return tuple(sorted(Path(value).resolve() for value in values))


@lru_cache(maxsize=65536)
def _allowed_repo_module_path(path: Path) -> bool:
    allowed_files = _allowed_runtime_repo_files()
    production_root = _production_runtime_root()
    if path in allowed_files or _is_relative_to(path, production_root):
        return True
    if path.suffix.casefold() == ".pyc":
        try:
            source = Path(importlib.util.source_from_cache(str(path))).resolve()
        except (ValueError, OSError):
            return False
        return source in allowed_files or _is_relative_to(source, production_root)
    return False


@lru_cache(maxsize=65536)
def _runtime_dependency_origin(path: Path) -> bool:
    return any(_is_relative_to(path, root) for root in _runtime_dependency_roots())


def _module_origin_values(module: ModuleType) -> list[Any]:
    # Some third-party ``ModuleType`` subclasses (notably ``torch.ops``)
    # synthesize arbitrary attributes through ``__getattr__``.  Using getattr
    # here turns a missing ``__file__`` into the fake relative path
    # ``_ops.py`` and makes a clean site-package look like an unapproved repo
    # module when the public runner is executed after another test imported
    # torch.  Origin inspection must only trust attributes physically stored
    # on the module/spec/loader objects.
    module_dict = vars(module)
    values: list[Any] = [
        module_dict.get("__file__"),
        module_dict.get("__cached__"),
    ]
    spec = module_dict.get("__spec__")
    if spec is not None:
        values.extend((getattr(spec, "origin", None), getattr(spec, "cached", None)))
    loaders = [module_dict.get("__loader__")]
    if spec is not None:
        loaders.append(getattr(spec, "loader", None))
    for loader in loaders:
        if loader is not None:
            values.extend((getattr(loader, "path", None), getattr(loader, "archive", None)))
    return values


def _module_repo_violation(module: ModuleType) -> Path | None:
    for value in _module_origin_values(module):
        if value is None or (
            isinstance(value, str) and (
                value in {"built-in", "frozen", "namespace"}
                or (value.startswith("<") and value.endswith(">"))
            )
        ):
            continue
        try:
            origin = os.fspath(value)
        except TypeError:
            continue
        if not isinstance(origin, str):
            continue
        path = _normalized_module_origin(origin)
        if path is None:
            continue
        if _allowed_repo_module_path(path) or _runtime_dependency_origin(path):
            continue
        return path
    return None


def _assert_clean_preloaded_modules() -> None:
    for name, module in list(sys.modules.items()):
        if not isinstance(module, ModuleType):
            continue
        module_path = getattr(module, "__file__", None)
        violation = _module_repo_violation(module)
        if (
            _denied_text(name)
            or (module_path is not None and _denied_text(str(module_path)))
            or violation is not None
        ):
            raise PublicIsolationError(f"denied module was preloaded: {name}")
    package = sys.modules.get("agent.evaluation")
    if package is not None:
        for attribute, value in vars(package).items():
            module_name = getattr(value, "__name__", "") if isinstance(value, ModuleType) else ""
            module_path = getattr(value, "__file__", "") if isinstance(value, ModuleType) else ""
            violation = _module_repo_violation(value) if isinstance(value, ModuleType) else None
            if (
                _denied_text(attribute)
                or _denied_text(module_name)
                or _denied_text(str(module_path))
                or violation is not None
            ):
                raise PublicIsolationError(f"denied parent package attribute was preloaded: {attribute}")


class _DeniedImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001
        if _denied_text(fullname):
            raise PublicIsolationError(f"denied import during public run: {fullname}")
        return None


@contextmanager
def public_runtime_isolation(
    *, public_cases_path: Path, public_catalog_path: Path, run_dir: Path | None = None,
):
    """Fail closed on hidden data/module reads and writes outside the run directory.

    When ``run_dir`` is provided, a unique run-local scratch/temp directory is
    provisioned *before* the write guard is installed, and ``tempfile.tempdir``
    plus the ``TEMP``/``TMP``/``TMPDIR`` environment variables are pointed at it
    for the duration of the guarded block.  CPython discovers the default temp
    dir lazily: ``tempfile.gettempdir()`` -> ``_get_default_tempdir()`` probes
    candidate dirs by creating and unlinking a random file.  Redirecting it here
    means that probe (and every later default tempfile file) lands inside the
    run directory instead of the system temp dir, which the write guard would
    otherwise reject before the first model call.  No external path is ever
    whitelisted, so explicit writes to the system temp dir or anywhere else
    still fail closed.  All overrides are restored on normal, exception and
    nested exit, and the scratch directory is removed when this entry created it.
    """

    _assert_clean_preloaded_modules()
    cases = Path(public_cases_path).resolve()
    catalog = Path(public_catalog_path).resolve()
    allowed_data = {cases, catalog}
    output_root = Path(run_dir).resolve() if run_dir is not None else None
    allowed_repo_files = _allowed_runtime_repo_files()
    production_root = (REPO_ROOT / "agent" / "app").resolve()

    def assert_read(value: Any) -> None:
        path = _normalized_path(value)
        if path is None:
            return
        if (
            path in allowed_data
            or path in allowed_repo_files
            or _is_relative_to(path, production_root)
            or (output_root is not None and _is_relative_to(path, output_root))
            or _runtime_dependency_origin(path)
        ):
            return
        raise PublicIsolationError(f"denied public-run read: {path.name}")

    def assert_write(value: Any) -> None:
        path = _normalized_path(value)
        if path is None:
            return
        if output_root is None or not _is_relative_to(path, output_root):
            raise PublicIsolationError(f"write outside public run directory: {path}")

    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    real_source = importlib.machinery.SourceFileLoader.get_data
    real_sourceless = importlib.machinery.SourcelessFileLoader.get_data

    def mode_is_write(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> bool:
        mode = str(kwargs.get("mode", args[0] if args else "r"))
        return any(token in mode for token in ("w", "a", "x", "+"))

    def guarded_open(file, *args, **kwargs):  # noqa: ANN001
        (assert_write if mode_is_write(args, kwargs) else assert_read)(file)
        return real_open(file, *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):  # noqa: ANN001
        (assert_write if mode_is_write(args, kwargs) else assert_read)(file)
        return real_io_open(file, *args, **kwargs)

    def guarded_os_open(file, flags, *args, **kwargs):  # noqa: ANN001
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        (assert_write if flags & write_flags else assert_read)(file)
        return real_os_open(file, flags, *args, **kwargs)

    def guarded_read_text(path, *args, **kwargs):  # noqa: ANN001
        assert_read(path)
        return real_read_text(path, *args, **kwargs)

    def guarded_read_bytes(path, *args, **kwargs):  # noqa: ANN001
        assert_read(path)
        return real_read_bytes(path, *args, **kwargs)

    def guarded_source(loader, path):  # noqa: ANN001
        assert_read(path)
        return real_source(loader, path)

    def guarded_sourceless(loader, path):  # noqa: ANN001
        assert_read(path)
        return real_sourceless(loader, path)

    finder = _DeniedImportFinder()
    scratch_dir: Path | None = None
    owns_scratch = False
    sys.meta_path.insert(0, finder)
    try:
        if output_root is not None:
            scratch_dir = output_root / RUN_LOCAL_TEMP_DIR_NAME
            try:
                scratch_dir.mkdir(parents=True, exist_ok=False)
                owns_scratch = True
            except FileExistsError:
                # Nested isolation or a leftover from an interrupted run reuses
                # the existing scratch; only the entry that created it removes it.
                owns_scratch = False
        with ExitStack() as stack:
            if scratch_dir is not None:
                stack.enter_context(patch.object(tempfile, "tempdir", str(scratch_dir)))
                stack.enter_context(patch.dict(os.environ, {
                    "TMPDIR": str(scratch_dir),
                    "TMP": str(scratch_dir),
                    "TEMP": str(scratch_dir),
                }))
            stack.enter_context(patch.object(builtins, "open", guarded_open))
            stack.enter_context(patch.object(io, "open", guarded_io_open))
            stack.enter_context(patch.object(os, "open", guarded_os_open))
            stack.enter_context(patch.object(Path, "read_text", guarded_read_text))
            stack.enter_context(patch.object(Path, "read_bytes", guarded_read_bytes))
            stack.enter_context(patch.object(importlib.machinery.SourceFileLoader, "get_data", guarded_source))
            stack.enter_context(patch.object(importlib.machinery.SourcelessFileLoader, "get_data", guarded_sourceless))
            yield
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
        if owns_scratch and scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)


class InMemoryRedis:
    """Minimal async Redis used by production TaskState without network writes."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.sorted_sets: dict[str, dict[str, int]] = {}
        self.sets: dict[str, set[str]] = {}

    @staticmethod
    def _slice(values: list[str], start: int, end: int) -> list[str]:
        length = len(values)
        start = max(length + start, 0) if start < 0 else start
        end = length + end if end < 0 else end
        if start >= length or start > end:
            return []
        return values[start : end + 1]

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.strings.pop(key, None)

    async def set(self, key: str, value: str, **_kwargs) -> bool:
        self.strings[key] = value
        return True

    async def eval(self, _script: str, numkeys: int, *args):
        if numkeys != 1 or len(args) != 4:
            raise NotImplementedError("runner Redis only supports TaskState CAS")
        key, expected_raw, payload, _ttl = args
        raw = self.strings.get(key)
        if raw is None:
            return [-1, -1]
        actual = int(json.loads(raw)["revision"])
        expected = int(expected_raw)
        if actual != expected:
            return [0, actual]
        self.strings[key] = payload
        return [1, expected + 1]

    async def expire(self, key: str, _seconds: int) -> bool:
        return any(key in store for store in (self.strings, self.lists, self.sorted_sets, self.sets))

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            for store in (self.strings, self.lists, self.sorted_sets, self.sets):
                deleted += int(store.pop(key, None) is not None)
        return deleted

    async def rpush(self, key: str, value: str) -> int:
        values = self.lists.setdefault(key, [])
        values.append(value)
        return len(values)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self._slice(self.lists.get(key, []), start, end)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.lists[key] = self._slice(self.lists.get(key, []), start, end)

    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        values = self.sorted_sets.setdefault(key, {})
        before = len(values)
        values.update(mapping)
        return len(values) - before

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        ordered = [member for member, _ in sorted(self.sorted_sets.get(key, {}).items(), key=lambda item: item[1])]
        return self._slice(ordered, start, end)

    async def zrem(self, key: str, *members: str) -> int:
        values = self.sorted_sets.get(key, {})
        return sum(values.pop(member, None) is not None for member in members)

    async def sadd(self, key: str, *members: str) -> int:
        values = self.sets.setdefault(key, set())
        before = len(values)
        values.update(members)
        return len(values) - before

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def srem(self, key: str, *members: str) -> int:
        values = self.sets.get(key, set())
        before = len(values)
        values.difference_update(members)
        return before - len(values)


class InMemoryTraceStore:
    def __init__(self) -> None:
        self.traces: dict[str, Any] = {}

    async def save(self, trace) -> bool:  # noqa: ANN001
        self.traces[str(trace.run_id)] = trace
        return True

    async def get(self, run_id: str):
        return self.traces.get(run_id)

    async def delete(self, run_id: str) -> None:
        self.traces.pop(run_id, None)


@contextmanager
def in_memory_production_stores():
    import agent.app.agent_trace as trace_module
    import agent.app.task_state as task_state_module

    prior_task_client = task_state_module._client
    prior_trace_store = trace_module._trace_store
    task_state_module._client = InMemoryRedis()
    trace_module._trace_store = InMemoryTraceStore()
    task_state_module._task_locks.clear()
    task_state_module._session_locks.clear()
    try:
        yield
    finally:
        task_state_module._client = prior_task_client
        trace_module._trace_store = prior_trace_store
        task_state_module._task_locks.clear()
        task_state_module._session_locks.clear()


class RecordingCompletions:
    def __init__(self, create, ledger: list[dict[str, Any]]) -> None:  # noqa: ANN001
        self._create = create
        self._ledger = ledger

    async def create(self, **kwargs):
        if kwargs.get("tools"):
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body["thinking"] = {"type": "disabled"}
            kwargs["extra_body"] = extra_body
        record = {
            "index": len(self._ledger),
            "outcome": "attempted",
            "model": kwargs.get("model"),
            "messageSha256": hashlib.sha256(canonical_json_bytes(kwargs.get("messages", []))).hexdigest(),
            "toolNames": [item.get("function", {}).get("name") for item in kwargs.get("tools", [])],
            "stream": bool(kwargs.get("stream")),
        }
        self._ledger.append(record)
        started = time.perf_counter()
        try:
            response = await self._create(**kwargs)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                outcome = "cancelled"
            elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                outcome = "timeout"
            else:
                outcome = "error"
            record.update({
                "ok": False,
                "outcome": outcome,
                "errorType": type(exc).__name__,
                "durationMs": round((time.perf_counter() - started) * 1000, 2),
            })
            raise
        message = response.choices[0].message if getattr(response, "choices", None) else None
        record.update({
            "ok": True,
            "outcome": "success",
            "durationMs": round((time.perf_counter() - started) * 1000, 2),
            "response": {
                "contentSha256": hashlib.sha256(str(getattr(message, "content", "")).encode()).hexdigest(),
                "toolCalls": [
                    {"name": call.function.name, "arguments": call.function.arguments}
                    for call in (getattr(message, "tool_calls", None) or [])
                ],
            },
        })
        return response


class RecordingClient:
    def __init__(self, raw_client: Any) -> None:
        self.raw_client = raw_client
        self.ledger: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=RecordingCompletions(
            raw_client.chat.completions.create, self.ledger,
        ))

    def fork(self) -> "RecordingClient":
        return RecordingClient(self.raw_client)


def make_real_client() -> RecordingClient:
    from openai import AsyncOpenAI
    from agent.app.settings import settings

    endpoint = urlparse(settings.deepseek_base_url)
    if (
        endpoint.scheme.casefold() != "https"
        or (endpoint.hostname or "").casefold() != "api.deepseek.com"
        or endpoint.port not in {None, 443}
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise PublicRunnerError("DeepSeek endpoint pin mismatch")
    if not settings.deepseek_api_key:
        raise PublicRunnerError("DEEPSEEK_API_KEY is not configured")
    return RecordingClient(AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=30.0,
        max_retries=0,
    ))


@contextmanager
def production_network_guard(
    network_ledger: list[dict[str, Any]],
    tool_invocation_ledger: list[dict[str, Any]] | None = None,
):
    """Allow DeepSeek plus read-semantic requests to the pinned Java catalog only."""

    import httpx
    import agent.app.tools as tools_module
    import agent.app.domains.ecommerce.tools as ecommerce_tools
    import agent.app.llm as llm_module
    from agent.app.settings import settings

    real_call_tool = tools_module.call_tool
    real_send = httpx.AsyncClient.send

    async def guarded_call_tool(name: str, arguments: dict[str, Any]):
        if name not in ALLOWED_PRODUCT_TOOLS:
            raise PublicIsolationError(f"non-read product tool denied: {name}")
        entry = {
            "index": len(tool_invocation_ledger or []),
            "tool": name,
            "arguments": deepcopy(arguments),
            "argumentsSha256": hashlib.sha256(canonical_json_bytes(arguments)).hexdigest(),
            "outcome": "attempted",
        }
        if tool_invocation_ledger is not None:
            tool_invocation_ledger.append(entry)
        try:
            result = await real_call_tool(name, arguments)
        except BaseException as exc:
            entry.update({"outcome": "error", "errorType": type(exc).__name__})
            raise
        serialized = _trace_dict(result)
        entry.update({
            "outcome": "success" if serialized.get("ok") is True else "toolFailure",
            "resultSha256": hashlib.sha256(canonical_json_bytes(serialized)).hexdigest(),
        })
        return result

    async def guarded_send(client, request, *args, **kwargs):  # noqa: ANN001
        url = request.url
        origin = (url.scheme.casefold(), (url.host or "").casefold(), url.port)
        path = url.path
        method = request.method.upper()
        java_allowed = origin == ("http", "127.0.0.1", 18081) and (
            (method == "GET" and path in {"/api/products", "/api/products/retrieval"})
            or (method == "POST" and path == "/api/products/resolve")
        )
        model_allowed = origin in {
            ("https", "api.deepseek.com", None),
            ("https", "api.deepseek.com", 443),
        } and method == "POST" and path.rstrip("/").endswith("/chat/completions")
        if not java_allowed and not model_allowed:
            raise PublicIsolationError(f"network target denied: {method} {url}")
        network_ledger.append({
            "kind": "javaProductRead" if java_allowed else "model",
            "method": method,
            "origin": f"{url.scheme}://{url.host}:{url.port or (443 if url.scheme == 'https' else 80)}",
            "path": path,
            "businessWrite": False,
        })
        return await real_send(client, request, *args, **kwargs)

    def qdrant_disabled(*_args, **_kwargs):
        raise PublicIsolationError("Qdrant is outside the pinned Java read boundary")

    with (
        patch.object(settings, "backend_base_url", JAVA_BASE_URL),
        patch.object(settings, "ecommerce_guide_enabled", True),
        patch.object(settings, "agent_transaction_enabled", False),
        patch.object(settings, "product_retrieval_mode", "bm25"),
        patch.object(settings, "agent_context_mode", "context_pack"),
        patch.object(settings, "agent_orchestrator_mode", "unified"),
        patch.object(settings, "agent_legacy_fallback_enabled", False),
        patch.object(settings, "agent_request_deadline_seconds", 20.0),
        patch.object(settings, "agent_tool_transport_mode", "live"),
        patch.object(settings, "evidence_critic_enabled", False),
        patch.object(tools_module, "call_tool", guarded_call_tool),
        patch.object(llm_module, "call_tool", guarded_call_tool),
        patch.object(httpx.AsyncClient, "send", guarded_send),
        patch.object(ecommerce_tools, "_product_query_embedding", qdrant_disabled),
    ):
        yield


JAVA_PRODUCT_FIELDS = (
    "attributeText", "brand", "categoryL1", "categoryL2", "categoryL3",
    "currency", "dataNature", "datasetRevision", "id", "priceStatus",
    "provenanceUrl", "seller", "snapshotPriceMinor", "source", "sourceItemId",
    "sourceLicense", "title",
)
JAVA_ATTRIBUTE_FIELDS = (
    "key", "confidence", "evidenceField", "extractionMethod", "normalizedBoolean",
    "normalizedNumber", "normalizedText", "rawValue", "unit", "valueType",
)


def _json_get(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def _json_post(url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def verify_java_public_catalog(bundle: PublicBundle, base_url: str = JAVA_BASE_URL) -> dict[str, Any]:
    if base_url.rstrip("/") != JAVA_BASE_URL:
        raise PublicRunnerError("Java API endpoint pin mismatch")
    manifest_response = _json_get(f"{JAVA_BASE_URL}/internal/catalog/manifest")
    manifest = manifest_response.get("data") if manifest_response.get("success") else None
    if not isinstance(manifest, dict) or {
        "catalogVersion": manifest.get("catalogVersion"),
        "productCount": manifest.get("productCount"),
        "contentHash": manifest.get("contentHash"),
    } != {
        "catalogVersion": JAVA_CATALOG_VERSION,
        "productCount": PUBLIC_CATALOG_COUNT,
        "contentHash": JAVA_CATALOG_CONTENT_SHA256,
    }:
        raise PublicRunnerError("Java catalog manifest mismatch")
    ids: list[int] = []
    after_id: int | None = None
    while True:
        query: dict[str, Any] = {"catalogVersion": JAVA_CATALOG_VERSION, "limit": 200}
        if after_id is not None:
            query["afterId"] = after_id
        response = _json_get(f"{JAVA_BASE_URL}/internal/catalog/products?{urlencode(query)}")
        page = response.get("data") if response.get("success") else None
        if not isinstance(page, dict):
            raise PublicRunnerError("Java catalog pagination failed")
        items = page.get("items")
        if not isinstance(items, list) or not all(type(item) is int and item > 0 for item in items):
            raise PublicRunnerError("Java catalog page identity mismatch")
        ids.extend(items)
        if page.get("complete"):
            break
        after_id = page.get("nextAfterId")
        if type(after_id) is not int or after_id <= 0:
            raise PublicRunnerError("Java catalog pagination stalled")
    if [str(item) for item in ids] != sorted(bundle.catalog_by_id, key=int):
        raise PublicRunnerError("Java/public catalog item identity mismatch")
    products: list[dict[str, Any]] = []
    for index in range(0, len(ids), 10):
        batch = ids[index:index + 10]
        response = _json_post(f"{JAVA_BASE_URL}/api/products/resolve", {"productIds": batch})
        rows = response.get("data") if response.get("success") else None
        if not isinstance(rows, list) or len(rows) != len(batch):
            raise PublicRunnerError("Java resolve batch mismatch")
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int:
                raise PublicRunnerError("Java product shape mismatch")
            normalized = {key: row.get(key) for key in JAVA_PRODUCT_FIELDS}
            attributes = row.get("attributes")
            if not isinstance(attributes, list):
                raise PublicRunnerError("Java attribute collection mismatch")
            normalized["attributes"] = [
                {key: attribute.get(key) for key in JAVA_ATTRIBUTE_FIELDS}
                for attribute in attributes
                if isinstance(attribute, dict)
            ]
            products.append(normalized)
    products.sort(key=lambda row: row["id"])
    projection_sha = hashlib.sha256(json.dumps(
        products, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    attribute_count = sum(len(row["attributes"]) for row in products)
    if projection_sha != JAVA_PROJECTION_SHA256 or attribute_count != JAVA_ATTRIBUTE_COUNT:
        raise PublicRunnerError("Java product/attribute projection identity mismatch")
    return {
        "attributeCount": attribute_count,
        "catalogVersion": JAVA_CATALOG_VERSION,
        "contentSha256": JAVA_CATALOG_CONTENT_SHA256,
        "javaProjectionSha256": projection_sha,
        "productCount": len(products),
        "resolveBatchCount": (len(products) + 9) // 10,
        "networkCallCount": 29,
        "businessWriteNetworkUsed": False,
    }


def audit_public_inputs(
    *, public_cases_path: Path, public_catalog_path: Path, verify_java: bool = True,
) -> dict[str, Any]:
    _assert_clean_preloaded_modules()
    verify_production_scope()
    with public_runtime_isolation(
        public_cases_path=public_cases_path,
        public_catalog_path=public_catalog_path,
    ):
        bundle = load_public_bundle(public_cases_path, public_catalog_path)
    result = {
        "schemaVersion": "used-phone-public-agent-audit-v1",
        "caseCount": len(bundle.cases),
        "catalogCount": len(bundle.catalog_by_id),
        "datasetRevision": DATASET_REVISION,
        "publicCasesSha256": PUBLIC_CASES_SHA256,
        "publicCatalogSha256": PUBLIC_CATALOG_SHA256,
        "productionCodeScopeSha256": PRODUCTION_CODE_SCOPE_SHA256,
        "sevenAttributeContract": {
            "codeSha256": ATTRIBUTE_CONTRACT_CODE_SHA256,
            "groups": list(CONTROLLED_GROUPS),
            "ruleset": ATTRIBUTE_RULESET_VERSION,
        },
        "java": None,
        "hiddenArtifactsRead": False,
        "modelCalls": 0,
    }
    if verify_java:
        result["java"] = verify_java_public_catalog(bundle)
    return result


def _trace_dict(trace: Any) -> dict[str, Any]:
    if hasattr(trace, "model_dump"):
        return trace.model_dump(by_alias=True, mode="json")
    if isinstance(trace, dict):
        return deepcopy(trace)
    raise PublicRunnerError("production tool trace is not serializable")


def _latest_guide_state(snapshots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for snapshot in reversed(snapshots):
        state = snapshot.get("state")
        domain = state.get("domainState") if isinstance(state, Mapping) else None
        guide = domain.get("shoppingGuide") if isinstance(domain, Mapping) else None
        if isinstance(guide, Mapping):
            return deepcopy(dict(guide))
    return {}


def _task_state_extraction_decisions(
    snapshots: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Expose server-owned parser routing receipts without copying user values."""

    required_keys = {
        "schemaVersion",
        "route",
        "reason",
        "mentionedKeys",
        "coveredKeys",
        "uncoveredKeys",
    }
    allowed_reasons = {
        "deterministic_complete": {
            "complete_controlled_coverage",
            "bound_comparison",
            "bound_substitution",
        },
        "model_fallback": {
            "partial_controlled_coverage",
            "no_deterministic_signal",
            "retained_value_missing",
            "outside_deterministic_phone_contract",
        },
    }
    decisions: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for snapshot in snapshots:
        state = snapshot.get("state")
        domain = state.get("domainState") if isinstance(state, Mapping) else None
        receipt = domain.get("taskStateExtraction") if isinstance(domain, Mapping) else None
        if receipt is None:
            continue
        if not isinstance(receipt, Mapping) or set(receipt) != required_keys:
            raise PublicRunnerError("task-state extraction receipt shape is invalid")
        if receipt.get("schemaVersion") != "used-phone-task-state-extraction-decision-v1":
            raise PublicRunnerError("task-state extraction receipt schema is invalid")
        route = receipt.get("route")
        reason = receipt.get("reason")
        if route not in allowed_reasons or reason not in allowed_reasons[route]:
            raise PublicRunnerError("task-state extraction receipt route is invalid")

        key_sets: dict[str, list[str]] = {}
        for field in ("mentionedKeys", "coveredKeys", "uncoveredKeys"):
            values = receipt.get(field)
            if (
                not isinstance(values, list)
                or not all(isinstance(value, str) and value in CONTROLLED_GROUPS for value in values)
                or values != sorted(set(values))
            ):
                raise PublicRunnerError(f"task-state extraction receipt {field} is invalid")
            key_sets[field] = values
        if not set(key_sets["coveredKeys"]).issubset(key_sets["mentionedKeys"]):
            raise PublicRunnerError("task-state extraction covered keys are not mentioned")
        if set(key_sets["uncoveredKeys"]) != (
            set(key_sets["mentionedKeys"]) - set(key_sets["coveredKeys"])
        ):
            raise PublicRunnerError("task-state extraction uncovered keys are inconsistent")

        revision = state.get("revision")
        phase = snapshot.get("phase")
        if not isinstance(revision, int) or revision < 0 or not isinstance(phase, str) or not phase:
            raise PublicRunnerError("task-state extraction snapshot identity is invalid")
        normalized = deepcopy(dict(receipt))
        identity = (revision, json.dumps(normalized, ensure_ascii=False, sort_keys=True))
        if identity in seen:
            continue
        seen.add(identity)
        decisions.append({
            "phase": phase,
            "taskRevision": revision,
            "decision": normalized,
        })
    return decisions


def _constraint_groups(guide: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"hard": [], "soft": []}
    seen: set[tuple[Any, ...]] = set()
    for requirement in guide.get("requirements", []):
        if not isinstance(requirement, Mapping):
            continue
        group = requirement.get("key")
        operator = requirement.get("operator")
        importance = requirement.get("priority")
        value = requirement.get("value")
        if group not in CONTROLLED_GROUPS or importance not in {"hard", "soft"}:
            continue
        if operator == "eq" and isinstance(value, str):
            allowed, public_operator = [value], "IN"
        elif operator in {"in", "not_in"} and isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            allowed, public_operator = list(dict.fromkeys(value)), "IN" if operator == "in" else "NOT_IN"
        else:
            continue
        identity = (group, public_operator, importance, tuple(allowed))
        if identity in seen:
            continue
        seen.add(identity)
        result[importance].append({
            "allowedValues": allowed,
            "group": group,
            "importance": importance,
            "operator": public_operator,
        })
    # Preserve TaskState's semantic update order.  For a multi-turn update this
    # is the public contract order (replacement, addition, retained preference),
    # not an alphabetic presentation order.
    return result


def _successful_product_traces(tool_traces: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        trace for trace in tool_traces
        if trace.get("tool") in ALLOWED_PRODUCT_TOOLS
        and trace.get("ok") is True
        and isinstance(trace.get("detail"), Mapping)
    ]


def _last_successful_trace(
    tool_traces: Sequence[Mapping[str, Any]], tool_name: str,
    final_turn_index: int | None = None,
) -> Mapping[str, Any] | None:
    return next(
        (
            trace for trace in reversed(_successful_product_traces(tool_traces))
            if trace.get("tool") == tool_name
            and (
                final_turn_index is None
                or trace.get("publicTurnIndex") == final_turn_index
            )
        ),
        None,
    )


def _actual_ranked_ids(
    tool_traces: Sequence[Mapping[str, Any]], final_turn_index: int | None = None,
) -> list[str]:
    trace = _last_successful_trace(tool_traces, "search_products", final_turn_index)
    if trace is not None:
        detail = trace["detail"]
        ids = detail.get("candidateIds")
        if isinstance(ids, list):
            ranked: list[str] = []
            for item in ids:
                if type(item) is int and item > 0 and str(item) not in ranked:
                    ranked.append(str(item))
            return ranked
    return []


def _actual_compare_ids(
    tool_traces: Sequence[Mapping[str, Any]], final_turn_index: int | None = None,
) -> set[str]:
    result: set[str] = set()
    trace = _last_successful_trace(tool_traces, "compare_products", final_turn_index)
    if trace is not None:
        products = trace["detail"].get("products")
        if isinstance(products, list):
            for product in products:
                if not isinstance(product, Mapping):
                    continue
                snapshot = product.get("product") if isinstance(product.get("product"), Mapping) else product
                if type(snapshot.get("id")) is int:
                    result.add(str(snapshot["id"]))
    return result


def _trace_product_rows(trace: Mapping[str, Any]) -> dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]:
    detail = trace.get("detail")
    if not isinstance(detail, Mapping):
        return {}
    key = "candidates" if trace.get("tool") == "search_products" else "products"
    rows = detail.get(key)
    if not isinstance(rows, list):
        return {}
    result: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for wrapper in rows:
        if not isinstance(wrapper, Mapping):
            continue
        snapshot = wrapper.get("product") if isinstance(wrapper.get("product"), Mapping) else wrapper
        product_id = snapshot.get("id")
        if type(product_id) is int and product_id > 0 and str(product_id) not in result:
            result[str(product_id)] = (wrapper, snapshot)
    return result


def _citation_options_from_trace(
    case: PublicCase,
    catalog: Mapping[str, Mapping[str, Any]],
    trace: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if trace is None:
        return {}
    from agent.app.domains.ecommerce.used_phone_attributes import (
        USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
        USED_PHONE_ATTRIBUTE_RULESET_VERSION,
        observe_used_phone_attributes,
    )

    detail = trace.get("detail")
    evidence_rows = detail.get("evidence") if isinstance(detail, Mapping) else None
    if not isinstance(evidence_rows, list):
        return {}
    products = _trace_product_rows(trace)
    options: dict[str, dict[str, Any]] = {}
    reference_pattern = re.compile(r"product:([1-9][0-9]*):attribute:(%s)" % "|".join(CONTROLLED_GROUPS))
    for evidence in evidence_rows:
        if not isinstance(evidence, Mapping):
            continue
        ref_value = evidence.get("ref")
        match = reference_pattern.fullmatch(ref_value) if isinstance(ref_value, str) else None
        if match is None:
            continue
        item_id, group = match.groups()
        raw_value = evidence.get("rawValue")
        if (
            evidence.get("field") != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
            or evidence.get("method") != USED_PHONE_ATTRIBUTE_RULESET_VERSION
            or not isinstance(raw_value, str)
            or item_id not in products
        ):
            continue
        wrapper, snapshot = products[item_id]
        wrapper_refs = wrapper.get("evidenceRefs")
        if not isinstance(wrapper_refs, list) or ref_value not in wrapper_refs:
            continue
        attributes = snapshot.get("attributes")
        matching_attributes = [
            attribute for attribute in attributes
            if isinstance(attribute, Mapping) and attribute.get("key") == group
        ] if isinstance(attributes, list) else []
        if len(matching_attributes) != 1:
            continue
        attribute = matching_attributes[0]
        public_product = catalog.get(item_id)
        public_observation = (
            public_product.get("attributes", {}).get(group)
            if isinstance(public_product, Mapping)
            else None
        )
        public_value = public_observation.get("value") if isinstance(public_observation, Mapping) else None
        if (
            not isinstance(public_observation, Mapping)
            or public_observation.get("status") != "known"
            or not isinstance(public_value, str)
            or attribute.get("rawValue") != raw_value
            or attribute.get("normalizedText") != public_value
            or attribute.get("normalizedNumber") is not None
            or attribute.get("normalizedBoolean") is not None
            or attribute.get("evidenceField") != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
            or attribute.get("extractionMethod") != USED_PHONE_ATTRIBUTE_RULESET_VERSION
        ):
            continue
        observed = observe_used_phone_attributes(raw_value).get(group)
        if (
            observed is None
            or observed.status != "known"
            or observed.fact is None
            or observed.fact.value != public_value
        ):
            continue
        matching_checks = [
            check for check in wrapper.get("checks", [])
            if isinstance(check, Mapping) and check.get("evidenceRef") == ref_value
        ] if isinstance(wrapper.get("checks"), list) else []
        if trace.get("tool") != "compare_products":
            if (
                len(matching_checks) != 1
                or matching_checks[0].get("key") != group
                or matching_checks[0].get("actual") != public_value
                or matching_checks[0].get("status") != "pass"
            ):
                continue
        public_refs = public_observation.get("evidenceRefs")
        canonical_refs = [
            public_ref for public_ref in public_refs
            if isinstance(public_ref, Mapping)
            and public_ref.get("source") == "relevance"
            and public_ref.get("field") == "attr_value"
            and public_ref.get("rawValue") == raw_value
            and type(public_ref.get("lineNumber")) is int
            and public_ref["lineNumber"] >= 1
        ] if isinstance(public_refs, list) else []
        if len(canonical_refs) != 1:
            continue
        citation = {
            "itemId": item_id,
            "group": group,
            "source": "relevance",
            "field": "attr_value",
            "lineNumber": canonical_refs[0]["lineNumber"],
            "rawValue": raw_value,
        }
        ref_id = "pubref-" + hashlib.sha256(canonical_json_bytes({
            "caseId": case.case_id, "citation": citation,
        })).hexdigest()[:20]
        options[ref_id] = citation
    return dict(sorted(options.items()))


def _citation_options(
    case: PublicCase,
    catalog: Mapping[str, Mapping[str, Any]],
    tool_traces: Sequence[Mapping[str, Any]],
    *,
    tool_name: str | None = None,
    final_turn_index: int | None = None,
) -> dict[str, dict[str, Any]]:
    names = (tool_name,) if tool_name is not None else ("search_products", "compare_products")
    options: dict[str, dict[str, Any]] = {}
    for name in names:
        options.update(_citation_options_from_trace(
            case, catalog, _last_successful_trace(tool_traces, name, final_turn_index),
        ))
    return dict(sorted(options.items()))


def _matching_tool_invocation(
    trace: Mapping[str, Any] | None,
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    if trace is None or tool_invocations is None:
        return None
    trace_payload = {key: value for key, value in trace.items() if key != "publicTurnIndex"}
    trace_sha = hashlib.sha256(canonical_json_bytes(trace_payload)).hexdigest()
    return next(
        (
            invocation for invocation in reversed(tool_invocations)
            if invocation.get("tool") == trace.get("tool")
            and invocation.get("outcome") == "success"
            and invocation.get("resultSha256") == trace_sha
            and invocation.get("publicTurnIndex") == trace.get("publicTurnIndex")
        ),
        None,
    )


def _projection_tool_schema(citation_ids: Sequence[str]) -> dict[str, Any]:
    citation_array: dict[str, Any] = {
        "type": "array",
        "uniqueItems": True,
        "items": (
            {"type": "string", "enum": list(citation_ids)}
            if citation_ids else {"type": "string"}
        ),
    }
    if not citation_ids:
        # An unavailable citation is absence, not a synthetic evidence ID.
        citation_array["maxItems"] = 0
    return {
        "type": "function",
        "function": {
            "name": "submit_used_phone_public_projection",
            "description": "Select the observed public action and only citations actually used by the answer.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["predictedAction", "citationRefIds"],
                "properties": {
                    "predictedAction": {"type": "string", "enum": list(FORMAL_ACTIONS)},
                    "citationRefIds": citation_array,
                },
            },
        },
    }


def materialize_prediction(
    *, case: PublicCase, catalog: Mapping[str, Mapping[str, Any]],
    tool_traces: Sequence[Mapping[str, Any]], snapshots: Sequence[Mapping[str, Any]],
    selection: Mapping[str, Any],
    tool_invocations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if set(selection) != {"predictedAction", "citationRefIds"}:
        raise PublicRunnerError("projection selection fields mismatch")
    action = selection.get("predictedAction")
    citation_ids = selection.get("citationRefIds")
    if action not in FORMAL_ACTIONS or not isinstance(citation_ids, list) or len(citation_ids) != len(set(citation_ids)):
        raise PublicRunnerError("projection selection value mismatch")
    final_turn_index = len(case.turns) - 1
    actual_ranked = _actual_ranked_ids(tool_traces, final_turn_index)
    if not actual_ranked and action in RETRIEVAL_ACTIONS:
        raise PublicRunnerError("projection cannot claim retrieval without a successful tool result")
    if any(item_id not in catalog for item_id in actual_ranked):
        raise PublicRunnerError("successful retrieval returned an item outside the pinned public catalog")
    ranked = actual_ranked if action in RETRIEVAL_ACTIONS else []
    guide = _latest_guide_state(snapshots)
    constraints = _constraint_groups(guide)
    decision_tool = (
        "compare_products" if action == "COMPARE_WITH_FIELD_EVIDENCE"
        else "search_products" if action in RETRIEVAL_ACTIONS
        else None
    )
    options = (
        _citation_options(
            case, catalog, tool_traces,
            tool_name=decision_tool, final_turn_index=final_turn_index,
        )
        if decision_tool is not None else {}
    )
    decision_trace = (
        _last_successful_trace(tool_traces, decision_tool, final_turn_index)
        if decision_tool is not None else None
    )
    invocation = _matching_tool_invocation(decision_trace, tool_invocations)
    if tool_invocations is not None and decision_tool is not None and invocation is None:
        raise PublicRunnerError("final decision trace is not bound to its production tool invocation")
    if any(ref_id not in options for ref_id in citation_ids):
        raise PublicRunnerError("projection selected a cross-case or unsupported citation")
    prediction: dict[str, Any] = {
        "caseId": case.case_id,
        "rankedItemIds": ranked,
        "predictedAction": action,
        "predictedConstraints": constraints,
        "evidenceCitations": [options[ref_id] for ref_id in citation_ids],
    }
    if len(case.turns) > 1:
        prediction["predictedState"] = constraints
    if action == "COMPARE_WITH_FIELD_EVIDENCE":
        visible_pair = [str(candidate["itemId"]) for candidate in case.visible_candidates]
        compared = _actual_compare_ids(tool_traces, final_turn_index)
        if len(visible_pair) != 2 or not set(visible_pair).issubset(compared):
            raise PublicRunnerError("comparison lacks current-case successful compare evidence")
        if invocation is not None:
            requested_ids = invocation.get("arguments", {}).get("productIds")
            if (
                not isinstance(requested_ids, list)
                or not all(type(item) is int and item > 0 for item in requested_ids)
                or not set(visible_pair).issubset({str(item) for item in requested_ids})
                or not compared.issubset({str(item) for item in requested_ids})
            ):
                raise PublicRunnerError("comparison trace is not bound to one matching productIds request")
        prediction["comparison"] = {"candidateItemIds": visible_pair}
    Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))).validate(prediction)
    return prediction


def _observed_projection_action(
    case: PublicCase,
    tool_traces: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    selected_action: str,
) -> str:
    """Resolve action from public intent plus bound production tool facts.

    This is label blind: it does not read ``behavior_family``.  A projection
    model may phrase the completed turn differently, but it must not erase a
    successful tool execution or claim one that did not occur.
    """
    final_turn_index = len(case.turns) - 1
    compared = _actual_compare_ids(tool_traces, final_turn_index)
    visible_pair = {str(candidate["itemId"]) for candidate in case.visible_candidates}
    if len(visible_pair) == 2 and visible_pair.issubset(compared):
        return "COMPARE_WITH_FIELD_EVIDENCE"

    ranked = _actual_ranked_ids(tool_traces, final_turn_index)
    if ranked:
        text = str(case.turns[-1].get("text", ""))
        guide = _latest_guide_state(snapshots)
        requirements = guide.get("requirements", []) if isinstance(guide, Mapping) else []
        search_trace = next((
            row for row in reversed(tool_traces)
            if row.get("tool") == "search_products"
            and row.get("ok") is True
            and row.get("publicTurnIndex") == final_turn_index
        ), None)
        candidates = (
            search_trace.get("detail", {}).get("candidates", [])
            if isinstance(search_trace, Mapping) else []
        )
        has_complete_match = any(
            isinstance(row, Mapping) and row.get("selectionType") == "full_match"
            for row in candidates
        )
        support_labeled = bool(candidates) and all(
            isinstance(row, Mapping)
            and row.get("selectionType") in {"full_match", "closest_alternative"}
            for row in candidates
        )
        if requirements and support_labeled and not has_complete_match:
            return "ABSTAIN_OR_EXPLAIN"
        if len(case.turns) > 1:
            return "UPDATE_STATE_THEN_RETRIEVE"
        # One public visible candidate is the production UI's reference-item
        # context.  A successful search in that shape is observably a
        # substitute retrieval, independent of model wording.
        if len(case.visible_candidates) == 1:
            return "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS"
        if "如果" in text and ("无解" in text or "不要硬推" in text):
            return "ABSTAIN_OR_EXPLAIN"
        if not requirements and not case.visible_candidates:
            return "CLARIFY"
        if "替代" in text or "不降级" in text:
            return "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS"
        if "取舍" in text or "不能兼得" in text:
            return "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF"
        if "信息不明确" in text or "不明确" in text or "未知" in text:
            return "RETRIEVE_FILTER_AND_RANK_WITH_UNKNOWNS"
        return "RETRIEVE_FILTER_AND_RANK"

    latest_state = snapshots[-1].get("state", {}) if snapshots else {}
    if isinstance(latest_state, Mapping) and latest_state.get("status") == "collecting_information":
        return "CLARIFY"
    return selected_action


def _normalize_projection_selection(
    *,
    case: PublicCase,
    catalog: Mapping[str, Mapping[str, Any]],
    tool_traces: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    action = _observed_projection_action(
        case, tool_traces, snapshots, str(selection.get("predictedAction", "")),
    )
    raw_ids = selection.get("citationRefIds")
    citation_ids = list(raw_ids) if isinstance(raw_ids, list) else []
    decision_tool = (
        "compare_products" if action == "COMPARE_WITH_FIELD_EVIDENCE"
        else "search_products" if action in RETRIEVAL_ACTIONS
        else None
    )
    if decision_tool is not None and not citation_ids:
        citation_ids = list(_citation_options(
            case,
            catalog,
            tool_traces,
            tool_name=decision_tool,
            final_turn_index=len(case.turns) - 1,
        ))
    return {"predictedAction": action, "citationRefIds": citation_ids}


def _deterministic_projection_selection(
    *,
    case: PublicCase,
    catalog: Mapping[str, Mapping[str, Any]],
    tool_traces: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project only observable public facts, without a second model judgment.

    The adapter is intentionally label-blind: action comes from terminal state,
    successful bound tool calls and explicit user wording already covered by
    the public protocol.  Citations are a minimal deterministic witness set—at
    most one bound passing ref per final constraint group—instead of treating
    every available evidence option as a claim made by the answer.
    """

    text = str(case.turns[-1].get("text", ""))
    fallback = (
        "ABSTAIN_OR_EXPLAIN"
        if "如果" in text and ("无解" in text or "不要硬推" in text)
        else "CLARIFY"
    )
    action = _observed_projection_action(
        case, tool_traces, snapshots, fallback
    )
    decision_tool = (
        "compare_products" if action == "COMPARE_WITH_FIELD_EVIDENCE"
        else "search_products" if action in RETRIEVAL_ACTIONS
        else None
    )
    if decision_tool is None:
        return {"predictedAction": action, "citationRefIds": []}

    final_turn_index = len(case.turns) - 1
    options = _citation_options(
        case, catalog, tool_traces,
        tool_name=decision_tool, final_turn_index=final_turn_index,
    )
    if action == "COMPARE_WITH_FIELD_EVIDENCE":
        return {"predictedAction": action, "citationRefIds": list(options)}
    constraints = _constraint_groups(_latest_guide_state(snapshots))
    groups = [
        row["group"]
        for importance in ("hard", "soft")
        for row in constraints[importance]
    ]
    ranked = _actual_ranked_ids(tool_traces, final_turn_index)
    rank = {item_id: index for index, item_id in enumerate(ranked)}
    selected: list[str] = []
    for group in dict.fromkeys(groups):
        candidates = [
            (rank.get(option["itemId"], len(rank)), ref_id)
            for ref_id, option in options.items()
            if option["group"] == group
            and (not ranked or option["itemId"] in rank)
        ]
        if candidates:
            selected.append(min(candidates)[1])
    return {"predictedAction": action, "citationRefIds": selected}


async def project_prediction(
    client: RecordingClient,
    *, case: PublicCase, answer: str, catalog: Mapping[str, Mapping[str, Any]],
    tool_traces: Sequence[Mapping[str, Any]], snapshots: Sequence[Mapping[str, Any]],
    model_name: str,
    tool_invocations: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    final_turn_index = len(case.turns) - 1
    options = _citation_options(
        case, catalog, tool_traces, final_turn_index=final_turn_index,
    )
    payload = {
        "turns": list(case.turns),
        "visibleCandidates": list(case.visible_candidates),
        "answer": answer,
        "finalConstraintState": _constraint_groups(_latest_guide_state(snapshots)),
        "actualRankedItemIds": _actual_ranked_ids(tool_traces, final_turn_index),
        "successfulTools": [
            trace.get("tool") for trace in _successful_product_traces(tool_traces)
            if trace.get("publicTurnIndex") == final_turn_index
        ],
        "citationOptions": [
            {"refId": key, "itemId": value["itemId"], "group": value["group"]}
            for key, value in options.items()
        ],
    }
    tool = _projection_tool_schema(list(options))
    messages = [
        {"role": "system", "content": (
            "You are a label-blind projection adapter over an already completed production Agent turn. "
            "Use only the observed answer, TaskState-derived constraints and successful tool facts supplied here. "
            "Do not infer hidden expectations. Select the action the Agent actually performed and only citation IDs "
            "that the answer relied on. If there was no successful retrieval, do not claim retrieval. Return exactly "
            "one submit_used_phone_public_projection tool call."
        )},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]
    last_error = ""
    for repair_count in range(2):
        selected = None
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": "submit_used_phone_public_projection"}},
            )
        except Exception:
            raise
        try:
            calls = response.choices[0].message.tool_calls or []
            selected = next(
                (call for call in calls if call.function.name == "submit_used_phone_public_projection"),
                None,
            )
            if selected is None:
                raise PublicRunnerError("projection tool call missing")
            selection = json.loads(selected.function.arguments or "{}")
            if not isinstance(selection, dict):
                raise PublicRunnerError("projection arguments are not an object")
            selection = _normalize_projection_selection(
                case=case,
                catalog=catalog,
                tool_traces=tool_traces,
                snapshots=snapshots,
                selection=selection,
            )
            prediction = materialize_prediction(
                case=case, catalog=catalog, tool_traces=tool_traces,
                snapshots=snapshots, selection=selection,
                tool_invocations=tool_invocations,
            )
            return prediction, repair_count
        except (PublicRunnerError, ValidationError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
            if repair_count == 0:
                call_id = str(getattr(selected, "id", None) or "projection-invalid")
                raw_arguments = getattr(getattr(selected, "function", None), "arguments", "{}")
                messages.extend([
                    {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": call_id, "type": "function",
                            "function": {"name": "submit_used_phone_public_projection", "arguments": raw_arguments},
                        }],
                    },
                    {
                        "role": "tool", "tool_call_id": call_id,
                        "content": f"Public projection validation failed: {last_error}. Repair once using only supplied IDs.",
                    },
                ])
    raise PublicRunnerError(f"projection invalid after one repair: {last_error}")


def _reference_candidate_requirements(
    case: PublicCase,
    catalog: Mapping[str, Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Bind the reference product's non-relaxable public attributes.

    A single visible candidate is a substitution anchor, not merely a title.
    The frozen public catalog is already the runner's authoritative product
    snapshot, so its known OS and battery-health fields may seed TaskState as
    server-derived hard requirements.  Unknown/conflicting observations remain
    unbound and must never be guessed from the title.
    """

    if len(case.visible_candidates) != 1 or catalog is None:
        return []
    item_id = str(case.visible_candidates[0]["itemId"])
    product = catalog.get(item_id)
    attributes = product.get("attributes") if isinstance(product, Mapping) else None
    if not isinstance(attributes, Mapping):
        return []
    requirements: list[dict[str, Any]] = []
    for key in ("os", "battery_health"):
        observation = attributes.get(key)
        if not isinstance(observation, Mapping) or observation.get("status") != "known":
            continue
        value = observation.get("value")
        if not isinstance(value, str):
            continue
        requirements.append({
            "key": key,
            "operator": "eq",
            "value": value,
            "unit": "enum",
            "priority": "hard",
            "source": f"system:reference_product:{item_id}",
        })
    return requirements


def _initial_domain_state(
    case: PublicCase,
    catalog: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    visible_ids = [int(candidate["itemId"]) for candidate in case.visible_candidates]
    comparison_ids = visible_ids if len(visible_ids) == 2 else []
    prior_candidate_ids = visible_ids if len(visible_ids) == 1 else []
    return {
        "origin": "used_phone_public_production_runner",
        "turnCount": 0,
        "shoppingGuide": {
            "mode": "compare" if comparison_ids else "recommend",
            "category": "phone",
            "useCases": [],
            "requirements": _reference_candidate_requirements(case, catalog),
            "candidateIds": prior_candidate_ids,
            "comparedIds": comparison_ids,
            "evidenceStatus": "missing",
        },
    }


async def run_case(
    *, case: PublicCase, catalog: Mapping[str, Mapping[str, Any]], client: RecordingClient,
    model_name: str, timeout_seconds: float,
    runtime_audit: dict[str, Any] | None = None,
) -> CaseResult:
    import agent.app.llm as llm_module
    from agent.app.task_state import TaskFact, TaskStateCreateRequest, create_task_state

    snapshots: list[dict[str, Any]] = []
    tool_traces: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    answers: list[str] = []
    runtime_audit = runtime_audit if runtime_audit is not None else {}
    network_ledger: list[dict[str, Any]] = runtime_audit.setdefault("networkCalls", [])
    tool_invocation_ledger: list[dict[str, Any]] = runtime_audit.setdefault("toolInvocations", [])
    runtime_audit.update({
        "model": model_name,
        "modelEndpoint": DEEPSEEK_ENDPOINT,
        "javaEndpoint": JAVA_BASE_URL,
    })
    facts = [
        TaskFact(
            key=f"visible_candidate_{candidate['candidateLabel'].casefold()}",
            value={"itemId": candidate["itemId"], "title": candidate["title"]},
            source="system",
        )
        for candidate in case.visible_candidates
    ]

    async def execute() -> CaseResult:
        with in_memory_production_stores(), production_network_guard(
            network_ledger, tool_invocation_ledger,
        ), patch.object(
            llm_module, "get_client", lambda: client,
        ):
            state = await create_task_state(TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal=str(case.turns[0]["text"]),
                sessionId=f"public-{case.case_id.casefold()}",
                facts=facts,
                domainState=_initial_domain_state(case, catalog),
            ))

            async def capture_state(updated, phase: str) -> None:  # noqa: ANN001
                nonlocal state
                state = updated
                snapshots.append({
                    "phase": phase,
                    "state": updated.model_dump(by_alias=True, mode="json"),
                })

            snapshots.append({"phase": "initial", "state": state.model_dump(by_alias=True, mode="json")})
            for turn_index, turn in enumerate(case.turns):
                invocation_start = len(tool_invocation_ledger)
                answer, traces, turn_messages, _run_id, trace_summary = await llm_module.run_agent(
                    str(turn["text"]),
                    history=history,
                    task_state=state,
                    on_task_state=capture_state,
                    domain_hint="ecommerce",
                )
                answers.append(answer)
                for trace in traces:
                    serialized_trace = _trace_dict(trace)
                    serialized_trace["publicTurnIndex"] = turn_index
                    tool_traces.append(serialized_trace)
                for invocation in tool_invocation_ledger[invocation_start:]:
                    invocation["publicTurnIndex"] = turn_index
                history.extend(deepcopy(turn_messages))
                snapshots.append({
                    "phase": "turn_terminal",
                    "traceSummary": (
                        trace_summary.model_dump(by_alias=True, mode="json")
                        if trace_summary is not None else None
                    ),
                    "state": state.model_dump(by_alias=True, mode="json"),
                })
            selection = _deterministic_projection_selection(
                case=case,
                catalog=catalog,
                tool_traces=tool_traces,
                snapshots=snapshots,
            )
            prediction = materialize_prediction(
                case=case,
                catalog=catalog,
                tool_traces=tool_traces,
                snapshots=snapshots,
                selection=selection,
                tool_invocations=tool_invocation_ledger,
            )
            repair_count = 0
            return CaseResult(
                prediction=prediction,
                trace={
                    "schemaVersion": "used-phone-public-agent-trace-v1",
                    "protocolVersion": PROTOCOL_VERSION,
                    "caseId": case.case_id,
                    "turns": list(case.turns),
                    "answers": answers,
                    "taskStateSnapshots": snapshots,
                    "taskStateExtractionDecisions": _task_state_extraction_decisions(snapshots),
                    "toolTraces": tool_traces,
                    "toolInvocations": deepcopy(tool_invocation_ledger),
                    "modelCalls": deepcopy(client.ledger),
                    "networkCalls": network_ledger,
                    "projectionRepairCount": repair_count,
                    "businessNetworkUsed": any(item["kind"] == "javaProductRead" for item in network_ledger),
                    "businessWriteNetworkUsed": any(item["businessWrite"] for item in network_ledger),
                },
            )

    return await asyncio.wait_for(execute(), timeout=max(timeout_seconds, 0.1))


def _atomic_write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


def _atomic_terminal(attempt_dir: Path, terminal_name: str, payload: bytes) -> str:
    """Replace the sole running marker with exactly one terminal sidecar."""

    if terminal_name not in {"success.json", "failure.json"}:
        raise PublicRunnerError("invalid terminal filename")
    running = attempt_dir / "running.json"
    terminal = attempt_dir / terminal_name
    sibling = attempt_dir / ("failure.json" if terminal_name == "success.json" else "success.json")
    if not running.is_file() or terminal.exists() or sibling.exists():
        raise PublicRunnerError("attempt is not in a single-running-marker state")
    fd, temporary = tempfile.mkstemp(prefix=".terminal.", suffix=".tmp", dir=str(attempt_dir))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, running)
        os.replace(running, terminal)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


def _attempt_dirs(case_root: Path) -> list[Path]:
    return sorted(
        path for path in case_root.glob("attempt-*")
        if path.is_dir() and re.fullmatch(r"attempt-[0-9]{3}", path.name)
    )


def _terminal_state(attempt_dir: Path) -> str:
    present = [name for name in ("success.json", "failure.json") if (attempt_dir / name).is_file()]
    if len(present) == 1 and not (attempt_dir / "running.json").exists():
        return present[0].removesuffix(".json")
    if not present and (attempt_dir / "running.json").is_file():
        return "interrupted"
    raise PublicRunnerError(f"invalid attempt terminal layout: {attempt_dir}")


def _new_attempt_dir(case_root: Path) -> tuple[Path, int]:
    attempts = _attempt_dirs(case_root)
    number = int(attempts[-1].name.removeprefix("attempt-")) + 1 if attempts else 1
    attempt = case_root / f"attempt-{number:03d}"
    attempt.mkdir(parents=True, exist_ok=False)
    return attempt, number


def _case_context_hash(case: PublicCase) -> str:
    return hashlib.sha256(canonical_json_bytes({
        "caseId": case.case_id,
        "turns": case.turns,
        "visibleCandidates": case.visible_candidates,
    })).hexdigest()


def _runner_code_hashes() -> dict[str, str]:
    paths = {
        "agent/evaluation/used_phone_public_agent_runner_v1.py": Path(__file__).resolve(),
        "agent/evaluation/schemas/used_phone_public_agent_prediction_v1.schema.json": SCHEMA_PATH.resolve(),
        "agent/scripts/run_used_phone_public_agent_v1.py": REPO_ROOT / "agent" / "scripts" / "run_used_phone_public_agent_v1.py",
    }
    return {name: sha256_file(path) for name, path in paths.items()}


def _run_contract(
    *, bundle: PublicBundle, selected: Sequence[PublicCase], model_name: str,
    timeout_seconds: float, java_audit: Mapping[str, Any], allow_sealed_test: bool,
) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "publicCasesSha256": PUBLIC_CASES_SHA256,
        "publicCatalogSha256": PUBLIC_CATALOG_SHA256,
        "productionCodeScopeSha256": PRODUCTION_CODE_SCOPE_SHA256,
        "runnerCodeSha256": _runner_code_hashes(),
        "attributeContractCodeSha256": ATTRIBUTE_CONTRACT_CODE_SHA256,
        "attributeRuleset": ATTRIBUTE_RULESET_VERSION,
        "javaCatalog": dict(java_audit),
        "model": model_name,
        "modelEndpoint": DEEPSEEK_ENDPOINT,
        "productionRuntimeConfig": dict(PRODUCTION_RUNTIME_CONFIG),
        "timeoutSeconds": timeout_seconds,
        "caseCount": len(bundle.cases),
        "selectedCaseIds": [case.case_id for case in selected],
        "selectedSplits": sorted({case.split for case in selected}),
        "allowSealedTest": allow_sealed_test,
    }


def _manifest_core_sha(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(contract)).hexdigest()


def _terminal_runtime_identity(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": contract["protocolVersion"],
        "productionCodeScopeSha256": contract["productionCodeScopeSha256"],
        "runnerCodeSha256": deepcopy(contract["runnerCodeSha256"]),
        "attributeContractCodeSha256": contract["attributeContractCodeSha256"],
        "attributeRuleset": contract["attributeRuleset"],
        "publicCasesSha256": contract["publicCasesSha256"],
        "publicCatalogSha256": contract["publicCatalogSha256"],
        "javaProjectionSha256": contract["javaCatalog"]["javaProjectionSha256"],
        "productionRuntimeConfig": deepcopy(contract["productionRuntimeConfig"]),
        "model": contract["model"],
        "modelEndpoint": contract["modelEndpoint"],
    }


def _validate_case_attempt_layout(case_root: Path) -> list[Path]:
    if not case_root.exists():
        return []
    entries = list(case_root.iterdir())
    if any(
        not entry.is_dir() or re.fullmatch(r"attempt-[0-9]{3}", entry.name) is None
        for entry in entries
    ):
        raise PublicRunnerError(f"invalid case attempt entry: {case_root}")
    attempts = sorted(entries)
    expected = [f"attempt-{index:03d}" for index in range(1, len(attempts) + 1)]
    if [path.name for path in attempts] != expected:
        raise PublicRunnerError(f"non-contiguous case attempt sequence: {case_root}")
    for attempt in attempts:
        _terminal_state(attempt)
    return attempts


def _validate_resumed_prediction(
    *, case: PublicCase, catalog: Mapping[str, Mapping[str, Any]],
    prediction: Mapping[str, Any], trace: Mapping[str, Any],
) -> None:
    validate_prediction_row(prediction)
    if prediction.get("caseId") != case.case_id or trace.get("caseId") != case.case_id:
        raise PublicRunnerError("resumed success case identity mismatch")
    if (
        trace.get("schemaVersion") != "used-phone-public-agent-trace-v1"
        or trace.get("protocolVersion") != PROTOCOL_VERSION
    ):
        raise PublicRunnerError("resumed trace protocol mismatch")
    tool_traces = trace.get("toolTraces")
    snapshots = trace.get("taskStateSnapshots")
    invocations = trace.get("toolInvocations")
    if not isinstance(tool_traces, list) or not isinstance(snapshots, list) or not isinstance(invocations, list):
        raise PublicRunnerError("resumed trace provenance is incomplete")
    action = prediction.get("predictedAction")
    if action not in FORMAL_ACTIONS:
        raise PublicRunnerError("resumed prediction action is missing")
    decision_tool = (
        "compare_products" if action == "COMPARE_WITH_FIELD_EVIDENCE"
        else "search_products" if action in {
            "RETRIEVE_FILTER_AND_RANK", "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
            "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS",
            "RETRIEVE_FILTER_AND_RANK_WITH_UNKNOWNS", "UPDATE_STATE_THEN_RETRIEVE",
        }
        else None
    )
    citation_options = (
        _citation_options(
            case, catalog, tool_traces,
            tool_name=decision_tool, final_turn_index=len(case.turns) - 1,
        )
        if decision_tool is not None else {}
    )
    selected_ids: list[str] = []
    for citation in prediction.get("evidenceCitations", []):
        matches = [ref_id for ref_id, option in citation_options.items() if option == citation]
        if len(matches) != 1 or matches[0] in selected_ids:
            raise PublicRunnerError("resumed citation is not bound to final tool provenance")
        selected_ids.append(matches[0])
    expected = materialize_prediction(
        case=case,
        catalog=catalog,
        tool_traces=tool_traces,
        snapshots=snapshots,
        selection={"predictedAction": action, "citationRefIds": selected_ids},
        tool_invocations=invocations,
    )
    if expected != dict(prediction):
        raise PublicRunnerError("resumed prediction semantic/provenance mismatch")


def _validate_resumed_success(
    *, attempt_dir: Path, case: PublicCase, catalog: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any], contract_sha: str,
) -> dict[str, Any]:
    terminal = _load_manifest(attempt_dir / "success.json")
    attempt_number = int(attempt_dir.name.removeprefix("attempt-"))
    if (
        terminal.get("schemaVersion") != "used-phone-public-agent-case-success-v1"
        or terminal.get("terminalState") != "success"
        or terminal.get("caseId") != case.case_id
        or terminal.get("attempt") != attempt_number
        or terminal.get("protocolVersion") != PROTOCOL_VERSION
        or terminal.get("contractSha256") != contract_sha
        or terminal.get("contextSha256") != _case_context_hash(case)
        or terminal.get("runtimeIdentity") != _terminal_runtime_identity(contract)
        or terminal.get("predictionPath") != "prediction.json"
        or terminal.get("tracePath") != "trace.json"
    ):
        raise PublicRunnerError("resumed success terminal provenance mismatch")
    prediction_path = attempt_dir / "prediction.json"
    trace_path = attempt_dir / "trace.json"
    try:
        prediction_bytes = prediction_path.read_bytes()
        trace_bytes = trace_path.read_bytes()
        prediction = json.loads(prediction_bytes)
        trace = json.loads(trace_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicRunnerError("resumed prediction/trace file is invalid") from exc
    if (
        hashlib.sha256(prediction_bytes).hexdigest() != terminal.get("predictionSha256")
        or hashlib.sha256(trace_bytes).hexdigest() != terminal.get("traceSha256")
        or canonical_json_bytes(prediction) != prediction_bytes
        or canonical_json_bytes(trace) != trace_bytes
        or terminal.get("prediction") != prediction
        or terminal.get("trace") != trace
    ):
        raise PublicRunnerError("resumed prediction/trace byte hash mismatch")
    _validate_resumed_prediction(
        case=case, catalog=catalog, prediction=prediction, trace=trace,
    )
    return terminal


def _select_cases(
    bundle: PublicBundle, *, splits: Sequence[str] | None, case_ids: Sequence[str] | None,
    allow_sealed_test: bool,
) -> list[PublicCase]:
    requested_splits = set(splits or ([] if case_ids else ["dev"]))
    if not requested_splits.issubset(EXPECTED_SPLIT_COUNTS):
        raise PublicRunnerError("unknown split selection")
    selected = [
        case for case in bundle.cases
        if not requested_splits or case.split in requested_splits
    ]
    if case_ids:
        requested_ids = set(case_ids)
        unknown = requested_ids - set(bundle.by_id())
        if unknown:
            raise PublicRunnerError(f"unknown case IDs: {sorted(unknown)}")
        selected = [case for case in selected if case.case_id in requested_ids]
        selected_ids = {case.case_id for case in selected}
        if selected_ids != requested_ids:
            raise PublicRunnerError("case IDs do not belong to the selected split")
    if any(case.split == "test" for case in selected) and not allow_sealed_test:
        raise PublicRunnerError("sealed test selection requires --allow-sealed-test")
    if not selected:
        raise PublicRunnerError("case selection is empty")
    return selected


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicRunnerError("resume manifest is invalid") from exc
    if not isinstance(value, dict):
        raise PublicRunnerError("resume manifest must be an object")
    return value


async def run_public_agent(
    *, public_cases_path: Path, public_catalog_path: Path, run_dir: Path,
    splits: Sequence[str] | None = None, case_ids: Sequence[str] | None = None,
    allow_sealed_test: bool = False, resume: bool = False,
    timeout_seconds: float = 90.0, client: RecordingClient | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    _assert_clean_preloaded_modules()
    verify_production_scope()
    bundle = load_public_bundle(public_cases_path, public_catalog_path)
    selected = _select_cases(
        bundle, splits=splits, case_ids=case_ids, allow_sealed_test=allow_sealed_test,
    )
    java_audit = verify_java_public_catalog(bundle)
    if model_name is None:
        from agent.app.settings import settings
        model_name = settings.deepseek_model
    contract = _run_contract(
        bundle=bundle, selected=selected, model_name=model_name,
        timeout_seconds=timeout_seconds, java_audit=java_audit,
        allow_sealed_test=allow_sealed_test,
    )
    contract_sha = _manifest_core_sha(contract)
    output = Path(run_dir).resolve()
    if resume:
        if not output.is_dir():
            raise PublicRunnerError("resume run directory is missing")
        prior = _load_manifest(output / "run_contract.json")
        if prior.get("contractSha256") != contract_sha or prior.get("contract") != contract:
            raise PublicRunnerError("resume protocol/code/data contract mismatch")
    else:
        output.mkdir(parents=True, exist_ok=False)
        _atomic_write(output / "run_contract.json", canonical_json_bytes({
            "schemaVersion": "used-phone-public-agent-run-contract-v1",
            "contractSha256": contract_sha,
            "contract": contract,
        }))

    shared_client = client or make_real_client()
    terminal_by_case: dict[str, dict[str, Any]] = {}
    control_exception: BaseException | None = None
    with public_runtime_isolation(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=output,
    ):
        for case in selected:
            case_root = output / "cases" / case.case_id
            attempts = _validate_case_attempt_layout(case_root)
            if resume and attempts:
                latest_state = _terminal_state(attempts[-1])
                if latest_state == "success":
                    terminal_by_case[case.case_id] = _validate_resumed_success(
                        attempt_dir=attempts[-1], case=case,
                        catalog=bundle.catalog_by_id, contract=contract,
                        contract_sha=contract_sha,
                    )
                    continue
            attempt_dir, attempt = _new_attempt_dir(case_root)
            running = {
                "schemaVersion": "used-phone-public-agent-running-v1",
                "caseId": case.case_id,
                "attempt": attempt,
                "contractSha256": contract_sha,
                "contextSha256": _case_context_hash(case),
            }
            _atomic_write(attempt_dir / "running.json", canonical_json_bytes(running))
            case_client = shared_client.fork()
            case_runtime_audit: dict[str, Any] = {}
            started = time.perf_counter()
            try:
                result = await run_case(
                    case=case, catalog=bundle.catalog_by_id, client=case_client,
                    model_name=model_name, timeout_seconds=timeout_seconds,
                    runtime_audit=case_runtime_audit,
                )
                prediction_sha = _atomic_write(
                    attempt_dir / "prediction.json", canonical_json_bytes(result.prediction),
                )
                trace_sha = _atomic_write(
                    attempt_dir / "trace.json", canonical_json_bytes(result.trace),
                )
                success = {
                    "schemaVersion": "used-phone-public-agent-case-success-v1",
                    "terminalState": "success",
                    "protocolVersion": PROTOCOL_VERSION,
                    "caseId": case.case_id,
                    "attempt": attempt,
                    "contractSha256": contract_sha,
                    "contextSha256": _case_context_hash(case),
                    "durationMs": round((time.perf_counter() - started) * 1000, 2),
                    "modelCallCount": len(case_client.ledger),
                    "model": model_name,
                    "modelEndpoint": DEEPSEEK_ENDPOINT,
                    "javaEndpoint": JAVA_BASE_URL,
                    "runtimeIdentity": _terminal_runtime_identity(contract),
                    "modelNetworkUsed": any(
                        item.get("kind") == "model"
                        for item in result.trace.get("networkCalls", [])
                    ),
                    "businessNetworkUsed": any(
                        item.get("kind") == "javaProductRead"
                        for item in result.trace.get("networkCalls", [])
                    ),
                    "businessWriteNetworkUsed": any(
                        item.get("businessWrite") is True
                        for item in result.trace.get("networkCalls", [])
                    ),
                    "predictionPath": "prediction.json",
                    "tracePath": "trace.json",
                    "predictionSha256": prediction_sha,
                    "traceSha256": trace_sha,
                    "prediction": result.prediction,
                    "trace": result.trace,
                }
                _atomic_terminal(attempt_dir, "success.json", canonical_json_bytes(success))
                terminal_by_case[case.case_id] = success
            except BaseException as exc:
                failure_outcome = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError)
                    else "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                    else "error"
                )
                failure = {
                    "schemaVersion": "used-phone-public-agent-case-failure-v1",
                    "terminalState": "failure",
                    "protocolVersion": PROTOCOL_VERSION,
                    "caseId": case.case_id,
                    "attempt": attempt,
                    "contractSha256": contract_sha,
                    "contextSha256": _case_context_hash(case),
                    "durationMs": round((time.perf_counter() - started) * 1000, 2),
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:2000],
                    "failureOutcome": failure_outcome,
                    "modelCallCount": len(case_client.ledger),
                    "model": model_name,
                    "modelEndpoint": DEEPSEEK_ENDPOINT,
                    "javaEndpoint": JAVA_BASE_URL,
                    "runtimeIdentity": _terminal_runtime_identity(contract),
                    "modelCalls": deepcopy(case_client.ledger),
                    "networkCalls": deepcopy(case_runtime_audit.get("networkCalls", [])),
                    "modelNetworkUsed": any(
                        item.get("kind") == "model"
                        for item in case_runtime_audit.get("networkCalls", [])
                    ),
                    "businessNetworkUsed": any(
                        item.get("kind") == "javaProductRead"
                        for item in case_runtime_audit.get("networkCalls", [])
                    ),
                    "businessWriteNetworkUsed": any(
                        item.get("businessWrite") is True
                        for item in case_runtime_audit.get("networkCalls", [])
                    ),
                }
                _atomic_terminal(attempt_dir, "failure.json", canonical_json_bytes(failure))
                terminal_by_case[case.case_id] = failure
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    control_exception = exc
                    break

        prediction_rows: list[dict[str, Any]] = []
        execution: dict[str, bool] = {}
        total_model_calls = 0
        total_java_reads = 0
        total_model_network_calls = 0
        for case in selected:
            terminal = terminal_by_case.get(case.case_id, {
                "terminalState": "failure",
                "failureOutcome": "interruptedBeforeAttempt",
                "modelCallCount": 0,
                "networkCalls": [],
            })
            total_model_calls += int(terminal.get("modelCallCount", 0))
            if terminal.get("terminalState") == "success":
                prediction_rows.append(terminal["prediction"])
                execution[case.case_id] = True
                total_java_reads += sum(
                    item.get("kind") == "javaProductRead"
                    for item in terminal["trace"].get("networkCalls", [])
                )
                total_model_network_calls += sum(
                    item.get("kind") == "model"
                    for item in terminal["trace"].get("networkCalls", [])
                )
            else:
                prediction_rows.append({"caseId": case.case_id, "rankedItemIds": []})
                execution[case.case_id] = False
                total_java_reads += sum(
                    item.get("kind") == "javaProductRead"
                    for item in terminal.get("networkCalls", [])
                )
                total_model_network_calls += sum(
                    item.get("kind") == "model"
                    for item in terminal.get("networkCalls", [])
                )
        predictions_payload = b"".join(canonical_json_bytes(row) for row in prediction_rows)
        predictions_sha = _atomic_write(output / "predictions.jsonl", predictions_payload)
        manifest = {
            "schemaVersion": "used-phone-public-agent-run-manifest-v1",
            "contractSha256": contract_sha,
            "contract": contract,
            "selectedCaseIds": [case.case_id for case in selected],
            "selectedSplits": sorted({case.split for case in selected}),
            "execution": execution,
            "succeededCaseIds": [case_id for case_id, ok in execution.items() if ok],
            "failedCaseIds": [case_id for case_id, ok in execution.items() if not ok],
            "failureOutcomes": {
                case_id: terminal_by_case.get(case_id, {}).get(
                    "failureOutcome", "interruptedBeforeAttempt",
                )
                for case_id, ok in execution.items() if not ok
            },
            "prediction": {"path": "predictions.jsonl", "rowCount": len(prediction_rows), "sha256": predictions_sha},
            "modelCallCount": total_model_calls,
            "javaProductReadNetworkCallCount": total_java_reads,
            "modelEndpoint": DEEPSEEK_ENDPOINT,
            "javaEndpoint": JAVA_BASE_URL,
            "modelNetworkCallCount": total_model_network_calls,
            "modelNetworkUsed": total_model_network_calls > 0,
            "businessNetworkUsed": total_java_reads > 0,
            "businessWriteNetworkUsed": False,
            "hiddenArtifactsRead": False,
            "failurePolicy": "terminal failure emits empty prediction and remains in denominator",
        }
        _atomic_write(output / "manifest.json", canonical_json_bytes(manifest))
    if control_exception is not None:
        raise control_exception
    return manifest


def validate_prediction_row(row: Mapping[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(dict(row))


__all__ = [
    "CaseResult", "PublicBundle", "PublicCase", "PublicIsolationError",
    "PublicRunnerError", "RUN_LOCAL_TEMP_DIR_NAME", "RecordingClient",
    "audit_public_inputs", "canonical_json_bytes", "load_public_bundle",
    "materialize_prediction", "production_scope_sha256", "public_runtime_isolation",
    "run_case", "run_public_agent", "validate_prediction_row",
    "verify_java_public_catalog",
]
