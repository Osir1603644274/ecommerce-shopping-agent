"""Real SDK transport timeout, failed-turn persistence and same-id recovery."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import threading
import uuid
from live_evaluation import LiveDriver,save
from managed_bff import ManagedBff
from progress_oracle import grade_progress

def main(runtime,output):
    output.mkdir(parents=True,exist_ok=False)
    sources={}
    for name in ('model_timeout_acceptance.py','managed_bff.py','live_evaluation.py','progress_oracle.py'):
        data=Path(__file__).with_name(name).read_bytes();(output/name).write_bytes(data)
        sources[name]=hashlib.sha256(data).hexdigest()
    save(output/'SOURCE_HASHES.json',sources)
    release=threading.Event();arrivals=[]
    class TimeoutHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            # Consume but never persist body or headers. Send no response before SDK timeout.
            self.rfile.read(int(self.headers['Content-Length']))
            arrivals.append({'path':self.path,'syntheticCredential':self.headers.get('Authorization')=='Bearer synthetic-timeout-fixture'})
            release.wait(45)
            self.close_connection=True
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),TimeoutHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    bff=ManagedBff();bff.model_fault_url=f'http://127.0.0.1:{server.server_port}/v1'
    d=None
    scenario={'expected':{'routeOrOutcome':'logistics:original_order'},'fixture':{'kind':'received','unitPriceMinor':113,'purchasedQuantity':4,'requestedQuantity':2}}
    try:
        bff.start();d=LiveDriver(runtime,bff.origin);d.prepare(scenario)
        before=d.snapshot();save(output/'before.json',before)
        request=uuid.uuid4().hex;message='查询这笔订单原始物流的发货和签收状态。'
        failed=d.chat(message,request);save(output/'timeout.json',failed)
        after=d.snapshot();save(output/'after-timeout.json',after)
        trace=failed['trace'];receipt=trace['attempts'][0]['modelReceipt']
        assert before==after and trace['status']=='FAILED'
        assert receipt['errorType']=='APITimeoutError' and receipt['usage'] is None and receipt['cost'] is None
        assert len(arrivals)==1 and arrivals[0]['syntheticCredential']
        assert trace['result']['kind']=='error' and not any(trace['result'].get(k) for k in ('preview','ticketDraft','actionDraft','ticketReplyDraft'))
        release.set();bff.model_fault_url=None;restart=bff.restart();save(output/'restart.json',restart)
        recovered=d.chat(message,request);save(output/'recovered.json',recovered)
        final=d.snapshot();save(output/'final.json',final)
        assert recovered['trace']['status']=='COMPLETED' and len(recovered['trace']['attempts'])==2
        assert recovered['trace']['attempts'][0]==trace['attempts'][0]
        calls=[a['modelReceipt'] for a in recovered['trace']['attempts']]
        assert len({c['modelCallId'] for c in calls})==2
        verdict=grade_progress(scenario,recovered,before,final);save(output/'VERDICT.json',verdict)
        assert verdict['verdict']=='PASS',verdict
        save(output/'METERING.json',{'modelCalls':calls,'unknownUsageCalls':sum(c['usage'] is None for c in calls),
            'timeoutElapsedMs':failed['elapsedMs'],'recoveryElapsedMs':recovered['elapsedMs'],
            'combinedRequestMs':failed['elapsedMs']+recovered['elapsedMs'],'estimatedTotalCostUSD':None})
        save(output/'FAULT.json',{'kind':'real loopback HTTP read timeout','arrivals':arrivals,'providerCalledDuringFault':False})
        print('model timeout and original-id recovery PASS',flush=True)
    finally:
        release.set();bff.stop();server.shutdown();server.server_close()
        if d:d.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();main(args.runtime,args.output)
