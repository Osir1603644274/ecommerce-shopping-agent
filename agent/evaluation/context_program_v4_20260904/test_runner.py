"""Offline guards: no real provider constructed or invoked."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import pytest
from .common import RecordedClient, BudgetStop, append, file_sha, json_new, rows, writer_lock


class Provider:
    max_retries = 0
    base_url = 'https://provider.invalid/beta/'

    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls += 1
        if self.callback:
            return self.callback(kwargs)
        usage = SimpleNamespace(model_dump=lambda: {'total_tokens': 20})
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(finish_reason='stop')],
                               model_dump=lambda: {'usage': {'total_tokens': 20}, 'choices': []})


def setup(tmp_path, callback=None, **updates):
    root = tmp_path / 'current'; inherited = tmp_path / 'old'; output = root / 'p4/test001'
    output.mkdir(parents=True); inherited.mkdir()
    (inherited / 'provider_ledger.jsonl').touch()
    contract = {'inheritedRequests': 1718, 'inheritedTokens': 6692647,
                'inheritedPhaseRequests': {'P4': 895}, 'phaseRequestCaps': {'P4': 1680},
                'requestCap': 3000, 'tokenCap': 10000000,
                'deadlineAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                'model': 'test', 'endpoint': Provider.base_url,
                'inheritedLedgerSha256': file_sha(inherited / 'provider_ledger.jsonl')}
    contract.update(updates)
    json_new(root / 'p0/contract.json', contract)
    provider = Provider(callback)
    client = RecordedClient(provider, phase='P4', output=output, root=root, inherited_root=inherited)
    return client, provider, contract


def call(client, **kwargs):
    return asyncio.run(client.create(model='test', messages=[{'role': 'user', 'content': 'fixture'}], **kwargs))


@pytest.mark.parametrize('status', [401, 402])
def test_first_account_refusal_halts_across_new_client(tmp_path, status):
    class Refusal(Exception):
        status_code = status
    def refuse(_):
        raise Refusal()
    client, provider, _ = setup(tmp_path, refuse)
    with pytest.raises(Refusal):
        call(client)
    assert client.halted and provider.calls == 1
    with pytest.raises(BudgetStop):
        call(client)
    other = RecordedClient(provider, phase='P4', output=client.output, root=client.root, inherited_root=client.inherited_root)
    with pytest.raises(BudgetStop, match='persistent_account_refusal'):
        call(other)
    assert provider.calls == 1 and rows(client.ledger)[-1]['httpStatus'] == status


@pytest.mark.parametrize('updates', [
    {'requestCap': 1718}, {'tokenCap': 6692647}, {'phaseRequestCaps': {'P4': 895}},
    {'deadlineAt': '2020-01-01T00:00:00+00:00'},
])
def test_inherited_limits_stop_before_dispatch(tmp_path, updates):
    client, provider, _ = setup(tmp_path, **updates)
    with pytest.raises(BudgetStop):
        call(client)
    assert provider.calls == 0 and not client.ledger.exists()


def test_unknown_start_reservation_and_unique_next_id(tmp_path):
    client, provider, _ = setup(tmp_path)
    append(client.ledger, {'event': 'START', 'requestId': 'ctxv4-call-00001', 'phase': 'P4', 'reservedTokens': 100000})
    _, starts, charged = client.usage_state()
    assert charged == 6792647 and len(starts) == 1
    call(client)
    assert rows(client.ledger)[-1]['requestId'] == 'ctxv4-call-00002'


@pytest.mark.parametrize('kwargs', [{'stream': True}, {'max_tokens': 4097}, {'max_tokens': True}])
def test_unsupported_requests_not_dispatched(tmp_path, kwargs):
    client, provider, _ = setup(tmp_path)
    with pytest.raises(BudgetStop):
        call(client, **kwargs)
    assert provider.calls == 0


def test_write_ahead_receipt_and_effective_options(tmp_path):
    client, provider, _ = setup(tmp_path)
    def observed(kwargs):
        assert rows(client.ledger)[-1]['event'] == 'START'
        assert rows(client.output / 'private_requests.jsonl')[-1]['request'] == kwargs
        assert kwargs['extra_body']['thinking']['type'] == 'disabled'
        assert kwargs['max_tokens'] == 4096 and kwargs['temperature'] == 0
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason='stop')], model_dump=lambda: {})
    provider.callback = observed
    call(client)
    _, _, charged = client.usage_state()
    assert charged == 6692647 + rows(client.ledger)[0]['reservedTokens']


def test_old_writer_lock_blocks_new_dispatch(tmp_path):
    client, provider, _ = setup(tmp_path)
    with writer_lock(client.inherited_root / 'provider-writer.lock'):
        with pytest.raises(BudgetStop, match='writer_already_active'):
            call(client)
    assert provider.calls == 0


def test_inherited_ledger_drift_blocked(tmp_path):
    client, provider, _ = setup(tmp_path)
    append(client.inherited_root / 'provider_ledger.jsonl', {'changed': True})
    with pytest.raises(BudgetStop, match='inherited_ledger_changed'):
        call(client)
    assert provider.calls == 0


def test_no_sdk_retry(tmp_path):
    client, provider, _ = setup(tmp_path)
    provider.max_retries = 2
    with pytest.raises(BudgetStop, match='sdk_retries_must_be_zero'):
        call(client)
    assert provider.calls == 0


def test_frozen_schedule_coverage_and_order():
    from .common import HERE, V3, sha
    mapping = json.loads((HERE / 'p1/recovery_mapping.json').read_text(encoding='utf-8'))
    schedule = json.loads((HERE / 'p1/schedule.json').read_text(encoding='utf-8'))
    original = json.loads((V3 / 'p4/confirm001/schedule.json').read_text(encoding='utf-8'))
    assert schedule == [r for r in original if int(r['conversationId'].rsplit('-', 1)[1]) >= 55]
    assert len(schedule) == 186 and mapping['counts'] == {
        'NEW_PLANNED_OUTCOME': 165, 'ACCOUNT_REFUSAL_RECOVERY': 3, 'WHOLE_CONVERSATION_PREREQUISITE_REPLAY': 18}
    data = rows(HERE / 'p1/recovery.jsonl')
    assert len(data) == 10 and sum(len(c['turns']) for c in data) * 2 == 186
    assert all(sha(t['rawUserText']) == t['messageSha256'] for c in data for t in c['turns'])
