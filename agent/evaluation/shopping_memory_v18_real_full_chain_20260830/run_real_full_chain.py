"""One-shot real-infrastructure Shopping Memory V18 longitudinal E2E.

This runner covers the remaining integration gap after V17 extraction quality:
real DeepSeek extraction through the production worker, real Redis stream/BFF,
real Java consent/command/projection over MySQL, cross-session projection into
the live ReAct web endpoint, real Elasticsearch retrieval, deterministic
memory reranking, task override, revocation, and the browser HTML contracts.

The 20 commerce-eligible products are an explicitly labelled E2E fixture built
from the frozen 439-product snapshot.  They prove plumbing and governance, not
retrieval quality or real-time price/inventory truth.  Existing attempt
directories are immutable and this runner must never be rerun in place.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx


BASE = Path(__file__).resolve().parent
AGENT_ROOT = BASE.parents[1]
REPOSITORY_ROOT = AGENT_ROOT.parent
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from evaluation import shopping_memory_v13_real_infra_three_session_v1 as artifacts
from evaluation.shopping_memory_v18_real_full_chain_20260830 import seed_used_phone_catalog as seed_catalog


SCHEMA = "shopping-memory-v18-real-full-chain-v6"
VERDICT = "BOUNDED_REAL_FULL_CHAIN_V6_ACCEPT"
HOLD = "HOLD_REAL_FULL_CHAIN_FAILED"
CATALOG_REVISION = artifacts.CATALOG_REVISION
CATALOG_VALUES = artifacts.CATALOG_VALUES
CATALOG_VALUES_SHA256 = artifacts.EXPECTED_VALUES_SHA256
QUERY = "请推荐 e2e-memory-fixture 二手手机，只展示前三款。"
EXPLICIT = "以后买手机请记住，我喜欢原装屏幕，适合我自己。"
ORIGIN = "http://testserver"
ORIGIN_HEADERS = {"Origin": ORIGIN}
OWNER_LABEL = "shopping-memory-v18-20260830"
FORBIDDEN_KEYS = {
    "password", "accessToken", "refreshToken", "csrfToken", "ownerBinding",
    "entryId", "consentEventId", "commandId", "memoryHandle",
    "shopping_memory_session", "sessionBinding", "candidateId", "jobId",
}


class RunFailure(RuntimeError):
    pass


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return seed_catalog.sha256_file(path)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _run(arguments: list[str], label: str) -> str:
    completed = subprocess.run(
        arguments, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if completed.returncode != 0:
        raise RunFailure(f"{label} failed with exit {completed.returncode}")
    return completed.stdout.strip()


def docker_inspect(name: str) -> dict[str, Any]:
    values = json.loads(_run(["docker", "inspect", name], f"inspect {name}"))
    if type(values) is not list or len(values) != 1 or type(values[0]) is not dict:
        raise RunFailure(f"invalid Docker identity: {name}")
    return values[0]


def env_map(inspect: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in inspect.get("Config", {}).get("Env", []):
        if type(entry) is str and "=" in entry:
            key, value = entry.split("=", 1)
            result[key] = value
    return result


def source_defaults(path: Path) -> dict[str, Any]:
    wanted = {
        "memory_bff_enabled", "memory_projection_client_enabled",
        "memory_rerank_lambda",
    }
    values: dict[str, Any] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for item in node.body:
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id in wanted
                and isinstance(item.value, ast.Constant)
            ):
                values[item.target.id] = item.value.value
    if set(values) != wanted:
        raise RunFailure("memory defaults are not literal or are missing")
    return values


def assert_artifact_safe(value: object, secret_values: set[str]) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if str(key) in FORBIDDEN_KEYS:
                raise RunFailure(f"authority field escaped into artifact: {key}")
            assert_artifact_safe(item, secret_values)
    elif type(value) is list:
        for item in value:
            assert_artifact_safe(item, secret_values)
    payload = canonical_bytes(value)
    for secret in secret_values:
        if len(secret) >= 8 and secret.encode("utf-8") in payload:
            raise RunFailure("runtime secret escaped into artifact")


def configure_process(args: argparse.Namespace) -> None:
    overrides = {
        "BACKEND_BASE_URL": args.backend_url,
        "REDIS_URL": args.redis_url,
        "MEMORY_BFF_ENABLED": "true",
        "MEMORY_PROJECTION_CLIENT_ENABLED": "true",
        "MEMORY_BFF_CANARY_USERNAMES": args.username,
        "MEMORY_BFF_COOKIE_SECURE": "false",
        "MEMORY_BFF_EPOCH": "v18-real-full-chain-6",
        "MEMORY_CANDIDATE_STREAM_KEY": args.stream_key,
        "MEMORY_ACTIVE_CATALOG_REVISION": CATALOG_REVISION,
        "MEMORY_CATALOG_VALUES_PATH": str(CATALOG_VALUES),
        "MEMORY_CATALOG_VALUES_SHA256": CATALOG_VALUES_SHA256,
        # V13 selected this only as a bounded canary weight; on-disk remains 0.01.
        "MEMORY_RERANK_LAMBDA": "0.08",
        "PRODUCT_RETRIEVAL_MODE": "elasticsearch",
        "USED_PHONE_SYNTHETIC_PRICE_POLICY": "disabled",
        "ECOMMERCE_GUIDE_ENABLED": "true",
        "AGENT_ORCHESTRATOR_MODE": "unified",
        "AGENT_CONTROL_RUNTIME": "react_v1",
        "AGENT_REACT_LIVE_ENABLED": "true",
        "AGENT_LEGACY_FALLBACK_ENABLED": "false",
        "WEB_QUERY_INTAKE_ENABLED": "false",
    }
    for key, value in overrides.items():
        os.environ[key] = value


def infrastructure(args: argparse.Namespace) -> dict[str, Any]:
    names = {
        "mysql": args.mysql_container,
        "redis": args.redis_container,
        "elasticsearch": args.elasticsearch_container,
        "java": args.java_container,
    }
    inspected = {key: docker_inspect(value) for key, value in names.items()}
    for key, item in inspected.items():
        if item.get("Config", {}).get("Labels", {}).get("codex.memory-v18.owner") != OWNER_LABEL:
            raise RunFailure(f"unowned infrastructure: {key}")
        if item.get("State", {}).get("Running") is not True:
            raise RunFailure(f"infrastructure not running: {key}")
    java_env = env_map(inspected["java"])
    mysql_env = env_map(inspected["mysql"])
    safe_checks = {
        "singleOwnedNetwork": all(
            args.network in item.get("NetworkSettings", {}).get("Networks", {})
            for item in inspected.values()
        ),
        "javaDatabaseBound": (
            java_env.get("DB_HOST") == args.mysql_container
            and java_env.get("DB_NAME") == mysql_env.get("MYSQL_DATABASE")
            and java_env.get("DB_USER") == mysql_env.get("MYSQL_USER")
        ),
        "javaRedisBound": java_env.get("REDIS_HOST") == args.redis_container,
        "javaElasticsearchBound": (
            java_env.get("ELASTICSEARCH_URL")
            == f"http://{args.elasticsearch_container}:9200"
        ),
        "javaSearchIndexBound": (
            java_env.get("SEARCH_INDEX_PREFIX") == args.search_index_prefix
        ),
        "memoryEnabledOnlyInCanaryJava": java_env.get("SHOPPING_MEMORY_ENABLED") == "true",
        "searchEnabledInCanaryJava": java_env.get("SEARCH_ENABLED") == "true",
        "sideEffectsDisabled": all(java_env.get(key) == "false" for key in (
            "MESSAGING_ENABLED", "FLASH_SALE_ENABLED", "RATE_LIMIT_ENABLED",
            "PAYMENT_SIMULATOR_ENABLED",
        )),
    }
    mounts = inspected["java"].get("Mounts", [])
    java_ports = inspected["java"].get("NetworkSettings", {}).get("Ports", {})
    port_bindings = java_ports.get("8080/tcp") if isinstance(java_ports, dict) else None
    safe_checks["javaHostPortBound"] = (
        isinstance(port_bindings, list)
        and len(port_bindings) == 1
        and port_bindings[0].get("HostIp") == "127.0.0.1"
        and port_bindings[0].get("HostPort") == str(args.java_host_port)
    )
    jar_mounts = [
        item for item in mounts
        if type(item) is dict and item.get("Destination") == "/app/app.jar"
    ]
    if len(jar_mounts) != 1 or jar_mounts[0].get("RW") is not False:
        raise RunFailure("Java JAR is not a read-only bind")
    host_jar = Path(args.java_jar_path).resolve()
    host_hash = sha256_file(host_jar)
    container_hash = _run(
        ["docker", "exec", args.java_container, "sha256sum", "/app/app.jar"],
        "container JAR hash",
    ).split()[0]
    safe_checks["hostContainerJarHashMatch"] = host_hash == container_hash
    if not all(safe_checks.values()):
        raise RunFailure("infrastructure binding mismatch")
    return {
        "containers": names,
        "network": args.network,
        "javaJarSha256": host_hash,
        "hostPort": args.java_host_port,
        "elasticsearchHostPort": 19283,
        "checks": safe_checks,
    }


def account_snapshot(container: str, username: str) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9._-]{3,64}", username) is None:
        raise RunFailure("unsafe E2E username")
    sql = f"""
