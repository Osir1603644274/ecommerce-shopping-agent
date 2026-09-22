"""Independent loopback warehouse outage; no shipping receipts are fabricated."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading


class WarehouseUnavailable:
    def __init__(self):
        self.events=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.fail()
            def do_POST(self):self.fail()
            def fail(self):
                owner.events.append({'method':self.command,'path':self.path,'statusCode':503})
                self.send_response(503);self.send_header('Content-Length','0');self.end_headers()
                self.close_connection=True
            def log_message(self,*_args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
