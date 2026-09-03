"""Real-infrastructure, no-model, three-session Shopping Memory V13 E2E.

This runner deliberately excludes LLM extraction and browser rendering.  It
uses the production FastAPI BFF ASGI app, real Java identity/catalog/consent/
command/projection endpoints, real Redis session state, the production V3
resolver, ContextProjector and soft reranker.  Output directories are
immutable: an existing attempt is an error.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = AGENT_ROOT.parent
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

SCHEMA = "shopping-memory-v13-real-infra-three-session-v1"
VERDICT = "REAL_INFRA_NO_MODEL_THREE_SESSION_ACCEPT"
CATALOG_REVISION = "used-phone-439-09807c773ce6"
CATALOG_MANIFEST = AGENT_ROOT / "evaluation/assets/used_phone_memory_catalog_v13_20260830/manifest.json"
CATALOG_VALUES = AGENT_ROOT / "evaluation/assets/used_phone_memory_catalog_v13_20260830/catalog-values.jsonl"
PRODUCT_CATALOG = REPOSITORY_ROOT / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/catalog.jsonl"
EXPECTED_MANIFEST_SHA256 = "d058d03ff491868bb4faf660f3bb8d41ffc65523ea07c78bf9872c1a3064f3ea"
EXPECTED_VALUES_SHA256 = "2ce140b3670521078ba1821eaaf55029d73efc4715f6c0f6e16c117180756c36"
EXPECTED_PRODUCT_SHA256 = "725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75"
PREFERENCE = {
    "categoryId": "phone",
    "preferenceKind": "prefer",
    "attributeKey": "screen_originality",
    "normalizedValue": "original",
    "catalogRevision": CATALOG_REVISION,
    "recipientScope": "self",
    "source": "user_confirmed",
}
RERANK_WEIGHT = 0.08
ASGI_BASE_URL = "http://testserver"
ORIGIN_HEADERS = {"Origin": ASGI_BASE_URL}
SECRET_KEYS = {
    "accessToken", "refreshToken", "password", "ownerBinding", "entryId",
    "consentEventId", "commandId", "memoryHandle", "shopping_memory_session",
}


class RunFailure(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if type(value) is not dict:
                raise RunFailure(f"invalid JSONL row: {path}")
            rows.append(value)
    return rows


def create_attempt_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)


def write_json_exclusive(path: Path, value: object) -> None:
    with path.open("xb") as stream:
        stream.write(canonical_bytes(value) + b"\n")


def _product_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    item_id = str(row.get("itemId", ""))
    return (int(item_id), item_id) if item_id.isdigit() else (2**63 - 1, item_id)


def _presentation(row: dict[str, Any]) -> dict[str, object]:
    raw_attributes = row.get("attributes")
    if type(raw_attributes) is not dict:
        raise RunFailure("invalid product attribute fixture")
    attributes: list[dict[str, object]] = []
    for key in sorted(raw_attributes):
        item = raw_attributes[key]
        if type(item) is not dict:
            raise RunFailure("invalid product attribute fixture")
        status = item.get("status")
        value = item.get("value")
        attributes.append({
            "key": key,
            "status": "known" if status == "known" and type(value) is str else "unknown",
            "value": value if status == "known" and type(value) is str else None,
        })
    return {
        "productId": str(row["itemId"]),
        "title": str(row.get("title", "")),
        "brand": str(row.get("brand", "")),
        "attributes": attributes,
    }


def load_candidate_fixture() -> tuple[list[dict[str, object]], dict[str, Any]]:
    observed = {
        "manifestSha256": sha256_file(CATALOG_MANIFEST),
        "catalogValuesSha256": sha256_file(CATALOG_VALUES),
        "productCatalogSha256": sha256_file(PRODUCT_CATALOG),
    }
    expected = {
        "manifestSha256": EXPECTED_MANIFEST_SHA256,
        "catalogValuesSha256": EXPECTED_VALUES_SHA256,
        "productCatalogSha256": EXPECTED_PRODUCT_SHA256,
    }
    if observed != expected:
        raise RunFailure("catalog fixture hash mismatch")
    manifest = json.loads(CATALOG_MANIFEST.read_text(encoding="utf-8"))
    if (
        manifest.get("catalogRevision") != CATALOG_REVISION
        or manifest.get("catalogValueCount") != 29
        or manifest.get("productCount") != 439
    ):
        raise RunFailure("catalog fixture manifest mismatch")
    values = read_jsonl(CATALOG_VALUES)
    tuple_key = (
        PREFERENCE["catalogRevision"], PREFERENCE["categoryId"],
        PREFERENCE["attributeKey"], PREFERENCE["normalizedValue"],
    )
    value_keys = {
        (row.get("catalogRevision"), row.get("categoryId"),
         row.get("attributeKey"), row.get("normalizedValue"))
        for row in values
    }
    if tuple_key not in value_keys:
        raise RunFailure("preference is outside frozen catalog values")
    products = sorted(read_jsonl(PRODUCT_CATALOG), key=_product_sort_key)
    if len(products) != 439:
        raise RunFailure("product fixture cardinality mismatch")

    def attr_value(row: dict[str, Any]) -> object:
        attributes = row.get("attributes")
        if type(attributes) is not dict:
            return None
        item = attributes.get(PREFERENCE["attributeKey"])
        return item.get("value") if type(item) is dict and item.get("status") == "known" else None

    matches = [row for row in products if attr_value(row) == PREFERENCE["normalizedValue"]]
    nonmatches = [row for row in products if attr_value(row) not in {None, PREFERENCE["normalizedValue"]}]
    if not matches or not nonmatches:
        raise RunFailure("catalog cannot construct paired rerank fixture")
    baseline_first, preferred_second = nonmatches[0], matches[0]
    used = {str(baseline_first["itemId"]), str(preferred_second["itemId"])}
    fillers = [row for row in products if str(row["itemId"]) not in used][:18]
    selected = [baseline_first, preferred_second, *fillers]
    presentations = [_presentation(row) for row in selected]
    ids = [str(item["productId"]) for item in presentations]
    if len(presentations) != 20 or len(set(ids)) != 20:
        raise RunFailure("invalid candidate fixture")
    return presentations, {
        **observed,
        "catalogRevision": CATALOG_REVISION,
        "catalogValueCount": len(values),
        "productCount": len(products),
        "candidateCount": len(presentations),
        "inputProductIds": ids,
        "baselineFirstProductId": ids[0],
        "preferredSecondProductId": ids[1],
        "preference": dict(PREFERENCE),
    }


def assert_artifact_safe(value: object) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if key in SECRET_KEYS:
                raise RunFailure(f"secret field in artifact: {key}")
            assert_artifact_safe(item)
    elif type(value) is list:
        for item in value:
            assert_artifact_safe(item)


def verdict_for_checks(checks: dict[str, bool]) -> str:
    return VERDICT if checks and all(value is True for value in checks.values()) else "HOLD_REAL_INFRA_E2E_FAILED"


def configure_process(*, backend_url: str, redis_url: str, username: str) -> None:
    overrides = {
        "BACKEND_BASE_URL": backend_url,
        "REDIS_URL": redis_url,
        "MEMORY_BFF_ENABLED": "true",
        "MEMORY_PROJECTION_CLIENT_ENABLED": "true",
        "MEMORY_BFF_CANARY_USERNAMES": username,
        "MEMORY_BFF_COOKIE_SECURE": "false",
        "MEMORY_ACTIVE_CATALOG_REVISION": CATALOG_REVISION,
        "MEMORY_CATALOG_VALUES_PATH": str(CATALOG_VALUES),
        "MEMORY_CATALOG_VALUES_SHA256": EXPECTED_VALUES_SHA256,
        "MEMORY_RERANK_LAMBDA": str(RERANK_WEIGHT),
    }
    for key, value in overrides.items():
        os.environ[key] = value


async def _expect(response: httpx.Response, status: int, label: str) -> dict[str, Any]:
    if response.status_code != status:
        raise RunFailure(f"{label} returned HTTP {response.status_code}")
    try:
        value = response.json()
    except ValueError as exc:
        raise RunFailure(f"{label} returned invalid JSON") from exc
    if type(value) is not dict:
        raise RunFailure(f"{label} returned invalid object")
    return value


async def _login(app: object, username: str, password: str) -> tuple[httpx.AsyncClient, str, str]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ASGI_BASE_URL,
    )
    response = await client.post(
        "/api/web-memory/login", json={"username": username, "password": password},
        headers=ORIGIN_HEADERS,
    )
    body = await _expect(response, 200, "BFF login")
    cookie = client.cookies.get("shopping_memory_session")
    csrf = body.get("csrfToken")
    if type(cookie) is not str or not cookie or type(csrf) is not str or not csrf:
        await client.aclose()
        raise RunFailure("BFF login omitted opaque session or CSRF")
    return client, cookie, csrf


def _pack(task_id: str, conversation_id: str, *, override: bool = False):
    from app.context_pack import ContextPack, TaskConstraint

    hard = []
    requirements = []
    if override:
        hard = [TaskConstraint(
            key=PREFERENCE["attributeKey"], operator="eq",
            value="non_original", source="user",
        )]
        requirements = [{
            "key": PREFERENCE["attributeKey"], "operator": "eq",
            "value": "non_original", "unit": "enum", "priority": "hard",
            "source": "user",
        }]
    return ContextPack(
        run_id=conversation_id,
        task_id=task_id,
        base_context_revision=1,
        goal="推荐二手手机",
        task_type="ecommerce_guide",
        confirmed_facts=[],
        hard_constraints=hard,
        soft_preferences=[],
        unknowns=[],
        pending_questions=[],
        allowed_tools=["search_products"],
        evidence_refs=[],
        history_summaries=[],
        shopping_guide_state={
            "mode": "recommend", "category": "phone",
            "requirements": requirements, "candidateIds": [],
            "comparedIds": [], "evidenceStatus": "missing",
        },
    )


async def execute(args: argparse.Namespace, attempt_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    configure_process(
        backend_url=args.backend_url, redis_url=args.redis_url, username=args.username,
    )
    # Imports must occur after the isolated canary settings are installed.
    import redis.asyncio as redis
    from app.api import memory_bff
    from app.context_view import ContextProjector
    from app.main import app
    from app.memory.v3_runtime import rerank_product_presentations
    from app.settings import settings

    started_at = datetime.now(timezone.utc).isoformat()
    presentations, fixture = load_candidate_fixture()
    runner_path = Path(__file__).resolve()
    source_paths = [
        runner_path,
        AGENT_ROOT / "app/api/memory_bff.py",
        AGENT_ROOT / "app/memory/v3_runtime.py",
        AGENT_ROOT / "app/context_view.py",
        AGENT_ROOT / "app/main.py",
    ]
    source_hashes_before = {str(path.relative_to(REPOSITORY_ROOT)): sha256_file(path) for path in source_paths}

    async with httpx.AsyncClient(base_url=args.backend_url, timeout=5.0) as java:
        actuator = await java.get("/actuator/health")
        api_health = await java.get("/api/health")
        if actuator.status_code != 200 or actuator.json().get("status") != "UP":
            raise RunFailure("Java actuator is not UP")
        if api_health.status_code != 200 or api_health.json().get("success") is not True:
            raise RunFailure("Java API health is not UP")
        password = os.environ.get(args.password_env)
        registered = False
        generated_password = password is None
        if password is None:
            password = "Mv13!" + secrets.token_urlsafe(24)
        registration = await java.post(
            "/api/auth/register", json={"username": args.username, "password": password},
        )
        if registration.status_code == 201:
            registered = True
        elif registration.status_code == 409 and not generated_password:
            registered = False
        elif registration.status_code == 409:
            raise RunFailure("canary user already exists; provide the password environment variable")
        else:
            raise RunFailure(f"registration returned HTTP {registration.status_code}")

    redis_client = redis.from_url(args.redis_url, decode_responses=True)
    try:
        if await redis_client.ping() is not True:
            raise RunFailure("Redis ping failed")
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", args.mysql_container],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if inspect.returncode != 0:
            raise RunFailure("MySQL container inspect failed")
        mysql_state = json.loads(inspect.stdout)
        if mysql_state.get("Running") is not True or mysql_state.get("Status") != "running":
            raise RunFailure("MySQL container is not running")

        ids = [{
            "sessionOrdinal": index,
            "conversationId": f"memory-v13-e2e-conversation-{index}-{secrets.token_hex(8)}",
            "taskId": f"memory-v13-e2e-task-{index}-{secrets.token_hex(8)}",
        } for index in (1, 2, 3)]
        clients: list[httpx.AsyncClient] = []
        cookies: list[str] = []
        csrf_tokens: list[str] = []
        for _ in range(3):
            client, cookie, csrf = await _login(app, args.username, password)
            clients.append(client)
            cookies.append(cookie)
            csrf_tokens.append(csrf)
        if len(set(cookies)) != 3:
            raise RunFailure("BFF did not issue three distinct browser sessions")

        try:
            session_traces: list[dict[str, Any]] = []
            first_before = await _expect(await clients[0].get("/api/web-memory/entries"), 200, "session1 entries before")
            if first_before.get("entries") != []:
                raise RunFailure("fresh canary account contains pre-existing active memory")

            session_binding = digest_text(cookies[0])
            server_session = await memory_bff.session_for_binding(session_binding)
            if server_session is None:
                raise RunFailure("BFF worker session lookup failed")
            validated = await memory_bff._java(
                "POST", "/api/memory/catalog/validate/v3",
                access_token=server_session["accessToken"], body=dict(PREFERENCE),
            )
            if type(validated) is not dict or validated.get("valid") is not True:
                raise RunFailure("Java catalog authority rejected frozen tuple")
            candidate_id = await memory_bff.store_validated_candidate_for_binding(
                session_binding=session_binding,
                preference=dict(PREFERENCE),
                display_text=str(validated.get("displayLabel", "目录已验证偏好")),
            )
            if type(candidate_id) is not str:
                raise RunFailure("validated candidate was not stored")
            cards = await _expect(await clients[0].get("/api/web-memory/candidates"), 200, "session1 candidates")
            if len(cards.get("candidates", [])) != 1 or cards["candidates"][0].get("status") != "pending":
                raise RunFailure("session1 candidate isolation failed")
            confirmed = await _expect(await clients[0].post(
                f"/api/web-memory/candidates/{candidate_id}/decision",
                json={"action": "confirm"},
                headers={**ORIGIN_HEADERS, memory_bff.CSRF_HEADER: csrf_tokens[0]},
            ), 200, "session1 confirm")
            if confirmed.get("confirmed") is not True:
                raise RunFailure("session1 confirmation failed")
            first_after = await _expect(await clients[0].get("/api/web-memory/entries"), 200, "session1 entries after")
            if len(first_after.get("entries", [])) != 1:
                raise RunFailure("session1 projection did not expose confirmed memory")
            resolution1 = await memory_bff.resolve_memory_run_for_browser_session(
                cookies[0], category_id="phone", recipient_scope="self",
                catalog_revision=CATALOG_REVISION, task_id=ids[0]["taskId"],
            )
            if len(resolution1.binding.preferences) != 1:
                raise RunFailure("session1 V3 resolver did not retain memory")
            session_traces.append({
                **ids[0], "cookieSha256": digest_text(cookies[0]),
                "projectionBeforeCount": 0, "projectionAfterCount": 1,
                "projectionRevision": first_after.get("revision"),
                "resolverSummary": resolution1.summary,
                "catalogValidated": True, "candidateConfirmed": True,
            })

            second_cards = await _expect(await clients[1].get("/api/web-memory/candidates"), 200, "session2 candidates")
            if second_cards.get("candidates") != []:
                raise RunFailure("candidate handle crossed browser sessions")
            second_entries = await _expect(await clients[1].get("/api/web-memory/entries"), 200, "session2 entries")
            if len(second_entries.get("entries", [])) != 1:
                raise RunFailure("session2 did not recall cross-session memory")
            resolution2 = await memory_bff.resolve_memory_run_for_browser_session(
                cookies[1], category_id="phone", recipient_scope="self",
                catalog_revision=CATALOG_REVISION, task_id=ids[1]["taskId"],
            )
            if len(resolution2.binding.preferences) != 1:
                raise RunFailure("session2 V3 resolver did not retain memory")
            pack = _pack(ids[1]["taskId"], ids[1]["conversationId"])
            planner = ContextProjector(
                pack, long_term_memory_context=resolution2.binding,
                long_term_memory_enabled=True,
            ).planner_view(["search_products"], "ready", user_message="推荐二手手机")
            expected_channel = [{
                "categoryId": "phone", "preferenceKind": "prefer",
                "attributeKey": PREFERENCE["attributeKey"],
                "normalizedValue": PREFERENCE["normalizedValue"],
            }]
            if planner.long_term_memory != expected_channel:
                raise RunFailure("ContextProjector did not publish bounded memory")
            if any(secret in json.dumps(planner.model_dump(by_alias=True), ensure_ascii=False) for secret in ("entryId", "ownerBinding", "memoryRevision")):
                raise RunFailure("ContextProjector leaked authority identifiers")
            reranked, rerank_receipt = rerank_product_presentations(
                resolution2.binding, presentations, weight=settings.memory_rerank_lambda,
            )
            input_ids = [str(item["productId"]) for item in presentations]
            output_ids = [str(item["productId"]) for item in reranked]
            if output_ids[0] != fixture["preferredSecondProductId"] or set(output_ids) != set(input_ids):
                raise RunFailure("production soft rerank did not change order without filtering")
            override_pack = _pack(ids[1]["taskId"], ids[1]["conversationId"], override=True)
            override_view = ContextProjector(
                override_pack, long_term_memory_context=resolution2.binding,
                long_term_memory_enabled=True,
            ).planner_view(["search_products"], "ready", user_message="这次要非原装屏")
            if override_view.long_term_memory != []:
                raise RunFailure("current task did not suppress long-term memory")
            override_ranked, override_receipt = rerank_product_presentations(
                resolution2.binding, presentations, weight=settings.memory_rerank_lambda,
                suppressed_attribute_keys=frozenset({PREFERENCE["attributeKey"]}),
            )
            if [str(item["productId"]) for item in override_ranked] != input_ids:
                raise RunFailure("current task override did not restore base ranking")
            public_entry = second_entries["entries"][0]
            memory_handle = public_entry.get("memoryHandle")
            if type(memory_handle) is not str:
                raise RunFailure("session2 did not receive opaque memory handle")
            revoked = await _expect(await clients[1].delete(
                f"/api/web-memory/entries/{memory_handle}",
                headers={**ORIGIN_HEADERS, memory_bff.CSRF_HEADER: csrf_tokens[1]},
            ), 200, "session2 revoke")
            if revoked.get("revoked") is not True:
                raise RunFailure("session2 revoke failed")
            second_after = await _expect(await clients[1].get("/api/web-memory/entries"), 200, "session2 entries after revoke")
            if second_after.get("entries") != []:
                raise RunFailure("revoked memory remained in projection")
            session_traces.append({
                **ids[1], "cookieSha256": digest_text(cookies[1]),
                "projectionBeforeCount": 1, "projectionAfterRevokeCount": 0,
                "projectionRevisionBefore": second_entries.get("revision"),
                "projectionRevisionAfter": second_after.get("revision"),
                "resolverSummary": resolution2.summary,
                "contextProjectorChannel": planner.long_term_memory,
                "currentTaskOverrideChannel": override_view.long_term_memory,
                "rerank": rerank_receipt,
                "overrideRerank": override_receipt,
                "memoryHandleSha256": digest_text(memory_handle),
                "revoked": True,
            })

            third_entries = await _expect(await clients[2].get("/api/web-memory/entries"), 200, "session3 entries")
            if third_entries.get("entries") != []:
                raise RunFailure("session3 observed revoked memory")
            resolution3 = await memory_bff.resolve_memory_run_for_browser_session(
                cookies[2], category_id="phone", recipient_scope="self",
                catalog_revision=CATALOG_REVISION, task_id=ids[2]["taskId"],
            )
            if resolution3.binding.preferences:
                raise RunFailure("session3 V3 resolver retained revoked memory")
            third_pack = _pack(ids[2]["taskId"], ids[2]["conversationId"])
            third_view = ContextProjector(
                third_pack, long_term_memory_context=resolution3.binding,
                long_term_memory_enabled=True,
            ).planner_view(["search_products"], "ready", user_message="推荐二手手机")
            third_ranked, third_receipt = rerank_product_presentations(
                resolution3.binding, presentations, weight=settings.memory_rerank_lambda,
            )
            if third_view.long_term_memory != [] or [str(item["productId"]) for item in third_ranked] != input_ids:
                raise RunFailure("session3 still had a memory effect")
            session_traces.append({
                **ids[2], "cookieSha256": digest_text(cookies[2]),
                "projectionCount": 0, "projectionRevision": third_entries.get("revision"),
                "resolverSummary": resolution3.summary,
                "contextProjectorChannel": third_view.long_term_memory,
                "rerank": third_receipt,
            })

            task_modes = [await redis_client.get(memory_bff._task_mode_key(item["taskId"])) for item in ids]
            checks = {
                "javaHealthy": True,
                "mysqlContainerRunning": True,
                "redisRealPing": True,
                "threeDistinctCookies": len(set(cookies)) == 3,
                "threeDistinctConversationIds": len({item["conversationId"] for item in ids}) == 3,
                "threeDistinctTaskIds": len({item["taskId"] for item in ids}) == 3,
                "realCatalogValidation": True,
                "realConsentCommandProjection": first_after.get("revision") is not None,
                "crossSessionRecall": second_entries.get("entries") != [],
                "productionV3Resolver": resolution2.summary.get("reason") == "available_retained",
                "productionContextProjector": planner.long_term_memory == expected_channel,
                "productionSoftRerankChangedOrder": output_ids != input_ids,
                "candidateSetUnchanged": set(output_ids) == set(input_ids),
                "currentTaskOverrideWins": override_view.long_term_memory == [],
                "revocationVisible": second_after.get("entries") == [],
                "thirdSessionNoMemoryEffect": third_view.long_term_memory == [] and [str(item["productId"]) for item in third_ranked] == input_ids,
                "memoryTasksStickyNonDurable": task_modes[:2] == ["memory_non_durable", "memory_non_durable"],
                "emptyThirdTaskNotMarked": task_modes[2] is None,
                "noModelCalls": True,
                "defaultsUnmodifiedOnDisk": True,
            }
            decision = verdict_for_checks(checks)
            trace = {
                "schemaVersion": SCHEMA,
                "attemptId": args.attempt_id,
                "startedAt": started_at,
                "finishedAt": datetime.now(timezone.utc).isoformat(),
                "scope": {
                    "included": [
                        "real_bff_asgi", "real_java_auth_catalog_consent_command_projection",
                        "real_redis", "production_v3_resolver", "production_context_projector",
                        "production_soft_rerank", "three_isolated_browser_sessions",
                    ],
                    "notEvaluated": [
                        "llm_extraction", "memory_candidate_worker", "browser_rendering",
                        "full_chat_agent", "default_enablement", "external_confirmation",
                    ],
                },
                "infrastructure": {
                    "javaBaseUrl": args.backend_url,
                    "javaActuatorStatus": "UP",
                    "mysqlContainer": args.mysql_container,
                    "mysqlState": {"status": mysql_state.get("Status"), "running": mysql_state.get("Running")},
                    "redisUrl": args.redis_url,
                    "redisPing": "PONG",
                    "accountRegisteredThisRun": registered,
                },
                "fixture": fixture,
                "sessions": session_traces,
                "taskModes": task_modes,
                "checks": checks,
                "decision": decision,
            }
            report = {
                "schemaVersion": f"{SCHEMA}-report",
                "attemptId": args.attempt_id,
                "decision": decision,
                "checks": checks,
                "sessionCount": 3,
                "catalogRevision": CATALOG_REVISION,
                "rerankWeight": settings.memory_rerank_lambda,
                "inputFirstProductId": input_ids[0],
                "memoryRerankedFirstProductId": output_ids[0],
                "productionDefaultChanged": False,
                "limits": [
                    "NO_LLM_EXTRACTION", "NO_BROWSER_RENDER", "NO_FULL_CHAT_AGENT",
                    "NO_DEFAULT_SWITCH", "NO_VALIDATION_OR_SEALED_DATA",
                ],
            }
        finally:
            for client in clients:
                await client.aclose()

        source_hashes_after = {str(path.relative_to(REPOSITORY_ROOT)): sha256_file(path) for path in source_paths}
        if source_hashes_before != source_hashes_after:
            raise RunFailure("source changed during E2E execution")
        receipt = {
            "schemaVersion": f"{SCHEMA}-receipt",
            "attemptId": args.attempt_id,
            "decision": report["decision"],
            "runnerSha256": source_hashes_after[str(runner_path.relative_to(REPOSITORY_ROOT))],
            "sourceSha256": source_hashes_after,
            "inputSha256": {
                str(CATALOG_MANIFEST.relative_to(REPOSITORY_ROOT)): EXPECTED_MANIFEST_SHA256,
                str(CATALOG_VALUES.relative_to(REPOSITORY_ROOT)): EXPECTED_VALUES_SHA256,
                str(PRODUCT_CATALOG.relative_to(REPOSITORY_ROOT)): EXPECTED_PRODUCT_SHA256,
            },
            "infrastructureBinding": {
                "javaBaseUrl": args.backend_url,
                "redisUrl": args.redis_url,
                "mysqlContainer": args.mysql_container,
            },
            "artifactSha256": {},
        }
        return trace, {"report": report, "receipt": receipt}
    finally:
        await redis_client.aclose()


def materialize(attempt_dir: Path, trace: dict[str, Any], bundle: dict[str, Any]) -> None:
    assert_artifact_safe(trace)
    report = bundle["report"]
    receipt = bundle["receipt"]
    assert_artifact_safe(report)
    assert_artifact_safe(receipt)
    trace_path = attempt_dir / "trace.json"
    report_path = attempt_dir / "report.json"
    receipt_path = attempt_dir / "receipt.json"
    write_json_exclusive(trace_path, trace)
    write_json_exclusive(report_path, report)
    receipt["artifactSha256"] = {
        "trace.json": sha256_file(trace_path),
        "report.json": sha256_file(report_path),
    }
    write_json_exclusive(receipt_path, receipt)
    sums = {
        "trace.json": sha256_file(trace_path),
        "report.json": sha256_file(report_path),
        "receipt.json": sha256_file(receipt_path),
    }
    with (attempt_dir / "SHA256SUMS.txt").open("x", encoding="utf-8", newline="\n") as stream:
        for name in sorted(sums):
            stream.write(f"{sums[name]}  {name}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-url", default="http://127.0.0.1:18084")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16381/0")
    parser.add_argument("--mysql-container", default="agent-memory-v13-e2e-mysql-20260830")
    parser.add_argument("--username", default="memory-v13-e2e")
    parser.add_argument("--password-env", default="MEMORY_V13_E2E_PASSWORD")
    parser.add_argument("--attempt-id", default="shopping_memory_v13_real_infra_three_session_v1_attempt001_20260830")
    parser.add_argument(
        "--output-dir", type=Path,
        default=AGENT_ROOT / "evaluation/results/shopping_memory_v13_real_infra_three_session_v1_attempt001_20260830",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        create_attempt_dir(args.output_dir)
    except FileExistsError:
        print(f"attempt directory already exists: {args.output_dir}", file=sys.stderr)
        return 2
    try:
        trace, bundle = asyncio.run(execute(args, args.output_dir))
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
            "decision": "HOLD_REAL_INFRA_E2E_FAILED",
            "errorType": type(exc).__name__,
            "error": str(exc),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            assert_artifact_safe(failure)
            write_json_exclusive(args.output_dir / "failure.json", failure)
        except Exception:
            pass
        print(f"E2E failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
