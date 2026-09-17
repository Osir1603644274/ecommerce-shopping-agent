"""Adapter mechanics only; FakeRedis/no provider and no claims of live quality."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from . import multiturn
from .common import freeze, rows, sha


def test_multiturn_binds_histories_identities_and_no_obsolete_client(tmp_path, monkeypatch):
    import openai
    from agent.app import llm, task_state, agent_trace
    from agent.tests.fake_redis import FakeRedis
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2 import lane_runtime as old
    monkeypatch.setattr(task_state, '_client', FakeRedis())
    monkeypatch.setattr(agent_trace, '_trace_store', SimpleNamespace(get=AsyncMock(return_value=None)))
    monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:1/0')
    monkeypatch.setattr(llm.settings, 'redis_url', 'redis://127.0.0.1:1/0')
    for name in ('multi_agent_v2_enabled', 'memory_projection_client_enabled', 'task_state_extraction_strict_enabled', 'deepseek_base_url', 'used_phone_synthetic_price_dir', 'used_phone_synthetic_price_policy'):
        monkeypatch.setattr(llm.settings, name, getattr(llm.settings, name))
    called = []
    async def fake_run(message, **kwargs):
        from agent.app.domains.ecommerce.synthetic_prices import canonical_synthetic_price_value
        value, metadata = canonical_synthetic_price_value(956972,
            directory=llm.settings.used_phone_synthetic_price_dir,
            policy=llm.settings.used_phone_synthetic_price_policy, allow_budget=True)
        assert value is not None and metadata is not None
        cap = kwargs['evaluation_context_arm']
        called.append((cap.arm, message, kwargs['history'], cap.identity.run_id))
        return ('fixture answer', [], [], cap.identity.run_id,
            SimpleNamespace(control_policy='react_v1', agent_status='ok', model_dump=lambda **kw: {'fixture': True}))
    monkeypatch.setattr(llm, 'run_agent', fake_run)
    class Provider:
        max_retries = 0
        base_url = 'https://api.deepseek.com/beta'
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=AssertionError('no model'))))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr(openai, 'AsyncOpenAI', Provider)
    # If the old constructor is used it would install the forbidden 1024 cap.
    monkeypatch.setattr(old.IsolatedLaneRuntime, '__init__', lambda *a, **k: (_ for _ in ()).throw(AssertionError('old client constructor')))
    turns = [{'turnId':f'c-t{i}', 'semanticTurn':i, 'rawUserText':m,
              'messageSha256':sha(m), 'turnProvenance':'FIXTURE'} for i,m in enumerate(('first','second'),1)]
    freeze(tmp_path / 'source_freeze.json')
    asyncio.run(multiturn.run(tmp_path, [{'conversationId':'c','turns':turns}], True))
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['completedArmTurns'] == 4 and result['modelCalls'] == 0
    assert len({x[3] for x in called}) == 4
    assert all(h is None for a,m,h,r in called if a=='CONTEXT_TREATMENT')
    assert next(h for a,m,h,r in called if a=='RAW_FULL_CONTROL' and m=='second') == [
        {'role':'user','content':'first'}, {'role':'assistant','content':'fixture answer'}]
    assert all(r['runIdentityMatched'] for r in rows(tmp_path / 'outputs.jsonl'))
