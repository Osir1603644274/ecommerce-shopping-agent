"""Loopback-only inventory proxy; lose acknowledgements after real upstream execution.

Only explicitly armed command IDs are affected. Never records credentials or bodies.
This is simulator infrastructure, not evidence that an Agent scenario passed.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote

import httpx


class InventoryFault:
    def __init__(self, upstream, token):
        parsed = urlsplit(upstream)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('explicit loopback inventory origin required')
        self.lock = threading.Lock()
        self.armed = set()
        self.blocked_lookups = set()
        self.events = []
        self.client = httpx.Client(base_url=upstream, timeout=5, trust_env=False,
                                  headers={'X-Inventory-Service-Token': token})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                # Do not accept an absolute target or proxy arbitrary service routes.
                target = urlsplit(self.path)
                if target.scheme or target.netloc or not target.path.startswith('/internal/inventory/'):
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                command_id = None
                if self.command == 'GET' and target.path.startswith('/internal/inventory/commands/'):
                    command_id = unquote(target.path[len('/internal/inventory/commands/'):])
                if self.command == 'POST' and target.path == '/internal/inventory/commands':
                    try:
                        payload = json.loads(body)
                        command_id = payload.get('commandId') if isinstance(payload, dict) else None
                        if not isinstance(command_id, str):
                            command_id = None
                    except (ValueError, UnicodeDecodeError):
                        pass
                try:
                    result = owner.client.request(self.command, self.path, content=body,
                                                  headers={'Content-Type': 'application/json'})
                except httpx.HTTPError:
                    self.send_error(502, 'Inventory upstream unavailable')
                    return
                with owner.lock:
                    lose = result.is_success and (command_id in owner.armed if self.command=='POST' else command_id in owner.blocked_lookups)
                    owner.events.append({'method': self.command, 'commandId': command_id,
                                         'upstreamStatus': result.status_code, 'ackLost': lose})
                if lose:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                self.send_response(result.status_code)
                self.send_header('Content-Type', result.headers.get('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(result.content)))
                self.end_headers()
                self.wfile.write(result.content)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def arm(self, command_id, block_lookup=False):
        if not isinstance(command_id, str) or not command_id:
            raise ValueError('nonempty exact command ID required')
        with self.lock:
            self.armed.add(command_id)
            if block_lookup:self.blocked_lookups.add(command_id)

    def recover(self, command_id):
        with self.lock:
            self.armed.discard(command_id)
            self.blocked_lookups.discard(command_id)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.client.close()
