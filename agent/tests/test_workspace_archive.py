import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.workspace_archive import WorkspaceArchive
from app.api import commerce_workspace as ws, commerce_controls as controls
from .test_commerce_workspace import setup, client, async_test, state_key


def test_archive_is_idempotent_private_and_not_limited_to_redis_tail(tmp_path):
    archive = WorkspaceArchive(tmp_path / 'conversations.sqlite3')
    state = dict(engine='private-engine', conversationId='conv', csrf='do-not-store', messages=[])
    for n in range(65):
        state['messages'].append(dict(role='user', content=f'question {n}', requestId=str(n), source='automated_test'))
        state['messages'] = state['messages'][-60:]
        archive.record('owner', state)
    archive.record('owner', state)
    assert len(archive.read('owner', 'conv')['messages']) == 65
    assert archive.read('other', 'conv') is None
    assert archive.history('other')['conversations'] == []
    assert 'do-not-store' not in json.dumps(archive.read('owner', 'conv'))


def test_redaction_immutable_versions_and_raw_answer_kept_separately(tmp_path):
    archive = WorkspaceArchive(tmp_path / 'archive.sqlite3')
    state = dict(engine='e', messages=[dict(role='user', content='token=abcd1234', requestId='r')])
    archive.record('o', state, kind='diagnostic', payload=dict(requestId='r', rawAnswer='unformatted answer',
        password='private', toolTrace=[dict(accessToken='secret')], source='legacy_unverified', benchmarkEligible=False))
    with sqlite3.connect(archive.path) as db:
        row = json.loads(db.execute('SELECT payload_json FROM workspace_events').fetchone()[0])
        assert row['rawAnswer'] == 'unformatted answer' and 'password' not in row
        assert row['toolTrace'] == [{}] and row['benchmarkEligible'] is False
        stored = db.execute('SELECT payload_json FROM workspace_messages').fetchone()[0]
        assert 'abcd1234' not in stored
    state['messages'][0]['content'] = 'changed'
    with pytest.raises(ValueError, match='identity_conflict'):
        archive.record('o', state)


@async_test
async def test_new_conversation_preserves_history_and_owner_isolation(setup):
    app, store, login = setup
    async with client(app) as a, client(app) as b:
        login(a, 'alice'); login(b, 'bob')
        first = (await a.get('/api/commerce-demo/workspace')).json()
        await b.get('/api/commerce-demo/workspace')
        key = state_key(store, ':user:' + ws.auth._digest('alice'))
        state = await ws._load(key)
        engine = state['engine']
        state['messages'] = [dict(role='user', content='my genuine wish', requestId='request-1')]
        async with ws._lock(key):
            await ws._save(key, state)
        original = state['conversationId']
        result = await a.post('/api/commerce-demo/workspace/conversations', json={'expectedConversationId': original})
        assert result.status_code == 200 and result.json()['messages'] == []
        assert (await ws._load(key))['engine'] != engine
        assert result.json()['conversationId'] != original
        assert a.cookies.get(ws.auth.COOKIE_NAME) == 'session-alice'
        old = await a.get('/api/commerce-demo/workspace/conversations/' + original)
        assert old.status_code == 200 and old.json()['readOnly']
        assert old.json()['messages'][0]['content'] == 'my genuine wish'
        assert engine not in old.text
        assert (await b.get('/api/commerce-demo/workspace/conversations/' + original)).status_code == 404
        assert (await a.post('/api/commerce-demo/workspace/conversations', json={'expectedConversationId': original})).status_code == 409


@async_test
async def test_new_conversation_rejects_active_pending_and_csrf(setup):
    app, store, login = setup
    async with client(app) as a:
        login(a, 'alice'); first = (await a.get('/api/commerce-demo/workspace')).json()
        key = state_key(store, ':user:' + ws.auth._digest('alice'))
        body = dict(expectedConversationId=first['conversationId'])
        async with ws._lock(key):
            await controls.save_run(key, dict(id='r', requestId='req', revision=1, status='paused'))
        assert (await a.post('/api/commerce-demo/workspace/conversations', json=body)).status_code == 409
        await store.delete(key + ':run')
        state = await ws._load(key); state['checkout'] = dict(pending=True)
        async with ws._lock(key):
            await ws._save(key, state)
        assert (await a.post('/api/commerce-demo/workspace/conversations', json=body)).status_code == 409
        a.headers[ws.auth.CSRF_HEADER] = 'wrong'
        assert (await a.post('/api/commerce-demo/workspace/conversations', json=body)).status_code == 403


@async_test
async def test_archive_disk_failure_prevents_new_question_execution(setup, monkeypatch):
    from app import workspace_archive
    app, store, login = setup
    async with client(app) as a:
        login(a, 'alice'); await a.get('/api/commerce-demo/workspace')
        key = state_key(store, ':user:' + ws.auth._digest('alice'))
        state = await ws._load(key)
        original = list(state['messages'])
        state['messages'].append(dict(role='user',content='must not disappear',requestId='req'))
        def fail(*args, **kwargs): raise OSError('disk unavailable')
        monkeypatch.setattr(workspace_archive.WorkspaceArchive, 'record', fail)
        async with ws._lock(key):
            with pytest.raises(OSError): await ws._save(key, state)
        assert (await ws._load(key))['messages'] == original


@async_test
async def test_phase_projection_includes_real_tool_but_not_null_or_secret(monkeypatch):
    from app import agent_trace
    trace = SimpleNamespace(session_id='e', phases=[SimpleNamespace(phase='executor', outcome='step_executed',
        duration_ms=20, task_revision=None, started_at='today', finished_at='today', code_location='test:fixture',
        detail=dict(tool='compare_products',toolOk=True,password='hidden'))])
    monkeypatch.setattr(agent_trace, 'get_trace_store', lambda: SimpleNamespace(get=AsyncMock(return_value=trace)))
    nodes = await controls.lifecycle_nodes(dict(runId='r'),dict(engine='e',nodes=[],requestId='req'))
    assert nodes[0]['detail'] == dict(tool='compare_products',toolOk=True)
    assert 'hidden' not in json.dumps(nodes)


def test_comparison_has_honest_rule_and_input_summary():
    from app.api.commerce_evidence import evidence_view
    nodes = evidence_view([dict(tool='compare_products',ok=True,detail=dict(userQuery='续航好', productIds=[1,2]))], [])
    assert nodes[0]['detail']['userQuery'] == '续航好'
    assert '知识库' in nodes[0]['detail']['evidencePolicy']


@async_test
async def test_legacy_conversation_id_survives_execution_identity_rotation(setup):
    app, store, login = setup
    async with client(app) as a:
        login(a, 'alice'); await a.get('/api/commerce-demo/workspace')
        key = state_key(store, ':user:' + ws.auth._digest('alice'))
        state = json.loads(store.values[key]); state.pop('conversationId')
        store.values[key] = json.dumps(state)
        loaded = await ws._load(key)
        cid = loaded['conversationId']
        loaded['engine'] = 'new-execution-identity'
        async with ws._lock(key):
            await ws._save(key, loaded)
        assert (await ws._load(key))['conversationId'] == cid
