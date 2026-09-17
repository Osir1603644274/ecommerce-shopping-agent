"""Owned Redis, correct import/cwd/temp boundaries, no external HTTP."""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from .common import HERE, ROOT, file_sha, json_new, now


def child(out, scratch):
    import httpx
    import pytest
    blocked = []
    original_async, original_sync = httpx.AsyncClient.send, httpx.Client.send
    def assert_local(client, request):
        # A fake URL routed to an in-process fixture is not network traffic.
        transport = client._transport_for_url(request.url)
        if isinstance(transport, (httpx.MockTransport, httpx.ASGITransport)):
            return
        if request.url.host not in {'testserver', '127.0.0.1', 'localhost', '::1'}:
            blocked.append({'host': request.url.host, 'method': request.method})
            raise RuntimeError('external_http_disabled_in_offline_suite')
    async def guarded_async(self, request, *args, **kwargs):
        assert_local(self, request)
        return await original_async(self, request, *args, **kwargs)
    def guarded_sync(self, request, *args, **kwargs):
        assert_local(self, request)
        return original_sync(self, request, *args, **kwargs)
    with patch.object(httpx.AsyncClient, 'send', guarded_async), patch.object(httpx.Client, 'send', guarded_sync):
        code = pytest.main(['agent/tests', '-q', '--junitxml=' + str(out / 'pytest.xml'), '--basetemp=' + str(scratch / 'cases')])
    json_new(out / 'http_guard.json', {'blockedRequests': blocked, 'externalProviderCalls': 0, 'exitCode': code})
    return code


def run(attempt):
    out = HERE / 'p8' / attempt
    out.mkdir(parents=True, exist_ok=False)
    runtime = out / 'runtime'; runtime.mkdir()
    scratch = Path(tempfile.mkdtemp(prefix='context-v3-offline-'))
    if scratch.is_relative_to(ROOT): raise RuntimeError('scratch_must_be_external')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    env = dict(os.environ)
    env.update(PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / 'agent')]), PYTHONIOENCODING='utf-8',
        TASK_STATE_ATOMIC_REDIS_URL=f'redis://127.0.0.1:{port}/0',
        DEEPSEEK_API_KEY='offline-placeholder-not-a-credential', OPENAI_API_KEY='offline-placeholder-not-a-credential')
    redis_exe = shutil.which('redis-server') or r'C:\Program Files\Redis\redis-server.exe'
    with (out / 'redis.log').open('x', encoding='utf-8') as rlog, (out / 'pytest.log').open('x', encoding='utf-8') as log:
        server = subprocess.Popen([redis_exe, '--bind', '127.0.0.1', '--port', str(port),
            '--save', '', '--appendonly', 'no', '--dir', str(runtime)], stdout=rlog, stderr=subprocess.STDOUT, creationflags=flags)
        process = None
        try:
            import redis
            client = redis.Redis(host='127.0.0.1', port=port, socket_timeout=1)
            for _ in range(30):
                try:
                    if client.ping(): break
                except redis.RedisError: time.sleep(.2)
            else: raise RuntimeError('owned_redis_not_ready')
            assert client.dbsize() == 0
            command = [sys.executable, '-m', 'agent.evaluation.context_program_v3_20260904.offline_suite',
                       '--child', str(out), '--scratch', str(scratch)]
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            json_new(out / 'started.json', {'at': now(), 'pid': process.pid, 'redisPid': server.pid,
                'redisPort': port, 'scratchRetained': str(scratch), 'cwd': str(ROOT), 'command': command,
                'hardTimeoutSeconds': 5400, 'dummyKeyAndHTTPGuard': True, 'runnerSha256': file_sha(__file__)})
            started = time.monotonic()
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed > 5400: raise TimeoutError('owned_suite_hard_timeout')
                print(f'P8 alive pid={process.pid} elapsed={int(elapsed)}s logBytes={(out / "pytest.log").stat().st_size}', flush=True)
                try: process.wait(timeout=30)
                except subprocess.TimeoutExpired: pass
            json_new(out / 'result.json', {'at': now(), 'exitCode': process.returncode,
                'durationSeconds': time.monotonic()-started, 'providerCalls': 0})
        finally:
            if process is not None and process.poll() is None:
                # Only the child process tree created and recorded above.
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], creationflags=flags, capture_output=True)
            if server.poll() is None:
                server.terminate()
                try: server.wait(timeout=10)
                except subprocess.TimeoutExpired: server.kill(); server.wait()
    print('P8 complete; inspect JUnit', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--attempt')
    parser.add_argument('--child', type=Path)
    parser.add_argument('--scratch', type=Path)
    args = parser.parse_args()
    if args.child: sys.exit(child(args.child, args.scratch))
    if not args.attempt or not __import__('re').fullmatch('full[0-9]{3}', args.attempt): raise ValueError('invalid_attempt')
    run(args.attempt)
