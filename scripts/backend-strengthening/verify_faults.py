"""V3 real HTTP/Kafka/MySQL two-instance experiments, always new output directories."""
import argparse
import concurrent.futures
import hashlib
import hmac
import http.client
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
import pymysql
from verify_real_backend import Verification


class FaultVerification(Verification):
    def __init__(self, runtime, output, expected_project='backend-strengthening-v3'):
        config = json.loads((runtime / 'compose.validation.json').read_text())
        assert expected_project in ('backend-strengthening-v3','backend-strengthening-v4')
        assert config['name'] == expected_project
        super().__init__(runtime, output)
        self.compose = ['docker', 'compose', '--env-file', str(self.runtime / 'compose.env'),
                        '-f', str(self.runtime / 'compose.validation.json')]
        self.lock = threading.Lock()
        self.http = []
        self.flags = {}
        self.started = time.time()
        self.jar = Path(config['services']['app1']['volumes'][0]['source'])
        self.source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.jar_hash = hashlib.sha256(self.jar.read_bytes()).hexdigest()
        second_jar = Path(config['services']['app2']['volumes'][0]['source'])
        assert hashlib.sha256(second_jar.read_bytes()).hexdigest() == self.jar_hash
        self.intents = []
        (self.output/'executed-verify_faults.py').write_bytes(Path(__file__).read_bytes())
        (self.output/'compose.validation.json').write_text(json.dumps(config,indent=2),encoding='utf-8')
        dependencies = {}
        for name in ('verify_real_backend.py','analyze_mixed_workload.py','mixed_workload.py'):
            raw=Path(__file__).with_name(name).read_bytes()
            dependencies[name]=hashlib.sha256(raw).hexdigest()
            (self.output/('dependency-'+name)).write_bytes(raw)
        (self.output/'dependency-hashes.json').write_text(json.dumps(dependencies,indent=2),encoding='utf-8')
        mysql_id=self.command(['ps','-q','mysql']).strip()
        assert mysql_id,'Dedicated MySQL container is not running'
        inspected=__import__('subprocess').run(['docker','inspect','--format',
            '{{ index .Config.Labels "com.docker.compose.project" }}',mysql_id],capture_output=True,check=True,text=True)
        assert inspected.stdout.strip()==config['name']
        inside_uuid=self.command(['exec','-T','mysql','sh','-c',
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -e "SELECT @@server_uuid"']).strip()
        host_uuid=self.sql('SELECT @@server_uuid AS uuid')[0]['uuid']
        assert inside_uuid==host_uuid,'Host port connected to another MySQL instance'
        (self.output/'database-binding.json').write_text(json.dumps({'project':config['name'],
            'containerId':mysql_id,'serverUuid':host_uuid,'port':33316},indent=2),encoding='utf-8')

    def command(self, arguments, timeout=50, env=None):
        result = subprocess.run(self.compose + arguments, capture_output=True, timeout=timeout,
                                env=dict(os.environ, **self.flags, **(env or {})))
        with self.lock:
            with (self.output / 'commands.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps({'at': time.time(), 'args': arguments, 'returncode': result.returncode,
                    'stdout': result.stdout.decode(errors='replace'), 'stderr': result.stderr.decode(errors='replace')}) + '\n')
        assert result.returncode == 0, result.stderr.decode(errors='replace')[:500]
        return result.stdout.decode(errors='replace')

    def apps(self, worker, kafka, names=('app1', 'app2')):
        for name in names:
            self.flags[name.upper() + '_WORKER'] = str(worker).lower()
            self.flags[name.upper() + '_KAFKA'] = str(kafka).lower()
        self.command(['--profile', 'app', 'up', '-d', '--no-deps', '--force-recreate', *names])
        for name in names:
            port = 38079 + int(name[-1])
            def ready():
                try:
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                    conn.request('GET', '/api/health'); response = conn.getresponse()
                    response.read(); conn.close(); return response.status == 200
                except OSError:
                    return False
            self.wait(name + ' ready', ready, 100)
            assert hashlib.sha256(self.jar.read_bytes()).hexdigest() == self.jar_hash
            actual=self.command(['exec','-T',name,'sha256sum','/app/backend.jar']).split()[0]
            assert actual == self.jar_hash, (name,actual,self.jar_hash)

    def request(self, method, path, body=None, expected=200, token=None, headers=None, port=38080):
        started = time.time(); began = time.perf_counter(); status = 0; content = b''
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=12)
        failure = None
        try:
            hdr = {'Content-Type': 'application/json', **(headers or {})}
            if token or self.token:
                hdr['Authorization'] = 'Bearer ' + (token or self.token)
            conn.request(method, path, json.dumps(body) if body is not None else None, hdr)
            response = conn.getresponse(); status = response.status; content = response.read()
        except OSError as error:
            failure = str(error)
        finally:
            conn.close()
        row = {'startedUnix': started, 'port': port, 'method': method, 'path': path,
               'status': status, 'phase':getattr(self,'phase','setup'),
               'milliseconds': (time.perf_counter() - began) * 1000, 'transportError': failure}
        with self.lock:
            self.http.append(row)
            with (self.output / 'http.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(row) + '\n')
        if expected is not None:
            assert status == expected, (method, path, status, content.decode(errors='replace')[:400], failure)
        return (status, json.loads(content).get('data') if content else None) if expected is None else (json.loads(content).get('data') if content else None)

    def connect(self):
        return pymysql.connect(host='127.0.0.1', port=33316, user='root',
            password=self.secret['BENCH_DB_PASSWORD'], database='backend_strengthening',
            autocommit=True, cursorclass=pymysql.cursors.DictCursor)

    def fixture(self):
        self.password = secrets.token_urlsafe(24)
        self.username = 'fault-v3-' + uuid.uuid4().hex[:12]
        account = self.request('POST', '/api/auth/register', {'username': self.username, 'password': self.password}, 201)
        self.token, self.user = account['accessToken'], account['user']['id']
        self.refresh = account['refreshToken']
        self.product = int(time.time() * 1000)
        self.sql('''INSERT INTO product(id,source,source_item_id,title,brand,seller,category_l1,category_l2,category_l3,
          snapshot_price_minor,currency,price_status,attribute_text,data_nature,dataset_revision,source_license,provenance_url)
          VALUES(%s,'fixture',%s,'Fault V3 Phone','Fixture','Fixture','digital','phone','smartphone',
          100,'CNY','verified','test','deterministic_fixture','backend-strengthening-v3','test-only','fixture://fault-v3')''',
          (self.product, str(self.product)))
        self.sql("INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity) VALUES('PRODUCT',%s,100000,100000)", (self.product,))

    def callback(self, payment, port=38080, expected=200):
        body = {'eventId': str(uuid.uuid4()), 'paymentNo': payment['paymentNo'],
                'providerTradeNo': 'V3-' + uuid.uuid4().hex, 'amountMinor': payment['amountMinor'],
                'status': 'SUCCESS', 'timestamp': int(time.time())}
        canonical = '|'.join(str(body[key]) for key in ('eventId','paymentNo','providerTradeNo','amountMinor','status','timestamp'))
        signature = hmac.new(self.secret['BENCH_CALLBACK_SECRET'].encode(), canonical.encode(), hashlib.sha256).hexdigest()
        return self.request('POST', '/api/payments/callbacks/LOCAL_SIMULATOR', body, expected,
                            headers={'X-Payment-Signature': signature}, port=port)

    def new_order(self, outcome='paid', port=38080):
        key = 'fault-v3-' + uuid.uuid4().hex
        intent={'key':key,'outcome':outcome,'port':port,'startedUnix':time.time()}
        with self.lock:
            self.intents.append(intent)
            with (self.output/'order-intents.jsonl').open('a',encoding='utf-8') as log:
                log.write(json.dumps(intent)+'\n')
        order = self.request('POST', '/api/orders', {'itemType':'PRODUCT','itemId':self.product,'quantity':1},
                             201, headers={'Idempotency-Key': key}, port=port)
        identity = order['id']
        with self.lock:
            self.orders.append(identity)
        if outcome == 'cancelled':
            self.request('POST', '/api/orders/' + identity + '/cancel', {}, port=port)
        elif outcome in ('paid', 'refunded'):
            payment = self.request('POST', '/api/payments/orders/' + identity, {}, 201, port=port)
            self.callback(payment, port)
            if outcome == 'refunded':
                refund = self.request('POST', '/api/payments/orders/' + identity + '/refunds', {'reason':'V3 unshipped'}, 201, port=port)
                self.request('POST', '/api/payments/refunds/' + refund['id'] + '/simulate-success', {}, port=port)
        return identity

    def offsets(self, label):
        output = self.command(['exec','-T','kafka','/opt/kafka/bin/kafka-consumer-groups.sh',
            '--bootstrap-server','localhost:9092','--describe','--group','backend-strengthening-v3-fulfillment'])
        (self.output / (label + '-offsets.txt')).write_text(output, encoding='utf-8')
        return output

    def reconcile(self, seconds=100):
        def done():
            return self.sql('''SELECT COUNT(*) AS n FROM customer_order o LEFT JOIN fulfillment_task f ON f.order_id=o.id
                WHERE o.user_id=%s AND (f.order_id IS NULL OR (o.status='PAID' AND f.status<>'SHIPPED') OR
                (o.status IN ('CANCELLED','REFUNDED','EXPIRED') AND f.status<>'CANCELLED'))''', (self.user,))[0]['n'] == 0
        start = time.time(); self.wait('business terminal states', done, seconds)
        rows = self.sql('''SELECT o.id,o.idempotency_key,o.status,p.status AS payment,r.status AS reservation,f.status AS fulfillment,
            (SELECT MAX(status) FROM refund_record x WHERE x.order_id=o.id) AS refund,
            f.attempts,f.fence,f.request_key FROM customer_order o
            LEFT JOIN payment_record p ON p.order_id=o.id
            LEFT JOIN inventory_reservation r ON r.order_id=o.id
            LEFT JOIN fulfillment_task f ON f.order_id=o.id WHERE o.user_id=%s ORDER BY o.id''', (self.user,))
        paid = 0
        assert rows and set(self.orders).issubset({row['id'] for row in rows})
        assert {row['idempotency_key'] for row in rows}.issubset({intent['key'] for intent in self.intents})
        for row in rows:
            count = self.shipment_count(row['id']); row['shipments'] = count
            if row['status'] == 'PAID':
                paid += 1
                assert row['payment'] == 'SUCCESS' and row['reservation'] == 'CONFIRMED' and count == 1 and row['fulfillment']=='SHIPPED', row
            elif row['status'] in ('CANCELLED','EXPIRED','REFUNDED'):
                assert count == 0 and row['fulfillment']=='CANCELLED', row
                assert row['reservation'] == {'CANCELLED':'RELEASED','EXPIRED':'EXPIRED','REFUNDED':'REFUNDED'}[row['status']], row
                if row['status'] != 'REFUNDED':
                    assert row['payment'] in (None, 'CREATED', 'FAILED'), row
                else:
                    assert row['payment']=='SUCCESS' and row['refund']=='SUCCESS',row
            else:
                raise AssertionError(row)
        stock = self.sql('SELECT available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_id=%s', (self.product,))[0]
        assert stock == {'available_quantity':100000-paid,'reserved_quantity':0,'sold_quantity':paid}, stock
        (self.output / 'business-final.json').write_text(json.dumps({'orders':rows, 'stock':stock,
            'loadEndToTerminalCheckSeconds':time.time()-start}, indent=2), encoding='utf-8')
        return {'orders':len(rows),'paid':paid,'stock':stock,'terminalCheckWaitSeconds':time.time()-start}

    def backlog(self):
        self.apps(False, False); self.fixture()
        paid = [self.new_order('paid', 38080 + i % 2) for i in range(36)]
        cancelled = [self.new_order('cancelled', 38080 + i % 2) for i in range(12)]
        refunded = [self.new_order('refunded', 38080 + i % 2) for i in range(12)]
        self.wait('all fixture outbox published', lambda: self.sql('''SELECT COUNT(*) AS n FROM outbox_event e
            JOIN customer_order o ON o.id=e.aggregate_id WHERE o.user_id=%s AND e.status<>'PUBLISHED' ''', (self.user,))[0]['n'] == 0)
        before = self.sql('''SELECT f.status,COUNT(*) AS n FROM fulfillment_task f JOIN customer_order o ON o.id=f.order_id
            WHERE o.user_id=%s GROUP BY f.status''', (self.user,))
        assert sum(self.shipment_count(identity) for identity in self.orders) == 0
        assert all(self.task(identity)['status'] == 'WAITING_PAYMENT' for identity in paid)
        self.accept('consumer_and_reconciler_disabled_backlog', orders=60, taskStates=before,
            published=self.sql('SELECT COUNT(*) AS n FROM outbox_event e JOIN customer_order o ON o.id=e.aggregate_id WHERE o.user_id=%s',(self.user,))[0]['n'])
        self.apps(False, True)
        self.wait('paid ready by Kafka alone', lambda: all(self.task(x)['status'] == 'READY' for x in paid))
        self.wait('cancelled aligned by Kafka alone', lambda: all(self.task(x)['status'] == 'CANCELLED' for x in cancelled+refunded))
        assert sum(self.shipment_count(x) for x in self.orders) == 0
        self.offsets('consumer-only')
        self.accept('consumer_only_recovery', ready=36, cancelled=24, shipments=0)
        sequence = []
        for identity in (paid[0], cancelled[0], refunded[0]):
            source = self.sql('SELECT * FROM outbox_event WHERE aggregate_id=%s ORDER BY created_at DESC,id DESC', (identity,))
            # Explicit semantic order even if database timestamps tie.
            rank = {'order.refunded.v1':0,'order.cancelled.v1':0,'order.paid.v1':1,'order.created.v1':2}
            for event in sorted(source, key=lambda e:rank[e['event_type']]):
                envelope = {'id':str(uuid.uuid4()),'aggregateType':'ORDER','aggregateId':identity,
                    'eventType':event['event_type'],'payloadJson':event['payload_json'],'occurredAt':event['created_at'].isoformat()}
                sequence.extend([envelope,envelope])
        (self.output/'reordered-events.json').write_text(json.dumps(sequence,indent=2),encoding='utf-8')
        value = '\n'.join(json.dumps(event) for event in sequence) + '\n'
        produced = subprocess.run(self.compose+['exec','-T','kafka','/opt/kafka/bin/kafka-console-producer.sh',
            '--bootstrap-server','localhost:9092','--topic','backend-strengthening-v3.domain-events.v1'],
            input=value.encode(),capture_output=True,timeout=40)
        assert produced.returncode == 0
        ids = set(e['id'] for e in sequence)
        self.wait('all reordered event receipts', lambda: self.sql('SELECT COUNT(*) AS n FROM inbox_event WHERE consumer_name=\'fulfillment-v1\' AND event_id IN ('+','.join(['%s']*len(ids))+')', tuple(ids))[0]['n']==len(ids))
        assert self.task(paid[0])['status']=='READY'
        assert all(self.task(x)['status']=='CANCELLED' for x in (cancelled[0],refunded[0]))
        self.accept('reverse_event_order_and_duplicate', deliveries=len(sequence),uniqueEvents=len(ids),taskStates=['READY','CANCELLED','CANCELLED'])
        self.fault('commit_then_delay')
        self.apps(True, True)
        self.wait('both instance owners claimed',lambda: self.sql('''SELECT COUNT(DISTINCT a.detail) AS n FROM fulfillment_attempt a
            JOIN customer_order o ON o.id=a.order_id WHERE o.user_id=%s AND a.outcome='CLAIMED' ''',(self.user,))[0]['n']>=2)
        self.fault('normal')
        result = self.reconcile()
        owners=self.sql('''SELECT a.detail AS owner,COUNT(*) AS n FROM fulfillment_attempt a JOIN customer_order o ON o.id=a.order_id
            WHERE o.user_id=%s AND a.outcome='CLAIMED' GROUP BY a.detail''',(self.user,))
        self.accept('two_instance_claim_and_drain',owners=owners,**result)
        self.offsets('final')

    def save(self, status, error=None):
        result = {'status':status,'cases':self.results,'orders':self.orders,'error':error,
            'startedUnix':self.started,'finishedUnix':time.time(), 'agentCalls':0,
            'realPaymentOrLogistics':False,'sameHost':True,
            'scriptSha256':self.source_hash,
            'jarSha256':self.jar_hash}
        (self.output/'result.json').write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--case',choices=['backlog'],required=True)
    a=p.parse_args(); runner=FaultVerification(a.runtime,a.output)
    try:
        getattr(runner,a.case)();runner.save('BOUNDED_ACCEPT')
    except Exception as error:
        runner.save('FAILED',str(error));raise
    finally:
        runner.fault('normal')
        runner.command(['logs','--no-color','app1','app2'])
