import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from .test_commerce_workspace import setup, client, async_test, state_key
from app.api import commerce_controls as controls, commerce_workspace as ws, commerce_demo as auth


def decode(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith('data: ')))


@async_test
async def test_subscription_owner_csrf_origin_and_terminal_snapshot(setup):
    app, store, login = setup
    async with client(app) as alice, client(app) as bob:
        login(alice, 'alice'); login(bob, 'bob')
        await alice.get('/api/commerce-demo/workspace'); await bob.get('/api/commerce-demo/workspace')
        key = state_key(store, ':user:' + auth._digest('alice'))
        state = await ws._load(key)
        async with ws._lock(key):
            await controls.save_run(key, dict(id='owned', requestId='req', revision=2, mode='continuous',
                status='paused', nodes=[], engine=state['engine'], privateToken='do-not-send'))
        path = '/api/commerce-demo/workspace/control/events?runId=owned'
        assert (await bob.get(path)).status_code == 409
        assert (await alice.get(path, headers={'X-CSRF-Token':'wrong'})).status_code == 403
        assert (await alice.get(path, headers={'Origin':'https://evil.invalid'})).status_code == 403
        result = await alice.get(path)
        assert result.status_code == 200
        assert result.headers['content-type'].startswith('text/event-stream')
        assert result.headers['x-accel-buffering'] == 'no'
        assert 'no-transform' in result.headers['cache-control']
        assert 'snapshot' in result.text and 'settled' in result.text
        assert state['engine'] not in result.text and 'do-not-send' not in result.text


@async_test
async def test_first_progress_arrives_before_job_completes_and_disconnect_never_cancels(monkeypatch):
    from app import task_state
    state = ws._fresh()
    run = dict(id='owned', requestId='req', revision=1, mode='continuous', status='running',
        nodes=[], engine=state['engine'], initialStepKeys=[])
    monkeypatch.setattr(ws, '_load', AsyncMock(side_effect=lambda key: run if key.endswith(':run') else state))
    monkeypatch.setattr(ws, '_identity', AsyncMock(return_value=('key', state, None)))
    monkeypatch.setattr(task_state, 'get_session_task_state', AsyncMock(return_value=None))
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    release = asyncio.Event(); job = asyncio.create_task(release.wait()); controls.JOBS['key'] = job
    stream = controls.run_events(request, 'key', 'owned')
    try:
        first = await anext(stream)
        assert decode(first)['workspace']['run']['status'] == 'running'
        assert not job.done()
        await stream.aclose()
        assert not job.done() and not job.cancelled()
    finally:
        release.set(); await job; controls.JOBS.pop('key', None)


@async_test
async def test_progress_uses_only_new_allowlisted_executor_receipts(monkeypatch):
    from app import task_state
    state = ws._fresh()
    row = dict(taskId='task',planId='p',stepId='new',toolName='search_products',outcome='tool_succeeded',
        resolvedArguments={'query':'续航手机','authorization':'private-token'},toolTrace={'detail':{'count':3,'password':'secret'}})
    task = SimpleNamespace(domain_state={'stepExecutionResults':[dict(row,stepId='old'),row]})
    run = dict(id='owned',requestId='req',revision=1,mode='continuous',status='running',nodes=[],
        engine='private-engine',initialStepKeys=['task|p|old'])
    monkeypatch.setattr(controls, 'refresh', AsyncMock(return_value=run))
    monkeypatch.setattr(ws, '_load', AsyncMock(return_value=state))
    monkeypatch.setattr(task_state, 'get_session_task_state', AsyncMock(return_value=task))
    result = await controls.stream_snapshot('key','owned')
    assert len(result['run']['nodes']) == 1
    assert result['run']['nodes'][0]['input'] == {'query':'续航手机'}
    encoded = json.dumps(result)
    assert 'private-token' not in encoded and 'secret' not in encoded and 'private-engine' not in encoded
    assert run['nodes'] == []  # UI projection does not mutate stored run.


@async_test
async def test_revocation_during_stream_stops_before_another_snapshot(monkeypatch):
    monkeypatch.setattr(controls, 'STREAM_INTERVAL', 0)
    monkeypatch.setattr(ws, '_identity', AsyncMock(side_effect=[('key',{},None), HTTPException(401,'expired')]))
    view = dict(messages=[],cards=[],selection=None,checkout=None,run=dict(id='owned',status='running'))
    reader = AsyncMock(return_value=view)
    monkeypatch.setattr(controls, 'stream_snapshot', reader)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    stream = controls.run_events(request,'key','owned')
    assert decode(await anext(stream))['type'] == 'snapshot'
    assert decode(await anext(stream))['status'] == 401
    with pytest.raises(StopAsyncIteration): await anext(stream)
    assert reader.await_count == 1


