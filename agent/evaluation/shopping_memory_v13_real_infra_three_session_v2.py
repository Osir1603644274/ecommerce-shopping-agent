"""Strengthened real-infrastructure, no-model, three-session Memory V13 E2E.

V2 closes the attempt001 evidence gaps: every conversation is bound to a real
Redis TaskState, Java is bound by container inspection to the isolated MySQL,
Flyway/catalog and per-account memory revisions are read through SQL, and all
loaded ``get_client`` aliases are replaced by a fail-fast model-call tripwire.
Existing attempt directories are never overwritten.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx


AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = AGENT_ROOT.parent
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from evaluation import shopping_memory_v13_real_infra_three_session_v1 as base


SCHEMA = "shopping-memory-v13-real-infra-three-session-v2"
VERDICT = "REAL_INFRA_NO_MODEL_THREE_SESSION_V2_ACCEPT"
CLAIM_UPPER_BOUND = base.VERDICT
HOLD = "HOLD_REAL_INFRA_E2E_FAILED"
ASGI_BASE_URL = base.ASGI_BASE_URL
ORIGIN_HEADERS = base.ORIGIN_HEADERS
CATALOG_REVISION = base.CATALOG_REVISION
PREFERENCE = base.PREFERENCE
FORBIDDEN_ARTIFACT_KEY_PARTS = ("password", "accesstoken", "refreshtoken", "jwtsecret")
FORBIDDEN_RAW_ARTIFACT_KEYS = {
    "ownerbinding", "entryid", "consenteventid", "commandid",
    "memoryhandle", "csrf", "csrftoken", "sessionbinding",
    "shoppingmemorysession",
}
EXPECTED_SETTINGS_SHA256 = "25be1ef6483735145746c64b5127cbd9a14057b8c976454c0c9d27cea2ddb570"
EXPECTED_JAVA_JAR_SHA256 = "cf0104dd73771b55b8879410353fafc95471c78990883d15ca978276aeddafab"


class RunFailure(RuntimeError):
    pass


def _run(args: list[str], label: str) -> str:
    completed = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if completed.returncode != 0:
        raise RunFailure(f"{label} failed with exit {completed.returncode}")
    return completed.stdout.strip()


def _docker_inspect(container: str) -> dict[str, Any]:
    value = json.loads(_run(["docker", "inspect", container], f"inspect {container}"))
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise RunFailure(f"invalid inspect result for {container}")
    return value[0]


def _env_map(inspect: dict[str, Any]) -> dict[str, str]:
    raw = inspect.get("Config", {}).get("Env", [])
    result: dict[str, str] = {}
    if type(raw) is not list:
        raise RunFailure("container env is invalid")
    for item in raw:
        if type(item) is str and "=" in item:
            key, value = item.split("=", 1)
            result[key] = value
    return result


def inspect_database_binding(args: argparse.Namespace) -> dict[str, Any]:
    java = _docker_inspect(args.java_container)
    mysql = _docker_inspect(args.mysql_container)
    java_env = _env_map(java)
    mysql_env = _env_map(mysql)
    java_networks = java.get("NetworkSettings", {}).get("Networks", {})
    mysql_networks = mysql.get("NetworkSettings", {}).get("Networks", {})
    if type(java_networks) is not dict or type(mysql_networks) is not dict:
        raise RunFailure("container network inspect is invalid")
    common = sorted(set(java_networks) & set(mysql_networks))
    aliases = {
        alias
        for name in common
        for alias in (mysql_networks[name].get("Aliases") or [])
        if type(alias) is str
    }
    state_ok = all(
        item.get("State", {}).get("Running") is True
        and item.get("State", {}).get("Status") == "running"
        for item in (java, mysql)
    )
    ports = java.get("NetworkSettings", {}).get("Ports", {})
    port_bindings = ports.get("8080/tcp") if type(ports) is dict else None
    mounts = java.get("Mounts", [])
    jar_mounts = [
        mount for mount in mounts
        if type(mount) is dict and mount.get("Destination") == "/app/app.jar"
    ] if type(mounts) is list else []
    host_jar = Path(args.java_jar_path).resolve()
    if len(jar_mounts) != 1 or not host_jar.is_file():
        raise RunFailure("Java JAR bind mount is missing")
    jar_mount = jar_mounts[0]
    mounted_source = str(jar_mount.get("Source", "")).replace("\\", "/").casefold()
    expected_source = str(host_jar).replace("\\", "/").casefold()
    container_jar_output = _run([
        "docker", "exec", args.java_container, "sha256sum", "/app/app.jar",
    ], "container JAR hash")
    container_jar_sha = container_jar_output.split()[0].casefold()
    host_jar_sha = base.sha256_file(host_jar)
    command = java.get("Config", {}).get("Cmd")
    checks = {
        "containersRunning": state_ok,
        "singleSharedNetwork": len(common) == 1,
        "javaUsesMysqlAlias": java_env.get("DB_HOST") == "mysql" and "mysql" in aliases,
        "databaseNameBound": java_env.get("DB_NAME") == mysql_env.get("MYSQL_DATABASE") == args.database_name,
        "databaseUserBound": java_env.get("DB_USER") == mysql_env.get("MYSQL_USER"),
        "databasePortBound": java_env.get("DB_PORT") == "3306",
        "hostPort18084MapsContainer8080": port_bindings == [{"HostIp": "127.0.0.1", "HostPort": "18084"}],
        "javaJarReadOnlyBind": (
            jar_mount.get("Type") == "bind"
            and jar_mount.get("RW") is False
            and jar_mount.get("Mode") == "ro"
            and mounted_source == expected_source
        ),
        "javaProcessUsesMountedJar": command == ["java", "-jar", "/app/app.jar"],
        "hostContainerJarShaMatch": host_jar_sha == container_jar_sha,
        "javaJarMatchesFrozenContract": host_jar_sha == EXPECTED_JAVA_JAR_SHA256,
    }
    if not all(checks.values()):
        raise RunFailure("Java to MySQL container binding mismatch")
    return {
        "javaContainer": args.java_container,
        "mysqlContainer": args.mysql_container,
        "sharedNetwork": common[0],
        "databaseHost": java_env["DB_HOST"],
        "databasePort": java_env["DB_PORT"],
        "databaseName": java_env["DB_NAME"],
        "databaseUser": java_env["DB_USER"],
        "mysqlAliasPresent": True,
        "javaContainerId": java.get("Id"),
        "javaImageId": java.get("Image"),
        "javaImageName": java.get("Config", {}).get("Image"),
        "hostPortBinding": "127.0.0.1:18084->8080/tcp",
        "javaJarHostPath": str(host_jar),
        "javaJarMountReadOnly": True,
        "javaJarSha256": host_jar_sha,
        "checks": checks,
    }


def _mysql_json(args: argparse.Namespace, sql: str) -> list[dict[str, Any]]:
    output = _run([
        "docker", "exec", args.mysql_container, "sh", "-lc",
        'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -N -B -e "$1"',
        "sh", sql,
    ], "isolated MySQL query")
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line:
            continue
        value = json.loads(line)
        if type(value) is not dict:
            raise RunFailure("MySQL query did not return JSON objects")
        rows.append(value)
    return rows


def inspect_database_authority(args: argparse.Namespace) -> dict[str, Any]:
    migrations = _mysql_json(args, (
        "SELECT JSON_OBJECT('version',version,'description',description,"
        "'script',script,'success',success) FROM flyway_schema_history "
        "WHERE version IN ('13','14') ORDER BY installed_rank;"
    ))
    catalog_rows = _mysql_json(args, (
        "SELECT JSON_OBJECT('rows',COUNT(*),'revisionCount',COUNT(DISTINCT catalog_revision),"
        "'categoryCount',COUNT(DISTINCT category_id),'activeRows',SUM(active=1),"
        "'revision',MIN(catalog_revision)) FROM shopping_memory_catalog_value;"
    ))
    if len(catalog_rows) != 1:
        raise RunFailure("catalog authority count query failed")
    catalog = catalog_rows[0]
    checks = {
        "flywayV13Success": any(str(row.get("version")) == "13" and row.get("success") == 1 for row in migrations),
        "flywayV14Success": any(str(row.get("version")) == "14" and row.get("success") == 1 for row in migrations),
        "catalogRows29": catalog.get("rows") == 29,
        "catalogRevisionCount1": catalog.get("revisionCount") == 1,
        "catalogCategoryCount1": catalog.get("categoryCount") == 1,
        "catalogAllActive": catalog.get("activeRows") == 29,
        "catalogRevisionBound": catalog.get("revision") == CATALOG_REVISION,
    }
    if not all(checks.values()):
        raise RunFailure("Flyway or catalog SQL authority mismatch")
    return {"migrations": migrations, "catalog": catalog, "checks": checks}


def account_database_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9._-]{3,64}", args.username) is None:
        raise RunFailure("username is not SQL-safe")
    username = args.username.replace("'", "''")
    rows = _mysql_json(args, f"""
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
);""")
    if len(rows) != 1:
        raise RunFailure("account SQL snapshot failed")
    return rows[0]


def database_transition_checks(
    before: dict[str, Any], confirmed: dict[str, Any], revoked: dict[str, Any],
) -> dict[str, bool]:
    return {
        "freshAccountBeforeWrite": before == {
            "accountRows": 1, "memoryRows": 0, "maxMemoryVersion": 0,
            "activeRows": 0, "revokedRows": 0,
            "projectionHeadRows": 0, "projectionRevision": 0,
            "consentGrantRows": 0, "consumedConsentRows": 0,
            "commandResultRows": 0, "appliedCommandRows": 0,
        },
        "confirmationCommitted": confirmed == {
            "accountRows": 1, "memoryRows": 1, "maxMemoryVersion": 1,
            "activeRows": 1, "revokedRows": 0,
            "projectionHeadRows": 1, "projectionRevision": 1,
            "consentGrantRows": 1, "consumedConsentRows": 1,
            "commandResultRows": 1, "appliedCommandRows": 1,
        },
        "revocationCommitted": revoked == {
            "accountRows": 1, "memoryRows": 2, "maxMemoryVersion": 2,
            "activeRows": 1, "revokedRows": 1,
            "projectionHeadRows": 1, "projectionRevision": 2,
            "consentGrantRows": 2, "consumedConsentRows": 2,
            "commandResultRows": 2, "appliedCommandRows": 2,
        },
    }


def settings_defaults_from_source(path: Path) -> dict[str, bool]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, bool] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue
            if item.target.id not in {"memory_bff_enabled", "memory_projection_client_enabled"}:
                continue
            if not isinstance(item.value, ast.Constant) or type(item.value.value) is not bool:
                raise RunFailure(f"non-literal default for {item.target.id}")
            values[item.target.id] = item.value.value
    if set(values) != {"memory_bff_enabled", "memory_projection_client_enabled"}:
        raise RunFailure("memory defaults are missing from settings source")
    return values


def install_model_tripwire() -> tuple[dict[str, int], Callable[[], None]]:
    """Fail before any OpenAI client can be acquired and count attempts."""
    from app import llm

    original_get_client = llm.get_client
    original_observer = llm._observe_llm_call
    counters = {
        "getClientAttempts": 0,
        "modelObservationAttempts": 0,
        "patchedGetClientAliases": 0,
    }

    def blocked_get_client() -> object:
        counters["getClientAttempts"] += 1
        raise RunFailure("model client tripwire fired")

    def blocked_observer(*_args: object, **_kwargs: object) -> None:
        counters["modelObservationAttempts"] += 1
        raise RunFailure("model observation tripwire fired")

    patched: list[tuple[object, str, object]] = []
    for module in list(sys.modules.values()):
        if module is None or not str(getattr(module, "__name__", "")).startswith("app"):
            continue
        if getattr(module, "get_client", None) is original_get_client:
            patched.append((module, "get_client", original_get_client))
            setattr(module, "get_client", blocked_get_client)
            counters["patchedGetClientAliases"] += 1
    patched.append((llm, "_observe_llm_call", original_observer))
    llm._observe_llm_call = blocked_observer

    def restore() -> None:
        for module, name, original in reversed(patched):
            setattr(module, name, original)

    return counters, restore


def assert_artifact_safe(value: object) -> None:
    if type(value) is dict:
        for key, item in value.items():
            folded = str(key).replace("_", "").casefold()
            if any(part in folded for part in FORBIDDEN_ARTIFACT_KEY_PARTS):
                raise RunFailure(f"secret-bearing artifact key: {key}")
            if folded in FORBIDDEN_RAW_ARTIFACT_KEYS:
                raise RunFailure(f"raw authority artifact key: {key}")
            assert_artifact_safe(item)
    elif type(value) is list:
        for item in value:
            assert_artifact_safe(item)


def assert_no_secret_values(value: object, secret_values: set[str]) -> None:
    encoded = base.canonical_bytes(value)
    for secret in secret_values:
        if type(secret) is str and len(secret) >= 8 and secret.encode("utf-8") in encoded:
            raise RunFailure("runtime secret value reached an artifact")


def assert_no_authority_fields(value: object, *, allow_memory_handle: bool = False) -> None:
    forbidden = {
        "accesstoken", "refreshtoken", "ownerbinding", "entryid",
        "consenteventid", "commandid", "sessionbinding", "csrftoken",
    }
    if not allow_memory_handle:
        forbidden.add("memoryhandle")
    if type(value) is dict:
        for key, item in value.items():
            if str(key).replace("_", "").casefold() in forbidden:
                raise RunFailure(f"authority field escaped projection boundary: {key}")
            assert_no_authority_fields(item, allow_memory_handle=allow_memory_handle)
    elif type(value) is list:
        for item in value:
            assert_no_authority_fields(item, allow_memory_handle=allow_memory_handle)


def verdict_for_checks(checks: dict[str, bool]) -> str:
    return VERDICT if checks and all(value is True for value in checks.values()) else HOLD


async def _expect(response: httpx.Response, status: int, label: str) -> dict[str, Any]:
    try:
        return await base._expect(response, status, label)
    except base.RunFailure as exc:
        raise RunFailure(str(exc)) from exc


async def _login(
    app: object, username: str, password: str,
) -> tuple[httpx.AsyncClient, str, str]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ASGI_BASE_URL,
    )
    response = await client.post(
        "/api/web-memory/login",
        json={"username": username, "password": password},
        headers=ORIGIN_HEADERS,
    )
    body = await _expect(response, 200, "BFF login")
    if set(body) != {"authenticated", "username", "csrfToken"}:
        await client.aclose()
        raise RunFailure("BFF login exposed an unexpected field")
    cookie = client.cookies.get("shopping_memory_session")
    csrf = body.get("csrfToken")
    if (
        body.get("authenticated") is not True
        or body.get("username") != username
        or type(cookie) is not str or not cookie
        or type(csrf) is not str or not csrf
    ):
        await client.aclose()
        raise RunFailure("BFF login omitted opaque session or CSRF")
    return client, cookie, csrf


async def execute(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    settings_source = AGENT_ROOT / "app/settings.py"
    settings_sha_before = base.sha256_file(settings_source)
    settings_defaults = settings_defaults_from_source(settings_source)
    if settings_sha_before != EXPECTED_SETTINGS_SHA256:
        raise RunFailure("settings source hash differs from the frozen runner contract")
    if settings_defaults != {
        "memory_bff_enabled": False,
        "memory_projection_client_enabled": False,
    }:
        raise RunFailure("production memory defaults are not both disabled")
    base.configure_process(
        backend_url=args.backend_url, redis_url=args.redis_url, username=args.username,
    )

    import redis.asyncio as redis
    from app import task_state
    from app.api import memory_bff
    from app.context_pack import build_context_pack
    from app.context_view import ContextProjector
    from app.domains.ecommerce.models import ShoppingGuideState, ShoppingRequirement
    from app.domains.ecommerce.shopping_state_authority import (
        select_shopping_state_authority,
    )
    from app.domains.ecommerce.shopping_state_update import (
        build_shopping_state_transition_patch,
        clear_stale_guide_references,
    )
    from app.main import app
    from app.memory.v3_runtime import rerank_product_presentations
    from app.settings import settings

    started_at = datetime.now(timezone.utc).isoformat()
    presentations, fixture = base.load_candidate_fixture()
    runner_path = Path(__file__).resolve()
    source_paths = [
        runner_path,
        Path(base.__file__).resolve(),
        AGENT_ROOT / "app/settings.py",
        AGENT_ROOT / "app/llm.py",
        AGENT_ROOT / "app/main.py",
        AGENT_ROOT / "app/task_state.py",
        AGENT_ROOT / "app/api/memory_bff.py",
        AGENT_ROOT / "app/memory/v3_runtime.py",
        AGENT_ROOT / "app/context_view.py",
        AGENT_ROOT / "app/domains/ecommerce/shopping_state_update.py",
        AGENT_ROOT / "app/domains/ecommerce/shopping_state_authority.py",
    ]
    source_hashes_before = {
        str(path.relative_to(REPOSITORY_ROOT)): base.sha256_file(path)
        for path in source_paths
    }
    model_tripwire, restore_tripwire = install_model_tripwire()

    async with httpx.AsyncClient(base_url=args.backend_url, timeout=5.0) as java:
        actuator = await java.get("/actuator/health")
        api_health = await java.get("/api/health")
        if actuator.status_code != 200 or actuator.json().get("status") != "UP":
            raise RunFailure("Java actuator is not UP")
        if api_health.status_code != 200 or api_health.json().get("success") is not True:
            raise RunFailure("Java API health is not UP")
        password = os.environ.get(args.password_env)
        generated_password = password is None
        if password is None:
            password = "Mv13!" + secrets.token_urlsafe(24)
        runtime_secret_values: set[str] = {password}
        registration = await java.post(
            "/api/auth/register", json={"username": args.username, "password": password},
        )
        if registration.status_code == 201:
            registered = True
        elif registration.status_code == 409 and not generated_password:
            registered = False
        elif registration.status_code == 409:
            raise RunFailure("canary user already exists; provide its password environment variable")
        else:
            raise RunFailure(f"registration returned HTTP {registration.status_code}")

    database_binding = inspect_database_binding(args)
    database_authority = inspect_database_authority(args)
    database_before = account_database_snapshot(args)
    redis_client = redis.from_url(args.redis_url, decode_responses=True)
    clients: list[httpx.AsyncClient] = []
    try:
        if await redis_client.ping() is not True:
            raise RunFailure("Redis ping failed")

        guide = ShoppingGuideState(mode="recommend", category="phone")
        identities: list[dict[str, Any]] = []
        states: list[task_state.TaskState] = []
        for ordinal in (1, 2, 3):
            conversation_id = f"memory-v13-e2e-conversation-{ordinal}-{secrets.token_hex(8)}"
            state, created = await task_state.get_or_create_session_task_state(
                conversation_id,
                "推荐一台二手手机",
                task_type="ecommerce_guide",
                domain_state={
                    "origin": "real-infra-e2e", "turnCount": 0,
                    "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
                },
            )
            by_id = await task_state.get_task_state(state.task_id)
            by_session = await task_state.get_session_task_state(conversation_id)
            raw_binding = await redis_client.get(task_state._session_task_key(conversation_id))
            snapshot_exists = await redis_client.exists(task_state._state_key(state.task_id))
            v2_snapshot = state.domain_state.get("shoppingTaskStateV2")
            authority = select_shopping_state_authority(
                domain_state=state.domain_state,
                task_id=state.task_id,
                task_revision=state.revision,
                goal=state.goal,
                unknowns=state.unknowns,
                pending_questions=state.pending_questions,
                mode=settings.shopping_state_authority,
            )
            if not (
                created and by_id is not None and by_session is not None
                and by_id.task_id == by_session.task_id == state.task_id
                and by_id.session_id == by_session.session_id == conversation_id
                and raw_binding == state.task_id and snapshot_exists == 1
                and type(v2_snapshot) is dict
                and authority.source == "v2"
            ):
                raise RunFailure("real TaskState create/bind/read verification failed")
            identities.append({
                "sessionOrdinal": ordinal,
                "conversationId": conversation_id,
                "taskId": state.task_id,
                "taskRevision": state.revision,
                "taskStateCreated": created,
                "taskStateReadById": True,
                "taskStateReadBySession": True,
                "redisBindingReadBack": True,
                "shoppingTaskStateV2Schema": v2_snapshot.get("schemaVersion"),
                "shoppingStateAuthoritySource": authority.source,
            })
            states.append(state)

        cookies: list[str] = []
        csrf_tokens: list[str] = []
        for _ in range(3):
            client, cookie, csrf = await _login(app, args.username, password)
            clients.append(client)
            cookies.append(cookie)
            csrf_tokens.append(csrf)
            runtime_secret_values.update({cookie, csrf})
        if len(set(cookies)) != 3:
            raise RunFailure("BFF did not issue three distinct browser sessions")
        for cookie in cookies:
            isolated_session = await memory_bff.session_for_binding(base.digest_text(cookie))
            if isolated_session is None:
                raise RunFailure("BFF session was not persisted in real Redis")
            runtime_secret_values.update(
                value for key, value in isolated_session.items()
                if key in {"accessToken", "refreshToken", "csrfToken"}
                and type(value) is str
            )

        session_traces: list[dict[str, Any]] = []
        first_before = await _expect(await clients[0].get("/api/web-memory/entries"), 200, "session1 entries before")
        assert_no_authority_fields(first_before, allow_memory_handle=True)
        if first_before.get("entries") != []:
            raise RunFailure("fresh canary account contains pre-existing active memory")

        session_binding = base.digest_text(cookies[0])
        server_session = await memory_bff.session_for_binding(session_binding)
        if server_session is None:
            raise RunFailure("BFF worker session lookup failed")
        runtime_secret_values.update(
            value for key, value in server_session.items()
            if key in {"accessToken", "refreshToken", "csrfToken"}
            and type(value) is str
        )
        validated = await memory_bff._java(
            "POST", "/api/memory/catalog/validate/v3",
            access_token=server_session["accessToken"], body=dict(PREFERENCE),
        )
        if type(validated) is not dict or validated.get("valid") is not True:
            raise RunFailure("Java catalog authority rejected frozen tuple")
        assert_no_authority_fields(validated)
        candidate_id = await memory_bff.store_validated_candidate_for_binding(
            session_binding=session_binding,
            preference=dict(PREFERENCE),
            display_text=str(validated.get("displayLabel", "目录已验证偏好")),
        )
        if type(candidate_id) is not str:
            raise RunFailure("validated candidate was not stored")
        runtime_secret_values.add(candidate_id)
        cards = await _expect(await clients[0].get("/api/web-memory/candidates"), 200, "session1 candidates")
        assert_no_authority_fields(cards)
        if len(cards.get("candidates", [])) != 1 or cards["candidates"][0].get("status") != "pending":
            raise RunFailure("session1 candidate isolation failed")
        confirmed = await _expect(await clients[0].post(
            f"/api/web-memory/candidates/{candidate_id}/decision",
            json={"action": "confirm"},
            headers={**ORIGIN_HEADERS, memory_bff.CSRF_HEADER: csrf_tokens[0]},
        ), 200, "session1 confirm")
        if confirmed.get("confirmed") is not True:
            raise RunFailure("session1 confirmation failed")
        assert_no_authority_fields(confirmed)
        database_confirmed = account_database_snapshot(args)
        first_after = await _expect(await clients[0].get("/api/web-memory/entries"), 200, "session1 entries after")
        assert_no_authority_fields(first_after, allow_memory_handle=True)
        if len(first_after.get("entries", [])) != 1:
            raise RunFailure("session1 projection did not expose confirmed memory")
        resolution1 = await memory_bff.resolve_memory_run_for_browser_session(
            cookies[0], category_id="phone", recipient_scope="self",
            catalog_revision=CATALOG_REVISION, task_id=identities[0]["taskId"],
        )
        if len(resolution1.binding.preferences) != 1:
            raise RunFailure("session1 V3 resolver did not retain memory")
        session_traces.append({
            **identities[0], "cookieSha256": base.digest_text(cookies[0]),
            "projectionBeforeCount": 0, "projectionAfterCount": 1,
            "projectionRevision": first_after.get("revision"),
            "resolverSummary": resolution1.summary,
            "catalogValidated": True, "candidateConfirmed": True,
        })

        second_cards = await _expect(await clients[1].get("/api/web-memory/candidates"), 200, "session2 candidates")
        assert_no_authority_fields(second_cards)
        if second_cards.get("candidates") != []:
            raise RunFailure("candidate handle crossed browser sessions")
        second_entries = await _expect(await clients[1].get("/api/web-memory/entries"), 200, "session2 entries")
        assert_no_authority_fields(second_entries, allow_memory_handle=True)
        if len(second_entries.get("entries", [])) != 1:
            raise RunFailure("session2 did not recall cross-session memory")
        resolution2 = await memory_bff.resolve_memory_run_for_browser_session(
            cookies[1], category_id="phone", recipient_scope="self",
            catalog_revision=CATALOG_REVISION, task_id=identities[1]["taskId"],
        )
        if len(resolution2.binding.preferences) != 1:
            raise RunFailure("session2 V3 resolver did not retain memory")
        pack = await build_context_pack(
            states[1], allowed_tools=["search_products"], history=[],
            run_id=identities[1]["conversationId"],
        )
        if not (
            pack.task_id == states[1].task_id
            and pack.base_context_revision == states[1].revision
            and pack.shopping_state_authority_source == "v2"
        ):
            raise RunFailure("ContextPack was not built from the persisted TaskState authority")
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
        planner_payload = planner.model_dump(by_alias=True, mode="json")
        assert_no_authority_fields(planner_payload)
        reranked, rerank_receipt = rerank_product_presentations(
            resolution2.binding, presentations, weight=settings.memory_rerank_lambda,
        )
        input_ids = [str(item["productId"]) for item in presentations]
        output_ids = [str(item["productId"]) for item in reranked]
        if output_ids[0] != fixture["preferredSecondProductId"] or set(output_ids) != set(input_ids):
            raise RunFailure("production soft rerank did not change order without filtering")
        override_guide = ShoppingGuideState(
            mode="recommend", category="phone",
            requirements=[ShoppingRequirement(
                key=PREFERENCE["attributeKey"], operator="eq",
                value="non_original", unit="enum", priority="hard", source="user",
            )],
        )
        override_guide, constraints_changed = clear_stale_guide_references(
            states[1], override_guide,
        )
        if constraints_changed is not True:
            raise RunFailure("override did not register as a shopping constraint change")
        override_constraint = task_state.TaskConstraint(
            key=PREFERENCE["attributeKey"], operator="eq",
            value="non_original", source="user",
        )
        override_payload = {
            "upsertConstraints": [
                override_constraint.model_dump(by_alias=True, mode="json")
            ],
        }
        override_domain_patch = {
            "shoppingGuide": override_guide.model_dump(by_alias=True, mode="json"),
            **build_shopping_state_transition_patch(
                states[1], override_guide, override_payload,
                constraints_changed=constraints_changed,
            ),
        }
        override_state = await task_state.update_task_state(
            states[1].task_id,
            task_state.TaskStatePatchRequest(
                expected_revision=states[1].revision,
                actor="user",
                upsert_constraints=[override_constraint],
                domain_state_patch=override_domain_patch,
            ),
        )
        override_by_id = await task_state.get_task_state(override_state.task_id)
        override_by_session = await task_state.get_session_task_state(
            identities[1]["conversationId"],
        )
        if not (
            override_by_id is not None and override_by_session is not None
            and override_by_id.revision == override_by_session.revision == override_state.revision == 2
        ):
            raise RunFailure("override TaskState revision was not persisted and rebound")
        override_authority = select_shopping_state_authority(
            domain_state=override_state.domain_state,
            task_id=override_state.task_id,
            task_revision=override_state.revision,
            goal=override_state.goal,
            unknowns=override_state.unknowns,
            pending_questions=override_state.pending_questions,
            mode=settings.shopping_state_authority,
        )
        if override_authority.source != "v2":
            raise RunFailure("override TaskState did not retain V2 authority")
        states[1] = override_state
        override_pack = await build_context_pack(
            override_state, allowed_tools=["search_products"], history=[],
            run_id=identities[1]["conversationId"] + "-override",
        )
        if not (
            override_pack.task_id == override_state.task_id
            and override_pack.base_context_revision == 2
            and override_pack.shopping_state_authority_source == "v2"
        ):
            raise RunFailure("override ContextPack was not built from TaskState revision 2")
        override_view = ContextProjector(
            override_pack, long_term_memory_context=resolution2.binding,
            long_term_memory_enabled=True,
        ).planner_view(["search_products"], "ready", user_message="这次要非原装屏")
        override_ranked, override_receipt = rerank_product_presentations(
            resolution2.binding, presentations, weight=settings.memory_rerank_lambda,
            suppressed_attribute_keys=frozenset({PREFERENCE["attributeKey"]}),
        )
        if override_view.long_term_memory != [] or [str(item["productId"]) for item in override_ranked] != input_ids:
            raise RunFailure("current task override did not suppress memory")
        memory_handle = second_entries["entries"][0].get("memoryHandle")
        if type(memory_handle) is not str:
            raise RunFailure("session2 did not receive opaque memory handle")
        runtime_secret_values.add(memory_handle)
        revoked = await _expect(await clients[1].delete(
            f"/api/web-memory/entries/{memory_handle}",
            headers={**ORIGIN_HEADERS, memory_bff.CSRF_HEADER: csrf_tokens[1]},
        ), 200, "session2 revoke")
        if revoked.get("revoked") is not True:
            raise RunFailure("session2 revoke failed")
        assert_no_authority_fields(revoked)
        database_revoked = account_database_snapshot(args)
        second_after = await _expect(await clients[1].get("/api/web-memory/entries"), 200, "session2 entries after revoke")
        assert_no_authority_fields(second_after, allow_memory_handle=True)
        if second_after.get("entries") != []:
            raise RunFailure("revoked memory remained in projection")
        session_traces.append({
            **identities[1], "cookieSha256": base.digest_text(cookies[1]),
            "projectionBeforeCount": 1, "projectionAfterRevokeCount": 0,
            "projectionRevisionBefore": second_entries.get("revision"),
            "projectionRevisionAfter": second_after.get("revision"),
            "resolverSummary": resolution2.summary,
            "contextProjectorChannel": planner.long_term_memory,
            "currentTaskOverrideChannel": override_view.long_term_memory,
            "contextPackTaskRevision": pack.base_context_revision,
            "overrideTaskRevision": override_state.revision,
            "overrideTaskAuthoritySource": override_authority.source,
            "contextPackAuthoritySource": pack.shopping_state_authority_source,
            "rerank": rerank_receipt, "overrideRerank": override_receipt,
            "memoryHandleSha256": base.digest_text(memory_handle), "revoked": True,
        })

        third_entries = await _expect(await clients[2].get("/api/web-memory/entries"), 200, "session3 entries")
        assert_no_authority_fields(third_entries, allow_memory_handle=True)
        resolution3 = await memory_bff.resolve_memory_run_for_browser_session(
            cookies[2], category_id="phone", recipient_scope="self",
            catalog_revision=CATALOG_REVISION, task_id=identities[2]["taskId"],
        )
        third_pack = await build_context_pack(
            states[2], allowed_tools=["search_products"], history=[],
            run_id=identities[2]["conversationId"],
        )
        if not (
            third_pack.task_id == states[2].task_id
            and third_pack.base_context_revision == states[2].revision
            and third_pack.shopping_state_authority_source == "v2"
        ):
            raise RunFailure("session3 ContextPack was not built from persisted TaskState")
        third_view = ContextProjector(
            third_pack, long_term_memory_context=resolution3.binding,
            long_term_memory_enabled=True,
        ).planner_view(["search_products"], "ready", user_message="推荐二手手机")
        third_ranked, third_receipt = rerank_product_presentations(
            resolution3.binding, presentations, weight=settings.memory_rerank_lambda,
        )
        if third_entries.get("entries") != [] or resolution3.binding.preferences:
            raise RunFailure("session3 observed revoked memory")
        if third_view.long_term_memory != [] or [str(item["productId"]) for item in third_ranked] != input_ids:
            raise RunFailure("session3 still had a memory effect")
        session_traces.append({
            **identities[2], "cookieSha256": base.digest_text(cookies[2]),
            "projectionCount": 0, "projectionRevision": third_entries.get("revision"),
            "resolverSummary": resolution3.summary,
            "contextProjectorChannel": third_view.long_term_memory,
            "rerank": third_receipt,
        })

        task_modes = [
            await redis_client.get(memory_bff._task_mode_key(item["taskId"]))
            for item in identities
        ]
        db_transition = database_transition_checks(
            database_before, database_confirmed, database_revoked,
        )
        settings_sha_after = base.sha256_file(settings_source)
        source_hashes_after = {
            str(path.relative_to(REPOSITORY_ROOT)): base.sha256_file(path)
            for path in source_paths
        }
        task_state_checks = {
            "threeRealTaskStatesCreated": len(identities) == 3 and all(item["taskStateCreated"] for item in identities),
            "threeTaskStatesReadById": all(item["taskStateReadById"] for item in identities),
            "threeTaskStatesReadBySession": all(item["taskStateReadBySession"] for item in identities),
            "threeRedisBindingsReadBack": all(item["redisBindingReadBack"] for item in identities),
            "threeDistinctServerTaskIds": len({item["taskId"] for item in identities}) == 3,
            "threeDistinctConversationIds": len({item["conversationId"] for item in identities}) == 3,
            "shoppingTaskStateV2Present": all(item["shoppingTaskStateV2Schema"] == "shopping-task-state-v2.1" for item in identities),
            "shoppingStateAuthoritySelectedV2": all(item["shoppingStateAuthoritySource"] == "v2" for item in identities),
        }
        checks = {
            "javaHealthy": True,
            "redisRealPing": True,
            "threeDistinctCookies": len(set(cookies)) == 3,
            **task_state_checks,
            **database_binding["checks"],
            **database_authority["checks"],
            **db_transition,
            "realCatalogValidation": True,
            "realConsentCommandProjection": db_transition["confirmationCommitted"] and db_transition["revocationCommitted"],
            "crossSessionRecall": second_entries.get("entries") != [],
            "productionV3Resolver": resolution2.summary.get("reason") == "available_retained",
            "productionContextProjector": planner.long_term_memory == expected_channel,
            "contextPackBuiltFromPersistedTaskState": pack.task_id == states[1].task_id and pack.shopping_state_authority_source == "v2",
            "overridePersistedAsTaskStateRevision2": override_state.revision == 2,
            "overrideTaskStateAuthoritySelectedV2": override_authority.source == "v2",
            "productionSoftRerankChangedOrder": output_ids != input_ids,
            "candidateSetUnchanged": set(output_ids) == set(input_ids),
            "currentTaskOverrideWins": override_view.long_term_memory == [],
            "revocationVisible": second_after.get("entries") == [],
            "thirdSessionNoMemoryEffect": third_view.long_term_memory == [] and [str(item["productId"]) for item in third_ranked] == input_ids,
            "memoryTasksStickyNonDurable": task_modes[:2] == ["memory_non_durable", "memory_non_durable"],
            "emptyThirdTaskNotMarked": task_modes[2] is None,
            "modelTripwireInstalled": model_tripwire["patchedGetClientAliases"] >= 1,
            "modelTripwireUntouched": (
                model_tripwire["getClientAttempts"] == 0
                and model_tripwire["modelObservationAttempts"] == 0
            ),
            "productionDefaultsDisabledOnDisk": settings_defaults == {
                "memory_bff_enabled": False,
                "memory_projection_client_enabled": False,
            },
            "settingsSourceHashFrozen": settings_sha_before == EXPECTED_SETTINGS_SHA256,
            "settingsSourceHashStable": settings_sha_before == settings_sha_after,
            "allBoundSourceHashesStable": source_hashes_before == source_hashes_after,
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
                    "real_redis_task_state_and_session_binding", "real_mysql_flyway_catalog_and_memory_rows",
                    "production_v3_resolver", "production_context_projector",
                    "production_soft_rerank", "three_isolated_browser_sessions",
                    "fail_fast_model_client_tripwire",
                    "persisted_task_state_to_build_context_pack",
                ],
                "notEvaluated": [
                    "llm_extraction", "memory_candidate_worker", "browser_rendering",
                    "full_chat_agent", "default_enablement", "external_confirmation",
                    "real_product_retrieval",
                ],
            },
            "infrastructure": {
                "javaBaseUrl": args.backend_url,
                "javaActuatorStatus": "UP",
                "redisUrl": args.redis_url,
                "redisPing": "PONG",
                "accountRegisteredThisRun": registered,
                "databaseBinding": database_binding,
            },
            "databaseAuthority": database_authority,
            "databaseTransitions": {
                "beforeWrite": database_before,
                "afterConfirmation": database_confirmed,
                "afterRevocation": database_revoked,
                "checks": db_transition,
            },
            "fixture": fixture,
            "sessions": session_traces,
            "taskModes": task_modes,
            "modelTripwire": model_tripwire,
            "productionDefaultsFromSource": settings_defaults,
            "settingsSourceSha256": {"before": settings_sha_before, "after": settings_sha_after},
            "checks": checks,
            "decision": decision,
            "claimUpperBound": CLAIM_UPPER_BOUND,
        }
        report = {
            "schemaVersion": f"{SCHEMA}-report",
            "attemptId": args.attempt_id,
            "decision": decision,
            "checks": checks,
            "sessionCount": 3,
            "realTaskStateCount": 3,
            "catalogRevision": CATALOG_REVISION,
            "databaseMemoryRows": {
                "before": database_before["memoryRows"],
                "confirmed": database_confirmed["memoryRows"],
                "revoked": database_revoked["memoryRows"],
            },
            "databaseProjectionRevisions": {
                "before": database_before["projectionRevision"],
                "confirmed": database_confirmed["projectionRevision"],
                "revoked": database_revoked["projectionRevision"],
            },
            "databaseConsentGrantRows": {
                "before": database_before["consentGrantRows"],
                "confirmed": database_confirmed["consentGrantRows"],
                "revoked": database_revoked["consentGrantRows"],
            },
            "databaseCommandResultRows": {
                "before": database_before["commandResultRows"],
                "confirmed": database_confirmed["commandResultRows"],
                "revoked": database_revoked["commandResultRows"],
            },
            "modelTripwire": model_tripwire,
            "productionDefaultsFromSource": settings_defaults,
            "productionDefaultChanged": not (
                settings_defaults == {
                    "memory_bff_enabled": False,
                    "memory_projection_client_enabled": False,
                }
                and settings_sha_before == settings_sha_after == EXPECTED_SETTINGS_SHA256
            ),
            "claimUpperBound": CLAIM_UPPER_BOUND,
            "limits": [
                "NO_LLM_EXTRACTION", "NO_BROWSER_RENDER", "NO_FULL_CHAT_AGENT",
                "NO_DEFAULT_SWITCH", "NO_PRIVATE_EVALUATION_LABELS_READ",
                "NO_REAL_RETRIEVAL", "DETERMINISTIC_20_CANDIDATE_FIXTURE",
            ],
        }
        receipt = {
            "schemaVersion": f"{SCHEMA}-receipt",
            "attemptId": args.attempt_id,
            "decision": decision,
            "runnerSha256": source_hashes_after[str(runner_path.relative_to(REPOSITORY_ROOT))],
            "sourceSha256Before": source_hashes_before,
            "sourceSha256After": source_hashes_after,
            "inputSha256": {
                str(base.CATALOG_MANIFEST.relative_to(REPOSITORY_ROOT)): base.EXPECTED_MANIFEST_SHA256,
                str(base.CATALOG_VALUES.relative_to(REPOSITORY_ROOT)): base.EXPECTED_VALUES_SHA256,
                str(base.PRODUCT_CATALOG.relative_to(REPOSITORY_ROOT)): base.EXPECTED_PRODUCT_SHA256,
            },
            "infrastructureBinding": {
                "javaBaseUrl": args.backend_url,
                "redisUrl": args.redis_url,
                "javaContainer": args.java_container,
                "mysqlContainer": args.mysql_container,
                "sharedNetwork": database_binding["sharedNetwork"],
                "databaseHost": database_binding["databaseHost"],
                "javaContainerId": database_binding["javaContainerId"],
                "javaImageId": database_binding["javaImageId"],
                "javaJarSha256": database_binding["javaJarSha256"],
                "hostPortBinding": database_binding["hostPortBinding"],
            },
            "artifactSha256": {},
        }
        assert_artifact_safe(trace)
        assert_artifact_safe(report)
        assert_artifact_safe(receipt)
        assert_no_secret_values(trace, runtime_secret_values)
        assert_no_secret_values(report, runtime_secret_values)
        assert_no_secret_values(receipt, runtime_secret_values)
        return trace, {"report": report, "receipt": receipt}
    finally:
        restore_tripwire()
        for client in clients:
            await client.aclose()
        await redis_client.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-url", default="http://127.0.0.1:18084")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16381/0")
    parser.add_argument("--java-container", default="agent-memory-v13-e2e-java-20260830")
    parser.add_argument("--mysql-container", default="agent-memory-v13-e2e-mysql-20260830")
    parser.add_argument(
        "--java-jar-path",
        default=str(REPOSITORY_ROOT / "backend/target/local-life-backend-0.1.0-SNAPSHOT.jar"),
    )
    parser.add_argument("--database-name", default="memory_v13_e2e")
    parser.add_argument("--username", default="memory-v13-e2e-a004")
    parser.add_argument("--password-env", default="MEMORY_V13_E2E_A004_PASSWORD")
    parser.add_argument(
        "--attempt-id",
        default="shopping_memory_v13_real_infra_three_session_v2_attempt004_20260830",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=AGENT_ROOT / "evaluation/results/shopping_memory_v13_real_infra_three_session_v2_attempt004_20260830",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        base.create_attempt_dir(args.output_dir)
    except FileExistsError:
        print(f"attempt directory already exists: {args.output_dir}", file=sys.stderr)
        return 2
    try:
        trace, bundle = asyncio.run(execute(args))
        base.materialize(args.output_dir, trace, bundle)
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
            assert_artifact_safe(failure)
            base.write_json_exclusive(args.output_dir / "failure.json", failure)
        except Exception:
            pass
        print(f"E2E failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
