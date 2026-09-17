"""Derive paired results from every recorded phase; never alter source evidence."""
import argparse
import hashlib
import json
import re
import sqlite3
import statistics
from pathlib import Path


def metric_values(text, name):
    pattern = re.compile(r'^' + re.escape(name) + r'(?:\{[^\n]*\})? (\S+)', re.MULTILINE)
    return [float(value) for value in pattern.findall(text)]


def observations(directory):
    rows = [json.loads(line) for line in (directory / 'observations.jsonl').read_text(encoding='utf-8').splitlines()]
    metrics = {}
    for name in ['hikaricp_connections_active', 'hikaricp_connections_pending', 'hikaricp_connections_max',
                 'hikaricp_connections_timeout_total', 'local_life_fulfillment_worker_queued',
                 'local_life_fulfillment_worker_active', 'local_life_fulfillment_queue_rejected_total']:
        values = [value for row in rows for value in metric_values(row.get('prometheusRaw',''),name)]
        metrics[name] = {'max':max(values),'first':values[0],'last':values[-1]} if values else None
    statuses = [row['mysqlStatus'] for row in rows if 'mysqlStatus' in row]
    if not statuses:
        raise ValueError('No MySQL observations')
    for required in ['hikaricp_connections_active','hikaricp_connections_pending',
                     'hikaricp_connections_max','hikaricp_connections_timeout_total']:
        if metrics[required] is None:
            raise ValueError('Required metric was not observed: '+required)
    delta = {key: statuses[-1][key]-statuses[0][key] for key in
             ['Innodb_row_lock_waits','Innodb_row_lock_time','Innodb_deadlocks'] if key in statuses[0] and key in statuses[-1]}
    return {'samples':len(rows), 'prometheus':metrics, 'mysqlCounterDelta':delta,
            'maxDataLockWaits':max((row.get('dataLockWaits',0) for row in rows),default=0),
            'maxFulfillmentReady':max((sum(x['n'] for x in row.get('fulfillment',[]) if x['status']=='READY') for row in rows),default=0),
            'maxOutboxPending':max((sum(x['n'] for x in row.get('outbox',[]) if x['status']=='PENDING') for row in rows),default=0)}


def fixture_session_events(runtime):
    # Live runs keep session events beside infra; archived evidence keeps them
    # beside phases. Check both layouts, and fail on conflicting duplicate names.
    seen, events = {}, []
    for directory in (runtime, runtime.parent):
        for path in sorted(directory.glob('fixture-session-renewal-attempt*.json')):
            raw = path.read_bytes()
            previous = seen.setdefault(path.name, raw)
            if previous != raw:
                raise ValueError('Conflicting fixture session event: ' + path.name)
            value = json.loads(raw)
            if value not in events:
                events.append(value)
    return events


def warehouse_evidence(runtime):
    candidates = [directory / name for directory in (runtime, runtime.parent)
                  for name in ('warehouse-final-snapshot.sqlite', 'warehouse.sqlite')]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        return None, None
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        shipments = dict(db.execute('SELECT order_id,COUNT(*) FROM shipment GROUP BY order_id').fetchall())
    return path, shipments


