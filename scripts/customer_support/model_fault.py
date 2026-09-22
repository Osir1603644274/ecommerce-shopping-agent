"""Local read-timeout fixture. Never records request bodies or real credentials."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading

class ModelTimeout:
    def __init__(self):
        self.release=threading.Event();self.arrivals=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                owner.arrivals.append({'path':self.path,'syntheticCredential':self.headers.get('Authorization')=='Bearer synthetic-timeout-fixture'})
                owner.release.wait(45);self.close_connection=True
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/v1'
    def close(self):
        self.release.set();self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

def grade_timeout(observed,before,after,arrivals):
    from business_oracle import _result
    trace=observed.get('trace') or {};attempts=trace.get('attempts',[]);result=trace.get('result') or {}
    receipts=[a.get('modelReceipt',{}) for a in attempts]
    checks=[('read_only',before==after),('failed_turn',trace.get('status')=='FAILED'),
            ('real_timeout',len(receipts)==1 and receipts[0].get('errorType')=='APITimeoutError'),
            ('unknown_usage_preserved',bool(receipts) and all(c.get('usage') is None and c.get('cost') is None for c in receipts)),
            ('single_fault_request',len(arrivals)==1 and arrivals[0].get('syntheticCredential') is True),
            ('no_action_draft',result.get('kind')=='error' and not any(result.get(k) for k in ('preview','ticketDraft','actionDraft','ticketReplyDraft')))]
    return _result(checks,[] if before==after else ['unconfirmed_business_write'])
