"""Offline, exhaustive batch routing. A plan is never an execution result."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from remote_actions import REMOTE_ACTIONS

PROFILES = ('local', 'remote', 'logistics', 'receipt-retry')


def require_environment(rows, config, *, has_inventory_fault=False):
    profiles = {profile_for(row) for row in rows}
    if len(profiles) != 1:
        raise ValueError('mixed runtime requirements; execute separate profile batches')
    profile = next(iter(profiles))
    remote = bool(config.get('inventoryRuntime'))
    warehouse = bool(config.get('warehouseFaultActive'))
    recovery = bool(config.get('supportRecoveryActive'))
    if profile == 'local':
        ready = not (remote or warehouse or recovery)
    elif profile == 'remote':
        ready = remote and has_inventory_fault and not warehouse and not recovery
    elif profile == 'logistics':
        ready = warehouse and not recovery
    else:
        ready = remote and has_inventory_fault and recovery and not warehouse
    if not ready:
        raise ValueError('runtime not ready for profile: ' + profile)
    return profile


def profile_for(row):
    actions = {s['action'] for s in row.get('preSteps', []) + row['steps']}
    receipt = bool(actions & {'exhaust_receipt_retries', 'manual_original_retry'})
    logistics = row['fixture']['kind'] in {'dispatch_unknown', 'dispatch_review'}
    if receipt and logistics:
        raise ValueError('incompatible background workers: ' + row['id'])
    if receipt:
        return 'receipt-retry'
    if logistics:
        if actions & REMOTE_ACTIONS:
            raise ValueError('unsupported combined fault: ' + row['id'])
        return 'logistics'
    if actions & REMOTE_ACTIONS or row['fixture']['kind'] == 'exchange_reserve_unknown':
        return 'remote'
    return 'local'


def select_cases(rows, *, split='dev', profile=None, category=None, case_id=None,
                 outcome=None, limit=None):
    if split not in ('dev', 'heldout', 'all'):
        raise ValueError('invalid split')
    if profile is not None and profile not in PROFILES:
        raise ValueError('invalid profile')
    if limit is not None and limit < 1:
        raise ValueError('limit must be positive')
    ids = [r['id'] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate case IDs')
    if any(r['split'] not in ('dev', 'heldout') for r in rows):
        raise ValueError('unknown dataset split')
    selected = []
    for row in rows:
        if split != 'all' and row['split'] != split:
            continue
        if profile and profile_for(row) != profile:
            continue
        if category and row['category'] != category:
            continue
        if case_id and row['id'] != case_id:
            continue
        actual = row['expected']['routeOrOutcome']
        if outcome and not (actual.startswith(outcome[:-1]) if outcome.endswith('*') else actual == outcome):
            continue
        selected.append(row)
    selected = selected if limit is None else selected[:limit]
    if not selected:
        raise ValueError('no cases selected')
    return selected


def build_plan(dataset, repetitions=3):
    if repetitions != 3:
        raise ValueError('acceptance contract requires three repetitions')
    data = dataset.read_bytes()
    rows = select_cases([json.loads(line) for line in data.decode('utf-8').splitlines()], split='all')
    if len(rows) != 240 or Counter(r['split'] for r in rows) != {'dev': 120, 'heldout': 120}:
        raise ValueError('expected 240 cases with 120 per split')
    counts = Counter((r['category'], r['split']) for r in rows)
    if len(counts) != 20 or set(counts.values()) != {12}:
        raise ValueError('expected ten categories with twelve cases per split')
    families = {}
    for row in rows:
        if families.setdefault(row['familyId'], row['split']) != row['split']:
            raise ValueError('family crosses split: ' + row['familyId'])
    batches = []
    for repetition in range(1, repetitions + 1):
        for split in ('dev', 'heldout'):
            for profile in PROFILES:
                ids = [r['id'] for r in rows if r['split'] == split and profile_for(r) == profile]
                if ids:
                    batches.append(dict(repetition=repetition, split=split, profile=profile, caseIds=ids))
    return dict(status='PLANNED_NOT_EXECUTED', datasetSha256=hashlib.sha256(data).hexdigest(),
                modelConcurrency=1, plannedObservations=720, humanReviewed=False,
                batches=batches,
                limitations=['Draft silver labels; no human gold or blind-test claim.',
                             'Execution requires fixed code, model configuration and profile readiness.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = build_plan(args.dataset)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(plan, output, ensure_ascii=False, indent=2)
    print(json.dumps({'status': plan['status'], 'batches': len(plan['batches']),
                      'plannedObservations': plan['plannedObservations']}))
