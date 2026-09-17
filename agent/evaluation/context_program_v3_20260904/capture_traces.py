"""Read-only observer for one already-created, test-owned Redis lane."""
import json
import sys
import time
import redis
from .common import HERE, append, rows, sha

out = HERE / 'p4' / sys.argv[1]
identity = json.loads((out / 'redis_identity.json').read_text())
client = redis.Redis(host='127.0.0.1', port=identity['port'], decode_responses=True, socket_timeout=1)
seen = set()
for _ in range(1800):
    try:
        for row in rows(out / 'outputs.jsonl') if (out / 'outputs.jsonl').exists() else []:
            run_id = row['runId']
            if run_id in seen: continue
            raw = client.get('agent-run-trace:' + run_id)
            if raw:
                body = json.loads(raw)
                append(out / 'trace_capture.jsonl', {'executionOrdinal':row['executionOrdinal'], 'runId':run_id,
                    'traceSha256':sha(body), 'trace':body})
                seen.add(run_id)
        if (out / 'result.json').exists(): break
    except redis.RedisError: break
    time.sleep(1)
client.close()
print('Read-only trace capture:', len(seen))
