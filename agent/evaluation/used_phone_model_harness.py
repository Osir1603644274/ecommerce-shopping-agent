"""Label-blind used-phone evaluation composition over lifecycle components.

This module intentionally is *not* a production Agent entry point.  It composes
the production TaskState extractor and controlled ReAct lifecycle with a pinned,
evaluation-only catalog transport.  Hidden judgments and construction artifacts
are denied by both import and file guards while predictions are generated.
"""

from __future__ import annotations

import asyncio
import builtins
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import importlib.abc
import importlib.machinery
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse
from unittest.mock import patch

import httpx
from jsonschema import Draft202012Validator
from openai import AsyncOpenAI

from agent.app.schemas import ToolTrace
from agent.app.settings import settings
from agent.app.task_state import TaskFact, TaskState, TaskStateCreateRequest, create_task_state
from agent.evaluation.used_phone_offline_runtime import (
    PublicBundle,
    PublicCase,
    bm25_rank,
    canonical_json_bytes,
    load_public_bundle,
    normalize_public_fact,
    sha256_file,
)


COMPOSITION_NAME = "used-phone evaluation composition over current lifecycle components"
PREDICTION_SCHEMA_VERSION = "used-phone-complex-prediction-v1"
PROJECTION_PROTOCOL_VERSION = "v2-selection-materializer"
CATEGORY_KEY = "46/133/185"
EXPECTED_CATALOG_COUNT = 252
EXPECTED_EVIDENCE_AUDIT_SHA256 = "9e8f9e78ab6a630985dbfb3b202bc06ab010120cfc9bce3b61c042750db669b3"
PUBLIC_FACT_GROUPS = (
    "battery_health",
    "motherboard_repair",
    "screen_originality",
    "battery_originality",
    "scratch_level",
    "os",
)
NORMALIZED_VALUES = {
    "90_plus", "80_to_90", "below_80", "repaired", "not_repaired",
    "original", "non_original", "none", "light", "present", "ios",
    "android", "unknown",
}
FORMAL_ACTIONS = (
    "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
    "RETRIEVE_FILTER_AND_RANK",
    "CLARIFY",
    "COMPARE_WITH_FIELD_EVIDENCE",
    "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS",
    "ABSTAIN_OR_EXPLAIN",
    "UPDATE_STATE_THEN_RETRIEVE",
)
ALLOWED_TOOL_NAMES = {"search_products", "get_product_details", "compare_products"}
DENIED_IMPORT_FRAGMENTS = (
    "used_phone_complex_metrics",
    "used_phone_complex_pilot",
    "build_track_b_",
    "finalize_track_b_",
    "compare_track_b_",
    "kuaisearch_synthetic_evidence_benchmark",
)
DENIED_PATH_FRAGMENTS = (
    "used_phone_agent_pilot_ai_judged_freeze_v1",
    "ai_judged_candidate_qrel",
    "ai_judged_case_action",
    "ai_judged_query_constraints",
    "sealed",
    "reviewer",
    "adjudication",
    "prelabel",
    "used_phone_agent_cases_pending",
    "candidate_pool_provenance",
    "used_phone_complex_pilot.py",
    "build_track_b_",
    "finalize_track_b_",
    "compare_track_b_",
    "kuaisearch_synthetic_evidence_benchmark.py",
)


class PublicIsolationError(RuntimeError):
    pass


class CompositionError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _production_runtime() -> SimpleNamespace:
    """Delay the heavy production tool/embedding import until an actual Agent run."""

    from agent.app.agent_trace import TraceBuilder
    from agent.app.context_pack import build_context_pack, context_pack_hash, context_pack_token_count
    from agent.app.context_view import ContextProjector
    from agent.app.harness import run_harness_step
    from agent.app.llm import _update_task_state_for_unified_harness
    from agent.app.react_graph import ReActGraphRuntime, run_controlled_react_graph
    from agent.app.tools import TOOL_SCHEMAS

    return SimpleNamespace(
        TraceBuilder=TraceBuilder,
        build_context_pack=build_context_pack,
        context_pack_hash=context_pack_hash,
        context_pack_token_count=context_pack_token_count,
        ContextProjector=ContextProjector,
        run_harness_step=run_harness_step,
        update_task_state=_update_task_state_for_unified_harness,
        ReActGraphRuntime=ReActGraphRuntime,
        run_controlled_react_graph=run_controlled_react_graph,
        tool_schemas=TOOL_SCHEMAS,
    )


class _DeniedImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001
        lowered = fullname.casefold()
        if any(fragment in lowered for fragment in DENIED_IMPORT_FRAGMENTS):
            raise PublicIsolationError(f"public SUT denied import: {fullname}")
        return None


def _path_text(value: Any) -> str:
    try:
        return os.fspath(value).replace("\\", "/").casefold()
    except TypeError:
        return ""


def _assert_public_path(value: Any) -> None:
    lowered = _path_text(value)
    if lowered and any(fragment in lowered for fragment in DENIED_PATH_FRAGMENTS):
        raise PublicIsolationError(f"public SUT denied path: {Path(os.fspath(value)).name}")


@contextmanager
def public_isolation_guard():
    """Guard known Python import/read paths; this is not a process egress sandbox."""

    finder = _DeniedImportFinder()
    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open
    real_path_read_text = Path.read_text
    real_path_read_bytes = Path.read_bytes
    real_source_get_data = importlib.machinery.SourceFileLoader.get_data
    real_sourceless_get_data = importlib.machinery.SourcelessFileLoader.get_data

    def guarded_open(file, *args, **kwargs):  # noqa: ANN001
        _assert_public_path(file)
        return real_open(file, *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):  # noqa: ANN001
        _assert_public_path(file)
        return real_io_open(file, *args, **kwargs)

    def guarded_os_open(file, *args, **kwargs):  # noqa: ANN001
        _assert_public_path(file)
        return real_os_open(file, *args, **kwargs)

    def guarded_path_read_text(path, *args, **kwargs):  # noqa: ANN001
        _assert_public_path(path)
        return real_path_read_text(path, *args, **kwargs)

    def guarded_path_read_bytes(path, *args, **kwargs):  # noqa: ANN001
        _assert_public_path(path)
        return real_path_read_bytes(path, *args, **kwargs)

    def guarded_source_get_data(loader, path):  # noqa: ANN001
        _assert_public_path(path)
        return real_source_get_data(loader, path)

    def guarded_sourceless_get_data(loader, path):  # noqa: ANN001
        _assert_public_path(path)
        return real_sourceless_get_data(loader, path)

    preloaded = {
        name: module
        for name, module in list(sys.modules.items())
        if any(fragment in name.casefold() for fragment in DENIED_IMPORT_FRAGMENTS)
    }
    parent_attributes: list[tuple[Any, str, bool, Any]] = []
    for name in preloaded:
        parent_name, separator, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name) if separator else None
        if parent is not None:
            existed = child_name in vars(parent)
            parent_attributes.append((parent, child_name, existed, vars(parent).get(child_name)))
            vars(parent).pop(child_name, None)
        sys.modules.pop(name, None)
    sys.meta_path.insert(0, finder)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(builtins, "open", guarded_open))
            stack.enter_context(patch.object(io, "open", guarded_io_open))
            stack.enter_context(patch.object(os, "open", guarded_os_open))
            stack.enter_context(patch.object(Path, "read_text", guarded_path_read_text))
            stack.enter_context(patch.object(Path, "read_bytes", guarded_path_read_bytes))
            stack.enter_context(patch.object(
                importlib.machinery.SourceFileLoader, "get_data", guarded_source_get_data
            ))
            stack.enter_context(patch.object(
                importlib.machinery.SourcelessFileLoader, "get_data", guarded_sourceless_get_data
            ))
            yield
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
        for name, module in preloaded.items():
            sys.modules[name] = module
        for parent, child_name, existed, value in parent_attributes:
            if existed:
                vars(parent)[child_name] = value
            else:
                vars(parent).pop(child_name, None)


