"""Opt-in local runtime crash/restart acceptance. No purchases or database reset.

Only restarts the process owned by merged-commerce.ps1; refuses other active runs.
Run from repository root using .venv/Scripts/python.exe, with --restart-authorized.
"""
import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import redis

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--restart-authorized', action='store_true')
    args = parser.parse_args()
    if not args.restart_authorized:
        raise SystemExit('Explicit local process-restart authorization is required')
    r = redis.Redis.from_url('redis://[::1]:6379/0', decode_responses=True)
    for key in r.scan_iter(match='commerce:workspace:*:run', count=1000):
        run = json.loads(r.get(key))
        if run['status'] in {'running', 'pausing'}:
            raise SystemExit('Another run is active; do not restart its process')
    report = []
    for scenario in ('paused_restart', 'in_flight_crash'):
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=30, headers={'Sec-Fetch-Site':'same-origin'}) as c:
            base = '/api/commerce-demo/workspace'
            state = c.get(base).raise_for_status().json()
            c.headers['X-CSRF-Token'] = state['csrfToken']
            request_id = str(uuid.uuid4())
            result = c.post(base + '/run', json=dict(message='推荐2000元以内的二手手机', requestId=request_id, mode='continuous')).raise_for_status().json()
            run_id = result['run']['id']
            record_key = next(key for key in r.scan_iter(match='commerce:workspace:*:run', count=1000) if json.loads(r.get(key))['id'] == run_id)
            raw = json.loads(r.get(record_key))
            deadline = time.monotonic() + 30
            task_id = None
            while time.monotonic() < deadline:
                task_id = r.get('task-state-session:' + raw['engine'])
                cursor = r.get('graph-v2:cursor:' + task_id) if task_id else None
                if cursor:
                    break
                time.sleep(.03)
            assert task_id and cursor, 'No original cursor established'
            original_cursor = json.loads(cursor)
            if scenario == 'paused_restart':
                for _ in range(80):
                    result = c.get(base + '/control').raise_for_status().json()
                    run = result['run']
                    if run['status'] == 'paused':
                        break
                    if run['status'] == 'running':
                        c.post(base + '/control/pause', json=dict(runId=run['id'], revision=run['revision']))
                    time.sleep(.15)
                assert run['status'] == 'paused', run['status']
            else:
                result = c.get(base + '/control').raise_for_status().json()
                assert result['run']['status'] == 'running', 'Missed crash window'
            old_pid = json.loads((ROOT / '.runtime/merged-commerce/processes.json').read_text(encoding='utf-8-sig'))['agent']['pid']
            completed = subprocess.run(['pwsh', '-NoProfile', '-File', str(ROOT / 'scripts/merged-commerce.ps1'), 'restart-agent'],
                                       cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=120,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            assert completed.returncode == 0, 'Owned Agent restart failed; inspect launcher health/logs'
            new_pid = json.loads((ROOT / '.runtime/merged-commerce/processes.json').read_text(encoding='utf-8-sig'))['agent']['pid']
            assert old_pid != new_pid
            result = c.get(base).raise_for_status().json()
            run = result['run']
            assert run['id'] == run_id and run['status'] in {'interrupted', 'paused'}
            c.post(base + '/control/continue', json=dict(runId=run['id'], revision=run['revision'])).raise_for_status()
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                result = c.get(base + '/control').raise_for_status().json()
                if result['run']['status'] not in {'running', 'pausing'}:
                    break
                time.sleep(.5)
            assert result['run']['status'] == 'completed', result['run']
            assert len([m for m in result['messages'] if m['role'] == 'assistant' and m['requestId'] == request_id]) == 1
            final_cursor = json.loads(r.get('graph-v2:cursor:' + task_id))
            assert final_cursor['runId'] == original_cursor['runId']
            assert result['cards'] and result['checkout'] is None
            row = dict(scenario=scenario, status='PASS', oldPid=old_pid, newPid=new_pid,
                       requestId=request_id, taskId=task_id, runId=original_cursor['runId'], cardCount=len(result['cards']), assistantMessages=1)
            report.append(row)
            print(json.dumps(row), flush=True)
    target = ROOT / 'docs/acceptance/workspace-controls-20260909-recovery.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
