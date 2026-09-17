"""LAB ONLY. Transparent proxy with one-shot file-armed faults; no live route uses it."""
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
import http.client
import json
import os
import socket
import threading
import time
import hashlib

CONTROL=Path(os.environ.get('FAULT_CONTROL','/control'))
TARGET=os.environ.get('FAULT_TARGET','micro-e2e-inventory-20260915')
LOCK=threading.Lock()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):self.forward()
    def do_POST(self):self.forward()
    def forward(self):
        length=int(self.headers.get('Content-Length','0'))
        if length>65536:self.send_error(413);return
        body=self.rfile.read(length) if length else b''
        try:command=json.loads(body) if body else {}
        except ValueError:command={}
        rule=None
        with LOCK:
            path=CONTROL/'rule.json'
            if self.command=='POST' and self.path=='/internal/inventory/commands' and path.exists():
                candidate=json.loads(path.read_text())
                if candidate.get('kind')==command.get('kind') and (not candidate.get('orderId') or candidate['orderId']==command.get('orderId')):
                    rule=candidate;path.rename(CONTROL/('consumed-'+candidate['id']+'.json'))
        connection=http.client.HTTPConnection(TARGET,8083,timeout=10)
        try:
            headers={key:value for key,value in self.headers.items() if key.lower() not in {'host','connection','transfer-encoding'}}
            connection.request(self.command,self.path,body=body,headers=headers)
            upstream=connection.getresponse();result=upstream.read()
            if rule:
                event={'fault':rule,'commandId':command.get('commandId'),'orderId':command.get('orderId'),
                       'upstreamStatus':upstream.status,'responseSha256':hashlib.sha256(result).hexdigest()}
                (CONTROL/('fired-'+rule['id']+'.json')).write_text(json.dumps(event))
                if rule['mode']=='hold':time.sleep(30)
                if rule['mode'] in {'hold','drop'}:
                    self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
            self.send_response(upstream.status)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(result)));self.end_headers()
            self.wfile.write(result)
        except (OSError,http.client.HTTPException):
            try:self.send_error(502)
            except OSError:pass
        finally:connection.close()

if __name__=='__main__':
    CONTROL.mkdir(exist_ok=True)
    ThreadingHTTPServer(('0.0.0.0',18083),Handler).serve_forever()
