"""Read all sealed hashes, inherit budgets, select whole affected conversations."""
import json
import subprocess
from .common import HERE, V3, ROOT, file_sha, json_new, rows, now, sha


def main():
    seal = json.loads((V3 / 'SEAL.json').read_text(encoding='utf-8'))
    checks = {}
    for name, key in [('SHA256SUMS.txt', 'manifestSha256'), ('verification.json', 'verificationSha256'),
                      ('FINAL_DECISION.json', 'decisionSha256'), ('FINAL_REPORT.md', 'reportSha256')]:
        checks[name] = file_sha(V3 / name) == seal[key]
    mismatches = []
    entries = (V3 / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines()
    for entry in entries:
        expected, relative = entry.split('  ', 1)
        path = (V3 / relative).resolve()
        assert path.is_relative_to(V3.resolve())
        if file_sha(path) != expected:
            mismatches.append(relative)
    checks['all1136Artifacts'] = len(entries) == 1136 and not mismatches
    frozen = json.loads((V3 / 'p4/confirm001/source_freeze.json').read_text(encoding='utf-8'))
    drift = [r['path'] for r in frozen['sources'] if (r['path'].startswith('agent/app/') or r['path'] == '.env')
             and file_sha(ROOT / r['path']) != r['sha256']]
    checks['appAndConfigUnchanged'] = not drift
    assert all(checks.values()), (checks, mismatches, drift)
    decision = json.loads((V3 / 'FINAL_DECISION.json').read_text(encoding='utf-8'))
    contract = json.loads((V3 / 'p0/contract.json').read_text(encoding='utf-8'))
    budget = decision['budget']
    json_new(HERE / 'p0/preflight.json', {'at': now(), 'checks': checks, 'mismatches': mismatches,
        'sourceDrift': drift, 'gitHead': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'gitStatus': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True),
        'oldSealSha256': file_sha(V3 / 'SEAL.json'), 'noModelCalls': True})
    contract.update(programId=HERE.name, inheritedRequests=budget['totalRequests'],
        inheritedTokens=budget['chargedTokens'], inheritedPhaseRequests=budget['phaseRequests'],
        inheritedLedgerSha256=file_sha(V3 / 'provider_ledger.jsonl'),
        previousContractSha256=file_sha(V3 / 'p0/contract.json'), resumeAuthorizedAt=now(),
        resumeAuthorizingUserText='请继续', accountRefusalPolicy='FIRST_401_OR_402_HALT_PERSISTENT',
        totalCapsAndDeadlineUnchanged=True)
    json_new(HERE / 'p0/contract.json', contract)
    data = rows(V3 / 'p1/breadth001/confirm.jsonl')
    outputs = rows(V3 / 'p4/confirm001/outputs.jsonl')
    schedule = json.loads((V3 / 'p4/confirm001/schedule.json').read_text(encoding='utf-8'))
    by_cid = {c['conversationId']: [r for r in outputs if r['conversationId'] == c['conversationId']] for c in data}
    selected = [c for c in data if len(by_cid[c['conversationId']]) != 2 * len(c['turns'])
                or any(r['status'] != 'SUCCEEDED' for r in by_cid[c['conversationId']])]
    ids = {c['conversationId'] for c in selected}
    assert ids == {f'ctxv3-confirm-{i:03d}' for i in range(55, 65)}
    selected_schedule = [r for r in schedule if r['conversationId'] in ids]
    (HERE / 'p1').mkdir(exist_ok=True)
    from .common import append
    target = HERE / 'p1/recovery.jsonl'
    assert not target.exists()
    for item in selected:
        append(target, item)
    json_new(HERE / 'p1/schedule.json', selected_schedule)
    mapping = {(r['conversationId'], r['turnId'], r['arm']): r for r in outputs}
    mapping_rows = []
    for row in selected_schedule:
        previous = mapping.get((row['conversationId'], row['turnId'], row['arm']))
        mapping_rows.append({'executionOrdinal': row['executionOrdinal'], 'scheduleRowSha256': row['scheduleRowSha256'],
            'conversationId': row['conversationId'], 'turnId': row['turnId'], 'arm': row['arm'],
            'previousStatus': previous['status'] if previous else 'NOT_EXECUTED',
            'previousRecordSha256': sha(previous) if previous else None,
            'previousRunId': previous['runId'] if previous else None,
            'classification': 'NEW_PLANNED_OUTCOME' if previous is None else (
                'ACCOUNT_REFUSAL_RECOVERY' if previous['status'] != 'SUCCEEDED' else 'WHOLE_CONVERSATION_PREREQUISITE_REPLAY')})
    counts = {name: sum(r['classification'] == name for r in mapping_rows) for name in
              ('NEW_PLANNED_OUTCOME', 'ACCOUNT_REFUSAL_RECOVERY', 'WHOLE_CONVERSATION_PREREQUISITE_REPLAY')}
    json_new(HERE / 'p1/recovery_mapping.json', {'at': now(), 'rows': mapping_rows, 'counts': counts,
        'originalDatasetSha256': file_sha(V3 / 'p1/breadth001/confirm.jsonl'),
        'originalScheduleSha256': file_sha(V3 / 'p4/confirm001/schedule.json'),
        'policy': 'Replay whole affected conversations in new identities; never overwrite old failures. Original per-turn arm order and ordinals are retained.',
        'reason': 'Original owned Redis was intentionally ephemeral (--save empty, appendonly no) and is stopped. Endpoint TaskState captures do not establish a complete durable replay checkpoint and all dependent runtime records.',
        'notFreshBlindConfirmation': True})
    print({'checks': checks, 'selectedConversations': len(selected), 'armTurns': len(selected_schedule), 'mapping': counts,
           'remainingRequests': budget['remainingRequests'], 'remainingTokens': budget['remainingTokens'], 'deadlineAt': contract['deadlineAt']})


if __name__ == '__main__':
    main()