def _validated_llm_endpoint(base_url: str):
    endpoint = urlparse(base_url)
    if (
        endpoint.scheme.casefold() != "https"
        or (endpoint.hostname or "").casefold() != "api.deepseek.com"
        or endpoint.port not in {None, 443}
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise CompositionError("LLM endpoint must be https://api.deepseek.com on default HTTPS port")
    return endpoint


def _canonical_https_origin(scheme: str, hostname: str | None, port: int | None) -> tuple[str, str, None]:
    normalized_scheme = scheme.casefold()
    normalized_host = (hostname or "").casefold()
    if normalized_scheme != "https" or port not in {None, 443}:
        raise PublicIsolationError("only default-port HTTPS requests are allowed")
    return normalized_scheme, normalized_host, None


@contextmanager
def llm_endpoint_network_guard(base_url: str):
    """Allow known HTTPX paths only to the fixed DeepSeek HTTPS origin."""

    allowed = _validated_llm_endpoint(base_url)
    allowed_origin = _canonical_https_origin(allowed.scheme, allowed.hostname, allowed.port)
    async_send = httpx.AsyncClient.send
    sync_send = httpx.Client.send

    def check(request: httpx.Request) -> None:
        url = request.url
        if url.username or url.password:
            raise PublicIsolationError("userinfo is denied in model requests")
        origin = _canonical_https_origin(url.scheme, url.host, url.port)
        if origin != allowed_origin:
            raise PublicIsolationError(f"network origin denied: {url.scheme}://{url.host}")

    async def guarded_async_send(client, request, *args, **kwargs):  # noqa: ANN001
        check(request)
        return await async_send(client, request, *args, **kwargs)

    def guarded_sync_send(client, request, *args, **kwargs):  # noqa: ANN001
        check(request)
        return sync_send(client, request, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "send", guarded_async_send), patch.object(
        httpx.Client, "send", guarded_sync_send
    ):
        yield


async def _forbidden_business_call(*_args, **_kwargs):
    raise PublicIsolationError("business/live tool call is forbidden in public SUT")


def _forbidden_business_factory(*_args, **_kwargs):
    raise PublicIsolationError("business network client is forbidden in public SUT")


@contextmanager
def business_call_guard():
    """Fail closed if production transport or ecommerce network code is reached."""

    import agent.app.domains.ecommerce.tools as ecommerce_tools
    import agent.app.llm as llm_module
    import agent.app.tools as tools_module
    import agent.app.transport_resolver as resolver_module

    with ExitStack() as stack:
        stack.enter_context(patch.object(tools_module, "call_tool", _forbidden_business_call))
        stack.enter_context(patch.object(llm_module, "get_tool_transport", _forbidden_business_factory))
        stack.enter_context(patch.object(resolver_module, "get_tool_transport", _forbidden_business_factory))
        for module in (tools_module, ecommerce_tools):
            for name in ("search_products_tool", "get_product_details_tool", "compare_products_tool"):
                if hasattr(module, name):
                    stack.enter_context(patch.object(module, name, _forbidden_business_call))
        if hasattr(ecommerce_tools, "_product_http_client"):
            stack.enter_context(patch.object(ecommerce_tools, "_product_http_client", _forbidden_business_factory))
        yield


class InMemoryRedis:
    """Minimal async Redis contract used by production TaskState persistence."""

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
            raise NotImplementedError("evaluation Redis only supports TaskState CAS")
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
        values = [member for member, _ in sorted(self.sorted_sets.get(key, {}).items(), key=lambda row: row[1])]
        return self._slice(values, start, end)

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


@contextmanager
def in_memory_task_state_store():
    import agent.app.task_state as task_state_module

    previous = task_state_module._client
    task_state_module._client = InMemoryRedis()
    task_state_module._task_locks.clear()
    task_state_module._session_locks.clear()
    try:
        yield task_state_module._client
    finally:
        task_state_module._client = previous
        task_state_module._task_locks.clear()
        task_state_module._session_locks.clear()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_catalog_rows(path: Path) -> list[dict[str, Any]]:
    if sha256_file(path) != EXPECTED_EVIDENCE_AUDIT_SHA256:
        raise CompositionError("evidence audit hash mismatch")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line)
            if row.get("categoryKey") == CATEGORY_KEY:
                copied = dict(row)
                copied["_sourceLine"] = line_number
                rows.append(copied)
    if len(rows) != EXPECTED_CATALOG_COUNT:
        raise CompositionError(f"expected 252 used-phone catalog rows, got {len(rows)}")
    if len({str(row["itemId"]) for row in rows}) != EXPECTED_CATALOG_COUNT:
        raise CompositionError("duplicate used-phone itemId")
    return rows


def _candidate_facts(candidate: Mapping[str, Any]) -> dict[str, str]:
    facts: dict[str, str] = {}
    for group in PUBLIC_FACT_GROUPS:
        for ref in candidate.get("evidenceRefs", []):
            value = normalize_public_fact(group, ref.get("rawValue"))
            if value is not None:
                facts[group] = value
                break
    return facts


@dataclass(frozen=True)
class CatalogBinding:
    rows: tuple[Mapping[str, Any], ...]
    internal_id_by_item: Mapping[str, int]
    item_by_internal_id: Mapping[int, Mapping[str, Any]]
    public_to_item_by_case: Mapping[str, Mapping[str, str]]
    public_candidate_by_case: Mapping[str, Mapping[str, Mapping[str, Any]]]

    @classmethod
    def load(cls, evidence_audit: str | Path, bundle: PublicBundle) -> "CatalogBinding":
        rows = _read_catalog_rows(Path(evidence_audit).resolve())
        ordered = sorted(rows, key=lambda row: (int(str(row["itemId"])), str(row["itemId"])))
        internal = {str(row["itemId"]): index for index, row in enumerate(ordered, 1)}
        reverse = {internal[str(row["itemId"])]: row for row in ordered}
        public_to_item: dict[str, dict[str, str]] = {}
        public_candidates: dict[str, dict[str, Mapping[str, Any]]] = {}
        matched_rows = 0
        matched_items: set[str] = set()
        for case in bundle.cases:
            case_map: dict[str, str] = {}
            case_candidates: dict[str, Mapping[str, Any]] = {}
            for candidate in case.candidates:
                pilot_id = str(candidate.get("pilotId", ""))
                expected_fp = str(candidate.get("sourceTrace", {}).get("itemFingerprintSha256", ""))
                matches = [
                    str(row["itemId"])
                    for row in ordered
                    if _sha256_text(f"{pilot_id}:{row['itemId']}") == expected_fp
                ]
                if len(matches) != 1:
                    raise CompositionError(
                        f"public fingerprint did not uniquely resolve {candidate.get('candidateDisplayId')}"
                    )
                public_id = str(candidate["candidateDisplayId"])
                case_map[public_id] = matches[0]
                case_candidates[public_id] = candidate
                matched_rows += 1
                matched_items.add(matches[0])
            public_to_item[case.review_case_id] = case_map
            public_candidates[case.review_case_id] = case_candidates
        if matched_rows != 83 or len(matched_items) != 68:
            raise CompositionError(
                f"expected public mapping 83 rows/68 products, got {matched_rows}/{len(matched_items)}"
            )
        return cls(
            rows=tuple(ordered),
            internal_id_by_item=internal,
            item_by_internal_id=reverse,
            public_to_item_by_case=public_to_item,
            public_candidate_by_case=public_candidates,
        )

    def internal_for_public(self, case_id: str, public_id: str) -> int:
        return self.internal_id_by_item[self.public_to_item_by_case[case_id][public_id]]

    def public_for_internal(self, case_id: str, internal_id: int) -> str | None:
        item_id = str(self.item_by_internal_id[internal_id]["itemId"])
        for public_id, bound_item in self.public_to_item_by_case[case_id].items():
            if bound_item == item_id:
                return public_id
        return None


