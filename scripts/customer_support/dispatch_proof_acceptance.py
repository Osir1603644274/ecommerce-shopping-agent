"""Real HTTP expiry/dispatch proof acceptance, without heldout dialogue exposure."""
import argparse
import hashlib
from pathlib import Path
from live_evaluation import LiveDriver,save
from business_oracle import grade_final

def main(runtime,output):
    output.mkdir(parents=True,exist_ok=False)
    sources={}
    for name in ('dispatch_proof_acceptance.py','live_evaluation.py','business_oracle.py'):
        data=Path(__file__).with_name(name).read_bytes()
        (output/name).write_bytes(data);sources[name]=hashlib.sha256(data).hexdigest()
    save(output/'SOURCE_HASHES.json',sources)
    d=LiveDriver(runtime,'http://127.0.0.1:18000')
    scenario={'fixture':{'kind':'exchange_expired_dispatch_proof','unitPriceMinor':113,'purchasedQuantity':4,'requestedQuantity':2},
              'steps':[{'action':'query'},{'action':'expire_reservation'},{'action':'apply_dispatch'}]}
    try:
        d.prepare(scenario);before=d.snapshot();save(output/'before.json',before)
        assert before['support_case'][0]['phase']=='REPLACEMENT_READY'
        proof=[r for r in before['support_receipt'] if r['event_type']=='REPLACEMENT_DISPATCH_CONFIRMED']
        assert len(proof)==1 and proof[0]['status']=='PENDING'
        d.drive('expire_reservation',None);save(output/'after-expiry.json',d.snapshot())
        d.drive('apply_dispatch',None);final=d.snapshot();save(output/'final.json',final)
        assert final['support_replacement'][0]['status']=='SHIPPED'
        assert final['support_replacement'][0]['dispatch_receipt_id']==proof[0]['id']
        verdict=grade_final(scenario,before,final);save(output/'VERDICT.json',verdict)
        if verdict['verdict']!='PASS':raise AssertionError(verdict)
        print('dispatch proof expiry and replay PASS',flush=True)
    finally:
        save(output/'driver.json',d.driver_evidence);d.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();main(args.runtime,args.output)
