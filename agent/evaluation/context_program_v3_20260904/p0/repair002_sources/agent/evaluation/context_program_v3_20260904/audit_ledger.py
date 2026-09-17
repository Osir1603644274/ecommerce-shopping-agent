"""Read-only request/evidence edge audit, including inherited/unknown costs."""
import json
from collections import Counter
from .common import HERE, file_sha, rows, sha


def audit():
    contract = json.loads((HERE / 'p0/contract.json').read_text(encoding='utf-8'))
    events = rows(HERE / 'provider_ledger.jsonl')
    starts = {e['requestId']:e for e in events if e['event']=='START'}
    ends = {e['requestId']:e for e in events if e['event']=='END'}
    requests = [r for p in HERE.glob('p*/**/private_requests.jsonl') for r in rows(p)]
    responses = [r for p in HERE.glob('p*/**/private_responses.jsonl') for r in rows(p)]
    req = {r['requestId']:r for r in requests}; resp = {r['requestId']:r for r in responses}
    unknown = sorted(set(starts)-set(ends))
    charged = contract['inheritedTokens'] + sum((ends.get(k,{}).get('usage') or {}).get('total_tokens',s['reservedTokens']) for k,s in starts.items())
    checks = {
        'uniqueStartIds': len(starts)==sum(e['event']=='START' for e in events),
        'uniqueEndIds': len(ends)==sum(e['event']=='END' for e in events),
        'uniquePrivateIds': len(req)==len(requests) and len(resp)==len(responses),
        'allStartsHaveRawRequest': set(starts)==set(req),
        'allSuccessEndsHaveRawResponse': {k for k,e in ends.items() if e['status']=='SUCCEEDED'}==set(resp),
        'requestHashesMatch': all(sha(req[k]['request'])==s['requestSha256'] for k,s in starts.items()),
        'responseHashesMatch': all(sha(resp[k]['response'])==e['responseSha256'] for k,e in ends.items() if e['status']=='SUCCEEDED'),
        'noSDKRetry': all(r['sdkRetries']==0 for r in starts.values()),
        'requestCap': len(starts)+contract['inheritedRequests']<=contract['requestCap'],
        'tokenCapWithUnknownReservations': charged<=contract['tokenCap'],
        'phaseCaps': all(sum(s['phase']==p for s in starts.values())<=cap for p,cap in contract['phaseRequestCaps'].items()),
        'inheritedLedgerUnchanged': file_sha(HERE.with_name('context_program_v2_20260904')/'provider_ledger.jsonl')==contract['inheritedLedgerSha256'],
    }
    return {'status':'PASS_LEDGER_EDGES' if all(checks.values()) else 'FAIL', 'checks':checks,
        'newRequests':len(starts), 'inheritedRequests':contract['inheritedRequests'],
        'totalRequests':len(starts)+contract['inheritedRequests'], 'chargedTokens':charged,
        'phaseRequests':dict(Counter(s['phase'] for s in starts.values())),
        'unresolvedRequestIds':unknown, 'remainingRequests':contract['requestCap']-len(starts)-contract['inheritedRequests'],
        'remainingTokens':contract['tokenCap']-charged}


if __name__ == '__main__': print(json.dumps(audit(),indent=2))