@async_test
async def test_reconnect_replays_snapshot_without_launching_or_writing(monkeypatch):
    state=ws._fresh()
    state['messages']=[dict(role='assistant',content='已校验的回答',requestId='req')]
    run=dict(id='owned',requestId='req',revision=2,mode='continuous',status='completed',nodes=[])
    monkeypatch.setattr(controls,'refresh',AsyncMock(return_value=run))
    monkeypatch.setattr(ws,'_load',AsyncMock(return_value=state))
    monkeypatch.setattr(ws,'_identity',AsyncMock(return_value=('key',state,None)))
    def forbidden(*args,**kwargs): raise AssertionError('Subscription must not launch or write')
    monkeypatch.setattr(controls,'launch',forbidden);monkeypatch.setattr(ws,'_save',forbidden)
    request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    first=[frame async for frame in controls.run_events(request,'key','owned')]
    second=[frame async for frame in controls.run_events(request,'key','owned')]
    assert first == second and len(first) == 2
    assert decode(first[0])['workspace']['messages'][0]['content'] == '已校验的回答'


@async_test
async def test_terminal_answer_is_loaded_after_terminal_receipt(monkeypatch):
    order=[];state=ws._fresh()
    async def refresh(key): order.append('run');return dict(id='owned',status='completed',nodes=[])
    async def load(key): order.append('state');return state
    monkeypatch.setattr(controls,'refresh',refresh);monkeypatch.setattr(ws,'_load',load)
    await controls.stream_snapshot('key','owned')
    assert order == ['run','state']


@async_test
async def test_heartbeat_is_not_a_fake_progress_event(monkeypatch):
    monkeypatch.setattr(controls,'STREAM_INTERVAL',0)
    monkeypatch.setattr(controls,'STREAM_HEARTBEAT',0)
    monkeypatch.setattr(ws,'_identity',AsyncMock(return_value=('key',{},None)))
    monkeypatch.setattr(controls,'stream_snapshot',AsyncMock(return_value={'run':{'status':'running'}}))
    stream=controls.run_events(SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),'key','owned')
    assert decode(await anext(stream))['type']=='snapshot'
    assert await anext(stream)==': keep-alive\n\n'
    assert await anext(stream)==': keep-alive\n\n'  # unchanged snapshot is not resent
    await stream.aclose()


@async_test
async def test_real_http_stream_delivers_before_completion_without_model_or_database(monkeypatch):
    import socket
    import httpx
    import uvicorn
    from fastapi import FastAPI
    from app import task_state
    state=ws._fresh()
    run=dict(id='owned',requestId='req',revision=1,mode='continuous',status='running',nodes=[],engine=state['engine'],initialStepKeys=[])
    monkeypatch.setattr(ws,'_load',AsyncMock(side_effect=lambda key: run if key.endswith(':run') else state))
    monkeypatch.setattr(ws,'_identity',AsyncMock(return_value=('key',state,None)))
    monkeypatch.setattr(task_state,'get_session_task_state',AsyncMock(return_value=None))
    monkeypatch.setattr(controls,'STREAM_INTERVAL',0.02)
    app=FastAPI();app.include_router(ws.router)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',lifespan='off'))
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    release=asyncio.Event();job=asyncio.create_task(release.wait());controls.JOBS['key']=job
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done(): await serving
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient(timeout=3) as c:
                async with c.stream('GET',f'http://127.0.0.1:{port}/api/commerce-demo/workspace/control/events?runId=owned') as response:
                    lines=response.aiter_lines()
                    first=''
                    async for line in lines:
                        first+=line+'\n'
                        if not line:break
                    assert decode(first)['workspace']['run']['status']=='running'
                    assert not job.done()  # Bytes reached a real HTTP client before completion.
                    state['messages']=[dict(role='assistant',requestId='req',content='持久完成回答')]
                    run.update(status='completed',revision=2)
                    release.set();await job
                    rest='\n'.join([line async for line in lines])
                    assert '持久完成回答' in rest and 'settled' in rest
    finally:
        release.set();await job;controls.JOBS.pop('key',None)
        server.should_exit=True
        await serving
        sock.close()
