"""Loopback-only durable warehouse fixture; never represents a real logistics integration."""
from contextlib import closing
import argparse
import hashlib
import hmac
import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote


def create_server(database, token, port=19091, fault_file=None, host='127.0.0.1'):
    database = str(database)
    with closing(sqlite3.connect(database)) as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS shipment (
            request_key TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
            command_hash TEXT NOT NULL, tracking_no TEXT NOT NULL UNIQUE, command_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fault_used (request_key TEXT PRIMARY KEY);
        """)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def authorized(self):
            if hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                return True
            self.respond(401, {'error': 'unauthorized'})
            return False

        def respond(self, status, body):
            data = json.dumps(body, separators=(',', ':')).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == '/health':
                self.respond(200, {'status': 'ready', 'simulation': True})
                return
            if not self.path.startswith('/shipments/by-request/'):
                self.respond(404, {'error': 'not found'})
                return
            key = unquote(self.path.removeprefix('/shipments/by-request/'))
            with closing(sqlite3.connect(database, timeout=5)) as db:
                row = db.execute('SELECT request_key,order_id,command_hash,tracking_no FROM shipment WHERE request_key=?', (key,)).fetchone()
            self.respond(200, receipt(row)) if row else self.respond(404, {'error': 'not found'})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != '/shipments':
                self.respond(404, {'error': 'not found'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 8192:
                    raise ValueError('invalid length')
                raw = self.rfile.read(size)
                command = json.loads(raw)
                key, order = command['requestKey'], command['orderId']
                if not isinstance(key, str) or not isinstance(order, str) or len(key) > 96 or len(order) > 36:
                    raise ValueError('invalid identity')
                if 'schemaVersion' in command:
                    if command['schemaVersion'] != 'warehouse.cart.v2' or type(command.get('revision')) is not int or command['revision'] <= 0:
                        raise ValueError('invalid contract version')
                    if key != f"fulfillment-v2:{order}:{command['revision']}":
                        raise ValueError('invalid versioned identity')
                    items = command.get('items')
                    if not isinstance(items, list) or not 1 <= len(items) <= 50:
                        raise ValueError('invalid cart')
                    seen = set()
                    for item in items:
                        if (not isinstance(item, dict) or item.get('itemType') != 'PRODUCT' or type(item.get('itemId')) is not int or item['itemId'] <= 0
                                or type(item.get('quantity')) is not int or item['quantity'] <= 0 or item['itemId'] in seen):
                            raise ValueError('invalid cart item')
                        seen.add(item['itemId'])
                elif command['itemType'] != 'PRODUCT' or type(command['quantity']) is not int or command['quantity'] <= 0:
                    raise ValueError('invalid item')
                digest = hashlib.sha256(raw).hexdigest()
                mode = json.loads(Path(fault_file).read_text()).get('mode') if fault_file and Path(fault_file).exists() else None
                if mode == 'unavailable':
                    self.respond(503, {'error': 'injected unavailable'})
                    return
                with closing(sqlite3.connect(database, timeout=5)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT request_key,order_id,command_hash,tracking_no FROM shipment WHERE request_key=?', (key,)).fetchone()
                    if row and (row[1] != order or row[2] != digest):
                        self.respond(409, {'error': 'immutable command conflict'})
                        return
                    if not row:
                        row = (key, order, digest, 'SIM-' + hashlib.sha256(key.encode()).hexdigest()[:24])
                        db.execute('INSERT INTO shipment VALUES(?,?,?,?,?)', (*row, raw.decode()))
                    drop = mode == 'commit_then_drop_once' and not db.execute('SELECT 1 FROM fault_used WHERE request_key=?', (key,)).fetchone()
                    if drop:
                        db.execute('INSERT INTO fault_used VALUES(?)', (key,))
                    db.commit()
                if drop:
                    self.close_connection = True
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                if mode == 'commit_then_delay':
                    time.sleep(10)  # A real process-kill window after the durable side effect.
                self.respond(200, receipt(row))
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
                self.respond(409, {'error': 'invalid or conflicting command'})

    return ThreadingHTTPServer((host, port), Handler)


def receipt(row):
    return dict(zip(('requestKey', 'orderId', 'commandHash', 'trackingNo'), row))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--port', type=int, default=19091)
    parser.add_argument('--fault-file', type=Path)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', '0.0.0.0'])
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    token = args.token_file.read_text().strip()
    if len(token) < 24:
        raise SystemExit('Use a fixture token with at least 24 characters')
    server = create_server(args.database, token, args.port, args.fault_file, args.host)
    print(json.dumps({'ready': True, 'port': server.server_port, 'simulation': True}), flush=True)
    server.serve_forever()
