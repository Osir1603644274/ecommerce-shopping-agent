"""Isolated backend acceptance; no Agent, model API, or shared application database.

Requires this directory's Compose stack, packaged backend JAR and durable warehouse fixture.
Creates fresh users/orders. Restarts/kills ONLY the dedicated Compose backend service.
Never overwrites an existing result directory. Credentials remain in runtime/compose.env.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from contextlib import closing
from pathlib import Path
import pymysql


class Verification:
    def __init__(self, runtime, output):
        self.runtime, self.output = runtime.resolve(), output.resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.secret = dict(line.split('=', 1) for line in (self.runtime / 'compose.env').read_text().splitlines() if '=' in line)
        self.compose = ['docker', 'compose', '--env-file', str(self.runtime / 'compose.env'),
                        '-f', str(Path(__file__).with_name('compose.yml'))]
        self.db = pymysql.connect(host='127.0.0.1', port=33316, user='root',
                                  password=self.secret['BENCH_DB_PASSWORD'], database='backend_strengthening',
                                  autocommit=True, cursorclass=pymysql.cursors.DictCursor)
        self.results = []
        self.orders = []
        self.token = None

    def sql(self, query, args=()):
        with self.db.cursor() as cursor:
            cursor.execute(query, args)
            return cursor.fetchall()

    def request(self, method, path, body=None, expected=200, token=None, headers=None):
        request_headers = {'Content-Type': 'application/json', **(headers or {})}
        if token or self.token: request_headers['Authorization'] = 'Bearer ' + (token or self.token)
        request = urllib.request.Request('http://127.0.0.1:38080' + path,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers=request_headers, method=method)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status, content = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, content = error.code, error.read()
        with (self.output / 'http.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'method': method, 'path': path, 'status': status,
                                  'milliseconds': (time.perf_counter() - started) * 1000}) + '\n')
        assert status == expected, (method, path, status, content.decode()[:600])
        return json.loads(content).get('data') if content else None

    def wait(self, description, predicate, seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value: return value
            time.sleep(.2)
        raise AssertionError('Timeout: ' + description)

    def backend(self, enabled=True, kill=False):
        env = dict(os.environ, BENCH_WORKER_ENABLED=str(enabled).lower())
        if kill:
            subprocess.run(self.compose + ['kill', '-s', 'SIGKILL', 'backend'], env=env, check=True, capture_output=True)
        command = subprocess.run(self.compose + ['--profile', 'app', 'up', '-d', '--no-deps', '--force-recreate', 'backend'],
                                 env=env, check=True, capture_output=True)
        (self.output / ('restart-' + str(time.time_ns()) + '.log')).write_bytes(command.stdout + command.stderr)
        def ready():
            try:
                with urllib.request.urlopen('http://127.0.0.1:38080/api/health', timeout=2) as response:
                    return response.status == 200
            except (OSError, urllib.error.URLError): return False
        self.wait('backend ready', ready, 90)

    def fault(self, mode):
        temporary = self.runtime / 'fault.next.json'
        temporary.write_text(json.dumps({'mode': mode}), encoding='utf-8')
        temporary.replace(self.runtime / 'fault.json')

    def task(self, identity):
        return self.sql('SELECT * FROM fulfillment_task WHERE order_id=%s', (identity,))[0]

    def state(self, identity, expected):
        return self.wait(identity + ' -> ' + expected, lambda: self.task(identity)['status'] == expected)

    def shipment_count(self, identity):
        with closing(sqlite3.connect(self.runtime / 'warehouse.sqlite')) as connection:
            return connection.execute('SELECT COUNT(*) FROM shipment WHERE order_id=?', (identity,)).fetchone()[0]

    def order(self, pay=True, quantity=1):
        identity = self.request('POST', '/api/orders', {'itemType': 'PRODUCT', 'itemId': self.product,
                               'quantity': quantity}, 201, headers={'Idempotency-Key': str(uuid.uuid4())})['id']
        self.orders.append(identity)
        if pay:
            payment = self.request('POST', '/api/payments/orders/' + identity, {}, 201)
            self.request('POST', '/api/payments/' + payment['id'] + '/simulate-success', {})
        return identity

    def accept(self, name, **evidence):
        item = {'name': name, 'status': 'PASS', **evidence}
        self.results.append(item)
        print(json.dumps(item), flush=True)
        self.save('IN_PROGRESS')

    def save(self, status, error=None):
        result = {'status': status, 'cases': self.results, 'orders': self.orders, 'error': error,
                  'agentCalls': 0, 'realPaymentOrLogistics': False,
                  'sharedHostTiming': 'Descriptive only; concurrent Context work is not controlled',
                  'scriptSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        (self.output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')

    def run(self):
        self.backend(enabled=False)
        self.accept('flyway_v15_mysql', migration=self.sql("SELECT version,success FROM flyway_schema_history WHERE version='15'")[0])
        username, password = 'backend-' + uuid.uuid4().hex[:12], secrets.token_urlsafe(24)
        registered = self.request('POST', '/api/auth/register', {'username': username, 'password': password}, 201)
        self.token, user = registered['accessToken'], registered['user']['id']
        self.sql("INSERT INTO user_role(user_id,role_name) VALUES(%s,'ADMIN')", (user,))
        admin = self.request('POST', '/api/auth/login', {'username': username, 'password': password})['accessToken']
        self.product = int(time.time() * 1000)
        self.sql("""INSERT INTO product(id,source,source_item_id,title,brand,seller,category_l1,category_l2,category_l3,
                 snapshot_price_minor,currency,price_status,attribute_text,data_nature,dataset_revision,source_license,provenance_url)
                 VALUES(%s,'fixture',%s,'Backend Acceptance Phone','Fixture','Fixture Seller','digital','phone','smartphone',
                        199900,'CNY','verified','12GB+256GB','deterministic_fixture','backend-strengthening-v1','test-only','fixture://backend')""",
                 (self.product, str(self.product)))
        self.sql("INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity) VALUES('PRODUCT',%s,500,500)", (self.product,))

        refund = self.order()
        self.state(refund, 'READY')
        record = self.request('POST', '/api/payments/orders/' + refund + '/refunds', {'reason': 'isolated validation'}, 201)
        self.request('POST', '/api/payments/refunds/' + record['id'] + '/simulate-success', {})
        self.request('POST', '/api/payments/refunds/' + record['id'] + '/simulate-success', {})
        self.state(refund, 'CANCELLED')
        self.accept('refund_wins_before_dispatch', orderId=refund, shipments=self.shipment_count(refund))
        assert self.shipment_count(refund) == 0
        self.backend(enabled=True)

        identity = self.order(quantity=2)
        self.state(identity, 'SHIPPED')
        self.request('POST', '/api/orders/' + identity + '/complete', {})
        self.state(identity, 'RECEIVED')
        assert self.shipment_count(identity) == 1
        paid = self.sql("SELECT * FROM outbox_event WHERE aggregate_id=%s AND event_type='order.paid.v1'", (identity,))[0]
        envelope = {'id': paid['id'], 'aggregateType': paid['aggregate_type'], 'aggregateId': identity,
                    'eventType': paid['event_type'], 'payloadJson': paid['payload_json'], 'occurredAt': paid['created_at'].isoformat()}
        value = json.dumps(envelope)
        self.produce(value + '\n' + value)
        self.wait('dedicated Kafka inbox', lambda: self.sql("SELECT * FROM inbox_event WHERE consumer_name='fulfillment-v1' AND event_id=%s", (paid['id'],)))
        assert self.sql("SELECT COUNT(*) AS n FROM fulfillment_attempt WHERE order_id=%s AND outcome='READY'", (identity,))[0]['n'] == 1
        stock = self.sql("SELECT available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=%s", (self.product,))[0]
        assert stock == {'available_quantity': 498, 'reserved_quantity': 0, 'sold_quantity': 2}, stock
        self.accept('paid_kafka_ship_receive_duplicate', orderId=identity, shipments=1, stock=stock)

        failed_key = 'rollback-' + uuid.uuid4().hex
        before = self.sql('SELECT COUNT(*) AS n FROM fulfillment_task')[0]['n']
        self.sql("""CREATE TRIGGER backend_validation_reject_outbox BEFORE INSERT ON outbox_event FOR EACH ROW
                    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='injected isolated validation failure'""")
        try:
            self.request('POST', '/api/orders', {'itemType':'PRODUCT','itemId':self.product,'quantity':1}, 500,
                         headers={'Idempotency-Key':failed_key})
        finally:
            self.sql('DROP TRIGGER backend_validation_reject_outbox')
        assert self.sql('SELECT COUNT(*) AS n FROM customer_order WHERE user_id=%s AND idempotency_key=%s', (user,failed_key))[0]['n'] == 0
        assert self.sql('SELECT COUNT(*) AS n FROM fulfillment_task')[0]['n'] == before
        assert self.sql("SELECT available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=%s", (self.product,))[0] == stock
        self.accept('mysql_order_stock_task_outbox_atomic_rollback', ordersLeft=0, tasksLeft=0, stockUnchanged=True)

        self.fault('commit_then_drop_once')
        lost = self.order()
        self.state(lost, 'SHIPPED')
        assert self.shipment_count(lost) == 1
        assert self.task(lost)['attempts'] >= 2
        self.accept('committed_response_lost_reconciles', orderId=lost, shipments=1, attempts=self.task(lost)['attempts'])

        self.fault('unavailable')
        unknown = self.order()
        self.state(unknown, 'NEEDS_REVIEW')
        key = self.task(unknown)['request_key']
        self.request('POST', '/api/payments/orders/' + unknown + '/refunds', {'reason': 'must reject unknown'}, 409)
        self.request('POST', '/api/admin/fulfillment/' + unknown + '/retry', {}, 403)
        self.fault('normal')
        self.request('POST', '/api/admin/fulfillment/' + unknown + '/retry', {}, token=admin)
        self.state(unknown, 'SHIPPED')
        assert self.task(unknown)['request_key'] == key and self.shipment_count(unknown) == 1
        self.accept('unknown_refund_guard_admin_retry', orderId=unknown, sameRequestKey=True, shipments=1)

        self.fault('commit_then_delay')
        crashed = self.order()
        self.wait('warehouse durable result while local claim held', lambda: self.shipment_count(crashed) == 1 and self.task(crashed)['status'] == 'DISPATCHING')
        fence = self.task(crashed)['fence']
        self.backend(enabled=True, kill=True)
        self.fault('normal')
        self.state(crashed, 'SHIPPED')
        assert self.task(crashed)['fence'] > fence and self.shipment_count(crashed) == 1
        self.accept('process_killed_after_external_commit', orderId=crashed, previousFence=fence,
                    recoveredFence=self.task(crashed)['fence'], shipments=1)

        poison_marker = uuid.uuid4().hex
        malformed = '{"broken-fixture":"' + poison_marker + '"}'
        self.produce(malformed)
        dead = self.wait('durable Kafka dead letter', lambda: self.sql("SELECT * FROM dead_letter_event WHERE source='FULFILLMENT_KAFKA' AND payload_json LIKE %s", ('%'+poison_marker+'%',)))
        payload = json.loads(dead[-1]['payload_json'])
        def offset_committed():
            described = subprocess.run(self.compose + ['exec','-T','kafka','/opt/kafka/bin/kafka-consumer-groups.sh',
                                       '--bootstrap-server','localhost:9092','--describe','--group','backend-strengthening-fulfillment'],
                                       capture_output=True,timeout=20)
            (self.output/'kafka-group-after-dead-letter.txt').write_bytes(described.stdout + described.stderr)
            for line in described.stdout.decode().splitlines():
                parts = line.split()
                if len(parts)>3 and parts[0]=='backend-strengthening-fulfillment' and parts[2]==str(payload['partition']):
                    return parts[3].isdigit() and int(parts[3]) > payload['offset']
            return False
        self.wait('offset committed after durable dead letter',offset_committed)
        self.accept('kafka_poison_message_durable_dead_letter', count=len(dead), committedOffsetBeyondDeadLetter=True)
        replay_id = str(uuid.uuid4())
        self.sql("""INSERT INTO dead_letter_event(source,event_id,event_type,payload_json,attempts,last_error)
                 VALUES('FULFILLMENT_KAFKA',%s,'fulfillment.delivery.failed',%s,4,'fixture persisted delivery')""",
                 (replay_id, json.dumps({'topic': 'backend-strengthening.domain-events.v1', 'partition': 0, 'offset': 0, 'value': value})))
        self.request('POST', '/api/admin/fulfillment/events/' + replay_id + '/replay', {}, token=admin)
        assert self.sql("SELECT COUNT(*) AS n FROM dead_letter_event WHERE source='FULFILLMENT_KAFKA' AND event_id=%s", (replay_id,))[0]['n'] == 1
        self.accept('mysql_manual_replay_keeps_original', eventId=replay_id, seededDeadLetter=True)

        page = self.request('GET', '/api/orders/page?size=2')
        ids = [x['id'] for x in page['orders']]
        while page['hasMore']:
            page = self.request('GET', '/api/orders/page?size=2&cursor=' + urllib.parse.quote(page['nextCursor']))
            ids.extend(x['id'] for x in page['orders'])
        assert len(ids) == len(set(ids)) == len(self.orders)
        self.accept('mysql_http_cursor_traversal', orders=len(ids))
        for table in ('fulfillment_task', 'fulfillment_attempt', 'inbox_event', 'dead_letter_event'):
            (self.output / (table + '.json')).write_text(json.dumps(self.sql('SELECT * FROM ' + table), default=str, indent=2), encoding='utf-8')
        self.save('BOUNDED_REAL_BACKEND_ACCEPT')

    def produce(self, value):
        result = subprocess.run(self.compose + ['exec', '-T', 'kafka', '/opt/kafka/bin/kafka-console-producer.sh',
                                               '--bootstrap-server', 'localhost:9092', '--topic', 'backend-strengthening.domain-events.v1'],
                                input=(value + '\n').encode(), capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode()[:300]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    runner = Verification(args.runtime, args.output)
    try:
        runner.run()
    except Exception as error:
        runner.save('FAILED', str(error))
        raise
    finally:
        runner.fault('normal')
        runner.db.close()
