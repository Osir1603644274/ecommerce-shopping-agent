"""Independently assert P6 observed identities, effects and idempotent replay."""
import json
from .common import HERE, file_sha, json_new

out = HERE / 'p6/interrupt001'
cases = {name: json.loads((out / f'{name}.json').read_text(encoding='utf-8'))
         for name in ('clarification','operator_pause','state_diverged')}
c, p, d = (cases[n] for n in ('clarification','operator_pause','state_diverged'))
def result(process): return process.get('result') or {}
checks = {
    'clarificationParked': result(c['parked']).get('boundary') == 'clarification',
    'clarificationResumeCompleted': result(c['resumed']).get('boundary') == 'task_completed',
    'freshProcesses': c['parked']['pid'] != c['resumed']['pid'] and p['paused']['pid'] != p['resumed']['pid'],
    'sameRunAndThreadAfterClarification': all(result(c['parked']).get(k) == result(c['resumed']).get(k) and result(c['resumed']).get(k)
        for k in ('runId','threadId')),
    'sameRunAndThreadAfterPause': all(result(p['paused']).get(k) == result(p['resumed']).get(k) and result(p['resumed']).get(k)
        for k in ('runId','threadId')),
    'tripleReplayIdempotent': len(c['replays']) == 3 and all(r['returnCode']==0 and result(r).get('mode')=='idempotent_replay' for r in c['replays']),
    'replayRevisionUnchanged': c['beforeReplayState']['revision'] == c['finalState']['revision'],
    'singleEffectPerRecoveredTask': len(c['ledger']) == len(p['ledger']) == 1,
    'operatorPausedThenCompleted': result(p['paused']).get('boundary')=='operator_paused' and result(p['resumed']).get('boundary')=='task_completed',
    'driftRefusedWithoutEffect': len(d['restarts']) == 1 and result(d['restarts'][0]).get('boundary')=='state_diverged' and len(d['ledger'])==0,
    'noModelCalls': all(not row['modelCalls'] for row in cases.values()),
}
report = {'status': 'PASS_BOUNDED_PROCESS_RECOVERY' if all(checks.values()) else 'FAIL', 'checks': checks,
    'inputSha256': {name: file_sha(out / f'{name}.json') for name in cases},
    'scope': 'three deterministic probes; no model continuation, human judgments or external payment effects'}
json_new(out / 'verification.json', report)
print(report)
