import asyncio
import json
import hashlib
import pytest
from fastapi import HTTPException
from types import SimpleNamespace
from unittest.mock import AsyncMock

from .test_commerce_workspace import setup, client, async_test, state_key
from app.api import commerce_controls as controls, commerce_workspace as ws, commerce_demo as auth
from app.api.commerce_evidence import evidence_view


@async_test
async def test_crash_resume_does_not_require_pause_receipt_but_keeps_authorization(monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'get_session_pause', AsyncMock(side_effect=HTTPException(404, 'no pause')))
    assert await controls.optional_pause_receipt('owned-engine') == {}
    monkeypatch.setattr(main, 'get_session_pause', AsyncMock(side_effect=HTTPException(403, 'not owner')))
    with pytest.raises(HTTPException) as error:
        await controls.optional_pause_receipt('other-engine')
    assert error.value.status_code == 403


@async_test
async def test_control_owner_revision_and_pause_does_not_cancel_tool(setup, monkeypatch):
    from app import main
    app, store, login = setup
    paused = AsyncMock(return_value={'state':'pause_requested'})
    monkeypatch.setattr(main, 'pause_session_run', paused)
    async with client(app) as a, client(app) as b:
        login(a, 'alice'); login(b, 'bob')
        await a.get('/api/commerce-demo/workspace'); await b.get('/api/commerce-demo/workspace')
        key = state_key(store, ':user:' + auth._digest('alice'))
        engine = (await ws._load(key))['engine']
        run = dict(id='owned', revision=1, mode='continuous', status='running', nodes=[], engine=engine, requestId='req')
        async with ws._lock(key):
            await controls.save_run(key, run)
        job = asyncio.create_task(asyncio.sleep(10)); controls.JOBS[key] = job
        body = dict(runId='owned', revision=1)
        try:
            assert (await b.post('/api/commerce-demo/workspace/control/pause', json=body)).status_code == 409
            result = await a.post('/api/commerce-demo/workspace/control/pause', json=body)
            assert result.status_code == 200 and result.json()['run']['status'] == 'pausing'
            assert not job.cancelled() and not job.done()
            paused.assert_awaited_once_with(engine)
            assert engine not in result.text
            assert (await a.post('/api/commerce-demo/workspace/control/pause', json=body)).status_code == 200
            paused.assert_awaited_once_with(engine)
            assert (await a.post('/api/commerce-demo/workspace/selection', json={'productId':1})).status_code == 409
        finally:
            job.cancel(); controls.JOBS.pop(key, None)


@async_test
async def test_restart_marks_interrupted_without_launch_and_end_preserves_checkpoint(setup):
    app, store, login = setup
    async with client(app) as c:
        login(c, 'alice'); await c.get('/api/commerce-demo/workspace')
        key = state_key(store, ':user:' + auth._digest('alice'))
        old_engine = (await ws._load(key))['engine']
        async with ws._lock(key):
            await controls.save_run(key, dict(id='owned', revision=1, mode='continuous', status='running', nodes=[], requestId='req'))
        store.values['untouched-checkpoint'] = 'original'
        r = await c.get('/api/commerce-demo/workspace/control')
        assert r.json()['run']['status'] == 'interrupted' and not controls.JOBS
        r = await c.post('/api/commerce-demo/workspace/control/end', json=dict(runId='owned', revision=1))
        assert r.status_code == 200 and r.json()['run']['status'] == 'ended'
        assert (await ws._load(key))['engine'] != old_engine
        assert store.values['untouched-checkpoint'] == 'original'


def test_card_evidence_never_crosses_model_or_region_and_no_raw_secrets():
    cards = [dict(id=1), dict(id=2)]
    knowledge = dict(transport='MCP_STREAMABLE_HTTP', knowledgeVersion='test',
        bindings=[dict(itemId='1',status='CLEAR',modelKeys=['a'],region='CN'),dict(itemId='2',status='AMBIGUOUS',modelKeys=['a'])],
        evidence=[dict(evidenceId='e1',modelKey='a',text='5000mAh',field='battery',source=dict(url='https://example.org/a',region='CN')),
                  dict(evidenceId='e2',modelKey='b',text='8 hours',field='battery_test',source=dict(url='https://example.org/b'))])
    nodes = evidence_view([dict(tool='compare_products',ok=True,detail=dict(knowledge=knowledge,password='secret'))], cards)
    assert [e['id'] for e in cards[0]['evidence']] == ['e1']
    assert cards[1]['evidence'] == []
    assert 'secret' not in json.dumps(nodes)
    assert nodes[1]['detail']['evidenceCount'] == 2


@async_test
async def test_publish_never_promotes_raw_failed_plan_evidence(setup, monkeypatch):
    from app import task_state, harness
    app, store, login = setup
    monkeypatch.setattr(task_state, 'get_session_task_state', AsyncMock(return_value=object()))
    monkeypatch.setattr(harness, '_build_validated_results', lambda state, traces: [])
    monkeypatch.setattr(ws, '_card', AsyncMock(return_value=dict(id=1, title='phone')))
    async with client(app) as c:
        login(c, 'alice'); await c.get('/api/commerce-demo/workspace')
        key = state_key(store, ':user:' + auth._digest('alice'))
        run = dict(id='owned',status='running',revision=1,engine=(await ws._load(key))['engine'], requestId='req', nodes=[])
        knowledge = dict(transport='MCP_STREAMABLE_HTTP', bindings=[dict(itemId='1', status='CLEAR', modelKeys=['a'])],
            evidence=[dict(evidenceId='bad-plan', modelKey='a', text='must not publish', source=dict(url='https://example.org/a'))])
        data = dict(answer='safe answer', guideResult=dict(products=[dict(product=dict(id=1))]),
            tool_trace=[dict(tool='compare_products', ok=True, detail=dict(knowledge=knowledge))])
        async with ws._lock(key):
            await controls.save_run(key, run)
            await controls.publish(key, run, data)
            await controls.publish(key, run, data)
        saved = await ws._load(key)
        assert len([m for m in saved['messages'] if m['requestId'] == 'req']) == 1
        assert 'evidence' not in saved['cards'][0]


@async_test
async def test_finalization_projection_requires_matching_immutable_answer_receipt(monkeypatch):
    from app import task_state, harness, graph
    receipt = dict(baseTaskRevision=15, finalizationRevision=16, runId='run-1', threadId='thread-1',
        publicationId='pub-1', answerSha256=hashlib.sha256(b'answer').hexdigest())
    task = SimpleNamespace(task_id='task-1', revision=16, domain_state={'v2FinalAnswerReceipt':receipt})
    task.model_copy = lambda update: SimpleNamespace(**{**task.__dict__, **update})
    monkeypatch.setattr(task_state, 'get_session_task_state', AsyncMock(return_value=task))
    monkeypatch.setattr(graph, 'read_terminal_response_receipt', AsyncMock(return_value={'answer':'answer'}))
    revisions = []
    def validate(state, traces):
        revisions.append(state.revision)
        return [{'tool':'compare_products','evidence':{}}]
    monkeypatch.setattr(harness, '_build_validated_results', validate)
    run = dict(engine='owner', mode='continuous')
    assert await controls.published_evidence(run, dict(runId='run-1', answer='answer'))
    assert revisions == [15] and task.revision == 16
    assert await controls.published_evidence(run, dict(runId='wrong-run', answer='answer')) == []
    monkeypatch.setattr(graph, 'read_terminal_response_receipt', AsyncMock(return_value=None))
    assert await controls.published_evidence(run, dict(runId='run-1', answer='answer')) == []
    assert revisions == [15]
