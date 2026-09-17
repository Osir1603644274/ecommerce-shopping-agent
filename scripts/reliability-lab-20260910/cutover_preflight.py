"""Counts only; never print sessions, tokens, dialogue or transaction payloads."""
import json
import redis
from pathlib import Path
r=redis.Redis.from_url('redis://[::1]:6379/0',decode_responses=True)
counts={'runningOrPausing':0,'pendingTransactions':0,'unknownTransactions':0}
for key in r.scan_iter('commerce:workspace:*'):
    if r.type(key)!='string':continue
    try:value=json.loads(r.get(key))
    except (ValueError,TypeError):continue
    if not isinstance(value,dict):continue
    if key.endswith(':run') and value.get('status') in ('running','pausing'):counts['runningOrPausing']+=1
    checkout=value.get('checkout') or {}
    if isinstance(checkout,dict):
        if checkout.get('pending'):counts['pendingTransactions']+=1
        if isinstance(checkout.get('outcome'),dict) and checkout['outcome'].get('status')=='unknown':counts['unknownTransactions']+=1
print(json.dumps(counts))
assert not any(counts.values()),'Active/uncertain work: do not cut over'
