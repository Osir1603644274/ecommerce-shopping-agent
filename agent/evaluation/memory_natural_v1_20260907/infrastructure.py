"""Owned first-edition authority service; fresh database, no production services.

Build from a current source copy on D. Do not touch the shared backend target,
old experiment containers, or any user database. Secrets stay in process and
Docker's service environment; none are written to evidence or stdout.
"""
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = Path("D:/agent-experiments/memory-natural-v1-20260907/infrastructure001")
PREFIX = "memory-natural-v1-20260907"
LABEL = "codex.memory-natural.owner=" + PREFIX


def command(args, timeout=60, env=None):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        # Do not interpolate the original arguments (may include generated passwords).
        raise RuntimeError("infrastructure_command_failed:" + args[1])
    return result.stdout.strip()


def write(name, value):
    with (OUT / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)


def main():
    OUT.mkdir(exist_ok=False)
    source = OUT / "backend"
    shutil.copytree(ROOT / "backend", source,
        ignore=shutil.ignore_patterns("target", ".git", ".env", "*.log"))
    hashes = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in source.rglob("*") if path.is_file()}
    write("sources.json", hashes)
    build = PREFIX + "-build"
    print("BUILD_STARTED", flush=True)
    with (OUT / "build.log").open("x", encoding="utf-8") as log:
        result = subprocess.run(["docker", "run", "--name", build, "--label", LABEL,
            "--mount", f"type=bind,source={source},target=/workspace",
            "--mount", "type=volume,source=local-life-maven-cache,target=/root/.m2",
            "-w", "/workspace", "maven:3.9-eclipse-temurin-17",
            "mvn", "-B", "-Dmaven.test.skip=true", "package"],
            stdout=log, stderr=subprocess.STDOUT, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        write("failure.json", {"stage": "build", "exitCode": result.returncode})
        raise RuntimeError("isolated_backend_build_failed")
    jar = source / "target/local-life-backend-0.1.0-SNAPSHOT.jar"
    names = {key: PREFIX + "-" + key for key in ("net", "mysql", "redis", "java")}
    for key in ("mysql", "redis", "java"):
        existing = command(["docker", "ps", "-a", "--filter", "name=^/" + names[key] + "$",
                            "--format", "{{.Names}}"])
        if existing:
            raise RuntimeError("infrastructure_name_already_exists")
    command(["docker", "network", "create", "--label", LABEL, names["net"]])
    password = secrets.token_urlsafe(28)
    jwt = secrets.token_urlsafe(48)
    command(["docker", "run", "-d", "--name", names["mysql"], "--label", LABEL,
        "--network", names["net"], "-e", "MYSQL_ROOT_PASSWORD=" + password,
        "-e", "MYSQL_DATABASE=memory_first", "-e", "MYSQL_USER=memory_first",
        "-e", "MYSQL_PASSWORD=" + password, "mysql:8.4"])
    command(["docker", "run", "-d", "--name", names["redis"], "--label", LABEL,
        "--network", names["net"], "redis:7-alpine"])
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    variables = {"DB_HOST": names["mysql"], "DB_PORT": "3306", "DB_NAME": "memory_first",
        "DB_USER": "memory_first", "DB_PASSWORD": password, "JWT_SECRET": jwt,
        "REDIS_HOST": names["redis"], "REDIS_PORT": "6379", "SHOPPING_MEMORY_ENABLED": "true",
        "SEARCH_ENABLED": "false", "MESSAGING_ENABLED": "false", "FLASH_SALE_ENABLED": "false",
        "RATE_LIMIT_ENABLED": "false", "PAYMENT_SIMULATOR_ENABLED": "false",
        "SHOP_CACHE_ENABLED": "false", "PRODUCT_CACHE_ENABLED": "false",
        "MANAGEMENT_HEALTH_MAIL_ENABLED": "false"}
    # MySQL initialization is allowed a bounded readiness interval before Java.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        logs = command(["docker", "logs", names["mysql"]])
        # mysql emits on stderr; use a direct bounded readiness command instead.
        result = subprocess.run(["docker", "exec", names["mysql"], "mysqladmin", "ping", "--silent"],
            capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError("isolated_mysql_start_timeout")
    args = ["docker", "run", "-d", "--name", names["java"], "--label", LABEL,
        "--network", names["net"], "-p", f"127.0.0.1:{port}:8080",
        "--mount", f"type=bind,source={jar},target=/app/app.jar,readonly"]
    for key, value in variables.items():
        args.extend(["-e", key + "=" + value])
    args.extend(["eclipse-temurin:17-jre", "java", "-jar", "/app/app.jar"])
    command(args)
    backend = f"http://127.0.0.1:{port}"
    write("started.json", {"names": names, "backendUrl": backend,
        "jarSha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
        "freshDatabase": True, "globalSettingsChanged": False,
        "sourceProvenance": "current copied backend sources; full compile; tests skipped"})
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            response = httpx.get(backend + "/actuator/health", timeout=3)
            if response.status_code == 200:
                write("ready.json", {"backendUrl": backend, "status": response.json().get("status")})
                print("AUTHORITY_READY " + backend, flush=True)
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    write("failure.json", {"stage": "java_readiness"})
    raise RuntimeError("isolated_java_start_timeout")


if __name__ == "__main__":
    main()
