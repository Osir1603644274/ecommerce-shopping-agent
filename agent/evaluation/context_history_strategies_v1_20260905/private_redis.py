"""Owned, loopback-only Redis child; never accesses the user's existing DB."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import socket
import subprocess
import time
import asyncio

import redis.asyncio as redis

from .artifacts import now, write_new


@asynccontextmanager
async def private_redis(directory: Path):
    directory.mkdir(parents=True, exist_ok=False)
    executable = shutil.which("redis-server")
    if not executable:
        raise RuntimeError("isolated_redis_binary_unavailable")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    args = [executable, "--bind", "127.0.0.1", "--port", str(port), "--dir", str(directory),
            "--save", "", "--appendonly", "yes", "--appendfsync", "always"]
    process = None
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True,
                         socket_connect_timeout=1, socket_timeout=5)
    try:
        with (directory / "server.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(args, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            write_new(directory / "process.json", {"pid": process.pid, "port": port, "args": args, "at": now()})
            started = time.monotonic()
            while True:
                if process.poll() is not None:
                    raise RuntimeError("isolated_redis_child_exited")
                try:
                    info = await client.info("server")
                    if int(info["process_id"]) != process.pid:
                        raise RuntimeError("isolated_redis_pid_mismatch")
                    break
                except (redis.ConnectionError, redis.TimeoutError):
                    if time.monotonic() - started > 15:
                        raise RuntimeError("isolated_redis_startup_timeout")
                    await asyncio.sleep(0.1)
            yield client
    finally:
        # Terminate only this verified child. AOF remains in the attempt directory.
        if process is not None and process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=10)
        await client.aclose()
