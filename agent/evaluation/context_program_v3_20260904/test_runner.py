import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from . import common


def test_oracle_reads_effective_state_not_sparse_patch_status():
    from .extraction_gate import oracle
    from agent.evaluation.context_program_v2_20260904.datasets import synthetic_state, extraction_cases
    from agent.app import llm
    from agent.app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
    for case in extraction_cases():
        state = synthetic_state()
        # Known minimal extraction; the server applies explicit hard edits.
        payload, _ = llm._build_validated_task_state_payload(state, {'status': 'ready'}, message=case['message'], require_status=True)
        domain = {**state.domain_state, **payload['domainStatePatch']}
        bound = bind_authoritative_write({k: v for k, v in domain.items() if v is not None},
            task_id=state.task_id, task_revision=state.revision+1, goal=state.goal, unknowns=[], pending_questions=[])
        errors = oracle(state, payload, bound, case)
        assert 'wrong_status_or_blocking_question' not in errors
        if case['familyId'] == 'preserve_hard': assert not errors


def client_at(tmp_path, monkeypatch, **updates):
    monkeypatch.setattr(common, 'HERE', tmp_path)
    contract = {'deadlineAt': (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
        'inheritedRequests': 101, 'inheritedTokens': 348763, 'requestCap': 3000,
        'tokenCap': 10000000, 'phaseRequestCaps': {'P2': 240}}
    contract.update(updates)
    common.json_new(tmp_path / 'p0/contract.json', contract)
    class Response:
        choices = [SimpleNamespace(finish_reason='tool_calls')]
        usage = SimpleNamespace(model_dump=lambda: {'total_tokens': 20})
        def model_dump(self): return {'fixture': True}
    create = AsyncMock(return_value=Response())
    provider = SimpleNamespace(base_url='https://api.deepseek.com/beta', max_retries=0,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return common.RecordedClient(provider, phase='P2', output=tmp_path), create


def test_preserves_protocol_and_inherited_accounting(tmp_path, monkeypatch):
    client, create = client_at(tmp_path, monkeypatch)
    asyncio.run(client.create(model='fixture', messages=[], max_tokens=4096))
    args = create.await_args.kwargs
    assert args['max_tokens'] == 4096 and args['temperature'] == 0
    assert args['extra_body']['thinking']['type'] == 'disabled'
    assert client.usage_state()[2] == 348783


@pytest.mark.parametrize('updates', [
    {'requestCap': 101}, {'tokenCap': 348763}, {'phaseRequestCaps': {'P2': 0}},
    {'deadlineAt': '2020-01-01T00:00:00+00:00'},
])
def test_budget_stop_before_dispatch(tmp_path, monkeypatch, updates):
    client, create = client_at(tmp_path, monkeypatch, **updates)
    with pytest.raises(common.BudgetStop): asyncio.run(client.create(model='fixture', messages=[]))
    create.assert_not_awaited()


def test_unknown_usage_remains_charged_and_no_sdk_retry(tmp_path, monkeypatch):
    client, create = client_at(tmp_path, monkeypatch)
    create.side_effect = TimeoutError('fixture')
    with pytest.raises(TimeoutError): asyncio.run(client.create(model='fixture', messages=[]))
    assert create.await_count == 1
    assert client.usage_state()[2] > 348763 + 4096


def test_os_writer_lock_rejects_second_writer(tmp_path, monkeypatch):
    import msvcrt
    client, create = client_at(tmp_path, monkeypatch)
    with (tmp_path / 'provider-writer.lock').open('w+b') as guard:
        guard.write(b'0'); guard.flush(); guard.seek(0)
        msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            with pytest.raises(common.BudgetStop, match='writer_already_active'):
                asyncio.run(client.create(model='fixture', messages=[]))
        finally:
            guard.seek(0); msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
    create.assert_not_awaited()
