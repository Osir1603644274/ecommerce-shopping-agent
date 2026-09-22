"""Real isolated inventory HTTP/SQL acceptance. Never restart an unlabelled service.

Does not certify trade-journal, customer UI, or model quality. All tokens remain private.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
import uuid

import httpx
import pymysql


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True)
    if result.returncode:
        raise RuntimeError('isolated docker operation failed: ' + args[0])
    return result.stdout.decode()


def run(runtime: Path, output: Path):
    output.mkdir(exist_ok=False)
    private = json.loads((runtime / 'private.json').read_text())
    name = private['container']
    info = json.loads(docker('inspect', name))[0]
    assert info['Config']['Labels'].get('support.attempt') == 'inventory-network-001'
    binding = info['NetworkSettings']['Ports']['8083/tcp'][0]
    assert binding['HostIp'] == '127.0.0.1'
    url = 'http://127.0.0.1:' + binding['HostPort']
    report = {'scope': 'Independent inventory HTTP transactions and SQL oracle, not trade/Agent end-to-end',
              'status': 'RUNNING', 'checks': []}

    def save():
        (output / 'RESULT.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

    def check(label, evidence):
        report['checks'].append({'check': label, 'evidence': evidence})
        save()

    def ready():
        for _ in range(90):
            try:
                if httpx.get(url + '/actuator/health', timeout=2, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError('isolated inventory health timeout')

    def sql(query, args=()):
        with pymysql.connect(**{k: private[k] for k in ('host', 'port', 'user', 'password', 'database')},
                             autocommit=True, cursorclass=pymysql.cursors.DictCursor) as db, db.cursor() as c:
            c.execute(query, args)
            return c.fetchall()

    headers = {'X-Inventory-Service-Token': private['writeToken']}
    client = httpx.Client(base_url=url + '/internal/inventory', headers=headers, timeout=5, trust_env=False)
    stock_id = 730000000 + int(uuid.uuid4().hex[:6], 16)
    prefix = uuid.uuid4().hex[:12]

    def command(order, kind, qty=None):
        return {'commandId': kind.lower() + ':' + order, 'orderId': order, 'kind': kind,
                'items': [] if qty is None else [{'itemType': 'PRODUCT', 'itemId': stock_id, 'quantity': qty}],
                'expiresAt': '2026-12-31T00:00:00' if kind == 'RESERVE' else None}

    def apply(body):
        response = client.post('/commands', json=body)
        response.raise_for_status()
        return response.json()

    class LostResponse(BaseHTTPRequestHandler):
        # Forward a real committed transaction; deliberately deliver no HTTP response.
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            response = client.post('/commands', content=body, headers={'Content-Type': 'application/json'})
            self.server.upstream_status = response.status_code
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def log_message(self, *_args):
            pass

    proxy = ThreadingHTTPServer(('127.0.0.1', 0), LostResponse)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()

    def lost(body):
        proxy.upstream_status = None
        try:
            httpx.post(f'http://127.0.0.1:{proxy.server_port}/commands', json=body, timeout=5, trust_env=False)
            raise AssertionError('fault did not sever response')
        except httpx.TransportError:
            pass
        assert proxy.upstream_status == 200
        response = client.get('/commands/' + body['commandId'])
        response.raise_for_status()
        original = response.json()
        assert original['status'] == 'APPLIED'
        before = sql('SELECT * FROM inventory_stock WHERE item_id=%s', (stock_id,))
        assert apply(body) == original
        assert sql('SELECT * FROM inventory_stock WHERE item_id=%s', (stock_id,)) == before
        check('committed-response-lost-' + body['kind'], {'command': body, 'receipt': original, 'stock': before})
        return original

    stopped = False
    try:
        ready()
        response = client.post('/stocks', json={'itemType': 'PRODUCT', 'itemId': stock_id, 'quantity': 100})
        response.raise_for_status()
        original_order = prefix + '-original'
        lost(command(original_order, 'RESERVE', 4))
        lost(command(original_order, 'CONFIRM'))
        lost(command(original_order, 'RETURN_SELLABLE', 2))
        lost(command(original_order, 'RETURN_QUARANTINE', 1))
        release_order = prefix + '-replacement'
        lost(command(release_order, 'RESERVE', 1))
        release = command(release_order, 'RELEASE')
        receipt = lost(release)
        before = sql('SELECT * FROM inventory_stock WHERE item_id=%s', (stock_id,))
        docker('stop', '-t', '5', name)
        stopped = True
        try:
            client.post('/commands', json=release)
            raise AssertionError('stopped service unexpectedly responded')
        except httpx.TransportError:
            pass
        docker('start', name)
        stopped = False
        # Docker may reassign an ephemeral published port across stop/start.
        restarted = json.loads(docker('inspect', name))[0]
        binding = restarted['NetworkSettings']['Ports']['8083/tcp'][0]
        assert binding['HostIp'] == '127.0.0.1'
        url = 'http://127.0.0.1:' + binding['HostPort']
        client.close()
        client = httpx.Client(base_url=url + '/internal/inventory', headers=headers, timeout=5, trust_env=False)
        ready()
        assert client.get('/commands/' + release['commandId']).json() == receipt
        assert apply(release) == receipt
        assert sql('SELECT * FROM inventory_stock WHERE item_id=%s', (stock_id,)) == before
        check('restart-durable-receipt-and-replay', {'commandId': release['commandId'], 'stock': before})
        outcomes = []
        for i in range(8):
            order = prefix + '-race-' + str(i)
            assert apply(command(order, 'RESERVE', 1))['status'] == 'APPLIED'
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(apply, [command(order, 'CONFIRM'), command(order, 'RELEASE')]))
            assert sorted(r['status'] for r in results) == ['APPLIED', 'REJECTED']
            rows = sql('SELECT status,quantity FROM inventory_reservation WHERE order_id=%s', (order,))
            assert len(rows) == 1 and rows[0]['status'] in ('CONFIRMED', 'RELEASED')
            outcomes.append({'orderId': order, 'receipts': results, 'reservation': rows})
        check('concurrent-confirm-release-single-winner', outcomes)
        stock = sql('SELECT * FROM inventory_stock WHERE item_id=%s', (stock_id,))[0]
        confirmed = sum(x['reservation'][0]['status'] == 'CONFIRMED' for x in outcomes)
        assert stock['reserved_quantity'] == 0
        assert stock['sold_quantity'] == 1 + confirmed
        assert stock['total_quantity'] == 99
        assert stock['available_quantity'] == 98 - confirmed
        returns = sql('SELECT * FROM inventory_return_receipt WHERE order_id=%s', (original_order,))
        assert len(returns) == 2
        check('independent-final-stock-and-return-oracle', {'stock': stock, 'returnRows': returns})
        report['status'] = 'PASS'
    except Exception as error:
        report['status'] = 'FAIL'
        report['failure'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        if stopped:
            docker('start', name)
        proxy.shutdown()
        client.close()
        save()
    print(json.dumps({'status': report['status'], 'checks': len(report['checks']), 'output': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.runtime, args.output)
