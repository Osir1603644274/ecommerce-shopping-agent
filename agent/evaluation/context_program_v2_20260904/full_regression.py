"""The planned P8 full suite, with an owned Redis and explicit hard timeout."""
import os
import socket
import subprocess
import sys
import time

from .common import HERE, ROOT, check_freeze, file_sha, json_new, now


def main():
    check_freeze()
    out = HERE / "p8/full001"
    out.mkdir(parents=True, exist_ok=False)
    runtime = out / "runtime"
    runtime.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    redis_exe = __import__("shutil").which("redis-server")
    if not redis_exe: raise RuntimeError("test_redis_executable_missing")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    redis_cmd = [redis_exe, "--bind", "127.0.0.1", "--port", str(port),
                 "--save", "", "--appendonly", "no", "--dir", str(runtime)]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "agent")])
    env["TASK_STATE_ATOMIC_REDIS_URL"] = f"redis://127.0.0.1:{port}/0"
    # The full suite is offline; paid provider phases use separate processes.
    env["DEEPSEEK_API_KEY"] = ""
    command = [sys.executable, "-m", "pytest", "tests", "-q", "--junitxml=" + str(out / "pytest-full.xml"),
               "--basetemp=" + str(runtime / "pytest-tmp")]
    start = time.monotonic()
    with (out / "redis.log").open("x", encoding="utf-8") as redis_log, (out / "pytest.log").open("x", encoding="utf-8") as log:
        server = subprocess.Popen(redis_cmd, stdout=redis_log, stderr=subprocess.STDOUT, creationflags=flags)
        try:
            import redis
            client = redis.Redis(host="127.0.0.1", port=port, socket_timeout=1)
            for _ in range(30):
                try:
                    if client.ping(): break
                except redis.RedisError: time.sleep(.2)
            else: raise RuntimeError("owned_redis_not_ready")
            if client.dbsize() != 0: raise RuntimeError("owned_redis_not_empty")
            child = subprocess.Popen(command, cwd=ROOT / "agent", env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            json_new(out / "started.json", {"at": now(), "command": command, "cwd": str(ROOT / "agent"),
                "pid": child.pid, "redisPid": server.pid, "redisPort": port, "hardTimeoutSeconds": 5400,
                "runnerSha256": file_sha(__file__), "importPreflightPassed": True,
                "role": "PLANNED_FULL_SUITE_NOT_P6_ATTEMPT_OVERWRITE", "providerKeyDisabled": True})
            last_size = -1
            stagnant = 0
            while child.poll() is None:
                elapsed = time.monotonic() - start
                if elapsed > 5400:
                    print("HARD_TIMEOUT: terminating owned pytest process", flush=True)
                    child.terminate()
                    try: child.wait(timeout=10)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
                    break
                size = (out / "pytest.log").stat().st_size
                stagnant = stagnant + 1 if size == last_size else 0
                last_size = size
                print(f"P8 full suite alive pid={child.pid} elapsed={int(elapsed)}s logBytes={size}" +
                      (" STALL_ADVISORY" if stagnant == 3 else ""), flush=True)
                try: child.wait(timeout=30)
                except subprocess.TimeoutExpired: pass
            json_new(out / "process_result.json", {"at": now(), "exitCode": child.returncode,
                "durationSeconds": time.monotonic()-start, "modelCalls": 0,
                "junitExists": (out / "pytest-full.xml").exists()})
        finally:
            # Normal teardown of this test-owned auxiliary service, not a live service.
            if server.poll() is None:
                server.terminate()
                try: server.wait(timeout=10)
                except subprocess.TimeoutExpired: server.kill(); server.wait()
    check_freeze()
    print("P8 full suite finished; inspect JUnit and process_result", flush=True)


if __name__ == "__main__": main()