def _normalize_expected(group: str, value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    for item in values:
        text = str(item).strip().casefold()
        if text in NORMALIZED_VALUES:
            candidate = text
        else:
            candidate = normalize_public_fact(group, item)
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _normalize_group(key: Any) -> str | None:
    text = str(key).strip().casefold()
    aliases = {
        "battery_health": "battery_health", "电池健康": "battery_health",
        "motherboard_repair": "motherboard_repair", "主板维修": "motherboard_repair",
        "screen_originality": "screen_originality", "屏幕原装": "screen_originality",
        "battery_originality": "battery_originality", "电池原装": "battery_originality",
        "scratch_level": "scratch_level", "划痕": "scratch_level",
        "os": "os", "system": "os", "操作系统": "os",
    }
    return aliases.get(text)


def normalize_requirements(raw: Any) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        group = _normalize_group(item.get("key"))
        operator = str(item.get("operator", "eq")).casefold()
        expected = _normalize_expected(group, item.get("value")) if group else []
        if group is None or operator not in {"eq", "in", "not_in"} or not expected:
            continue
        requirements.append({
            "key": group,
            "operator": operator,
            "value": expected if operator in {"in", "not_in"} else expected[0],
            "unit": str(item.get("unit") or "enum"),
            "priority": "soft" if str(item.get("priority", "hard")).casefold() == "soft" else "hard",
            "source": str(item.get("source") or "user"),
        })
    return requirements


def _requirement_status(actual: str | None, requirement: Mapping[str, Any]) -> str:
    if actual is None:
        return "unknown"
    values = requirement["value"] if isinstance(requirement["value"], list) else [requirement["value"]]
    if requirement["operator"] in {"eq", "in"}:
        return "pass" if actual in values else "fail"
    if requirement["operator"] == "not_in":
        return "pass" if actual not in values else "fail"
    return "unknown"


@dataclass
class OfflineCatalogToolCaller:
    case: PublicCase
    catalog: CatalogBinding
    calls: list[dict[str, Any]] = field(default_factory=list)

    def _public_candidate_for_internal(self, internal_id: int) -> Mapping[str, Any] | None:
        public_id = self.catalog.public_for_internal(self.case.review_case_id, internal_id)
        if public_id is None:
            return None
        return self.catalog.public_candidate_by_case[self.case.review_case_id][public_id]

    def _evidence(self, public_id: str, facts: Mapping[str, str]) -> list[dict[str, Any]]:
        candidate = self.catalog.public_candidate_by_case[self.case.review_case_id][public_id]
        internal_id = self.catalog.internal_for_public(self.case.review_case_id, public_id)
        evidence: list[dict[str, Any]] = []
        for group, normalized in sorted(facts.items()):
            matching_index = None
            for index, ref in enumerate(candidate.get("evidenceRefs", [])):
                if normalize_public_fact(group, ref.get("rawValue")) == normalized:
                    matching_index = index
                    break
            if matching_index is None:
                continue
            evidence.append({
                "ref": f"public:{public_id}:{matching_index}:{group}",
                "productId": internal_id,
                "field": group,
                "value": normalized,
                "source": "public_raw_evidence",
                "candidateDisplayId": public_id,
                "evidenceRefIndex": matching_index,
            })
        return evidence

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> ToolTrace:
        if tool_name not in ALLOWED_TOOL_NAMES:
            raise PublicIsolationError(f"offline catalog denied tool: {tool_name}")
        started = time.perf_counter()
        if tool_name == "search_products":
            trace = self._search(arguments)
        elif tool_name == "get_product_details":
            trace = self._details(arguments)
        else:
            trace = self._compare(arguments)
        trace.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        self.calls.append({
            "index": len(self.calls),
            "toolName": tool_name,
            "arguments": deepcopy(arguments),
            "trace": trace.model_dump(by_alias=True, mode="json"),
        })
        return trace

    def _search(self, arguments: Mapping[str, Any]) -> ToolTrace:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolTrace(tool="search_products", ok=False, detail={"code": "missing_query"})
        requirements = normalize_requirements(arguments.get("requirements"))
        bm25_ids = bm25_rank(query, self.case.candidates)
        bm25_position = {candidate_id: position for position, candidate_id in enumerate(bm25_ids)}
        scored: list[tuple[int, int, int, int, str]] = []
        for candidate in self.case.candidates:
            public_id = str(candidate["candidateDisplayId"])
            facts = _candidate_facts(candidate)
            hard_failures = hard_unknowns = soft_passes = 0
            for requirement in requirements:
                status = _requirement_status(facts.get(requirement["key"]), requirement)
                if requirement["priority"] == "hard":
                    hard_failures += int(status == "fail")
                    hard_unknowns += int(status == "unknown")
                else:
                    soft_passes += int(status == "pass")
            scored.append((
                hard_failures,
                hard_unknowns,
                -soft_passes,
                bm25_position.get(public_id, len(bm25_position)),
                public_id,
            ))
        limit = arguments.get("limit", 20)
        limit = limit if type(limit) is int and 1 <= limit <= 20 else 20
        ranked_public = [row[-1] for row in sorted(scored)[:limit]]
        candidate_ids = [self.catalog.internal_for_public(self.case.review_case_id, item) for item in ranked_public]
        return ToolTrace(
            tool="search_products",
            ok=bool(candidate_ids),
            detail={
                "candidateIds": candidate_ids,
                "publicCandidateIds": ranked_public,
                "requirementsApplied": requirements,
                "catalogSize": EXPECTED_CATALOG_COUNT,
                "casePoolSize": len(self.case.candidates),
                "retrievalMode": "public_judged_pool_rerank",
            },
        )

    def _details(self, arguments: Mapping[str, Any]) -> ToolTrace:
        ids = arguments.get("productIds")
        if not isinstance(ids, list) or not ids:
            return ToolTrace(tool="get_product_details", ok=False, detail={"code": "missing_product_ids"})
        products: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        for internal_id in ids:
            if type(internal_id) is not int or internal_id not in self.catalog.item_by_internal_id:
                continue
            candidate = self._public_candidate_for_internal(internal_id)
            if candidate is None:
                continue
            public_id = self.catalog.public_for_internal(self.case.review_case_id, internal_id)
            assert public_id is not None
            facts = _candidate_facts(candidate)
            products.append({
                "id": internal_id,
                "title": candidate.get("candidate", {}).get("title", ""),
                "brand": candidate.get("candidate", {}).get("brand", ""),
                "seller": candidate.get("candidate", {}).get("seller", ""),
                "priceStatus": "unavailable",
                "usedPhoneFacts": facts,
                "candidateDisplayId": public_id,
            })
            evidence.extend(self._evidence(public_id, facts))
        return ToolTrace(
            tool="get_product_details",
            ok=bool(products) and len(products) == len(ids),
            detail={
                "products": products,
                "productIds": [row["id"] for row in products],
                "requestedProductIds": ids,
                "evidence": evidence,
                "evidenceRefs": [row["ref"] for row in evidence],
            },
        )

    def _compare(self, arguments: Mapping[str, Any]) -> ToolTrace:
        ids = arguments.get("productIds")
        if not isinstance(ids, list) or not ids:
            return ToolTrace(tool="compare_products", ok=False, detail={"code": "missing_product_ids"})
        requirements = normalize_requirements(arguments.get("requirements"))
        rows: list[dict[str, Any]] = []
        all_evidence: dict[str, dict[str, Any]] = {}
        eliminated: list[dict[str, Any]] = []
        for recall_position, internal_id in enumerate(ids):
            candidate = self._public_candidate_for_internal(internal_id)
            public_id = self.catalog.public_for_internal(self.case.review_case_id, internal_id)
            if candidate is None or public_id is None:
                continue
            facts = _candidate_facts(candidate)
            evidence = self._evidence(public_id, facts)
            by_group = {item["field"]: item for item in evidence}
            all_evidence.update({item["ref"]: item for item in evidence})
            checks: list[dict[str, Any]] = []
            hard_failures = hard_unknowns = soft_passes = 0
            for requirement in requirements:
                actual = facts.get(requirement["key"])
                status = _requirement_status(actual, requirement)
                if requirement["priority"] == "hard":
                    hard_failures += int(status == "fail")
                    hard_unknowns += int(status == "unknown")
                else:
                    soft_passes += int(status == "pass")
                evidence_item = by_group.get(requirement["key"])
                checks.append({
                    "key": requirement["key"],
                    "operator": requirement["operator"],
                    "expected": requirement["value"],
                    "unit": requirement["unit"],
                    "priority": requirement["priority"],
                    "source": requirement["source"],
                    "actual": actual,
                    "displayValue": actual if actual is not None else "unknown",
                    "status": status,
                    "evidenceRef": evidence_item["ref"] if evidence_item else None,
                })
            if hard_failures:
                eliminated.append({"productId": internal_id, "reason": "explicit_hard_constraint_violation", "checks": checks})
                continue
            soft_count = sum(item["priority"] == "soft" for item in requirements)
            recall_score = 1.0 - (recall_position / max(len(ids), 1))
            soft_score = soft_passes / soft_count if soft_count else 0.0
            evidence_score = len(facts) / len(PUBLIC_FACT_GROUPS)
            final = 0.55 * recall_score + 0.30 * soft_score + 0.15 * evidence_score
            product = {
                "id": internal_id,
                "title": candidate.get("candidate", {}).get("title", ""),
                "brand": candidate.get("candidate", {}).get("brand", ""),
                "seller": candidate.get("candidate", {}).get("seller", ""),
                "priceStatus": "unavailable",
                "usedPhoneFacts": facts,
                "candidateDisplayId": public_id,
            }
            rows.append({
                "product": product,
                "facts": facts,
                "checks": checks,
                "hardFailures": hard_failures,
                "hardUnknowns": hard_unknowns,
                "selectionType": "full_match" if hard_unknowns == 0 else "closest_alternative",
                "scoreBreakdown": {
                    "normalizedRecall": round(recall_score, 8),
                    "softRequirementMatch": round(soft_score, 8),
                    "evidenceCompleteness": round(evidence_score, 8),
                    "final": round(final, 8),
                },
                "evidenceRefs": [item["ref"] for item in evidence],
                "evidence": evidence,
            })
        rows.sort(key=lambda row: (
            row["hardUnknowns"] > 0,
            -row["scoreBreakdown"]["final"],
            row["product"]["id"],
        ))
        selected = rows[:3]
        evidence_list = [all_evidence[key] for key in sorted(all_evidence)]
        return ToolTrace(
            tool="compare_products",
            ok=True,
            detail={
                "products": selected,
                "eliminated": eliminated,
                "evidence": evidence_list,
                "evidenceRefs": [item["ref"] for item in evidence_list],
                "hasCompleteMatch": any(row["hardUnknowns"] == 0 for row in selected),
                "rankingTrace": {
                    "inputCandidateCount": len(ids),
                    "eligibleCandidateCount": len(rows),
                    "eliminatedHardViolationCount": len(eliminated),
                    "mode": "public_evidence_deterministic",
                },
            },
        )


class RecordingCompletions:
    def __init__(self, create, ledger: list[dict[str, Any]]) -> None:  # noqa: ANN001
        self._create = create
        self._ledger = ledger

    async def create(self, **kwargs):
        # The configured DeepSeek model may default to thinking mode, while the
        # production lifecycle relies on a named forced tool_choice.  DeepSeek
        # rejects that combination, so this evaluation client disables thinking
        # only for structured tool calls; no production source is changed.
        if kwargs.get("tools"):
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body["thinking"] = {"type": "disabled"}
            kwargs["extra_body"] = extra_body
        started = time.perf_counter()
        request = {
            "index": len(self._ledger),
            "model": kwargs.get("model"),
            "toolNames": [item.get("function", {}).get("name") for item in kwargs.get("tools", [])],
            "messageSha256": _sha256_text(json.dumps(kwargs.get("messages", []), ensure_ascii=False, sort_keys=True)),
            "stream": bool(kwargs.get("stream")),
            "thinkingDisabledForTools": bool(kwargs.get("tools")),
        }
        try:
            response = await self._create(**kwargs)
        except Exception as exc:
            request.update({
                "ok": False,
                "errorType": type(exc).__name__,
                "durationMs": round((time.perf_counter() - started) * 1000, 2),
            })
            self._ledger.append(request)
            raise
        message = response.choices[0].message if getattr(response, "choices", None) else None
        tool_calls = []
        for call in getattr(message, "tool_calls", None) or []:
            tool_calls.append({
                "name": call.function.name,
                "arguments": call.function.arguments,
            })
        request.update({
            "ok": True,
            "durationMs": round((time.perf_counter() - started) * 1000, 2),
            "response": {
                "content": getattr(message, "content", None),
                "toolCalls": tool_calls,
            },
        })
        self._ledger.append(request)
        return response


class RecordingClient:
    def __init__(self, client: Any) -> None:
        self._client = client
        self.ledger: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=RecordingCompletions(client.chat.completions.create, self.ledger)
        )

    def fork(self) -> "RecordingClient":
        """Give one case a private ledger while sharing only the raw SDK client."""

        return RecordingClient(self._client)


