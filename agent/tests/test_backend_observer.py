import asyncio
import json
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app import backend_observer as obs
from app.api import commerce_demo as auth


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setattr(obs.settings, 'backend_observer_enabled', True)
    monkeypatch.setattr(obs.settings, 'backend_observer_key', 'private-test-key-not-for-browser-123456')
    obs._tickets.clear()
    async def session(cookie):
        if not cookie:
            raise HTTPException(401)
        return {'csrfDigest': auth._digest('csrf')}
    monkeypatch.setattr(auth, '_load_session', session)
    app = FastAPI()
    app.add_middleware(obs.BackendObserverMiddleware)
    app.include_router(obs.router)
    @app.get('/api/commerce-demo/business')
    async def business():
        # No actual service call: simulate only the Java transport boundary.
        response = httpx.Response(200, headers={'X-Java-Trace-Id': '12345678-1234-1234-1234-123456789abc'})
        obs.observe_response(response, 'GET', '/api/orders/123456?private=hidden')
        return {'ok': True, 'sentDebugHeader': bool(obs.observer_headers())}
    with TestClient(app) as client:
        client.cookies.set(auth.COOKIE_NAME, 'owner-one')
        yield client
    obs._tickets.clear()


HEADERS = {'X-Backend-Observe': '1', 'Sec-Fetch-Site': 'same-origin', 'X-CSRF-Token': 'csrf'}


def test_disabled_and_unrequested_are_inert(system, monkeypatch):
    assert not system.get('/api/commerce-demo/business').json()['sentDebugHeader']
    monkeypatch.setattr(obs.settings, 'backend_observer_enabled', False)
    response = system.get('/api/commerce-demo/business', headers=HEADERS)
    assert 'X-Backend-Trace-Ticket' not in response.headers
    assert not response.json()['sentDebugHeader']


def test_owner_csrf_and_origin_are_required(system):
    response = system.get('/api/commerce-demo/business', headers=HEADERS)
    ticket = response.headers['X-Backend-Trace-Ticket']
    endpoint = '/api/commerce-demo/backend-traces/' + ticket
    assert system.get(endpoint).status_code == 403
    assert system.get(endpoint, headers={'Sec-Fetch-Site': 'same-origin'}).status_code == 403
    system.cookies.set(auth.COOKIE_NAME, 'owner-two')
    assert system.get(endpoint, headers=HEADERS).status_code == 404


def test_expired_ticket_is_not_readable(system):
    response = system.get('/api/commerce-demo/business', headers=HEADERS)
    ticket = response.headers['X-Backend-Trace-Ticket']
    _, owner, calls = obs._tickets[ticket]
    obs._tickets[ticket] = (0, owner, calls)
    assert system.get('/api/commerce-demo/backend-traces/' + ticket, headers=HEADERS).status_code == 404


def test_private_key_is_not_exposed_and_path_query_is_removed(system):
    response = system.get('/api/commerce-demo/business', headers=HEADERS)
    ticket = response.headers['X-Backend-Trace-Ticket']
    calls = obs._tickets[ticket][2]
    assert calls[0]['path'] == '/api/orders/:id'
    assert obs.settings.backend_observer_key not in response.text
    assert 'hidden' not in json.dumps(calls)
    assert obs._current.get() is None


def test_storage_failure_does_not_fail_business(system, monkeypatch):
    def fail(*args):
        raise RuntimeError('storage down')
    monkeypatch.setattr(obs, '_save', fail)
    result = system.get('/api/commerce-demo/business', headers=HEADERS)
    assert result.status_code == 200
    assert 'X-Backend-Trace-Ticket' not in result.headers


def test_collection_is_bounded_and_late_background_work_is_excluded():
    c = obs.Collection()
    token = obs._current.set(c)
    try:
        for _ in range(45):
            obs.observe_response(httpx.Response(200), 'GET', '/api/products')
        assert len(c.calls) == 30
        c.closed = True
        obs.observe_response(httpx.Response(201), 'POST', '/api/orders')
        assert len(c.calls) == 30
        assert obs.observer_headers() == {}
    finally:
        obs._current.reset(token)


def test_read_fetches_only_server_recorded_trace(system, monkeypatch):
    result = system.get('/api/commerce-demo/business', headers=HEADERS)
    ticket = result.headers['X-Backend-Trace-Ticket']
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, path, headers):
            assert path == '/api/diagnostics/backend-traces/12345678-1234-1234-1234-123456789abc'
            assert headers['X-Backend-Observer-Key'] == obs.settings.backend_observer_key
            return httpx.Response(200, json={'data': {'events': [], 'droppedEvents': 0}})
    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    response = system.get('/api/commerce-demo/backend-traces/' + ticket, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['calls'][0]['detail']['events'] == []
    assert 'X-Backend-Trace-Ticket' not in response.headers  # no recursive trace


def test_trace_is_read_from_allowlisted_issuing_instance(system, monkeypatch):
    monkeypatch.setattr(obs.settings, 'backend_observer_instances', {'trade-a': 'http://trade-a:8080'})
    result = system.get('/api/commerce-demo/business', headers=HEADERS)
    ticket = result.headers['X-Backend-Trace-Ticket']
    obs._tickets[ticket][2][0]['serviceInstance'] = 'trade-a'
    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, path, headers):
            assert path == 'http://trade-a:8080/api/diagnostics/backend-traces/12345678-1234-1234-1234-123456789abc'
            return httpx.Response(200, json={'data': {'events': []}})
    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    response = system.get('/api/commerce-demo/backend-traces/' + ticket, headers=HEADERS)
    assert response.json()['calls'][0]['detail']['events'] == []


def test_untrusted_instance_header_cannot_choose_trace_destination(monkeypatch):
    monkeypatch.setattr(obs.settings, 'backend_observer_instances', {'trade-a': 'http://trade-a:8080'})
    collection = obs.Collection(); token = obs._current.set(collection)
    try:
        obs.observe_response(httpx.Response(200, headers={'X-Service-Instance': 'http://untrusted.example'}), 'GET', '/api/orders')
        assert 'serviceInstance' not in collection.calls[0]
    finally:
        obs._current.reset(token)


def test_bff_transport_collects_even_rejected_java_response(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, *args, **kwargs):
            return httpx.Response(429, headers={'Retry-After': '4'}, json={'message': 'slow down'})
    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    c = obs.Collection(); token = obs._current.set(c)
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(auth._java('GET', '/api/orders'))
        assert caught.value.status_code == 429
        assert c.calls[0]['status'] == 429
    finally:
        obs._current.reset(token)


def test_bigint_identifiers_stay_exact_and_confirmation_is_required(monkeypatch):
    from app.api import commerce_capabilities as cap
    assert cap.ids({'id': 9223372036854775806})['id'] == '9223372036854775806'
    async def identity(*args, **kwargs): return ('owner', {}, {'accessToken': 'private'})
    monkeypatch.setattr(cap, '_identity', identity)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cap.purchase('1', cap.Purchase(confirmation='yes'), None, None))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cap.purchase('1/activate', cap.Purchase(confirmation='确认参加抢购'), None, None))
    assert exc.value.status_code == 422
