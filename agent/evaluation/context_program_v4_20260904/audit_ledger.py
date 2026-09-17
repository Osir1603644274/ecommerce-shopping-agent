"""Reconcile every current receipt plus the sealed inherited budget."""
from collections import Counter
import json
from .common import HERE, V3, file_sha, rows, sha, json_new


def audit():
    contract = json.loads((HERE / 'p0/contract.json').read_text(encoding='utf-8'))
    events = rows(HERE / 'provider_ledger.jsonl')
    starts = {e['requestId']: e for e in events if e['event'] == 'START'}
    ends = {e['requestId']: e for e in events if e['event'] == 'END'}
    requests = [r for p in HERE.glob('p*/**/private_requests.jsonl') for r in rows(p)]
    responses = [r for p in HERE.glob('p*/**/private_responses.jsonl') for r in rows(p)]
    req = {r['requestId']: r for r in requests}; resp = {r['requestId']: r for r in responses}
    charged = contract['inheritedTokens'] + sum(
        (ends.get(k, {}).get('usage') or {}).get('total_tokens', s['reservedTokens']) for k, s in starts.items())
    phase_used = dict(Counter(s['phase'] for s in starts.values()))
    combined_phase = {p: contract['inheritedPhaseRequests'].get(p, 0) + phase_used.get(p, 0)
                      for p in contract['phaseRequestCaps']}
    checks = {
        'uniqueStartIds': len(starts) == sum(e['event'] == 'START' for e in events),
        'uniqueEndIds': len(ends) == sum(e['event'] == 'END' for e in events),
        'allEndsHaveStarts': set(ends) <= set(starts),
        'uniquePrivateIds': len(req) == len(requests) and len(resp) == len(responses),
        'allStartsHaveRawRequest': set(starts) == set(req),
        'allSuccessEndsHaveRawResponse': {k for k,e in ends.items() if e['status']=='SUCCEEDED'} == set(resp),
        'requestHashesMatch': all(sha(req[k]['request']) == s['requestSha256'] for k,s in starts.items()),
        'responseHashesMatch': all(sha(resp[k]['response']) == e['responseSha256'] for k,e in ends.items() if e['status']=='SUCCEEDED'),
        'noSDKRetry': all(s['sdkRetries'] == 0 for s in starts.values()),
        'allStartsWithinTime': all(s['at'] < contract['deadlineAt'] for s in starts.values()),
        'requestCap': contract['inheritedRequests'] + len(starts) <= contract['requestCap'],
        'tokenCapWithUnknownReservations': charged <= contract['tokenCap'],
        'phaseCaps': all(count <= contract['phaseRequestCaps'][p] for p,count in combined_phase.items()),
        'inheritedLedgerUnchanged': file_sha(V3 / 'provider_ledger.jsonl') == contract['inheritedLedgerSha256'],
    }
    return {'status': 'PASS_LEDGER_EDGES' if all(checks.values()) else 'FAIL', 'checks': checks,
        'newRequests': len(starts), 'inheritedRequests': contract['inheritedRequests'],
        'totalRequests': contract['inheritedRequests'] + len(starts), 'chargedTokens': charged,
        'newPhaseRequests': phase_used, 'inheritedAndNewPhaseRequests': combined_phase,
        'unresolvedNewRequestIds': sorted(set(starts)-set(ends)),
        'inheritedUnresolvedRequestIds': ['ctxv3-call-00036'],
        'httpErrorCounts': dict(Counter(str(e['httpStatus']) for e in ends.values() if e.get('httpStatus'))),
        'remainingRequests': contract['requestCap']-contract['inheritedRequests']-len(starts),
        'remainingTokens': contract['tokenCap']-charged}


if __name__ == '__main__':
    result = audit(); json_new(HERE / 'p8/ledger_audit.json', result); print(json.dumps(result, indent=2))
