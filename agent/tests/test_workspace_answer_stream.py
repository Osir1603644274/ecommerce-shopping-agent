import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from app.api import workspace_answer_stream as output, commerce_workspace as ws
from app import catalog_model_client, workspace_archive
from .test_commerce_workspace import async_test, setup, client, state_key

REAL_COMPOSE = output.compose


@pytest.fixture
def harness(monkeypatch):
    run = dict(id='run-1', requestId='req-1', engine='engine-1', message='推荐手机', status='running')
    state = {'key': {'engine': run['engine'], 'messages': []}, 'key:run': deepcopy(run)}
    receipts = []
    async def load(key): return deepcopy(state.get(key))
    async def save(key, value): state[key] = deepcopy(value)
    @asynccontextmanager
    async def lock(key): yield
    monkeypatch.setattr(ws, '_load', load)
    monkeypatch.setattr(ws, '_save', save)
    monkeypatch.setattr(ws, '_lock', lock)
    monkeypatch.setattr(workspace_archive, 'get_archive', lambda:NS(record=lambda *a, **kw:receipts.append(deepcopy(kw))))
    return run, state, receipts


class Provider:
    def __init__(self, pieces, gate=None):
        self.pieces = pieces
        self.gate = gate
        self.closed = False
        self.calls = []

    async def create(self, **kwargs):
        assert kwargs['stream'] is True
        self.calls.append(kwargs)
        return self

    async def close(self): self.closed = True

    async def __aiter__(self):
        for index, piece in enumerate(self.pieces):
            if index and self.gate: await self.gate.wait()
            yield NS(id='provider-real-shape', choices=[NS(finish_reason=None,
                delta=NS(content=piece, reasoning_content='SECRET REASONING', tool_calls=[]))])
        yield NS(id='provider-real-shape', choices=[NS(finish_reason='stop',delta=NS(content=None))])


def install(monkeypatch, provider):
    @asynccontextmanager
    async def borrow(): yield NS(chat=NS(completions=NS(create=provider.create)))
    monkeypatch.setattr(catalog_model_client,'borrow_client',borrow)


@async_test
async def test_provider_delta_is_persisted_before_provider_completes(harness, monkeypatch):
    run,state,receipts=harness
    gate=asyncio.Event();provider=Provider(['你好，','先看这款。'],gate);install(monkeypatch,provider)
    job=asyncio.create_task(output.compose('key',run,{'fact':'手机'}))
    for _ in range(100):
        if state.get('key:answer',{}).get('text'):break
        await asyncio.sleep(.01)
    assert state['key:answer']['text']=='你好，'
    assert not job.done()  # Provider is still blocked, not a completed answer sliced later.
    assert state['key:answer']['status']=='generating'
    await asyncio.sleep(.03)  # Windows wall clock may have a ~15ms tick.
    gate.set()
    assert await job=='你好，先看这款。'
    assert provider.closed
    assert 'SECRET' not in state['key:answer']['text']
    assert state['key:answer']['firstDeltaAt']<state['key:answer']['completedAt']
    assert receipts[-1]['kind']=='answer_stream'
    # Reconnect/recovery of completed presentation never calls the model again.
    assert await output.compose('key',run,{'fact':'手机'})=='你好，先看这款。'
    assert len(provider.calls)==1


@async_test
async def test_pause_retains_prefix_and_continue_does_not_repeat_work(harness,monkeypatch):
    run,state,_=harness
    provider=Provider(['已知电池','不应收到'],asyncio.Event());install(monkeypatch,provider)
    job=asyncio.create_task(output.compose('key',run,{'fact':'电池'}))
    for _ in range(100):
        if state.get('key:answer',{}).get('text'):break
        await asyncio.sleep(.01)
    state['key:run']['pauseRequested']=True
    with pytest.raises(output.AnswerPaused): await asyncio.wait_for(job,1)
    assert provider.closed and state['key:answer']['text']=='已知电池'
    assert state['key:answer']['status']=='paused'
    state['key:run']['pauseRequested']=False
    continuation=Provider(['容量。']);install(monkeypatch,continuation)
    assert await output.compose('key',run,{'fact':'电池'})=='已知电池容量。'
    assert '已知电池' in continuation.calls[0]['messages'][1]['content']


@async_test
async def test_changed_evidence_cannot_resume_old_prefix(harness,monkeypatch):
    run,_,_=harness
    provider=Provider(['手机']);install(monkeypatch,provider)
    await output.compose('key',run,{'fact':'手机'})
    with pytest.raises(ValueError,match='answer_evidence_changed'):
        await output.compose('key',run,{'fact':'另一件商品'})
    assert len(provider.calls)==1


@async_test
async def test_stream_record_uses_real_workspace_owner_lock(setup, monkeypatch):
    from app.api import commerce_controls as controls
    app,store,_=setup
    provider=Provider(['真实锁校验','通过。']);install(monkeypatch,provider)
    async with client(app) as c:
        await c.get('/api/commerce-demo/workspace')
        key=state_key(store,':guest')
        state=await ws._load(key)
        run=dict(id='run-lock',requestId='req-lock',engine=state['engine'],message='手机',
                 revision=1,mode='continuous',status='running',nodes=[])
        async with ws._lock(key):await controls.save_run(key,run)
        assert await REAL_COMPOSE(key,run,{'answer':'资料已核验'})=='真实锁校验通过。'
        assert (await output.read(key,run['id']))['sequence']==2
        with pytest.raises(RuntimeError):await ws._save(key+':answer',{})
        async with ws._lock(key+'-another'):
            with pytest.raises(RuntimeError):await ws._save(key+':answer',{})