def make_real_client() -> RecordingClient:
    _validated_llm_endpoint(settings.deepseek_base_url)
    if not settings.deepseek_api_key:
        raise CompositionError("DEEPSEEK_API_KEY is not configured")
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=30.0,
        max_retries=2,
    )
    return RecordingClient(client)


def _tool_schemas() -> list[dict[str, Any]]:
    schemas = [
        schema for schema in _production_runtime().tool_schemas
        if schema["function"]["name"] in ALLOWED_TOOL_NAMES
    ]
    if {schema["function"]["name"] for schema in schemas} != ALLOWED_TOOL_NAMES:
        raise CompositionError("production product tool schema set is incomplete")
    return schemas


def _requirements_from_extractor(
    extractor_calls: Sequence[Mapping[str, Any]],
    state: TaskState,
) -> list[dict[str, Any]]:
    """Merge only this case's turn-local extractor proposals, then final state."""

    merged: dict[str, dict[str, Any]] = {}
    for call in extractor_calls:
        for tool_call in call.get("response", {}).get("toolCalls", []):
            if tool_call.get("name") != "update_task_state":
                continue
            try:
                arguments = json.loads(tool_call.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            guide = arguments.get("domainStatePatch", {}).get("shoppingGuide", {})
            normalized = normalize_requirements(guide.get("requirements"))
            for requirement in normalized:
                merged[requirement["key"]] = requirement
            for removed in arguments.get("removeConstraintKeys", []):
                group = _normalize_group(removed)
                if group is not None:
                    merged.pop(group, None)
    for constraint in state.constraints:
        group = _normalize_group(constraint.key)
        if group is None:
            continue
        normalized = normalize_requirements([{
            "key": group,
            "operator": constraint.operator,
            "value": constraint.value,
            "unit": "enum",
            "priority": "hard",
            "source": constraint.source,
        }])
        if normalized:
            if group in merged:
                normalized[0]["priority"] = merged[group]["priority"]
            merged[group] = normalized[0]
    return [merged[key] for key in sorted(merged)]


def _visible_context_facts(case: PublicCase) -> list[TaskFact]:
    facts: list[TaskFact] = []
    candidates = {item["candidateDisplayId"]: item for item in case.candidates}
    for index, context in enumerate(case.user_visible_candidate_context):
        candidate_id = context.get("candidateDisplayId")
        candidate = candidates.get(candidate_id)
        if candidate is None:
            continue
        facts.append(TaskFact(
            key=f"public_candidate_context_{index + 1}",
            value={
                "label": context.get("label"),
                "candidateDisplayId": candidate_id,
                "candidate": candidate.get("candidate"),
                "evidenceRefs": candidate.get("evidenceRefs"),
            },
            source="system",
        ))
    return facts


def _transition_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, mode="json")
    if isinstance(value, dict):
        return {key: _transition_dump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_transition_dump(item) for item in value]
    return value


def _projector_tool_schema() -> dict[str, Any]:
    candidate_id = {"type": "string", "pattern": "^BLIND-CASE-00[1-8]-CAND-[0-9a-f]{12}$"}
    evidence_id = {"type": "string", "pattern": "^evidence-[0-9]{3,}$"}
    fact_group = {"enum": list(PUBLIC_FACT_GROUPS)}
    normalized_value = {"enum": sorted(NORMALIZED_VALUES)}
    constraint = {
        "type": "object",
        "additionalProperties": False,
        "required": ["group", "operator", "allowedValues", "importance", "missingTreatment", "evidenceRule"],
        "properties": {
            "group": fact_group,
            "operator": {"enum": ["IN", "NOT_IN"]},
            "allowedValues": {
                "type": "array", "items": normalized_value, "minItems": 1, "uniqueItems": True,
            },
            "importance": {"enum": ["hard", "soft"]},
            "missingTreatment": {"const": "unknown_not_recommendable"},
            "evidenceRule": {"const": "attr_primary_title_conflict_check"},
        },
    }
    issue = {
        "type": "object",
        "additionalProperties": False,
        "required": ["group"],
        "properties": {"group": fact_group},
    }
    assessment_fact = {
        "type": "object",
        "additionalProperties": False,
        "required": ["group", "normalizedValue"],
        "properties": {"group": fact_group, "normalizedValue": normalized_value},
    }
    evidence_selection = {
        "type": "object",
        "additionalProperties": False,
        "required": ["group", "evidenceRefId"],
        "properties": {"group": fact_group, "evidenceRefId": evidence_id},
    }
    assessment = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidateDisplayId", "eligible", "hardViolations", "hardUnknowns",
            "assessmentFacts", "evidenceSelections",
        ],
        "properties": {
            "candidateDisplayId": candidate_id,
            "eligible": {"enum": [True, False, "unknown"]},
            "hardViolations": {"type": "array", "items": issue, "uniqueItems": True},
            "hardUnknowns": {"type": "array", "items": issue, "uniqueItems": True},
            "assessmentFacts": {"type": "array", "items": assessment_fact, "uniqueItems": True},
            "evidenceSelections": {
                "type": "array", "items": evidence_selection, "uniqueItems": True,
            },
        },
    }
    field_comparison = {
        "type": "object",
        "additionalProperties": False,
        "required": ["field", "candidateValues", "missingEvidenceCandidateIds", "evidenceRefIds"],
        "properties": {
            "field": fact_group,
            "candidateValues": {
                "type": "object", "minProperties": 2, "maxProperties": 2,
                "propertyNames": candidate_id, "additionalProperties": normalized_value,
            },
            "missingEvidenceCandidateIds": {
                "type": "array", "items": candidate_id, "uniqueItems": True,
            },
            "evidenceRefIds": {"type": "array", "items": evidence_id, "uniqueItems": True},
        },
    }
    tradeoff = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "field", "advantagedCandidateDisplayId", "preferredCandidateDisplayId",
            "advantagedValue", "preferredValue", "disclosureType",
        ],
        "properties": {
            "field": fact_group,
            "advantagedCandidateDisplayId": candidate_id,
            "preferredCandidateDisplayId": candidate_id,
            "advantagedValue": normalized_value,
            "preferredValue": normalized_value,
            "disclosureType": {"const": "preferred_candidate_loses_dimension"},
        },
    }
    comparison = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidateDisplayIds", "preferredCandidateDisplayId", "fieldComparisons",
            "tradeoffDisclosures", "missingEvidenceStated",
        ],
        "properties": {
            "candidateDisplayIds": {
                "type": "object", "additionalProperties": False,
                "required": ["candidateA", "candidateB"],
                "properties": {"candidateA": candidate_id, "candidateB": candidate_id},
            },
            "preferredCandidateDisplayId": candidate_id,
            "fieldComparisons": {
                "type": "array", "items": field_comparison, "minItems": 1, "uniqueItems": True,
            },
            "tradeoffDisclosures": {"type": "array", "items": tradeoff, "uniqueItems": True},
            "missingEvidenceStated": {"type": "boolean"},
        },
    }
    claim_selection = {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidateDisplayId", "factGroup", "normalizedValue", "evidenceRefIds"],
        "properties": {
            "candidateDisplayId": candidate_id,
            "factGroup": fact_group,
            "normalizedValue": normalized_value,
            "evidenceRefIds": {
                "type": "array", "items": evidence_id, "minItems": 1, "uniqueItems": True,
            },
        },
    }
    return {
        "type": "function",
        "function": {
            "name": "submit_used_phone_selection",
            "description": "Select public semantic output and evidence IDs; deterministic code builds citations.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "predictedAction", "extractedConstraints", "rankedCandidateIds",
                    "candidateAssessments", "comparison", "stateUpdate", "claimSelections",
                ],
                "properties": {
                    "predictedAction": {"enum": list(FORMAL_ACTIONS)},
                    "extractedConstraints": {"type": "array", "items": constraint},
                    "rankedCandidateIds": {
                        "type": "array", "items": candidate_id, "uniqueItems": True,
                    },
                    "candidateAssessments": {
                        "type": "array", "items": assessment, "uniqueItems": True,
                    },
                    "comparison": {"oneOf": [{"type": "null"}, comparison]},
                    "stateUpdate": {"type": ["object", "null"]},
                    "claimSelections": {
                        "type": "array", "items": claim_selection, "uniqueItems": True,
                    },
                },
            },
        },
    }


