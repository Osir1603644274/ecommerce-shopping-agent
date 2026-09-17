"""Versioned repair execution with inherited, globally bounded API accounting."""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agent.evaluation.context_program_v2_20260904.common import (
    ROOT, BudgetStop, append, canonical, file_sha, json_new, manifest_check, now, rows, sha,
)

HERE = Path(__file__).resolve().parent


def freeze(path):
    sources = sorted((ROOT / 'agent/app').rglob('*.py')) + sorted(HERE.glob('*.py'))
    sources += [ROOT / '.env', HERE / 'p0/contract.json']
    json_new(path, {'at': now(), 'sources': [
        {'path': str(p.relative_to(ROOT)).replace('\\', '/'), 'sha256': file_sha(p)} for p in sources]})


def check_freeze(path):
    record = json.loads(Path(path).read_text(encoding='utf-8'))
    for row in record['sources']:
        if file_sha(ROOT / row['path']) != row['sha256']:
            raise RuntimeError('source_drift:' + row['path'])


class RecordedClient:
    def __init__(self, provider, *, phase, output):
        self.provider, self.phase, self.output = provider, phase, Path(output)
        self.binding = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.lock = asyncio.Lock()
        self.ledger = HERE / 'provider_ledger.jsonl'
        self.halted = False
        self.transport_failures = 0

    def usage_state(self):
        contract = json.loads((HERE / 'p0/contract.json').read_text(encoding='utf-8'))
        events = rows(self.ledger) if self.ledger.exists() else []
        starts = {e['requestId']: e for e in events if e['event'] == 'START'}
        ends = {e['requestId']: e for e in events if e['event'] == 'END'}
        charged = contract['inheritedTokens'] + sum(
            (ends.get(k, {}).get('usage') or {}).get('total_tokens', s['reservedTokens']) for k, s in starts.items())
        return contract, starts, charged

    async def create(self, **kwargs):
        async with self.lock:
            # An OS lock spans reservation through receipt. A crashed process
            # releases the lock, but its write-ahead token reservation survives.
            with (HERE / 'provider-writer.lock').open('a+b') as guard:
                import msvcrt
                if guard.tell() == 0: guard.write(b'0'); guard.flush()
                guard.seek(0)
                try: msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc: raise BudgetStop('provider_writer_already_active') from exc
                try: return await self._dispatch(kwargs)
                finally:
                    guard.seek(0)
                    msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)

    async def _dispatch(self, kwargs):
        if self.halted: raise BudgetStop('provider_dispatch_halted')
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
        if (datetime.now(timezone.utc) >= datetime.fromisoformat(contract['deadlineAt'])
            or contract['inheritedRequests'] + len(starts) >= contract['requestCap']
            or sum(s['phase'] == self.phase for s in starts.values()) >= contract['phaseRequestCaps'][self.phase]
            or charged + reserved > contract['tokenCap']):
            self.halted = True
            raise BudgetStop('request_token_or_time_budget_exhausted')
        if self.provider.max_retries != 0: raise BudgetStop('sdk_retries_must_be_zero')
        request_id = f'ctxv3-call-{len(starts)+1:05d}'
        entry = {'event': 'START', 'requestId': request_id, 'phase': self.phase,
            'at': now(), 'binding': dict(self.binding), 'requestSha256': sha(kwargs),
            'reservedTokens': reserved, 'effectiveMaxTokens': kwargs['max_tokens'],
            'model': kwargs['model'], 'endpoint': str(self.provider.base_url), 'sdkRetries': 0}
        append(self.output / 'private_requests.jsonl', {**entry, 'request': kwargs})
        append(self.ledger, entry)
        start = asyncio.get_running_loop().time()
        try: response = await self.provider.chat.completions.create(**kwargs)
        except Exception as exc:
            self.transport_failures += 1
            append(self.ledger, {'event': 'END', 'requestId': request_id, 'at': now(),
                'status': 'FAILED_OR_UNKNOWN', 'errorType': type(exc).__name__,
                'httpStatus': getattr(exc, 'status_code', None), 'usage': None})
            if self.transport_failures >= 3: self.halted = True
            raise
        self.transport_failures = 0
        body = response.model_dump()
        append(self.output / 'private_responses.jsonl', {'requestId': request_id, 'response': body})
        append(self.ledger, {'event': 'END', 'requestId': request_id, 'at': now(), 'status': 'SUCCEEDED',
            'usage': response.usage.model_dump() if response.usage else None,
            'finishReason': response.choices[0].finish_reason,
            'durationMs': (asyncio.get_running_loop().time()-start)*1000, 'responseSha256': sha(body)})
        return response
