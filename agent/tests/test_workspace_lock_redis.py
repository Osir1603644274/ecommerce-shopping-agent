"""Opt-in real Redis fault tests; only isolated workspace keys and synthetic catalog facts.

WORKSPACE_LOCK_TEST_REDIS_URL must point to a disposable Redis instance.
The ASGI requests use the real selection route, identity/CSRF, lock and state writes.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import secrets
import threading

import pytest
from redis.asyncio import Redis

from app.api import commerce_workspace as ws
from .test_commerce_workspace import async_test, client
from fastapi import FastAPI


@asynccontextmanager
async def environment(monkeypatch, tmp_path):
    url = os.environ.get('WORKSPACE_LOCK_TEST_REDIS_URL')
    if not url:
        pytest.skip('set WORKSPACE_LOCK_TEST_REDIS_URL to an isolated Redis')
    redis = Redis.from_url(url, decode_responses=True)
    await redis.ping()
    epoch = 'lock-test-' + secrets.token_hex(12)
    monkeypatch.setattr(ws.auth, '_client', redis)
    monkeypatch.setattr(ws.auth.settings, 'commerce_demo_enabled', True)
    monkeypatch.setattr(ws.auth.settings, 'commerce_workspace_epoch', epoch)
    monkeypatch.setattr(ws.auth.settings, 'web_query_intake_path', str(tmp_path / 'queries.sqlite3'))
    app = FastAPI()
    app.include_router(ws.router)
    try:
        async with client(app) as a, client(app) as b:
            response = await a.get('/api/commerce-demo/workspace')
            assert response.status_code == 200
            a.headers[ws.auth.CSRF_HEADER] = response.json()['csrfToken']
            b.cookies.update(a.cookies)
            b.headers[ws.auth.CSRF_HEADER] = response.json()['csrfToken']
            keys = [k async for k in redis.scan_iter(match=f'commerce:workspace:{epoch}:*:guest')]
            assert len(keys) == 1
            key = keys[0]
            state = await ws._load(key)
            state['messages'] = [dict(role='assistant', requestId='fixture', content='test cards',
                                      source='automated_test', cards=[{'id':101}, {'id':202}])]
            async with ws._lock(key):
                await ws._save(key, state)
            yield redis, key, a, b
    finally:
        keys = [k async for k in redis.scan_iter(match=f'commerce:workspace:{epoch}:*')]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


def card(iid):
    return dict(id=iid, title=f'synthetic phone {iid}', priceMinor=100, available=2, purchasable=True)


async def expire_lock(redis, key):
    # Accelerate real Redis TTL expiry; do not wait 240 seconds or delete a live service key.
    assert await redis.pexpire(key + ':lock', 30)
    for _ in range(100):
        if not await redis.exists(key + ':lock'):
            return
        await asyncio.sleep(.01)
    raise AssertionError('test lock did not expire')


@pytest.mark.parametrize('successor', ['completed', 'holding', 'absent'])
@async_test
async def test_expired_selection_cannot_overwrite_successor(monkeypatch, tmp_path, successor):
    async with environment(monkeypatch, tmp_path) as (redis, key, a, b):
        entered, resume = asyncio.Event(), asyncio.Event()

        async def lookup(iid):
            if iid == 101:
                entered.set()
                await asyncio.wait_for(resume.wait(), 5)
            return card(iid)

        monkeypatch.setattr(ws, '_card', lookup)
        task = asyncio.create_task(a.post('/api/commerce-demo/workspace/selection', json={'productId':101}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await expire_lock(redis, key)
            if successor == 'completed':
                result = await b.post('/api/commerce-demo/workspace/selection', json={'productId':202})
                assert result.status_code == 200
            elif successor == 'holding':
                # A successor owns the same key; the old holder must not save or unlock it.
                assert await redis.set(key + ':lock', 'successor-token', nx=True, ex=5)
            before = await redis.get(key)
            resume.set()
            result = await task
            after = await redis.get(key)
            print(json.dumps(dict(scenario=successor, stale_http=result.status_code,
                                  state_unchanged=before == after), ensure_ascii=False))
            assert result.status_code == 409
            assert after == before
            if successor == 'completed':
                assert json.loads(after)['selection']['product']['id'] == 202
            if successor == 'holding':
                assert await redis.get(key + ':lock') == 'successor-token'
            # After the old request is gone, a fresh request works normally.
            if successor != 'holding':
                retry = await a.post('/api/commerce-demo/workspace/selection', json={'productId':101})
                assert retry.status_code == 200
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@async_test
async def test_unexpired_competing_selection_is_rejected(monkeypatch, tmp_path):
    async with environment(monkeypatch, tmp_path) as (redis, key, a, b):
        entered, resume = asyncio.Event(), asyncio.Event()

        async def lookup(iid):
            entered.set()
            await asyncio.wait_for(resume.wait(), 5)
            return card(iid)

        monkeypatch.setattr(ws, '_card', lookup)
        task = asyncio.create_task(a.post('/api/commerce-demo/workspace/selection', json={'productId':101}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert 0 < await redis.ttl(key + ':lock') <= 240
            rejected = await b.post('/api/commerce-demo/workspace/selection', json={'productId':202})
            assert rejected.status_code == 409
            resume.set()
            assert (await task).status_code == 200
            assert not await redis.exists(key + ':lock')
            assert (await ws._load(key))['selection']['product']['id'] == 101
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@async_test
async def test_expiry_during_archive_cannot_pass_final_atomic_save(monkeypatch, tmp_path):
    from app.workspace_archive import WorkspaceArchive
    async with environment(monkeypatch, tmp_path) as (redis, key, a, b):
        entered, resume = threading.Event(), threading.Event()
        original = WorkspaceArchive.record

        def archive(self, owner_key, state, **kwargs):
            if (state.get('selection') or {}).get('product', {}).get('id') == 101:
                entered.set()
                if not resume.wait(5):
                    raise TimeoutError('archive injection not resumed')
            return original(self, owner_key, state, **kwargs)

        async def lookup(iid):
            return card(iid)

        monkeypatch.setattr(ws, '_card', lookup)
        monkeypatch.setattr(WorkspaceArchive, 'record', archive)
        task = asyncio.create_task(a.post('/api/commerce-demo/workspace/selection', json={'productId':101}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await expire_lock(redis, key)
            assert (await b.post('/api/commerce-demo/workspace/selection', json={'productId':202})).status_code == 200
            before = await redis.get(key)
            resume.set()
            assert (await task).status_code == 409
            assert await redis.get(key) == before
        finally:
            resume.set()
            await asyncio.gather(task, return_exceptions=True)


@async_test
async def test_unlocked_and_inherited_task_cannot_save(monkeypatch, tmp_path):
    async with environment(monkeypatch, tmp_path) as (redis, key, a, b):
        state = await ws._load(key)
        before = await redis.get(key)
        with pytest.raises(RuntimeError, match='current task'):
            await ws._save(key, state)
        async with ws._lock(key):
            # asyncio propagates ContextVars to children, but not lock ownership.
            with pytest.raises(RuntimeError, match='current task'):
                await asyncio.create_task(ws._save(key, state))
            await ws._save(key + ':run', {'status':'waiting'})
            assert json.loads(await redis.get(key + ':run'))['status'] == 'waiting'
            await expire_lock(redis, key)
            with pytest.raises(ws.HTTPException) as error:
                await ws._save(key + ':run', {'status':'completed'})
            assert error.value.status_code == 409
            assert json.loads(await redis.get(key + ':run'))['status'] == 'waiting'
        assert await redis.get(key) == before


@async_test
async def test_cancellation_releases_owned_lock(monkeypatch, tmp_path):
    async with environment(monkeypatch, tmp_path) as (redis, key, a, b):
        entered = asyncio.Event()

        async def hold():
            async with ws._lock(key):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await redis.exists(key + ':lock')
        async with ws._lock(key):
            await ws._save(key, await ws._load(key))