def _public_case_payload(case: PublicCase) -> dict[str, Any]:
    citation_options: list[dict[str, Any]] = []
    option_index = 1
    for candidate in case.candidates:
        candidate_id = str(candidate["candidateDisplayId"])
        for ref_index, ref in enumerate(candidate.get("evidenceRefs", [])):
            for group in PUBLIC_FACT_GROUPS:
                normalized = normalize_public_fact(group, ref.get("rawValue"))
                if normalized is None:
                    continue
                citation_options.append({
                    "evidenceRefId": f"evidence-{option_index:03d}",
                    "candidateDisplayId": candidate_id,
                    "evidenceRefIndex": ref_index,
                    "field": ref.get("field"),
                    "rawValueSha256": hashlib.sha256(
                        canonical_json_bytes(ref.get("rawValue"))
                    ).hexdigest(),
                    "factGroup": group,
                    "normalizedValue": normalized,
                })
                option_index += 1
    return {
        "reviewCaseId": case.review_case_id,
        "messages": list(case.query_messages),
        "userVisibleCandidateContext": list(case.user_visible_candidate_context),
        "candidates": list(case.candidates),
        "evidenceReferenceOptions": citation_options,
    }


def _actual_tool_candidate_ids(
    tool_calls: Sequence[Mapping[str, Any]],
    *,
    tool_name: str | None = None,
) -> set[str]:
    candidate_ids: set[str] = set()
    for call in tool_calls:
        if tool_name is not None and call.get("toolName") != tool_name:
            continue
        trace = call.get("trace", {})
        if trace.get("ok") is not True:
            continue
        detail = trace.get("detail", {})
        for candidate_id in detail.get("publicCandidateIds", []):
            if isinstance(candidate_id, str):
                candidate_ids.add(candidate_id)
        for row in detail.get("products", []):
            if not isinstance(row, Mapping):
                continue
            candidate_id = row.get("candidateDisplayId")
            if not isinstance(candidate_id, str) and isinstance(row.get("product"), Mapping):
                candidate_id = row["product"].get("candidateDisplayId")
            if isinstance(candidate_id, str):
                candidate_ids.add(candidate_id)
    return candidate_ids


def _actual_compare_evidence(
    tool_calls: Sequence[Mapping[str, Any]],
) -> set[tuple[str, int, str, str]]:
    """Return candidate/ref/group/value identities explicitly present in successful compare traces."""

    identities: set[tuple[str, int, str, str]] = set()
    reference_pattern = re.compile(
        r"^public:(BLIND-CASE-00[1-8]-CAND-[0-9a-f]{12}):(\d+):([^:]+)$"
    )
    for call in tool_calls:
        if call.get("toolName") != "compare_products":
            continue
        trace = call.get("trace", {})
        if trace.get("ok") is not True:
            continue
        detail = trace.get("detail", {})
        declared_refs = {
            ref for ref in detail.get("evidenceRefs", []) if isinstance(ref, str)
        }
        for evidence in detail.get("evidence", []):
            if not isinstance(evidence, Mapping):
                continue
            reference = evidence.get("ref")
            if declared_refs and reference not in declared_refs:
                continue
            candidate_id = evidence.get("candidateDisplayId")
            ref_index = evidence.get("evidenceRefIndex")
            group = evidence.get("field")
            value = evidence.get("value")
            if (
                isinstance(candidate_id, str) and type(ref_index) is int
                and isinstance(group, str) and isinstance(value, str)
            ):
                identities.add((candidate_id, ref_index, group, value))
        for row in detail.get("products", []):
            if not isinstance(row, Mapping):
                continue
            product = row.get("product") if isinstance(row.get("product"), Mapping) else row
            candidate_id = product.get("candidateDisplayId") if isinstance(product, Mapping) else None
            for check in row.get("checks", []):
                if not isinstance(check, Mapping) or not isinstance(check.get("evidenceRef"), str):
                    continue
                match = reference_pattern.fullmatch(check["evidenceRef"])
                if match is None or match.group(1) != candidate_id:
                    continue
                group = str(check.get("key") or match.group(3))
                value = check.get("actual")
                if group == match.group(3) and isinstance(value, str):
                    identities.add((candidate_id, int(match.group(2)), group, value))
    return identities


