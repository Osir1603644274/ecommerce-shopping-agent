"""Inherited budget with durable refusals and cross-version writer exclusion."""
import asyncio
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from agent.evaluation.context_program_v2_20260904.common import (
    ROOT, BudgetStop, append, canonical, json_new, now, rows, sha,
)

HERE = Path(__file__).resolve().parent
V3 = HERE.with_name('context_program_v3_20260904')


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextmanager
def writer_lock(path):
    import msvcrt
    with Path(path).open('a+b') as guard:
        if guard.tell() == 0:
            guard.write(b'0'); guard.flush()
        guard.seek(0)
        try:
            msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BudgetStop('provider_writer_already_active') from exc
        try:
            yield
        finally:
            guard.seek(0); msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)


def freeze(path):
    sources = sorted((ROOT / 'agent/app').rglob('*.py')) + sorted(HERE.glob('*.py'))
    sources += [ROOT / '.env', HERE / 'p0/contract.json', HERE / 'p1/recovery.jsonl',
                HERE / 'p1/schedule.json']
    for directory in ('real_user_multiturn_ab_executor_20260902_v1',
                      'real_user_multiturn_ab_executor_20260902_v2'):
        sources += sorted((HERE.parent / directory).glob('*.py'))
    sources += [HERE.parent / 'context_program_v2_20260904/common.py',
                HERE.parent / 'context_program_v2_20260904/datasets.py']
    json_new(path, {'at': now(), 'sources': [
        {'path': p.relative_to(ROOT).as_posix(), 'sha256': file_sha(p)} for p in sources]})


def check_freeze(path):
    for row in json.loads(Path(path).read_text(encoding='utf-8'))['sources']:
        if file_sha(ROOT / row['path']) != row['sha256']:
            raise RuntimeError('source_drift:' + row['path'])


class RecordedClient:
    def __init__(self, provider, *, phase, output, root=HERE, inherited_root=V3):
        self.provider, self.phase, self.output = provider, phase, Path(output)
        self.root, self.inherited_root = Path(root), Path(inherited_root)
        self.binding = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.lock = asyncio.Lock()
        self.ledger = self.root / 'provider_ledger.jsonl'
        self.halted = False
        self.transport_failures = 0

    def usage_state(self):
        contract = json.loads((self.root / 'p0/contract.json').read_text(encoding='utf-8'))
        if file_sha(self.inherited_root / 'provider_ledger.jsonl') != contract['inheritedLedgerSha256']:
            raise BudgetStop('inherited_ledger_changed')
        events = rows(self.ledger) if self.ledger.exists() else []
        starts = {r['requestId']: r for r in events if r['event'] == 'START'}
        ends = {r['requestId']: r for r in events if r['event'] == 'END'}
        if len(starts) != sum(r['event'] == 'START' for r in events) or not set(ends) <= set(starts):
            raise BudgetStop('ledger_edges_invalid')
        if any(r.get('httpStatus') in (401, 402) for r in ends.values()):
            self.halted = True
            raise BudgetStop('persistent_account_refusal_no_automatic_retry')
        charged = contract['inheritedTokens'] + sum(
            (ends.get(k, {}).get('usage') or {}).get('total_tokens', s['reservedTokens'])
            for k, s in starts.items())
        return contract, starts, charged

    async def create(self, **kwargs):
        async with self.lock:
            with ExitStack() as stack:
                stack.enter_context(writer_lock(self.inherited_root / 'provider-writer.lock'))
                stack.enter_context(writer_lock(self.root / 'provider-writer.lock'))
                return await self._dispatch(kwargs)

    async def _dispatch(self, kwargs):
        if self.halted:
            raise BudgetStop('provider_dispatch_halted')
        if kwargs.get('stream'):
            raise BudgetStop('streaming_receipts_not_registered')
        kwargs = dict(kwargs)
        kwargs['temperature'] = 0
        kwargs.setdefault('max_tokens', 4096)
        if type(kwargs['max_tokens']) is not int or not 1 <= kwargs['max_tokens'] <= 4096:
            raise BudgetStop('unregistered_output_cap')
        extra = dict(kwargs.get('extra_body') or {})
        extra['thinking'] = {'type': 'disabled'}
        kwargs['extra_body'] = extra
        contract, starts, charged = self.usage_state()
        reserved = len(canonical(kwargs).encode()) + 8192 + kwargs['max_tokens']
        phase_used = contract['inheritedPhaseRequests'].get(self.phase, 0)
        if (datetime.now(timezone.utc) >= datetime.fromisoformat(contract['deadlineAt'])
            or contract['inheritedRequests'] + len(starts) >= contract['requestCap']
            or phase_used + sum(s['phase'] == self.phase for s in starts.values()) >= contract['phaseRequestCaps'][self.phase]
            or charged + reserved > contract['tokenCap']):
            self.halted = True
            raise BudgetStop('request_token_or_time_budget_exhausted')
        if self.provider.max_retries != 0:
            raise BudgetStop('sdk_retries_must_be_zero')
        if kwargs['model'] != contract['model'] or str(self.provider.base_url).rstrip('/') != contract['endpoint'].rstrip('/'):
            raise BudgetStop('provider_model_or_endpoint_drift')
        request_id = f'ctxv4-call-{len(starts)+1:05d}'
        entry = {'event': 'START', 'requestId': request_id, 'phase': self.phase, 'at': now(),
                 'binding': dict(self.binding), 'requestSha256': sha(kwargs),
                 'reservedTokens': reserved, 'effectiveMaxTokens': kwargs['max_tokens'],
                 'model': kwargs['model'], 'endpoint': str(self.provider.base_url), 'sdkRetries': 0}
        append(self.output / 'private_requests.jsonl', {**entry, 'request': kwargs})
        append(self.ledger, entry)
        started = asyncio.get_running_loop().time()
        try:
            response = await self.provider.chat.completions.create(**kwargs)
        except Exception as exc:
            self.transport_failures += 1
            status = getattr(exc, 'status_code', None)
            append(self.ledger, {'event': 'END', 'requestId': request_id, 'at': now(),
                'status': 'FAILED_OR_UNKNOWN', 'errorType': type(exc).__name__, 'httpStatus': status, 'usage': None})
            if status in (401, 402) or self.transport_failures >= 3:
                self.halted = True
            raise
        self.transport_failures = 0
        body = response.model_dump()
        append(self.output / 'private_responses.jsonl', {'requestId': request_id, 'response': body})
        append(self.ledger, {'event': 'END', 'requestId': request_id, 'at': now(), 'status': 'SUCCEEDED',
            'usage': response.usage.model_dump() if response.usage else None,
            'finishReason': response.choices[0].finish_reason,
            'durationMs': (asyncio.get_running_loop().time() - started) * 1000, 'responseSha256': sha(body)})
        return response