SELECT JSON_OBJECT(
  'accountRows',(SELECT COUNT(*) FROM user_account WHERE username='{username}'),
  'memoryRows',(SELECT COUNT(*) FROM user_shopping_memory m JOIN user_account u ON u.id=m.owner_user_id WHERE u.username='{username}'),
  'maxMemoryVersion',COALESCE((SELECT MAX(m.version) FROM user_shopping_memory m JOIN user_account u ON u.id=m.owner_user_id WHERE u.username='{username}'),0),
  'activeRows',(SELECT COUNT(*) FROM user_shopping_memory m JOIN user_account u ON u.id=m.owner_user_id WHERE u.username='{username}' AND m.status='ACTIVE'),
  'revokedRows',(SELECT COUNT(*) FROM user_shopping_memory m JOIN user_account u ON u.id=m.owner_user_id WHERE u.username='{username}' AND m.status='REVOKED'),
  'projectionHeadRows',(SELECT COUNT(*) FROM user_memory_projection_head h JOIN user_account u ON u.id=h.owner_user_id WHERE u.username='{username}'),
  'projectionRevision',COALESCE((SELECT MAX(h.revision) FROM user_memory_projection_head h JOIN user_account u ON u.id=h.owner_user_id WHERE u.username='{username}'),0),
  'consentGrantRows',(SELECT COUNT(*) FROM user_memory_consent_grant g JOIN user_account u ON u.id=g.owner_user_id WHERE u.username='{username}'),
  'consumedConsentRows',(SELECT COUNT(*) FROM user_memory_consent_grant g JOIN user_account u ON u.id=g.owner_user_id WHERE u.username='{username}' AND g.consumed_at IS NOT NULL),
  'commandResultRows',(SELECT COUNT(*) FROM user_memory_command_result r JOIN user_account u ON u.id=r.owner_user_id WHERE u.username='{username}'),
  'appliedCommandRows',(SELECT COUNT(*) FROM user_memory_command_result r JOIN user_account u ON u.id=r.owner_user_id WHERE u.username='{username}' AND r.status='APPLIED' AND r.completed_at IS NOT NULL)
);"""
    raw = seed_catalog.mysql(container, sql)
    value = json.loads(raw)
    if type(value) is not dict:
        raise RunFailure("invalid account database snapshot")
    return value


async def expect(response: httpx.Response, status: int, label: str) -> dict[str, Any]:
    if response.status_code != status:
        raise RunFailure(f"{label} returned HTTP {response.status_code}")
    value = response.json()
    if type(value) is not dict:
        raise RunFailure(f"{label} returned non-object JSON")
    return value


async def login(app: object, username: str, password: str) -> tuple[httpx.AsyncClient, str, str]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, timeout=70.0,
    )
    body = await expect(await client.post(
        "/api/web-memory/login",
        json={"username": username, "password": password},
        headers=ORIGIN_HEADERS,
    ), 200, "BFF login")
    cookie = client.cookies.get("shopping_memory_session")
    csrf = body.get("csrfToken")
    if (
        set(body) != {"authenticated", "username", "csrfToken"}
        or body.get("authenticated") is not True
        or type(cookie) is not str or not cookie
        or type(csrf) is not str or not csrf
    ):
        await client.aclose()
        raise RunFailure("BFF login contract mismatch")
    return client, cookie, csrf


def install_agent_usage_probe() -> tuple[
    list[dict[str, Any]], Callable[[str], None], Callable[[], None]
]:
    from app import llm

    original_get_client = llm.get_client
    records: list[dict[str, Any]] = []
    label = {"value": "unassigned"}

    class CompletionProxy:
        def __init__(self, inner: object):
            self._inner = inner

        async def create(self, *args: object, **kwargs: Any) -> Any:
            started = time.perf_counter()
            response = await self._inner.create(*args, **kwargs)
            usage = getattr(response, "usage", None)
            observed = usage is not None and all(
                type(getattr(usage, name, None)) is int
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            records.append({
                "requestLayer": label["value"],
                "model": str(kwargs.get("model", "unknown")),
                "stream": kwargs.get("stream") is True,
                "usageObserved": observed,
                "promptTokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completionTokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "totalTokens": int(getattr(usage, "total_tokens", 0) or 0),
                "durationMs": round((time.perf_counter() - started) * 1000, 3),
            })
            return response

    class ChatProxy:
        def __init__(self, inner: object):
            self._inner = inner
            self.completions = CompletionProxy(inner.completions)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    class ClientProxy:
        def __init__(self, inner: object):
            self._inner = inner
            self.chat = ChatProxy(inner.chat)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    def instrumented_get_client() -> object:
        return ClientProxy(original_get_client())

    llm.get_client = instrumented_get_client

    def set_label(value: str) -> None:
        label["value"] = value

    def restore() -> None:
        llm.get_client = original_get_client

    return records, set_label, restore


def chat_summary(body: dict[str, Any], label: str) -> dict[str, Any]:
    trace = body.get("trace") if type(body.get("trace")) is dict else {}
    guide = body.get("guideResult") if type(body.get("guideResult")) is dict else {}
    products = guide.get("products") if type(guide.get("products")) is list else []
    product_ids = [str(item.get("productId")) for item in products if type(item) is dict]
    tool_trace = body.get("toolTrace") if type(body.get("toolTrace")) is list else []
    search_traces = [
        item for item in tool_trace
        if type(item) is dict and item.get("tool") == "search_products"
    ]
    answer = body.get("answer") if type(body.get("answer")) is str else ""
    return {
        "label": label,
        "status": trace.get("status"),
        "agentStatus": trace.get("agentStatus"),
        "toolStatus": trace.get("toolStatus"),
        "modelCallCounts": trace.get("modelCallCounts", {}),
        "modelCallFailures": trace.get("modelCallFailures", {}),
        "llmDurationByStageMs": trace.get("llmDurationByStageMs", {}),
        "memoryLoad": trace.get("memoryLoad"),
        "toolNames": trace.get("toolNames", []),
        "candidateCount": trace.get("candidateCount"),
        "guideProductIds": product_ids,
        "guideMemoryRerank": guide.get("memoryRerank"),
        "searchTraceCount": len(search_traces),
        "answerSha256": digest_text(answer),
        "answerCharacters": len(answer),
    }


async def run_chat(
    client: httpx.AsyncClient,
    set_label: Callable[[str], None],
    *, label: str, message: str, session_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    set_label(label)
    body = await expect(await client.post(
        "/agent/chat-llm",
        json={
            "message": message, "sessionId": session_id,
            "domainHint": "ecommerce", "recipientScope": "self",
        },
    ), 200, f"{label} chat")
    set_label("idle")
    summary = chat_summary(body, label)
    if summary["status"] != "ok":
        raise RunFailure(f"{label} chat did not complete")
    return body, summary


async def wait_for_candidates(
    client: httpx.AsyncClient, *, count: int, timeout_seconds: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last: dict[str, Any] = {"candidates": []}
    while asyncio.get_running_loop().time() < deadline:
        last = await expect(
            await client.get("/api/web-memory/candidates"), 200, "candidate poll",
        )
        candidates = last.get("candidates")
        if type(candidates) is list and len(candidates) == count:
            return last
        await asyncio.sleep(0.25)
    raise RunFailure(f"candidate poll timed out at {len(last.get('candidates', []))}")


def screen_value(candidate: dict[str, Any]) -> str | None:
    attributes = candidate.get("attributes")
    if type(attributes) is not list:
        return None
    for item in attributes:
        if (
            type(item) is dict and item.get("key") == "screen_originality"
            and item.get("status") == "known" and type(item.get("value")) is str
        ):
            return item["value"]
    return None


def usage_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(item["durationMs"]) for item in records]
    return {
        "callCount": len(records),
        "usageObservedCount": sum(item["usageObserved"] is True for item in records),
        "promptTokens": sum(int(item["promptTokens"]) for item in records),
        "completionTokens": sum(int(item["completionTokens"]) for item in records),
        "totalTokens": sum(int(item["totalTokens"]) for item in records),
        "p50Ms": round(statistics.median(durations), 3) if durations else 0.0,
        "p95Ms": round(percentile(durations, 0.95), 3) if durations else 0.0,
        "byRequestLayer": {
            label: sum(item["requestLayer"] == label for item in records)
            for label in sorted({str(item["requestLayer"]) for item in records})
        },
    }


async def execute(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    settings_path = AGENT_ROOT / "app/settings.py"
    defaults_before = source_defaults(settings_path)
    settings_sha_before = sha256_file(settings_path)
    if defaults_before != {
        "memory_bff_enabled": False,
        "memory_projection_client_enabled": False,
        "memory_rerank_lambda": 0.01,
    }:
        raise RunFailure("on-disk memory defaults changed before canary")
    configure_process(args)

    import redis.asyncio as redis
    from app import memory_candidate_worker as worker
    from app.api import memory_bff
    from app.domains.ecommerce.ranking_contract import (
        project_validated_search_product_presentations,
    )
    from app.domains.ecommerce.tools import search_products_tool
    from app.main import app
    from app.memory.v3_runtime import rerank_product_presentations
    from app.settings import settings

    if (
        settings.memory_bff_enabled is not True
        or settings.memory_projection_client_enabled is not True
        or settings.memory_rerank_lambda != 0.08
        or settings.product_retrieval_mode != "elasticsearch"
        or settings.memory_candidate_stream_key != args.stream_key
    ):
        raise RunFailure("canary runtime settings mismatch")
    if not settings.deepseek_api_key:
        raise RunFailure("DeepSeek credential unavailable")

    infra = infrastructure(args)
    started_at = datetime.now(timezone.utc).isoformat()
    source_paths = [
        Path(__file__).resolve(), BASE / "seed_used_phone_catalog.py",
        BASE / "start_combined_java.ps1", settings_path,
        AGENT_ROOT / "app/memory_candidate_worker.py",
        AGENT_ROOT / "app/api/memory_bff.py", AGENT_ROOT / "app/main.py",
        AGENT_ROOT / "app/llm.py", AGENT_ROOT / "app/harness.py",
        AGENT_ROOT / "app/context_view.py", AGENT_ROOT / "app/memory/v3_runtime.py",
    ]
    source_before = {
        str(path.relative_to(REPOSITORY_ROOT)): sha256_file(path) for path in source_paths
    }
    runtime_secrets: set[str] = set()
    agent_usage, set_agent_label, restore_agent_probe = install_agent_usage_probe()
    extraction_observations: list[dict[str, Any]] = []
    original_extract = worker._extract
    clients: list[httpx.AsyncClient] = []
    redis_client = redis.from_url(args.redis_url, decode_responses=True)
    worker_stop = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None

    async def observed_extract(
        message: str, category_id: str, recipient_scope: str,
    ) -> list[dict[str, str]]:
        try:
            proposals, observation = await worker._extract_observed(
                message, category_id, recipient_scope,
            )
        except worker.MemoryExtractionFailure as exc:
            extraction_observations.append(exc.observation.plain())
            raise
        extraction_observations.append(observation.plain())
        return proposals

    worker._extract = observed_extract
    try:
        if await redis_client.ping() is not True:
            raise RunFailure("real Redis ping failed")
        if await redis_client.exists(args.stream_key):
            raise RunFailure("immutable worker stream already exists")
        async with httpx.AsyncClient(base_url=args.backend_url, timeout=15.0) as java:
            health = await java.get("/actuator/health")
            catalog = await java.get("/api/products", params={"category": "手机", "limit": 1500})
            retrieval = await java.get("/api/products/retrieval", params={
                "query": "e2e-memory-fixture", "category": "手机", "limit": 20,
            })
            if health.status_code != 200 or health.json().get("status") != "UP":
                raise RunFailure("Java health is not UP")
            if catalog.status_code != 200 or len(catalog.json().get("data", [])) != 439:
                raise RunFailure("Java catalog is not the frozen 439 snapshot")
            retrieval_data = retrieval.json().get("data", {})
            if not (
                retrieval.status_code == 200
                and retrieval_data.get("channel") == "elasticsearch"
                and retrieval_data.get("recallCount") == 20
                and retrieval_data.get("authoritativeEligibleCount") == 20
                and len(retrieval_data.get("products", [])) == 20
            ):
                raise RunFailure("Java/ES test fixture retrieval mismatch")
            password = "Mv18!" + secrets.token_urlsafe(24)
            runtime_secrets.add(password)
            registration = await java.post(
                "/api/auth/register", json={"username": args.username, "password": password},
            )
            if registration.status_code != 201:
                raise RunFailure(f"fresh account registration returned {registration.status_code}")

        first_client, first_cookie, first_csrf = await login(app, args.username, password)
        clients.append(first_client)
        runtime_secrets.update({first_cookie, first_csrf})
        database_before = account_snapshot(args.mysql_container, args.username)
        empty_entries = await expect(
            await first_client.get("/api/web-memory/entries"), 200, "initial entries",
        )
        if empty_entries.get("entries") != []:
            raise RunFailure("fresh account already has memory")

        worker_task = asyncio.create_task(worker.run_memory_candidate_worker(worker_stop))
        await asyncio.sleep(0.1)

        _, baseline = await run_chat(
            first_client, set_agent_label, label="agent_baseline_no_memory",
            message=QUERY, session_id="v18-baseline-" + secrets.token_hex(8),
        )
        no_candidate = await wait_for_candidates(
            first_client, count=0, timeout_seconds=1.0,
        )
        if no_candidate.get("candidates") != []:
            raise RunFailure("implicit baseline created a memory candidate")

        _, explicit = await run_chat(
            first_client, set_agent_label, label="agent_explicit_memory_turn",
            message=EXPLICIT, session_id="v18-explicit-" + secrets.token_hex(8),
        )
        candidate_body = await wait_for_candidates(
            first_client, count=1, timeout_seconds=45.0,
        )
        candidate = candidate_body["candidates"][0]
        candidate_id = candidate.get("candidateId")
        if (
            type(candidate_id) is not str
            or candidate.get("status") != "pending"
            or "原装" not in str(candidate.get("displayText"))
        ):
            raise RunFailure("real worker candidate card mismatch")
        runtime_secrets.add(candidate_id)
        if len(extraction_observations) != 1:
            raise RunFailure("real worker extraction call count mismatch")
        extraction = extraction_observations[0]
        if not (
            extraction.get("modelCalled") is True
            and extraction.get("outcome") == "accepted"
            and extraction.get("usageObserved") is True
        ):
            raise RunFailure("real worker extraction observation mismatch")

        confirmed = await expect(await first_client.post(
            f"/api/web-memory/candidates/{candidate_id}/decision",
            json={"action": "confirm"},
            headers={**ORIGIN_HEADERS, memory_bff.CSRF_HEADER: first_csrf},
        ), 200, "candidate confirmation")
        if confirmed != {"confirmed": True}:
            raise RunFailure("candidate confirmation failed")
        entries_first = await expect(
            await first_client.get("/api/web-memory/entries"), 200, "confirmed entries",
        )
        if type(entries_first.get("entries")) is not list or len(entries_first["entries"]) != 1:
            raise RunFailure("confirmed projection cardinality mismatch")
        entry = entries_first["entries"][0]
        memory_handle = entry.get("memoryHandle")
        if type(memory_handle) is not str:
            raise RunFailure("confirmed entry lacks opaque handle")
        runtime_secrets.add(memory_handle)
        expected_public_entry = {
            "categoryId": "phone", "preferenceKind": "prefer",
            "attributeKey": "screen_originality", "normalizedValue": "original",
            "source": "user_confirmed", "confidence": 1.0,
        }
        if any(entry.get(key) != value for key, value in expected_public_entry.items()):
            raise RunFailure("confirmed public entry mismatch")
        if any(type(entry.get(key)) is not str for key in ("createdAt", "updatedAt", "expiresAt")):
            raise RunFailure("confirmed entry governance timestamps missing")
        database_confirmed = account_snapshot(args.mysql_container, args.username)

        second_client, second_cookie, second_csrf = await login(app, args.username, password)
        clients.append(second_client)
        runtime_secrets.update({second_cookie, second_csrf})
        if second_cookie == first_cookie:
            raise RunFailure("cross-session login reused the same browser cookie")
        second_entries = await expect(
            await second_client.get("/api/web-memory/entries"), 200, "second session entries",
        )
        if len(second_entries.get("entries", [])) != 1:
            raise RunFailure("cross-session memory recall failed")
        second_handle = second_entries["entries"][0].get("memoryHandle")
        if type(second_handle) is not str:
            raise RunFailure("second session lacks memory handle")
        runtime_secrets.add(second_handle)

        treatment_body, treatment = await run_chat(
            second_client, set_agent_label, label="agent_treatment_cross_session",
            message=QUERY, session_id="v18-treatment-" + secrets.token_hex(8),
        )
        if not (
            type(treatment.get("memoryLoad")) is dict
            and treatment["memoryLoad"].get("reason") == "available_retained"
            and type(treatment.get("guideMemoryRerank")) is dict
            and treatment["guideMemoryRerank"].get("applied") is True
            and len(treatment.get("guideProductIds", [])) == 3
        ):
            raise RunFailure("live ReAct treatment did not expose memory effect")
        treatment_state = treatment_body.get("taskState")
        treatment_task_id = (
            treatment_state.get("taskId") if type(treatment_state) is dict else None
        )
        if type(treatment_task_id) is not str or not treatment_task_id:
            raise RunFailure("live treatment did not expose a TaskState identity")

        resolution = await memory_bff.resolve_memory_run_for_browser_session(
            second_cookie, category_id="phone", recipient_scope="self",
            catalog_revision=CATALOG_REVISION, task_id=treatment_task_id,
        )
        if (
            resolution.summary.get("reason") != "available_retained"
            or len(resolution.binding.preferences) != 1
        ):
            raise RunFailure("production V3 resolver did not retain memory")
        model_payload = resolution.binding.payload_for_phase("final_answer")
        if type(model_payload) is not dict:
            raise RunFailure("final-answer memory projection unavailable")
        assert_artifact_safe(model_payload, runtime_secrets)

        direct_trace = await search_products_tool(
            "e2e-memory-fixture", "手机", limit=20,
        )
        _normalized, direct_candidates = project_validated_search_product_presentations(
            direct_trace.detail, requirements=[], category="手机", limit=20,
        )
        if not direct_trace.ok or len(direct_candidates) != 20:
            raise RunFailure("production ES tool retrieval failed")
        baseline_ids = [str(item["productId"]) for item in direct_candidates]
        reranked, rerank_receipt = rerank_product_presentations(
            resolution.binding, direct_candidates, weight=0.08,
        )
        reranked_ids = [str(item["productId"]) for item in reranked]
        override_ranked, override_receipt = rerank_product_presentations(
            resolution.binding, direct_candidates, weight=0.08,
            suppressed_attribute_keys=frozenset({"screen_originality"}),
        )
        override_ids = [str(item["productId"]) for item in override_ranked]
        baseline_original_top3 = sum(screen_value(item) == "original" for item in direct_candidates[:3])
        reranked_original_top3 = sum(screen_value(item) == "original" for item in reranked[:3])
        if not (
            set(reranked_ids) == set(baseline_ids)
            and reranked_ids != baseline_ids
            and reranked_original_top3 >= baseline_original_top3
            and override_ids == baseline_ids
        ):
            raise RunFailure("memory rerank or current-task override invariant failed")

        revoked = await expect(await second_client.delete(
            f"/api/web-memory/entries/{second_handle}",
            headers={**ORIGIN_HEADERS, memory_bff.CSRF_HEADER: second_csrf},
        ), 200, "memory revocation")
        if revoked != {"revoked": True}:
            raise RunFailure("memory revocation failed")
        database_revoked = account_snapshot(args.mysql_container, args.username)
        after_revoke = await expect(
            await second_client.get("/api/web-memory/entries"), 200, "entries after revoke",
        )
        third_client, third_cookie, third_csrf = await login(app, args.username, password)
        clients.append(third_client)
        runtime_secrets.update({third_cookie, third_csrf})
        third_resolution = await memory_bff.resolve_memory_run_for_browser_session(
            third_cookie, category_id="phone", recipient_scope="self",
            catalog_revision=CATALOG_REVISION, task_id=None,
        )
        post_revoke_ranked, post_revoke_receipt = rerank_product_presentations(
            third_resolution.binding, direct_candidates, weight=0.08,
        )
        post_revoke_ids = [str(item["productId"]) for item in post_revoke_ranked]
        if (
            after_revoke.get("entries") != []
            or third_resolution.binding.preferences
            or post_revoke_ids != baseline_ids
        ):
            raise RunFailure("revocation remained influential in a later session")

        root = await third_client.get("/")
        html = root.text
        static_checks = {
            "rootHttp200": root.status_code == 200,
            "memoryLoginUiPresent": 'id="memory-login-open"' in html,
            "memoryDrawerPresent": 'id="memory-drawer"' in html,
            "candidateCardUiPresent": 'id="memory-candidates"' in html,
            "productCardUiPresent": "product-cards" in html,
            "correctionControlPresent": "memory-edit-through-chat" in html,
            "disableControlPresent": (
                'disable.textContent = "停用"' in html
                and '+ "/disable"' in html
            ),
            "deleteControlPresent": (
                'revoke.textContent = "忘掉"' in html
                and 'method: "DELETE"' in html
            ),
        }
        if not all(static_checks.values()):
            raise RunFailure("browser HTML memory/product-card contract incomplete")

        source_after = {
            str(path.relative_to(REPOSITORY_ROOT)): sha256_file(path) for path in source_paths
        }
        defaults_after = source_defaults(settings_path)
        settings_sha_after = sha256_file(settings_path)
        extraction_usage = usage_summary([{
            "requestLayer": "memory_extraction",
            **extraction,
        }])
        agent_runtime_usage = usage_summary(agent_usage)
        stage_counts: dict[str, int] = {}
        for summary in (baseline, explicit, treatment):
            for stage, count in summary.get("modelCallCounts", {}).items():
                stage_counts[str(stage)] = stage_counts.get(str(stage), 0) + int(count)
        database_checks = {
            "freshAccountBeforeWrite": database_before == {
                "accountRows": 1, "memoryRows": 0, "maxMemoryVersion": 0,
                "activeRows": 0, "revokedRows": 0,
                "projectionHeadRows": 0, "projectionRevision": 0,
                "consentGrantRows": 0, "consumedConsentRows": 0,
                "commandResultRows": 0, "appliedCommandRows": 0,
            },
            "confirmationCommitted": database_confirmed == {
                "accountRows": 1, "memoryRows": 1, "maxMemoryVersion": 1,
                "activeRows": 1, "revokedRows": 0,
                "projectionHeadRows": 1, "projectionRevision": 1,
                "consentGrantRows": 1, "consumedConsentRows": 1,
                "commandResultRows": 1, "appliedCommandRows": 1,
            },
            "revocationCommitted": database_revoked == {
                "accountRows": 1, "memoryRows": 2, "maxMemoryVersion": 2,
                "activeRows": 1, "revokedRows": 1,
                "projectionHeadRows": 1, "projectionRevision": 2,
                "consentGrantRows": 2, "consumedConsentRows": 2,
                "commandResultRows": 2, "appliedCommandRows": 2,
            },
        }
        checks = {
            **infra["checks"],
            **database_checks,
            **static_checks,
            "javaHealthUp": True,
            "frozenCatalog439": True,
            "realElasticsearchTop20": True,
            "realDeepSeekExtractionAccepted": extraction.get("outcome") == "accepted",
            "extractionUsageObserved": extraction_usage["usageObservedCount"] == 1,
            "implicitTurnCreatedNoCandidate": no_candidate.get("candidates") == [],
            "realWorkerCardVisible": True,
            "realConsentCommandProjection": True,
            "crossSessionRecall": len(second_entries.get("entries", [])) == 1,
            "modelProjectionContainsNoAuthority": True,
            "candidateSetUnchanged": set(reranked_ids) == set(baseline_ids),
            "memoryChangedDeterministicOrder": reranked_ids != baseline_ids,
            "memoryDidNotReducePreferredTop3": reranked_original_top3 >= baseline_original_top3,
            "currentTaskOverrideSuppressesMemory": override_ids == baseline_ids,
            "liveReactMemoryLoaded": treatment["memoryLoad"].get("reason") == "available_retained",
            "liveGuideMemoryRerankApplied": treatment["guideMemoryRerank"].get("applied") is True,
            "revocationStopsLaterInfluence": post_revoke_ids == baseline_ids,
            # Deterministic ReAct turns legitimately make zero Agent-runtime
            # model calls.  Zero must remain visible and must not be converted
            # into a failed memory experiment; any calls that do occur still
            # require complete provider usage accounting.
            "agentUsageAccountingComplete": (
                agent_runtime_usage["usageObservedCount"]
                == agent_runtime_usage["callCount"]
            ),
            "modelCallLayersSeparated": (
                extraction_usage["callCount"] == 1
                and agent_runtime_usage["callCount"] == sum(stage_counts.values())
            ),
            "onDiskDefaultsUnchanged": (
                defaults_before == defaults_after
                and settings_sha_before == settings_sha_after
            ),
            "boundSourcesStable": source_before == source_after,
        }
        decision = VERDICT if all(checks.values()) else HOLD
        trace = {
            "schemaVersion": SCHEMA,
            "attemptId": args.attempt_id,
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "scope": {
                "included": [
                    "real_deepseek_explicit_memory_extraction",
                    "production_async_worker_and_real_redis_stream",
                    "real_bff_three_browser_sessions",
                    "real_java_mysql_consent_command_projection",
                    "real_elasticsearch_top20_and_mysql_fact_validation",
                    "live_react_web_endpoint_final_answer",
                    "production_memory_rerank_task_override_and_revoke",
                    "static_browser_memory_and_product_card_contract",
                ],
                "boundedFixture": (
                    "20 commerce-eligible E2E products drawn from the frozen 439 snapshot; "
                    "prices/inventory are test fixtures, not retrieval-quality or market truth"
                ),
                "notAuthorityFor": [
                    "production_default_enablement", "causal_recommendation_quality",
                    "real_time_price_or_inventory", "sealed_or_independent_confirmation",
                    "all_categories", "automatic_behavior_to_preference_promotion",
                ],
            },
            "infrastructure": infra,
            "database": {
                "before": database_before,
                "confirmed": database_confirmed,
                "revoked": database_revoked,
                "checks": database_checks,
            },
            "sessions": [baseline, explicit, treatment],
            "extraction": {
                "observation": extraction,
                "candidateHandleSha256": digest_text(candidate_id),
                "entryHandleSha256": digest_text(memory_handle),
            },
            "crossSessionProjection": {
                "resolverSummary": resolution.summary,
                "modelPayload": model_payload,
                "publicEntry": {
                    **expected_public_entry,
                    "timestampsPresent": True,
                },
            },
            "deterministicRerank": {
                "inputProductIds": baseline_ids,
                "outputProductIds": reranked_ids,
                "overrideProductIds": override_ids,
                "postRevokeProductIds": post_revoke_ids,
                "baselineOriginalTop3": baseline_original_top3,
                "rerankedOriginalTop3": reranked_original_top3,
                "receipt": rerank_receipt,
                "overrideReceipt": override_receipt,
                "postRevokeReceipt": post_revoke_receipt,
            },
            "modelCalls": {
                "memoryExtraction": extraction_usage,
                "agentRuntime": agent_runtime_usage,
                "agentStageCounts": stage_counts,
                "classificationRule": (
                    "dedicated memory extractor is counted only in memoryExtraction; "
                    "TaskManager, ReAct decisions and final-answer calls remain agentRuntime"
                ),
            },
            "browserStatic": static_checks,
            "productionDefaults": {
                "before": defaults_before, "after": defaults_after,
                "settingsSha256Before": settings_sha_before,
                "settingsSha256After": settings_sha_after,
                "runtimeCanaryWeight": 0.08,
            },
            "checks": checks,
            "decision": decision,
        }
        report = {
            "schemaVersion": f"{SCHEMA}-report",
            "attemptId": args.attempt_id,
            "decision": decision,
            "checks": checks,
            "architectureDecision": "BOUNDED_ARCHITECTURE_ACCEPT" if decision == VERDICT else HOLD,
            "productionDefaultDecision": "HOLD_KEEP_GLOBAL_MEMORY_OFF",
            "extraction": extraction_usage,
            "agentRuntime": agent_runtime_usage,
            "agentStageCounts": stage_counts,
            "sessionCount": 3,
            "realProductCatalogCount": 439,
            "commerceFixtureCount": 20,
            "memoryRowsAfterRevoke": database_revoked.get("memoryRows"),
            "projectionRevisionAfterRevoke": database_revoked.get("projectionRevision"),
            "limits": trace["scope"]["notAuthorityFor"],
        }
        receipt = {
            "schemaVersion": f"{SCHEMA}-receipt",
            "attemptId": args.attempt_id,
            "decision": decision,
            "sourceSha256Before": source_before,
            "sourceSha256After": source_after,
            "inputSha256": {
                str(artifacts.CATALOG_MANIFEST.relative_to(REPOSITORY_ROOT)): artifacts.EXPECTED_MANIFEST_SHA256,
                str(CATALOG_VALUES.relative_to(REPOSITORY_ROOT)): CATALOG_VALUES_SHA256,
                str(artifacts.PRODUCT_CATALOG.relative_to(REPOSITORY_ROOT)): artifacts.EXPECTED_PRODUCT_SHA256,
                str(seed_catalog.PRICES.relative_to(REPOSITORY_ROOT)): seed_catalog.EXPECTED_PRICES_SHA256,
            },
            "infrastructureBinding": {
                "network": args.network,
                "javaContainer": args.java_container,
                "mysqlContainer": args.mysql_container,
                "redisContainer": args.redis_container,
                "elasticsearchContainer": args.elasticsearch_container,
                "javaJarSha256": infra["javaJarSha256"],
                "backendBaseUrl": args.backend_url,
                "redisEndpoint": "127.0.0.1:16382/0",
                "elasticsearchEndpoint": "127.0.0.1:19283",
            },
            "artifactSha256": {},
        }
        for artifact in (trace, report, receipt):
            assert_artifact_safe(artifact, runtime_secrets)
        return trace, {"report": report, "receipt": receipt}
    finally:
        set_agent_label("idle")
        worker._extract = original_extract
        restore_agent_probe()
        worker_stop.set()
        if worker_task is not None:
            try:
                await asyncio.wait_for(worker_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
        for client in clients:
            await client.aclose()
        await redis_client.aclose()


def write_json_exclusive(path: Path, value: object) -> None:
    with path.open("xb") as stream:
        stream.write(canonical_bytes(value) + b"\n")


def materialize(attempt: Path, trace: dict[str, Any], bundle: dict[str, Any]) -> None:
    trace_path = attempt / "trace.json"
    report_path = attempt / "report.json"
    receipt_path = attempt / "receipt.json"
    write_json_exclusive(trace_path, trace)
    write_json_exclusive(report_path, bundle["report"])
    bundle["receipt"]["artifactSha256"] = {
        "started.json": sha256_file(attempt / "started.json"),
        "trace.json": sha256_file(trace_path),
        "report.json": sha256_file(report_path),
    }
    write_json_exclusive(receipt_path, bundle["receipt"])
    names = ["started.json", "trace.json", "report.json", "receipt.json"]
    with (attempt / "SHA256SUMS.txt").open("x", encoding="utf-8", newline="\n") as stream:
        for name in sorted(names):
            stream.write(f"{sha256_file(attempt / name)}  {name}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-url", default="http://127.0.0.1:18087")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16382/0")
    parser.add_argument("--network", default="agent-memory-v18-e2e-net-20260830")
    parser.add_argument("--mysql-container", default="agent-memory-v18-e2e-mysql-v4-20260830")
    parser.add_argument("--redis-container", default="agent-memory-v18-e2e-redis-20260830")
    parser.add_argument("--elasticsearch-container", default="agent-memory-v18-e2e-es-20260830")
    parser.add_argument("--java-container", default="agent-memory-v18-e2e-java-search-v4-20260830")
    parser.add_argument("--java-host-port", type=int, default=18087)
    parser.add_argument("--search-index-prefix", default="memory-v18-e2e-v4")
    parser.add_argument(
        "--java-jar-path",
        default="D:/agent-e2e-memory-v18-20260830/target/local-life-backend-0.1.0-SNAPSHOT.jar",
    )
    parser.add_argument("--username", default="memory-v18-real-full-chain-a006")
    parser.add_argument(
        "--stream-key",
        default="memory:candidate:v18:real-full-chain:attempt006:20260830",
    )
    parser.add_argument(
        "--attempt-id",
        default="shopping_memory_v18_real_full_chain_v6_attempt006_20260830",
    )
    parser.add_argument("--output-dir", type=Path, default=BASE / "attempt006")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"attempt directory already exists: {args.output_dir}", file=sys.stderr)
        return 2
    started = {
        "schemaVersion": f"{SCHEMA}-started",
        "attemptId": args.attempt_id,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "claim": "CLAIMED_NO_RERUN_NO_OVERWRITE",
    }
    write_json_exclusive(args.output_dir / "started.json", started)
    try:
        trace, bundle = asyncio.run(execute(args))
        materialize(args.output_dir, trace, bundle)
        print(json.dumps({
            "decision": bundle["report"]["decision"],
            "outputDir": str(args.output_dir),
        }, ensure_ascii=False))
        return 0 if bundle["report"]["decision"] == VERDICT else 1
    except Exception as exc:
        failure = {
            "schemaVersion": f"{SCHEMA}-failure",
            "attemptId": args.attempt_id,
            "decision": HOLD,
            "errorType": type(exc).__name__,
            "error": str(exc),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            write_json_exclusive(args.output_dir / "failure.json", failure)
        except Exception:
            pass
        print(f"real full-chain E2E failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
