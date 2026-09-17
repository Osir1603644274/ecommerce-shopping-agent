"""Descriptive HTTP pagination load on the dedicated backend; no Agent calls.
Seeds 10,000 historical read-only order fixtures in the isolated application database.
These imports intentionally have no payment, inventory or fulfillment side effects.
"""
import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import secrets
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from verify_real_backend import Verification


def percentile(values, fraction):
    return sorted(values)[min(len(values)-1, math.ceil(len(values)*fraction)-1)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration', type=int, default=10)
    args = parser.parse_args()
    if not 5 <= args.duration <= 60: raise SystemExit('Duration must be 5..60 seconds per concurrency level')
    run = Verification(args.runtime, args.output)
    summary = {'status': 'IN_PROGRESS', 'orders': 10000, 'scope': 'One backend CPU, dedicated MySQL/Kafka, same physical host as concurrent Context work; descriptive HTTP load only',
               'agentCalls': 0, 'phases': [], 'scriptSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        account = run.request('POST', '/api/auth/register', {'username': 'load-' + uuid.uuid4().hex[:12], 'password': secrets.token_urlsafe(24)}, 201)
        run.token, user = account['accessToken'], account['user']['id']
        digest = hashlib.sha256()
        for start in range(0, 10000, 500):
            orders, items = [], []
            for i in range(start, start+500):
                identity = str(uuid.uuid5(uuid.UUID(user), str(i)))
                stamp = datetime(2026, 1, 1) + timedelta(seconds=i//3)
                row = (identity, 'READ-' + identity.replace('-', '')[:24], user, 'read-fixture-'+str(i), 'fixture', stamp, stamp)
                orders.append(row); items.append((identity,))
                digest.update(json.dumps(row, default=str).encode())
            with run.db.cursor() as cursor:
                cursor.executemany("""INSERT INTO customer_order(id,order_no,user_id,idempotency_key,request_hash,status,
                                   total_minor,discount_minor,payable_minor,currency,expires_at,created_at)
                                   VALUES(%s,%s,%s,%s,%s,'PAID',100,0,100,'CNY',%s,%s)""", orders)
                cursor.executemany("""INSERT INTO order_item(order_id,item_type,item_id,title_snapshot,unit_price_minor,quantity,subtotal_minor,evidence_json)
                                    VALUES(%s,'PRODUCT',1001,'historical read fixture',100,1,100,'{}')""", items)
        summary['fixtureSha256'] = digest.hexdigest()
        for _ in range(5): run.request('GET', '/api/orders/page?size=20')
        for concurrency in (1,4,8):
            started = time.monotonic(); deadline = started + args.duration
            def worker(number):
                connection = http.client.HTTPConnection('127.0.0.1', 38080, timeout=10)
                cursor, rows = None, []
                try:
                    while time.monotonic() < deadline:
                        begin = time.perf_counter(); status = 0; error = None
                        try:
                            path = '/api/orders/page?size=20' + ('&cursor=' + quote(cursor) if cursor else '')
                            connection.request('GET', path, headers={'Authorization': 'Bearer ' + run.token})
                            response = connection.getresponse(); status = response.status
                            data = json.loads(response.read()).get('data')
                            assert status == 200 and len(data['orders']) == 20
                            cursor = data['nextCursor'] if data['hasMore'] else None
                        except Exception as failure:
                            error = type(failure).__name__; connection.close()
                        rows.append({'worker': number, 'milliseconds': (time.perf_counter()-begin)*1000, 'status': status, 'error': error})
                finally: connection.close()
                return rows
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                samples = [row for group in pool.map(worker, range(concurrency)) for row in group]
            elapsed = time.monotonic()-started
            raw = run.output/('concurrency-'+str(concurrency)+'.jsonl')
            raw.write_text(''.join(json.dumps(row)+'\n' for row in samples), encoding='utf-8')
            durations = [row['milliseconds'] for row in samples]
            failed = sum(row['status'] != 200 or row['error'] is not None for row in samples)
            phase = {'concurrency': concurrency, 'requests': len(samples), 'failures': failed,
                     'elapsedSeconds': elapsed, 'observedRequestsPerSecond': len(samples)/elapsed,
                     'p50Ms': percentile(durations,.50), 'p95Ms': percentile(durations,.95), 'p99Ms': percentile(durations,.99)}
            summary['phases'].append(phase); print(json.dumps(phase), flush=True)
        summary['status'] = 'BOUNDED_HTTP_LOAD_ACCEPT' if all(p['failures']==0 for p in summary['phases']) else 'FAILED'
    except Exception as error:
        summary['status']='FAILED'; summary['errorType']=type(error).__name__; raise
    finally:
        (run.output/'result.json').write_text(json.dumps(summary,indent=2),encoding='utf-8'); run.db.close()


if __name__ == '__main__': main()
