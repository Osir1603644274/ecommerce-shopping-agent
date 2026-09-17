"""BFF renewal: expiry, rotation races, and unchanged shopping identity."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response

from app.api import commerce_demo as auth, commerce_workspace as ws
from tests.test_commerce_demo_api import FakeRedis, async_test


class SessionRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.ttls = {}

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def eval(self, script, numkeys, *args):
        assert 'COMMERCE_SESSION_CAS' in script and numkeys == 1
        key, expected, value, ttl = args
        if self.values.get(key) != expected:
            return 0
        if not value:
            await self.delete(key)
        else:
            await self.set(key, value, ex=ttl)
        return 1


def tokens(suffix='old', user='renew-user'):
    return dict(accessToken='access-' + suffix, refreshToken='refresh-' + suffix,
                tokenType='Bearer', accessExpiresInSeconds=900, refreshExpiresInSeconds=2592000,
                user={'username': user})


@pytest.fixture
def store(monkeypatch):
    value = SessionRedis()
    monkeypatch.setattr(auth, '_client', value)
    monkeypatch.setattr(auth.settings, 'commerce_demo_enabled', True)
    return value


async def establish(store, *, due=True):
    response = Response()
    result = await auth._establish_session(tokens(), response)
    from http.cookies import SimpleCookie
    cookies = SimpleCookie(); cookies.load(response.headers['set-cookie'])
    sid = cookies[auth.COOKIE_NAME].value
    key = auth._session_key(sid)
    value = json.loads(store.values[key])
    if due:
        value['accessExpiresAtEpoch'] = int(time.time()) - 1
        store.values[key] = auth._session_json(value)
    return sid, key, value, result, cookies


@async_test
async def test_new_cookie_and_redis_survive_access_expiry(store):
    sid, key, value, result, cookie = await establish(store, due=False)
    assert int(cookie[auth.COOKIE_NAME]['max-age']) == 2591970
    assert store.ttls[key] == 2591970
    assert value['sessionExpiresAtEpoch'] > value['accessExpiresAtEpoch'] + 1800
    assert await auth._load_session(sid) == value
    assert 'access-' not in json.dumps(result) and 'refresh-' not in json.dumps(result)


@async_test
async def test_concurrent_requests_rotate_once_keep_identity_and_csrf(store, monkeypatch):
    sid, key, before, result, _ = await establish(store)
    calls = []
    async def java(method, path, **kwargs):
        calls.append(kwargs['body']['refreshToken'])
        assert (method, path) == ('POST', '/api/auth/refresh')
        await asyncio.sleep(0.1)
        return tokens('new')
    monkeypatch.setattr(auth, '_java', java)
    values = await asyncio.gather(*(auth._load_session(sid) for _ in range(8)))
    assert calls == ['refresh-old']
    for v in values:
        assert v['accessToken'] == 'access-new' and v['refreshToken'] == 'refresh-new'
        for field in ('username', 'csrfDigest', 'sessionBinding', 'sessionExpiresAtEpoch'):
            assert v[field] == before[field]
        auth._require_csrf(v, result['csrfToken'])
    assert 'refreshPendingAtEpoch' not in json.loads(store.values[key])
    assert (await auth._load_session(sid))['accessToken'] == 'access-new'
    assert len(calls) == 1


@pytest.mark.parametrize('field', ['refreshExpiresAtEpoch', 'sessionExpiresAtEpoch'])
@async_test
async def test_expired_refresh_or_absolute_session_never_calls_authority(store, monkeypatch, field):
    sid, key, value, _, _ = await establish(store)
    value[field] = int(time.time()) - 1
    store.values[key] = auth._session_json(value)
    async def java(*a, **kw): pytest.fail('expired credentials must not refresh')
    monkeypatch.setattr(auth, '_java', java)
    with pytest.raises(HTTPException) as exc:
        await auth._load_session(sid)
    assert exc.value.status_code == 401 and key not in store.values


@pytest.mark.parametrize('failure', ['rejected', 'timeout', 'wrong_user'])
@async_test
async def test_failed_or_uncertain_refresh_is_not_retried(store, monkeypatch, failure):
    sid, key, _, _, _ = await establish(store)
    calls = []
    async def java(*a, **kw):
        calls.append(1)
        if failure == 'wrong_user': return tokens('new', user='other-user')
        if failure == 'timeout': raise TimeoutError()
        raise HTTPException(401, 'authentication rejected')
    monkeypatch.setattr(auth, '_java', java)
    for _ in range(2):
        with pytest.raises(HTTPException) as exc: await auth._load_session(sid)
        assert exc.value.status_code == 401
    assert calls == [1] and key not in store.values


@async_test
async def test_crashed_refresh_owner_cannot_reuse_old_token(store, monkeypatch):
    sid, key, value, _, _ = await establish(store)
    value['refreshPendingAtEpoch'] = int(time.time()) - 31
    store.values[key] = auth._session_json(value)
    async def java(*a, **kw): pytest.fail('in-doubt refresh must not replay')
    monkeypatch.setattr(auth, '_java', java)
    with pytest.raises(HTTPException) as exc: await auth._load_session(sid)
    assert exc.value.status_code == 401


@async_test
async def test_logout_during_refresh_cannot_resurrect_session(store, monkeypatch):
    sid, key, _, _, _ = await establish(store)
    async def java(*a, **kw):
        await store.delete(key)
        return tokens('new')
    monkeypatch.setattr(auth, '_java', java)
    with pytest.raises(HTTPException) as exc: await auth._load_session(sid)
    assert exc.value.status_code == 401 and key not in store.values


@async_test
async def test_me_during_renewal_preserves_new_pair(store, monkeypatch):
    sid, key, _, _, _ = await establish(store)
    calls = []
    async def java(*a, **kw):
        calls.append(1); await asyncio.sleep(0.1); return tokens('new')
    monkeypatch.setattr(auth, '_java', java)
    app = FastAPI(); app.include_router(auth.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
        c.cookies.set(auth.COOKIE_NAME, sid)
        result, _ = await asyncio.gather(c.get('/api/commerce-demo/me', headers={'Sec-Fetch-Site':'same-origin'}),
                                         auth._load_session(sid))
    assert result.status_code == 200 and calls == [1]
    saved = json.loads(store.values[key])
    assert saved['refreshToken'] == 'refresh-new'
    auth._require_csrf(saved, result.json()['csrfToken'])
    assert 'access-new' not in result.text and 'refresh-new' not in result.text


@async_test
async def test_expired_login_cookie_on_claimed_workspace_is_401(store):
    visitor = 'v' * 40
    prefix = 'commerce:workspace:' + (auth.settings.commerce_workspace_epoch + ':' if auth.settings.commerce_workspace_epoch else '')
    store.values[prefix + auth._digest(visitor) + ':claimed'] = '1'
    app = FastAPI(); app.include_router(ws.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
        c.cookies.set(ws.VISITOR_COOKIE, visitor)
        response = await c.get('/api/commerce-demo/workspace/conversations', headers={'Sec-Fetch-Site':'same-origin'})
    assert response.status_code == 401 and response.json()['detail'] == 'authentication expired'
