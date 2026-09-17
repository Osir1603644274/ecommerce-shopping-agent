"""Serialized, cancellable process bridge to the verified full-corpus runtime.

Own one worker per isolated experiment. Never install this process-global
settings scope in a concurrent web server.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import multiprocessing as mp
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))


def retrieval_worker(connection, model_path):
    sys.path.insert(0, str(ROOT / 'experiments/search-closure-v1'))
    from retrieval_runtime import RetrievalRuntime
    runtime = None
    try:
        runtime = RetrievalRuntime()
        manifest = runtime.strategy_manifest(profile='w211', model_path=model_path)
        connection.send({'kind': 'ready', 'strategy': manifest})
        while True:
            request = connection.recv()
            if request is None:
                break
            result = runtime.search_one(request['query'], request['source'],
                profile='w211', model_path=model_path, limit=request['limit'])
            connection.send({'kind': 'result', 'result': result})
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        connection.send({'kind': 'error', 'errorType': type(exc).__name__})
    finally:
        if runtime is not None:
            runtime.close()
        connection.close()


class LiveBridge:
    def __init__(self, model_path, *, worker=retrieval_worker):
        ctx = mp.get_context('spawn')
        self.connection, child = ctx.Pipe()
        self.process = ctx.Process(target=worker, args=(child, str(model_path)), daemon=True)
        self.lock = asyncio.Lock()
        self.closed = False
        self.strategy = None
        self.process.start()
        child.close()

    async def _receive(self, timeout):
        try:
            return await self._receive_message(timeout)
        except (EOFError, OSError) as exc:
            raise RuntimeError('retrieval_worker_disconnected') from exc

    async def _receive_message(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.connection.poll():
            if not self.process.is_alive():
                raise RuntimeError('retrieval_worker_exited')
            if time.monotonic() >= deadline:
                raise TimeoutError('retrieval_worker_timeout')
            await asyncio.sleep(0.025)
        message = self.connection.recv()
        if message['kind'] == 'error':
            raise RuntimeError('retrieval_worker_' + message['errorType'])
        return message

    async def start(self, timeout=600):
        try:
            message = await self._receive(timeout)
            if message['kind'] != 'ready':
                raise ValueError('worker_not_ready')
            self.strategy = message['strategy']
            return self.strategy
        except BaseException:
            self.close()
            raise

    def provider(self, binding, *, timeout=240, on_result=None):
        from app.catalog_evidence import CatalogSearchRequest

        async def search(request):
            request = CatalogSearchRequest.model_validate(request)
            if request.binding != binding or self.strategy is None or self.closed:
                raise ValueError('live_provider_binding_or_state_invalid')
            async with self.lock:
                if self.closed:
                    raise RuntimeError('retrieval_worker_closed')
                started = time.perf_counter()
                try:
                    self.connection.send({'query': request.query, 'source': request.source, 'limit': request.limit})
                    message = await self._receive(timeout)
                    if message['kind'] != 'result':
                        raise ValueError('worker_protocol_error')
                    result = message['result']
                    if on_result:
                        on_result(request, result, time.perf_counter() - started)
                    return {'binding': binding.model_dump(by_alias=True), 'query': request.query,
                            'source': request.source, 'hits': result['hits']}
                except BaseException:
                    # Cancellation must not leave GPU computation running or a
                    # late response available to the next request.
                    self.close()
                    raise
        return search

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=3)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=3)
        self.connection.close()


@contextmanager
def experiment_settings(binding, source, *, max_tokens=2048):
    from app.settings import settings
    updates = dict(catalog_evidence_enabled=True, catalog_evidence_data_root=binding.data_root,
        catalog_evidence_run_id=binding.run_id, catalog_evidence_manifest_sha256=binding.manifest_sha256,
        catalog_evidence_source=source, catalog_evidence_answer_max_tokens=max_tokens,
        catalog_evidence_timeout_seconds=250, agent_request_deadline_seconds=330,
        model_call_receipts_enabled=False, backend_observer_enabled=False)
    original = {key: deepcopy(getattr(settings, key)) for key in updates}
    try:
        for key, value in updates.items():
            setattr(settings, key, value)
        yield
    finally:
        for key, value in original.items():
            setattr(settings, key, value)
