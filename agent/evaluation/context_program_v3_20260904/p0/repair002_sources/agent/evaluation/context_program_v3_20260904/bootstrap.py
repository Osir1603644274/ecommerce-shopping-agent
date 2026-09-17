"""Freeze inherited evidence before autonomous repairs; no provider calls."""
import json
import shutil
from pathlib import Path

from agent.evaluation.context_program_v2_20260904.common import (
    ROOT, file_sha, json_new, manifest_check, now, rows,
)

HERE = Path(__file__).resolve().parent


def main():
    old = HERE.with_name('context_program_v2_20260904')
    checks = {}
    for relative in (
        'context_program_v2_20260904/SHA256SUMS.txt',
        'context_program_v1_20260904/FINAL_SHA256SUMS_v2.txt',
        'context_failure_repair_20260904/SHA256SUMS.txt',
    ):
        check = manifest_check(HERE.parent / relative)
        assert not check['mismatches'], relative
        checks[relative] = {'count': check['checked'], 'sha256': file_sha(HERE.parent / relative)}
    events = rows(old / 'provider_ledger.jsonl')
    starts = {e['requestId']: e for e in events if e['event'] == 'START'}
    ends = {e['requestId']: e for e in events if e['event'] == 'END'}
    tokens = sum((ends.get(k, {}).get('usage') or {}).get('total_tokens', s['reservedTokens']) for k, s in starts.items())
    contract = json.loads((old / 'p0/contract.json').read_text(encoding='utf-8'))
    contract.update(programId=HERE.name, revision='autonomous-repair-001',
        formalRetryPolicy='USER_AUTHORIZED_VERSIONED_REPAIR_THEN_NEW_ATTEMPT',
        inheritedRequests=len(starts), inheritedTokens=tokens,
        inheritedLedgerSha256=file_sha(old / 'provider_ledger.jsonl'),
        phaseRequestCaps={'P2': 240, 'P3': 240, 'P4': 1680, 'P5': 499, 'P6': 40, 'P7': 200},
        budgetReallocation='P2 new repair allowance from unused P4; inherited 101 calls charged globally; no cap increase',
        authorizedAt=now(), authorizingUserText='我希望你自动修复，除非到了人工不得不下场的时候，除此之外你自动修复自动推进')
    json_new(HERE / 'p0/contract.json', contract)
    sources = []
    for relative in ('agent/app/llm.py', 'agent/app/task_state_output_contract.py',
                     'agent/tests/test_context_failure_repairs.py'):
        source = ROOT / relative
        target = HERE / 'p0/before' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists(): raise FileExistsError(target)
        shutil.copyfile(source, target)
        sources.append({'path': relative, 'sha256': file_sha(source)})
    json_new(HERE / 'p0/baseline.json', {'at': now(), 'sources': sources, 'historicalManifests': checks,
        'status': 'BASELINE_CAPTURED_NO_REPAIR_YET', 'productionDefaultsChanged': False})
    print(json.dumps({'baseline': 'PASS', 'inheritedRequests': len(starts), 'inheritedTokens': tokens}))


if __name__ == '__main__': main()
