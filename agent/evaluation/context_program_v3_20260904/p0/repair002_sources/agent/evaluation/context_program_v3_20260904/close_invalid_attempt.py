"""Preserve the interrupted scorer-defect attempt without relabeling outputs."""
from .common import HERE, json_new, now, rows

out = HERE / 'p2/attempt001'
events = rows(HERE / 'provider_ledger.jsonl')
starts = {e['requestId']: e for e in events if e['event'] == 'START'}
ends = {e['requestId']: e for e in events if e['event'] == 'END'}
json_new(out / 'stop.json', {'at': now(), 'status': 'INVALID_RUNNER_ORACLE_ATTEMPT_RETAINED',
    'reason': 'Oracle read sparse patch status without defaulting to existing ready state',
    'ownedProcessIdsTerminated': [3100, 42580], 'completed': len(rows(out / 'cases.jsonl')),
    'startedRequests': len(starts), 'unknownRequestIds': sorted(set(starts)-set(ends)),
    'unknownUsageReservedNotZeroed': True, 'originalCaseScoresUnchanged': True})
print('Interrupted attempt retained; unknown in-flight call stays charged')
