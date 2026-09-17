"""Paired HTTP read/write workload and bounded row-lock injection on V2 only.

Reads traverse the same immutable 10,000-order fixture for every arm. Writers use
new accounts/products per phase and exercise actual order/payment/cancel APIs.
Raw HTTP timings, Prometheus scrapes, MySQL counters and business outcomes persist.
Closed-loop throughput is descriptive, not a fixed-arrival capacity claim.
"""
import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pymysql
from verify_real_backend import Verification


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)] if values else None


def describe(rows, seconds):
    values = [r['milliseconds'] for r in rows]
    return {'requests': len(rows), 'errors': sum(r['error'] is not None for r in rows),
            'observedRequestsPerSecond': len(rows) / seconds,
            'p50Ms': percentile(values, .50), 'p95Ms': percentile(values, .95),
            'p99Ms': percentile(values, .99)}


class Workload(Verification):
    def __init__(self, runtime, output, arm):
        # Validate the dedicated project before the superclass connects to MySQL.
        config = json.loads((runtime / 'compose.validation.json').read_text(encoding='utf-8'))
        if config.get('name') != 'backend-strengthening-v2':
            raise ValueError('Only the isolated V2 project is allowed')
        super().__init__(runtime, output)
        self.compose = ['docker', 'compose', '--env-file', str(self.runtime / 'compose.env'),
                        '-f', str(self.runtime / 'compose.validation.json')]
        self.arm = arm
        self.jar = self.runtime / json.loads((runtime / 'jars.json').read_text())[arm]['file']
        self.records, self.monitor_records, self.orders = [], [], []
        self.record_lock = threading.Lock()
        self.product = int(time.time() * 1000)

    def connection(self):
        return pymysql.connect(host='127.0.0.1', port=33316, user='root',
                               password=self.secret['BENCH_DB_PASSWORD'], database='backend_strengthening',
                               autocommit=True, cursorclass=pymysql.cursors.DictCursor)

    def start_backend(self):
        env = dict(os.environ, MIXED_BACKEND_JAR=str(self.jar), BENCH_WORKER_ENABLED='true', BENCH_KAFKA_ENABLED='true')
        result = subprocess.run(self.compose + ['--profile', 'app', 'up', '-d', '--no-deps',
                                               '--force-recreate', 'backend'], env=env,
                                capture_output=True, check=True)
        (self.output / 'backend-start.log').write_bytes(result.stdout + result.stderr)
        def ready():
            try:
                conn = http.client.HTTPConnection('127.0.0.1', 38080, timeout=2)
                conn.request('GET', '/api/health')
                response = conn.getresponse(); response.read(); conn.close()
                return response.status == 200
            except OSError:
                return False
        self.wait('backend ready', ready, 90)

    def register(self, prefix):
        return self.request('POST', '/api/auth/register', {'username': prefix + uuid.uuid4().hex[:12],
                            'password': secrets.token_urlsafe(24)}, 201)

    def fixture(self):
        saved = self.runtime / 'read-fixture-private.json'
        if saved.exists():
            result = json.loads(saved.read_text())
            self.read_user = result['user']
            count = self.sql('SELECT COUNT(*) AS n FROM customer_order WHERE user_id=%s', (self.read_user,))[0]['n']
            assert count == 10000, count
            if not result.get('refreshToken'):
                raise ValueError('Legacy fixture lacks a refresh token; use the explicit fixture-only renewal helper before the suite')
            issued = self.request('POST', '/api/auth/refresh', {'refreshToken': result['refreshToken']})
            assert issued['user']['id'] == self.read_user
            self.read_token = issued['accessToken']
            result['token'], result['refreshToken'] = self.read_token, issued['refreshToken']
            temporary = saved.with_suffix('.next.json')
            temporary.write_text(json.dumps(result), encoding='utf-8'); temporary.replace(saved)
            return result['sha256']
        account = self.register('mixed-read-')
        self.read_token, self.read_user = account['accessToken'], account['user']['id']
        digest = hashlib.sha256()
        for start in range(0, 10000, 500):
            orders, items = [], []
            for i in range(start, start + 500):
                identity = str(uuid.uuid5(uuid.UUID(self.read_user), str(i)))
                stamp = datetime(2026, 1, 1) + timedelta(seconds=i // 3)
                row = (identity, 'MIXR-' + identity.replace('-', '')[:24], self.read_user,
                       'mixed-read-' + str(i), 'fixture', stamp, stamp)
                orders.append(row); items.append((identity,))
                digest.update(json.dumps(row, default=str).encode())
            with self.db.cursor() as cursor:
                cursor.executemany('''INSERT INTO customer_order(id,order_no,user_id,idempotency_key,request_hash,status,
                    total_minor,discount_minor,payable_minor,currency,expires_at,created_at)
                    VALUES(%s,%s,%s,%s,%s,'PAID',100,0,100,'CNY',%s,%s)''', orders)
                cursor.executemany('''INSERT INTO order_item(order_id,item_type,item_id,title_snapshot,unit_price_minor,
                    quantity,subtotal_minor,evidence_json)
                    VALUES(%s,'PRODUCT',1001,'immutable read fixture',100,1,100,'{}')''', items)
        result = {'token': self.read_token, 'refreshToken': account['refreshToken'],
                  'user': self.read_user, 'sha256': digest.hexdigest()}
        saved.write_text(json.dumps(result), encoding='utf-8')  # Private credentials, never copied to report.
        return result['sha256']

    def writer_fixture(self):
        account = self.register('mixed-write-')
        self.token, self.user = account['accessToken'], account['user']['id']
        self.sql('''INSERT INTO product(id,source,source_item_id,title,brand,seller,category_l1,category_l2,category_l3,
          snapshot_price_minor,currency,price_status,attribute_text,data_nature,dataset_revision,source_license,provenance_url)
          VALUES(%s,'fixture',%s,'Mixed Workload Phone','Fixture','Fixture','digital','phone','smartphone',
          100,'CNY','verified','test','deterministic_fixture','backend-strengthening-v2','test-only','fixture://mixed')''',
          (self.product, str(self.product)))
        self.sql("INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity) VALUES('PRODUCT',%s,100000,100000)",
                 (self.product,))

    def call(self, conn, method, path, op, body=None, expected=200, token=None, extra=None, page=None):
        began = time.perf_counter(); stamp = time.time(); status = 0; error = None; data = None
        try:
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token or self.token), **(extra or {})}
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse(); status = response.status; raw = response.read()
            value = json.loads(raw); data = value.get('data')
            if status != expected:
                raise ValueError('HTTP ' + str(status) + ': ' + str(value)[:300])
            if op == 'page' and (not data or len(data['orders']) != 20):
                raise ValueError('Invalid 20-order page')
        except Exception as failure:
            error = type(failure).__name__ + ': ' + str(failure)[:400]
            conn.close()
        row = {'startedUnix': stamp, 'operation': op, 'status': status,
               'milliseconds': (time.perf_counter() - began) * 1000, 'error': error}
        if page is not None:
            row['page'] = page
            if error is None:
                row['contentSha256'] = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.record_lock:
            self.records.append(row)
            self.http_log.write(json.dumps(row) + '\n'); self.http_log.flush()
        if error is not None:
            return None
        return data

    def reader(self, deadline):
        conn = http.client.HTTPConnection('127.0.0.1', 38080, timeout=15)
        cursor, page = None, 0
        try:
            while time.monotonic() < deadline:
                path = '/api/orders/page?size=20' + ('&cursor=' + quote(cursor) if cursor else '')
                data = self.call(conn, 'GET', path, 'page', token=self.read_token, page=page)
                if data:
                    cursor = data['nextCursor'] if data['hasMore'] else None
                    page = page + 1 if cursor else 0
        finally:
            conn.close()

    def writer(self, deadline, number):
        conn = http.client.HTTPConnection('127.0.0.1', 38080, timeout=15)
        sequence = number
        try:
            while time.monotonic() < deadline:
                order = self.call(conn, 'POST', '/api/orders', 'create', {'itemType': 'PRODUCT',
                                  'itemId': self.product, 'quantity': 1}, 201,
                                  extra={'Idempotency-Key': 'mixed-v2-' + uuid.uuid4().hex})
                if order:
                    identity = order['id']
                    with self.record_lock:
                        self.orders.append(identity)
                    if sequence % 2 == 0:
                        payment = self.call(conn, 'POST', '/api/payments/orders/' + identity, 'payment', {}, 201)
                        if payment:
                            self.call(conn, 'POST', '/api/payments/' + payment['id'] + '/simulate-success', 'callback', {})
                    else:
                        self.call(conn, 'POST', '/api/orders/' + identity + '/cancel', 'cancel', {})
                sequence += 1
                time.sleep(.02)
        finally:
            conn.close()

    def monitor(self, stop):
        db = self.connection()
        conn = http.client.HTTPConnection('127.0.0.1', 38080, timeout=8)
        try:
            with (self.output / 'observations.jsonl').open('w', encoding='utf-8') as log:
                while not stop.is_set():
                    record = {'unix': time.time()}
                    try:
                        conn.request('GET', '/actuator/prometheus')
                        response = conn.getresponse()
                        record['prometheusStatus'] = response.status
                        record['prometheusRaw'] = response.read().decode()
                        with db.cursor() as cursor:
                            cursor.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Innodb_row_lock_current_waits','Innodb_row_lock_waits','Innodb_row_lock_time','Threads_running','Threads_connected','Innodb_deadlocks')")
                            record['mysqlStatus'] = {r['Variable_name']: int(r['Value']) for r in cursor.fetchall()}
                            cursor.execute('SELECT COUNT(*) AS n FROM performance_schema.data_lock_waits')
                            record['dataLockWaits'] = cursor.fetchone()['n']
                            cursor.execute('''SELECT f.status,COUNT(*) AS n FROM fulfillment_task f
                                JOIN customer_order o ON o.id=f.order_id WHERE o.user_id=%s GROUP BY f.status''', (self.user,))
                            record['fulfillment'] = cursor.fetchall()
                            cursor.execute('''SELECT e.status,COUNT(*) AS n FROM outbox_event e
                                JOIN customer_order o ON o.id=e.aggregate_id WHERE o.user_id=%s GROUP BY e.status''', (self.user,))
                            record['outbox'] = cursor.fetchall()
                    except Exception as failure:
                        record['error'] = type(failure).__name__ + ': ' + str(failure)[:200]
                        conn.close()
                    self.monitor_records.append(record)
                    log.write(json.dumps(record) + '\n'); log.flush()
                    stop.wait(.5)
        finally:
            db.close(); conn.close()

    def hold_inventory(self, starts, seconds):
        time.sleep(starts)
        db = self.connection()
        record = {'injection': 'exclusive lock on this phase fixture inventory only', 'requestedSeconds': seconds}
        try:
            db.begin()
            with db.cursor() as cursor:
                cursor.execute("SELECT id FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=%s FOR UPDATE", (self.product,))
            record['acquiredUnix'] = time.time()
            time.sleep(seconds)
        finally:
            db.rollback(); db.close()
            record['releasedUnix'] = time.time()
            (self.output / 'injected-lock.json').write_text(json.dumps(record, indent=2), encoding='utf-8')

    def reconcile(self):
        def terminal():
            return not self.sql('''SELECT f.order_id FROM fulfillment_task f JOIN customer_order o ON o.id=f.order_id
                WHERE o.user_id=%s AND f.status NOT IN ('SHIPPED','CANCELLED') LIMIT 1''', (self.user,))
        self.wait('all paid/cancelled tasks converge', terminal, 90)
        rows = self.sql('''SELECT o.id,o.status,r.status AS reservation_status,r.quantity,f.status AS fulfillment_status,
            f.attempts,f.fence FROM customer_order o JOIN inventory_reservation r ON r.order_id=o.id
            JOIN fulfillment_task f ON f.order_id=o.id WHERE o.user_id=%s ORDER BY o.id''', (self.user,))
        stock = self.sql("SELECT total_quantity,available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=%s", (self.product,))[0]
        with closing(sqlite3.connect(self.runtime / 'warehouse.sqlite')) as db:
            shipments = dict(db.execute('SELECT order_id,COUNT(*) FROM shipment GROUP BY order_id').fetchall())
        paid = cancelled = 0
        for row in rows:
            row['shipments'] = shipments.get(row['id'], 0)
            if row['status'] == 'PAID':
                paid += row['quantity']
                assert (row['reservation_status'], row['fulfillment_status'], row['shipments']) == ('CONFIRMED','SHIPPED',1), row
            elif row['status'] == 'CANCELLED':
                cancelled += row['quantity']
                assert (row['reservation_status'], row['fulfillment_status'], row['shipments']) == ('RELEASED','CANCELLED',0), row
            else:
                raise AssertionError(row)
        assert len(rows) == len(self.orders) and len(rows) > 0
        assert stock == {'total_quantity':100000,'available_quantity':100000-paid,'reserved_quantity':0,'sold_quantity':paid}, stock
        result = {'orders':len(rows), 'paid':paid, 'cancelled':cancelled, 'stock':stock, 'rows':rows,
                  'exactlyOneShipmentPerPaidOrder':True, 'noShipmentsForCancelledOrders':True}
        (self.output / 'business-invariants.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        return {k:v for k,v in result.items() if k != 'rows'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arm', choices=['batch','nplusone-control'], required=True)
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--readers', type=int, default=4)
    parser.add_argument('--writers', type=int, default=2)
    parser.add_argument('--lock-seconds', type=int, default=0)
    args = parser.parse_args()
    if not (10 <= args.duration <= 120 and 1 <= args.readers <= 16 and 1 <= args.writers <= 8
            and 0 <= args.lock_seconds <= 5):
        raise SystemExit('Bounded workload parameters required')
    run = Workload(args.runtime, args.output, args.arm)
    summary = {'status':'IN_PROGRESS','arm':args.arm,'seconds':args.duration,'readers':args.readers,'writers':args.writers,
               'injectedLockSeconds':args.lock_seconds,'agentCalls':0,'realPaymentOrLogistics':False,
               'jarSha256':hashlib.sha256(run.jar.read_bytes()).hexdigest(),
               'scriptSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'scope':'Same-host closed-loop mixed workload; paired query-control reconstruction; not production capacity'}
    stop = threading.Event(); monitor = None
    try:
        run.start_backend()
        summary['fixtureSha256'] = run.fixture()
        run.writer_fixture()
        for _ in range(40):
            run.request('GET','/api/orders/page?size=20',token=run.read_token)
        with (run.output/'requests.jsonl').open('w',encoding='utf-8') as run.http_log:
            monitor = threading.Thread(target=run.monitor,args=(stop,)); monitor.start()
            started = time.monotonic(); deadline = started + args.duration
            summary['startedUnix'] = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.readers+args.writers+1) as pool:
                jobs = [pool.submit(run.reader,deadline) for _ in range(args.readers)]
                jobs += [pool.submit(run.writer,deadline,i) for i in range(args.writers)]
                if args.lock_seconds:
                    jobs.append(pool.submit(run.hold_inventory,args.duration/3,args.lock_seconds))
                for job in jobs: job.result()
            elapsed = time.monotonic()-started
        summary['elapsedSeconds'] = elapsed
        summary['http'] = describe(run.records,elapsed)
        summary['byOperation'] = {op:describe([r for r in run.records if r['operation']==op],elapsed)
                                  for op in sorted({r['operation'] for r in run.records})}
        recovery_started = time.monotonic()
        summary['business'] = run.reconcile()
        summary['drainAfterLoadSeconds'] = time.monotonic()-recovery_started
        summary['status'] = 'BOUNDED_MIXED_WORKLOAD_ACCEPT' if summary['http']['errors']==0 else 'FAILED'
    except Exception as error:
        summary['status']='FAILED'; summary['error']=type(error).__name__+': '+str(error)[:1000]
        raise
    finally:
        stop.set()
        if monitor: monitor.join(10)
        summary['monitorSamples'] = len(run.monitor_records)
        summary['monitorErrors'] = sum('error' in row or row.get('prometheusStatus') != 200 for row in run.monitor_records)
        if not summary['monitorSamples'] or summary['monitorErrors']:
            summary['status']='FAILED'
        (run.output/'result.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        run.db.close(); print(json.dumps(summary),flush=True)
    if summary['status'] != 'BOUNDED_MIXED_WORKLOAD_ACCEPT':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