def verify_business(directory, result, shipments):
    path = directory / 'business-invariants.json'
    if not path.is_file():
        raise ValueError('Missing per-order business evidence: ' + directory.name)
    evidence = json.loads(path.read_text(encoding='utf-8'))
    summary = result.get('business', {})
    for field in ('exactlyOneShipmentPerPaidOrder', 'noShipmentsForCancelledOrders'):
        if evidence.get(field) is not True or summary.get(field) is not True:
            raise ValueError('Business acceptance flag is not true: ' + directory.name + ':' + field)
    rows = evidence.get('rows')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Missing per-order business rows: ' + directory.name)
    seen, paid, cancelled = set(), 0, 0
    for row in rows:
        order_id = row.get('id')
        if not isinstance(order_id, str) or not order_id or order_id in seen:
            raise ValueError('Invalid or duplicate business order: ' + directory.name)
        seen.add(order_id)
        # V2 creates exactly one unit per order; reject a different fixture scope.
        if type(row.get('quantity')) is not int or row['quantity'] != 1:
            raise ValueError('Unexpected V2 order quantity: ' + directory.name + ':' + order_id)
        if row.get('status') == 'PAID':
            paid += 1
            expected = ('CONFIRMED', 'SHIPPED', 1)
        elif row.get('status') == 'CANCELLED':
            cancelled += 1
            expected = ('RELEASED', 'CANCELLED', 0)
        else:
            raise ValueError('Nonterminal business order: ' + directory.name + ':' + order_id)
        actual = (row.get('reservation_status'), row.get('fulfillment_status'), row.get('shipments'))
        if type(row.get('shipments')) is not int or actual != expected:
            raise ValueError('Per-order business invariant failed: ' + directory.name + ':' + order_id)
        if shipments is not None and shipments.get(order_id, 0) != expected[2]:
            raise ValueError('Warehouse snapshot disagrees with order: ' + directory.name + ':' + order_id)
    totals = {'orders': len(rows), 'paid': paid, 'cancelled': cancelled}
    stock = {'total_quantity': 100000, 'available_quantity': 100000-paid,
             'reserved_quantity': 0, 'sold_quantity': paid}
    for record in (evidence, summary):
        if any(type(record.get(key)) is not int or record[key] != value for key, value in totals.items()):
            raise ValueError('Business counts disagree with per-order evidence: ' + directory.name)
        if record.get('stock') != stock:
            raise ValueError('Inventory disagrees with per-order evidence: ' + directory.name)
    return {**totals, 'perOrderEvidenceSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'warehouseSnapshotChecked': shipments is not None,
            'scope': 'Per-order recorded evidence validated; external snapshot checked only when explicitly available'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--selection',type=Path,help='Explicit primary and supplementary phase directory names with a reason')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Output exists; refusing overwrite')
    phases, pairs, content, content_arms = [], [], {}, {}
    selection = json.loads(args.selection.read_text(encoding='utf-8')) if args.selection else {
        'primary':[p.name for p in sorted(args.runtime.glob('pair*-attempt001'))],'supplementary':[]}
    renewal_events = fixture_session_events(args.runtime)
    warehouse_path, shipments = warehouse_evidence(args.runtime)
    for name in selection['primary']:
        directory = args.runtime/name
        result = json.loads((directory/'result.json').read_text(encoding='utf-8'))
        if result['status'] != 'BOUNDED_MIXED_WORKLOAD_ACCEPT':
            raise ValueError('Failed required phase: '+directory.name)
        result['businessVerification'] = verify_business(directory, result, shipments)
        result['directory'] = directory.name
        for event in renewal_events:
            overlap = min(result['startedUnix']+result['elapsedSeconds'],event['finishedUnix']) - max(result['startedUnix'],event['startedUnix'])
            if overlap > 0:
                raise ValueError('Authentication maintenance overlaps primary phase: '+directory.name)
        result['observations'] = observations(directory)
        phases.append(result)
        for line in (directory/'requests.jsonl').read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            if row['operation']=='page' and row['error'] is None:
                previous = content.setdefault(row['page'],row['contentSha256'])
                content_arms.setdefault(row['page'],set()).add(result['arm'])
                if previous != row['contentSha256']:
                    raise ValueError('Page content differs: '+str(row['page']))
    if len(phases) != 8 or len({p['fixtureSha256'] for p in phases}) != 1:
        raise ValueError('Eight phases using the same fixture are required')
    for number in range(1,5):
        rows = [p for p in phases if p['directory'].startswith('pair'+str(number)+'-')]
        baseline = next(p for p in rows if p['arm']=='nplusone-control')
        batch = next(p for p in rows if p['arm']=='batch')
        pair = {'pair':number,'order': [p['arm'] for p in sorted(rows,key=lambda x:x['startedUnix'])]}
        for field in ['p50Ms','p95Ms','p99Ms','observedRequestsPerSecond']:
            old = baseline['byOperation']['page'][field]; new = batch['byOperation']['page'][field]
            pair[field] = {'nplusone':old,'batch':new,'changePercent':(new/old-1)*100}
        pairs.append(pair)
    lockdir = args.runtime/'row-lock-attempt001'
    lock = json.loads((lockdir/'result.json').read_text(encoding='utf-8')) if (lockdir/'result.json').exists() else None
    if lock is None:
        raise ValueError('Required row-lock phase has not completed')
    if lock:
        if lock['status'] != 'BOUNDED_MIXED_WORKLOAD_ACCEPT':
            raise ValueError('Required row-lock phase failed')
        lock['businessVerification'] = verify_business(lockdir, lock, shipments)
        lock['observations'] = observations(lockdir)
        lock['injection'] = json.loads((lockdir/'injected-lock.json').read_text(encoding='utf-8'))
        acquired, released = lock['injection']['acquiredUnix'],lock['injection']['releasedUnix']
        rows = [json.loads(line) for line in (lockdir/'requests.jsonl').read_text(encoding='utf-8').splitlines()]
        windows = {'before':[],'during':[],'after':[]}
        for row in rows:
            window = 'before' if row['startedUnix']<acquired else ('during' if row['startedUnix']<released else 'after')
            windows[window].append(row)
        from mixed_workload import percentile
        lock['requestStartWindows'] = {name:{'requests':len(group),'errors':sum(r['error'] is not None for r in group),
           'p95Ms':percentile([r['milliseconds'] for r in group],.95),
           'maxMs':max((r['milliseconds'] for r in group),default=None)} for name,group in windows.items()}
        samples = [json.loads(line) for line in (lockdir/'observations.jsonl').read_text(encoding='utf-8').splitlines()]
        cleared = next((r for r in samples if r['unix'] >= released and r.get('dataLockWaits') == 0),None)
        lock['firstObservedZeroDbWaitAfterReleaseSeconds'] = cleared['unix']-released if cleared else None
        overlaps = [r for r in rows if r['startedUnix'] < released
                    and r['startedUnix'] + r['milliseconds']/1000 > acquired]
        lock['overlappingRequestMaxMs'] = max((r['milliseconds'] for r in overlaps),default=None)
    supplementary = []
    for name in selection.get('supplementary',[]):
        value = json.loads((args.runtime/name/'result.json').read_text(encoding='utf-8'))
        value['businessVerification'] = verify_business(args.runtime/name, value, shipments)
        value['directory'] = name; supplementary.append(value)
    summary = {'status':'BOUNDED_PAIRED_MIXED_ACCEPT','phases':phases,'pairs':pairs,'pageContentsEqual':True,
               'comparedPageCount':sum(len(arms)==2 for arms in content_arms.values()),
               'visitedPageCount':len(content),'rowLock':lock,
               'selection':selection,'supplementary':supplementary,'fixtureSessionEvents':renewal_events,
               'warehouseEvidence': {'path': str(warehouse_path) if warehouse_path else None,
                                     'snapshotChecked': shipments is not None},
               'medianPairChangePercent':{key:statistics.median(p[key]['changePercent'] for p in pairs)
                  for key in ['p50Ms','p95Ms','p99Ms','observedRequestsPerSecond']},
               'scriptSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'scope':'Descriptive same-host closed-loop comparison; no production capacity claim'}
    args.output.write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in summary.items() if key not in ['phases','rowLock','supplementary','fixtureSessionEvents']}))


if __name__ == '__main__':
    main()
