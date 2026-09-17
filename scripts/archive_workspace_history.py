"""Recover extant Redis dialogue/trace records into the private archive.

No model calls, no Redis writes, no gold labels. Already expired data cannot be
reconstructed. Original submission times are not invented for imported rows.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import redis
from agent.app.workspace_archive import WorkspaceArchive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--redis-url', default='redis://[::1]:6379/0')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    db = redis.Redis.from_url(args.redis_url, decode_responses=True)
    archive = WorkspaceArchive()
    traces = {}
    for key in db.scan_iter(match='agent-run-trace:*', count=100):
        value = db.get(key)
        if value:
            row = json.loads(value)
            traces.setdefault(row.get('sessionId'), []).append(row)
    counts = dict(conversations=0, messages=0, runs=0, traces=0, activeRuns=0, applied=args.apply)
    for key in db.scan_iter(match='commerce:workspace:merged439-v1:*', count=100):
        if not (key.endswith(':guest') or ':user:' in key and key.count(':') == 4):
            continue
        raw = db.get(key)
        if not raw:
            continue
        state = json.loads(raw)
        if not isinstance(state, dict) or 'messages' not in state:
            continue
        counts['conversations'] += 1
        counts['messages'] += len(state['messages'])
        raw_run = db.get(key + ':run')
        run = json.loads(raw_run) if raw_run else None
        if run:
            counts['runs'] += 1
            counts['activeRuns'] += run.get('status') not in {'completed', 'ended'}
        captured = traces.get(state['engine'], [])
        counts['traces'] += len(captured)
        if args.apply:
            archive.record(key, state, kind='legacy_recovery', payload=dict(
                run=run, traces=captured, source='legacy_unverified',
                labelingStatus='UNREVIEWED', benchmarkEligible=False,
                originalTimeKnown=False, redisKeyHash=__import__('hashlib').sha256(key.encode()).hexdigest()))
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