def _materialize_selection(
    selection: Mapping[str, Any],
    *,
    case: PublicCase,
    tool_calls: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Expand only model-selected public evidence IDs into the canonical prediction shape."""

    Draft202012Validator(_projector_tool_schema()["function"]["parameters"]).validate(selection)
    public_options = {
        option["evidenceRefId"]: option for option in _public_case_payload(case)["evidenceReferenceOptions"]
    }
    public_candidates = {str(row["candidateDisplayId"]) for row in case.candidates}
    actual_candidates = _actual_tool_candidate_ids(tool_calls)
    compare_candidates = _actual_tool_candidate_ids(tool_calls, tool_name="compare_products")
    compare_evidence = _actual_compare_evidence(tool_calls)
    visible_candidates = {
        str(row.get("candidateDisplayId"))
        for row in case.user_visible_candidate_context
        if row.get("candidateDisplayId") is not None
    }
    ranked = list(selection["rankedCandidateIds"])
    assessments = list(selection["candidateAssessments"])
    if (ranked or assessments) and not actual_candidates:
        raise CompositionError("selection cannot rank or assess without actual tool results")
    for candidate_id in ranked:
        if candidate_id not in public_candidates:
            raise CompositionError(f"ranked candidate is outside current case: {candidate_id}")
        if candidate_id not in actual_candidates:
            raise CompositionError(f"ranked candidate absent from actual tool results: {candidate_id}")
    for assessment in assessments:
        if assessment["candidateDisplayId"] not in public_candidates:
            raise CompositionError("assessment candidate is outside current case")
        if assessment["candidateDisplayId"] not in actual_candidates:
            raise CompositionError("assessment candidate absent from actual tool results")

    selected_ids: set[str] = set()

    def resolve_refs(
        evidence_ids: Sequence[str],
        *,
        candidate_id: str | None = None,
        fact_group: str | None = None,
        normalized_value: str | None = None,
    ) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for evidence_id in evidence_ids:
            option = public_options.get(evidence_id)
            if option is None:
                raise CompositionError(f"selection returned unknown evidenceRefId: {evidence_id}")
            if candidate_id is not None and option["candidateDisplayId"] != candidate_id:
                raise CompositionError("selected evidence candidate mismatch")
            if fact_group is not None and option["factGroup"] != fact_group:
                raise CompositionError("selected evidence factGroup mismatch")
            if normalized_value is not None and option["normalizedValue"] != normalized_value:
                raise CompositionError("selected evidence normalizedValue mismatch")
            selected_ids.add(evidence_id)
            resolved.append({key: value for key, value in option.items() if key != "evidenceRefId"})
        return resolved

    materialized_assessments: list[dict[str, Any]] = []
    for assessment in assessments:
        candidate_id = assessment["candidateDisplayId"]
        issue_groups = {
            item["group"] for item in assessment["hardViolations"] + assessment["hardUnknowns"]
        }
        fact_values: dict[str, str] = {}
        for fact in assessment["assessmentFacts"]:
            if fact["group"] in fact_values:
                raise CompositionError("assessmentFacts contains duplicate group")
            fact_values[fact["group"]] = fact["normalizedValue"]
        declared_groups = issue_groups | set(fact_values)
        citations: list[dict[str, Any]] = []
        selected_fact_groups: set[str] = set()
        for evidence_selection in assessment["evidenceSelections"]:
            group = evidence_selection["group"]
            if group not in declared_groups:
                raise CompositionError("assessment evidence has no declared group claim")
            citations.extend(resolve_refs(
                [evidence_selection["evidenceRefId"]],
                candidate_id=candidate_id,
                fact_group=group,
                normalized_value=fact_values.get(group),
            ))
            if group in fact_values:
                selected_fact_groups.add(group)
        if set(fact_values) - selected_fact_groups:
            raise CompositionError("assessment fact lacks an explicit evidence selection")
        materialized_assessments.append({
            "candidateDisplayId": candidate_id,
            "eligible": assessment["eligible"],
            "hardViolations": deepcopy(assessment["hardViolations"]),
            "hardUnknowns": deepcopy(assessment["hardUnknowns"]),
            "evidenceCitations": citations,
        })

    materialized_comparison = None
    comparison = selection["comparison"]
    if comparison is not None:
        pair = comparison["candidateDisplayIds"]
        pair_ids = {pair["candidateA"], pair["candidateB"]}
        if len(pair_ids) != 2 or not pair_ids.issubset(public_candidates):
            raise CompositionError("comparison requires two distinct current-case candidates")
        if not pair_ids.issubset(visible_candidates) or not pair_ids.issubset(compare_candidates):
            raise CompositionError("comparison candidates require user-visible A/B and actual compare results")
        if comparison["preferredCandidateDisplayId"] not in pair_ids:
            raise CompositionError("comparison preferred candidate is outside A/B")
        fields: list[dict[str, Any]] = []
        field_by_group: dict[str, Mapping[str, Any]] = {}
        for field in comparison["fieldComparisons"]:
            if field["field"] in field_by_group:
                raise CompositionError("comparison contains duplicate field")
            if set(field["candidateValues"]) != pair_ids:
                raise CompositionError("comparison candidateValues must match A/B")
            missing_ids = set(field["missingEvidenceCandidateIds"])
            if not missing_ids.issubset(pair_ids):
                raise CompositionError("comparison missing-evidence candidate is outside A/B")
            citations: list[dict[str, Any]] = []
            cited_candidates: set[str] = set()
            for evidence_id in field["evidenceRefIds"]:
                option = public_options.get(evidence_id)
                if option is None:
                    raise CompositionError(f"selection returned unknown evidenceRefId: {evidence_id}")
                candidate_id = option["candidateDisplayId"]
                if candidate_id not in pair_ids:
                    raise CompositionError("comparison evidence candidate mismatch")
                identity = (
                    candidate_id, option["evidenceRefIndex"],
                    option["factGroup"], option["normalizedValue"],
                )
                if identity not in compare_evidence:
                    raise CompositionError("comparison evidence absent from actual compare trace")
                citations.extend(resolve_refs(
                    [evidence_id], candidate_id=candidate_id, fact_group=field["field"],
                    normalized_value=field["candidateValues"][candidate_id],
                ))
                cited_candidates.add(candidate_id)
            for candidate_id, value in field["candidateValues"].items():
                if value == "unknown":
                    if candidate_id not in missing_ids:
                        raise CompositionError("unknown comparison value must be marked missing")
                else:
                    if candidate_id in missing_ids:
                        raise CompositionError("value-backed comparison candidate cannot be marked missing")
                    if candidate_id not in cited_candidates:
                        raise CompositionError("value-backed comparison field lacks actual compare evidence")
            fields.append({
                "field": field["field"],
                "candidateValues": deepcopy(field["candidateValues"]),
                "missingEvidenceCandidateIds": list(field["missingEvidenceCandidateIds"]),
                "evidenceCitations": citations,
            })
            field_by_group[field["field"]] = field
        for disclosure in comparison["tradeoffDisclosures"]:
            field = field_by_group.get(disclosure["field"])
            if field is None:
                raise CompositionError("tradeoff disclosure has no selected comparison field")
            advantaged = disclosure["advantagedCandidateDisplayId"]
            preferred = disclosure["preferredCandidateDisplayId"]
            if advantaged not in pair_ids or preferred not in pair_ids:
                raise CompositionError("tradeoff disclosure candidate is outside A/B")
            if (
                field["candidateValues"][advantaged] != disclosure["advantagedValue"]
                or field["candidateValues"][preferred] != disclosure["preferredValue"]
            ):
                raise CompositionError("tradeoff disclosure value mismatch")
        materialized_comparison = {
            "candidateDisplayIds": deepcopy(pair),
            "preferredCandidateDisplayId": comparison["preferredCandidateDisplayId"],
            "fieldComparisons": fields,
            "tradeoffDisclosures": deepcopy(comparison["tradeoffDisclosures"]),
            "missingEvidenceStated": comparison["missingEvidenceStated"],
        }

    claims: list[dict[str, Any]] = []
    for index, claim in enumerate(selection["claimSelections"], 1):
        candidate_id = claim["candidateDisplayId"]
        if candidate_id not in public_candidates:
            raise CompositionError("claim candidate is outside current case")
        if candidate_id not in actual_candidates | visible_candidates:
            raise CompositionError("claim candidate absent from actual tool or user-visible context")
        resolve_refs(
            claim["evidenceRefIds"], candidate_id=candidate_id,
            fact_group=claim["factGroup"], normalized_value=claim["normalizedValue"],
        )
        claims.append({
            "claimId": f"claim-{index:03d}",
            "claimType": "candidate_fact",
            "candidateDisplayId": candidate_id,
            "factGroup": claim["factGroup"],
            "normalizedValue": claim["normalizedValue"],
            "text": (
                f"candidate_fact:{candidate_id}:{claim['factGroup']}={claim['normalizedValue']}"
            ),
            "evidenceRefIds": list(claim["evidenceRefIds"]),
        })

    return {
        "predictedAction": selection["predictedAction"],
        "extractedConstraints": deepcopy(selection["extractedConstraints"]),
        "rankedCandidateIds": ranked,
        "candidateAssessments": materialized_assessments,
        "comparison": materialized_comparison,
        "stateUpdate": deepcopy(selection["stateUpdate"]),
        "answerClaims": claims,
        "evidenceRefs": [deepcopy(public_options[evidence_id]) for evidence_id in sorted(selected_ids)],
    }


def _public_semantic_validate(prediction: Mapping[str, Any], case: PublicCase) -> None:
    public_candidates = {str(row["candidateDisplayId"]): row for row in case.candidates}
    for candidate_id in prediction.get("rankedCandidateIds", []):
        if candidate_id not in public_candidates:
            raise CompositionError(f"projector returned out-of-pool candidate: {candidate_id}")
    def validate_citation(evidence: Mapping[str, Any]) -> None:
        candidate_id = evidence.get("candidateDisplayId")
        candidate = public_candidates.get(candidate_id)
        index = evidence.get("evidenceRefIndex")
        if candidate is None or type(index) is not int or not 0 <= index < len(candidate.get("evidenceRefs", [])):
            raise CompositionError("projector returned out-of-bounds evidence reference")
        public_ref = candidate["evidenceRefs"][index]
        raw_value = public_ref.get("rawValue")
        expected_hash = hashlib.sha256(canonical_json_bytes(raw_value)).hexdigest()
        if evidence.get("rawValueSha256") != expected_hash:
            raise CompositionError("projector evidence rawValue hash mismatch")
        if evidence.get("field") != public_ref.get("field"):
            raise CompositionError("projector evidence field mismatch")
        expected_value = normalize_public_fact(str(evidence.get("factGroup")), raw_value)
        if evidence.get("normalizedValue") != expected_value:
            raise CompositionError("projector evidence normalized value mismatch")

    evidence_ids: set[str] = set()
    for evidence in prediction.get("evidenceRefs", []):
        validate_citation(evidence)
        evidence_ids.add(str(evidence.get("evidenceRefId")))
    for assessment in prediction.get("candidateAssessments", []):
        for citation in assessment.get("evidenceCitations", []):
            validate_citation(citation)
    comparison = prediction.get("comparison")
    if isinstance(comparison, Mapping):
        for field_comparison in comparison.get("fieldComparisons", []):
            for citation in field_comparison.get("evidenceCitations", []):
                validate_citation(citation)
    for claim in prediction.get("answerClaims", []):
        if not set(claim.get("evidenceRefIds", [])).issubset(evidence_ids):
            raise CompositionError("projector claim cites an unknown evidenceRefId")


async def _project_prediction(
    client: RecordingClient,
    *,
    case: PublicCase,
    snapshots: list[dict[str, Any]],
    graph_trace: dict[str, Any] | None,
    tool_calls: list[dict[str, Any]],
    model_name: str,
    prediction_schema: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    public_payload = _public_case_payload(case)
    observed = {
        "taskStateSnapshots": snapshots,
        "graph": graph_trace,
        "toolCalls": tool_calls,
    }
    system = (
        "You are a label-blind evaluation projector. Use only the supplied public query, public raw evidence, "
        "and observed runtime state/trace. Never infer hidden labels. Missing evidence is unknown, not a pass. "
        "Return only the smaller public selection envelope. Every public evidence option already provides its "
        "stable evidenceRefId, candidate/ref index, field, fact group, normalized value, and raw-value hash. "
        "Select evidenceRefIds; never calculate hashes or construct citation objects. Deterministic code will "
        "expand only IDs you explicitly select and will create canonical claim text. If observed.toolCalls has "
        "no successful result, rankedCandidateIds and candidateAssessments must be empty. A comparison requires "
        "the user-visible A/B pair and a successful observed compare_products result; otherwise comparison is null. "
        "For each assessment, declare group-only hard violations/unknowns or explicit assessmentFacts, then bind "
        "every evidence selection as {group,evidenceRefId}; do not cite a group you did not declare. Comparison "
        "field values and evidence IDs must be present in the actual compare trace, not merely the public table. "
        "Do not infer ranking or assessments from the public candidate table alone. Return exactly one "
        "submit_used_phone_selection tool call."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"public": public_payload, "observed": observed}, ensure_ascii=False)},
    ]
    tool = _projector_tool_schema()
    last_error = ""
    for attempt in range(2):
        selected = None
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": "submit_used_phone_selection"}},
            )
            calls = response.choices[0].message.tool_calls or []
            selected = next(
                (call for call in calls if call.function.name == "submit_used_phone_selection"), None
            )
            if selected is None:
                raise CompositionError("projector omitted submit_used_phone_selection")
            selection = json.loads(selected.function.arguments or "{}")
            payload = _materialize_selection(selection, case=case, tool_calls=tool_calls)
            prediction = {
                "schemaVersion": PREDICTION_SCHEMA_VERSION,
                "reviewCaseId": case.review_case_id,
                **payload,
                "provenance": {
                    "producerName": COMPOSITION_NAME,
                    "producerType": "agent",
                    "evaluationOnly": True,
                    "networkUsed": True,
                    "modelUsed": True,
                    "humanGold": False,
                    "model": model_name,
                    "projectionProtocolVersion": PROJECTION_PROTOCOL_VERSION,
                },
            }
            Draft202012Validator(prediction_schema).validate(prediction)
            _public_semantic_validate(prediction, case)
            return prediction, attempt
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
            if selected is None:
                raise CompositionError(
                    f"projector request failed before a repairable selection: {last_error}"
                ) from exc
            if attempt == 0:
                tool_call_id = str(getattr(selected, "id", None) or "projector-invalid-call")
                raw_arguments = selected.function.arguments
                messages.extend([
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": "submit_used_phone_selection",
                                "arguments": raw_arguments,
                            },
                        }],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": (
                            "Public schema/semantic validation failed: "
                            f"{last_error}. Repair this exact JSON once; do not add unsupported evidence."
                        ),
                    },
                ])
    raise CompositionError(f"projector validation failed after one repair: {last_error}")


@dataclass
class CaseRunResult:
    prediction: dict[str, Any]
    trace: dict[str, Any]


async def run_case(
    *,
    case: PublicCase,
    catalog: CatalogBinding,
    client: RecordingClient,
    model_name: str,
    prediction_schema: Mapping[str, Any],
) -> CaseRunResult:
    runtime = _production_runtime()
    snapshots: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    graph_trace: dict[str, Any] | None = None
    trace_builder: Any | None = None
    offline_tool = OfflineCatalogToolCaller(case, catalog)
    extractor_calls: list[dict[str, Any]] = []
    with in_memory_task_state_store():
        state = await create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal=str(case.query_messages[0].get("text", "used phone evaluation")),
            sessionId=f"eval-{case.review_case_id.casefold()}",
            facts=_visible_context_facts(case),
            domainState={"origin": "evaluation_public", "turnCount": 0},
        ))

        async def capture_state(updated: TaskState, phase: str) -> None:
            snapshots.append({
                "phase": phase,
                "state": updated.model_dump(by_alias=True, mode="json"),
            })

        for turn_index, message in enumerate(case.query_messages):
            text = str(message.get("text", ""))
            call_start = len(client.ledger)
            state = await runtime.update_task_state(
                text,
                history=history,
                client=client,
                task_state=state,
                on_task_state=capture_state,
            )
            extractor_calls.extend(deepcopy(client.ledger[call_start:]))
            history.append({"role": "user", "content": text})
            if turn_index < len(case.query_messages) - 1:
                snapshots.append({
                    "phase": "evaluation_intermediate_turn_deferred",
                    "adapterLimitation": True,
                    "state": state.model_dump(by_alias=True, mode="json"),
                })
                continue

            schemas = _tool_schemas()
            pack = await runtime.build_context_pack(
                state,
                allowed_tools=[schema["function"]["name"] for schema in schemas],
                history=history[:-1],
                run_id=f"eval-{case.review_case_id.casefold()}",
            )
            projector = runtime.ContextProjector(pack)
            trace_builder = runtime.TraceBuilder(f"eval-{case.review_case_id.casefold()}", mode="context_pack")
            trace_builder.set_context(state.task_id, state.session_id)
            trace_builder.set_context_pack(
                runtime.context_pack_hash(pack),
                runtime.context_pack_token_count(pack),
            )
            trace_builder.set_revision_before(state.revision)
            trace_builder.set_base_context_revision(state.revision)
            requirements = _requirements_from_extractor(extractor_calls, state)

            async def on_transition(result) -> None:  # noqa: ANN001
                snapshots.append({
                    "phase": f"graph_{result.action}",
                    "state": result.task_state.model_dump(by_alias=True, mode="json"),
                })

            graph_state = await runtime.run_controlled_react_graph(
                state,
                runtime.ReActGraphRuntime(
                    user_message=text,
                    client=client,
                    model=model_name,
                    resolve_tool_schemas=lambda _current: schemas,
                    step_runner=runtime.run_harness_step,
                    tool_caller=offline_tool,
                    trace_builder=trace_builder,
                    projector=projector,
                    max_transitions=8,
                    system_policies={
                        "searchCategory": "手机",
                        "canonicalCategory": "phone",
                        "requirements": requirements,
                        "maxReplanAttempts": 3,
                    },
                    on_transition=on_transition,
                ),
            )
            graph_trace = _transition_dump(graph_state)

    prediction, repair_count = await _project_prediction(
        client,
        case=case,
        snapshots=snapshots,
        graph_trace=graph_trace,
        tool_calls=offline_tool.calls,
        model_name=model_name,
        prediction_schema=prediction_schema,
    )
    full_trace = trace_builder.finish().model_dump(by_alias=True, mode="json") if trace_builder else None
    return CaseRunResult(
        prediction=prediction,
        trace={
            "schemaVersion": "used-phone-agent-trace-v1",
            "reviewCaseId": case.review_case_id,
            "composition": COMPOSITION_NAME,
            "adapterLimitations": [
                "not_full_production_agent_entry",
                "public_judged_pool_rerank_over_pinned_252_catalog",
                "non_final_multiturn_turns_run_state_extractor_only",
                "prediction_projector_is_evaluation_only",
                "deepseek_thinking_disabled_for_structured_tool_calls",
            ],
            "stateSnapshots": snapshots,
            "graph": graph_trace,
            "toolCalls": offline_tool.calls,
            "agentTrace": full_trace,
            "modelCalls": deepcopy(client.ledger),
            "projectorFormatRepairCount": repair_count,
            "projectionProtocolVersion": PROJECTION_PROTOCOL_VERSION,
        },
    )


def _atomic_write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return digest


def _json_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value)


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(row) for row in rows)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _success_record(result: CaseRunResult, attempt: int) -> dict[str, Any]:
    return {
        "schemaVersion": "used-phone-agent-case-success-v1",
        "terminalState": "success",
        "attempt": attempt,
        "predictionSha256": hashlib.sha256(_json_bytes(result.prediction)).hexdigest(),
        "traceSha256": hashlib.sha256(_json_bytes(result.trace)).hexdigest(),
        "prediction": result.prediction,
        "trace": result.trace,
    }


def _load_success_record(path: Path) -> CaseRunResult:
    record = _load_json(path)
    if record.get("terminalState") != "success":
        raise CompositionError(f"invalid success terminal marker: {path}")
    prediction = record.get("prediction")
    trace = record.get("trace")
    if not isinstance(prediction, dict) or not isinstance(trace, dict):
        raise CompositionError(f"invalid success payload: {path}")
    if record.get("predictionSha256") != hashlib.sha256(_json_bytes(prediction)).hexdigest():
        raise CompositionError(f"success prediction hash mismatch: {path}")
    if record.get("traceSha256") != hashlib.sha256(_json_bytes(trace)).hexdigest():
        raise CompositionError(f"success trace hash mismatch: {path}")
    if trace.get("projectionProtocolVersion") != PROJECTION_PROTOCOL_VERSION:
        raise CompositionError(f"incompatible success projection protocol: {path}")
    provenance = prediction.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("producerName") != COMPOSITION_NAME
        or provenance.get("projectionProtocolVersion") != PROJECTION_PROTOCOL_VERSION
    ):
        raise CompositionError(f"incompatible success prediction provenance: {path}")
    return CaseRunResult(prediction=prediction, trace=trace)


def _latest_prior_terminal(case_root: Path, attempt: int) -> tuple[str, Path] | None:
    terminals: list[tuple[int, str, Path]] = []
    if not case_root.is_dir():
        return None
    for directory in case_root.glob("attempt-*"):
        match = re.fullmatch(r"attempt-(\d{3,})", directory.name)
        if not match or int(match.group(1)) >= attempt:
            continue
        success = directory / "success.json"
        failure = directory / "failure.json"
        if success.is_file() and failure.is_file():
            raise CompositionError(f"non-exclusive terminal markers: {directory}")
        if success.is_file():
            terminals.append((int(match.group(1)), "success", success))
        elif failure.is_file():
            terminals.append((int(match.group(1)), "failure", failure))
    if not terminals:
        return None
    _, state, path = max(terminals, key=lambda item: item[0])
    return state, path


def _assert_new_attempt_directory(case_dir: Path) -> None:
    terminal_names = ("running.json", "success.json", "failure.json")
    existing = [name for name in terminal_names if (case_dir / name).exists()]
    if existing:
        raise CompositionError(
            f"attempt directory already has state {existing}; use a new --attempt"
        )


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> str:
    """Write exactly the object returned to the caller; SHA stays external."""

    return _atomic_write(path, _json_bytes(manifest))


def _code_hashes() -> dict[str, str]:
    repository = Path(__file__).resolve().parents[2]
    agent_root = repository / "agent"
    production_paths = sorted(
        (agent_root / "app").rglob("*.py"),
        key=lambda path: path.relative_to(repository).as_posix(),
    )
    evaluation_paths = [
        Path(__file__).resolve(),
        agent_root / "scripts" / "run_used_phone_model_harness.py",
        agent_root / "tests" / "test_used_phone_model_harness.py",
        agent_root / "evaluation" / "used_phone_offline_runtime.py",
        agent_root / "evaluation" / "schemas" / "used_phone_complex_prediction_v1.schema.json",
    ]
    paths = [*production_paths, *evaluation_paths]
    return {path.relative_to(repository).as_posix(): sha256_file(path) for path in paths}


async def run_public_model_composition(
    *,
    public_bundle_dir: str | Path,
    evidence_audit_path: str | Path,
    run_dir: str | Path,
    case_ids: Sequence[str] | None = None,
    attempt: int = 1,
    resume_failed: bool = False,
    client: RecordingClient | None = None,
) -> dict[str, Any]:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    run_path = Path(run_dir).resolve()
    with public_isolation_guard():
        bundle = load_public_bundle(public_bundle_dir)
        catalog = CatalogBinding.load(evidence_audit_path, bundle)
    selected = set(case_ids or [case.review_case_id for case in bundle.cases])
    unknown = selected - set(bundle.by_id())
    if unknown:
        raise ValueError(f"unknown case ids: {sorted(unknown)}")
    schema_path = Path(__file__).resolve().parent / "schemas" / "used_phone_complex_prediction_v1.schema.json"
    with public_isolation_guard():
        prediction_schema = _load_json(schema_path)
    Draft202012Validator.check_schema(prediction_schema)
    model_name = settings.deepseek_model
    endpoint = _validated_llm_endpoint(settings.deepseek_base_url)
    recording_client = client or make_real_client()
    case_results: dict[str, CaseRunResult] = {}
    failures: dict[str, dict[str, Any]] = {}
    new_logical_calls = 0
    resumed_logical_calls = 0

    with public_isolation_guard(), business_call_guard(), llm_endpoint_network_guard(settings.deepseek_base_url):
        for case in bundle.cases:
            if case.review_case_id not in selected:
                continue
            case_root = run_path / "cases" / case.review_case_id
            case_dir = case_root / f"attempt-{attempt:03d}"
            _assert_new_attempt_directory(case_dir)
            if resume_failed:
                prior = _latest_prior_terminal(case_root, attempt)
                if prior is not None and prior[0] == "success":
                    resumed = _load_success_record(prior[1])
                    case_results[case.review_case_id] = resumed
                    resumed_logical_calls += len(resumed.trace.get("modelCalls", []))
                    continue
            running_path = case_dir / "running.json"
            _atomic_write(running_path, _json_bytes({
                "schemaVersion": "used-phone-agent-case-running-v1",
                "terminalState": "running",
                "reviewCaseId": case.review_case_id,
                "attempt": attempt,
                "startedAt": datetime.now(timezone.utc).isoformat(),
            }))
            case_client = recording_client.fork()
            try:
                result = await run_case(
                    case=case,
                    catalog=catalog,
                    client=case_client,
                    model_name=model_name,
                    prediction_schema=prediction_schema,
                )
                result.trace["modelCalls"] = deepcopy(case_client.ledger)
                new_logical_calls += len(case_client.ledger)
                case_results[case.review_case_id] = result
                _atomic_write(case_dir / "success.json", _json_bytes(_success_record(result, attempt)))
                running_path.unlink()
            except Exception as exc:
                new_logical_calls += len(case_client.ledger)
                failure = {
                    "schemaVersion": "used-phone-agent-case-failure-v1",
                    "terminalState": "failure",
                    "reviewCaseId": case.review_case_id,
                    "attempt": attempt,
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:2000],
                    "modelCalls": deepcopy(case_client.ledger),
                    "recordedAt": datetime.now(timezone.utc).isoformat(),
                }
                failures[case.review_case_id] = failure
                _atomic_write(case_dir / "failure.json", _json_bytes(failure))
                running_path.unlink()

    ordered_ids = [case.review_case_id for case in bundle.cases if case.review_case_id in case_results]
    predictions = [case_results[case_id].prediction for case_id in ordered_ids]
    traces = [case_results[case_id].trace for case_id in ordered_ids]
    prediction_sha = _atomic_write(run_path / "predictions.jsonl", _jsonl_bytes(predictions))
    trace_sha = _atomic_write(run_path / "traces.jsonl", _jsonl_bytes(traces))
    manifest = {
        "schemaVersion": "used-phone-model-composition-run-v1",
        "composition": COMPOSITION_NAME,
        "evaluationOnly": True,
        "notFullProductionAgent": True,
        "attempt": attempt,
        "requestedCaseIds": sorted(selected),
        "succeededCaseIds": ordered_ids,
        "failedCases": failures,
        "currentAttemptLogicalModelCallCount": new_logical_calls,
        "resumedLogicalModelCallCount": resumed_logical_calls,
        "totalLogicalModelCallCount": new_logical_calls + resumed_logical_calls,
        "prediction": {"path": "predictions.jsonl", "sha256": prediction_sha},
        "trace": {"path": "traces.jsonl", "sha256": trace_sha},
        "publicInputSha256": dict(bundle.input_sha256),
        "evidenceAuditSha256": EXPECTED_EVIDENCE_AUDIT_SHA256,
        "catalog": {
            "categoryKey": CATEGORY_KEY,
            "rowCount": len(catalog.rows),
            "publicCandidateRows": 83,
            "publicUniqueProducts": 68,
            "retrievalMode": "public_judged_pool_rerank",
        },
        "model": model_name,
        "endpointHost": endpoint.hostname,
        "modelEndpointConfigSha256": _sha256_text(json.dumps({
            "model": model_name,
            "scheme": endpoint.scheme,
            "host": endpoint.hostname,
            "port": endpoint.port,
        }, sort_keys=True)),
        "codeSha256": _code_hashes(),
        "productionCodeScope": "agent/app/**/*.py full snapshot because llm/tool import closure is broad",
        "projectionProtocolVersion": PROJECTION_PROTOCOL_VERSION,
        "hiddenInputsRead": False,
        "networkUsed": new_logical_calls > 0,
        "modelUsed": new_logical_calls + resumed_logical_calls > 0,
        "businessNetworkUsed": False,
        "networkGuardScope": "known_httpx_and_business_call_paths_not_process_egress",
        "multiturnAdapter": "non-final turns run TaskState extractor only",
        "modelToolCallAdapter": "DeepSeek thinking disabled only when tools are present",
    }
    manifest["manifestCoreSha256"] = hashlib.sha256(_json_bytes(manifest)).hexdigest()
    _write_manifest(run_path / "manifest.json", manifest)
    return manifest


def run_public_model_composition_sync(**kwargs) -> dict[str, Any]:
    return asyncio.run(run_public_model_composition(**kwargs))
