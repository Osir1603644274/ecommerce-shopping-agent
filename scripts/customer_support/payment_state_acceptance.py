"""Isolated signed failure callback and actual expiry-job state verification."""
import argparse
import hashlib
from pathlib import Path
from live_evaluation import LiveDriver,save

def main(runtime,output):
    output.mkdir(parents=True,exist_ok=False)
    hashes={}
    for name in ('payment_state_acceptance.py','live_evaluation.py'):
        data=Path(__file__).with_name(name).read_bytes();(output/name).write_bytes(data);hashes[name]=hashlib.sha256(data).hexdigest()
    save(output/'SOURCE_HASHES.json',hashes)
    for kind in ('payment_failed','expired'):
        d=LiveDriver(runtime,'http://127.0.0.1:18000');folder=output/kind;folder.mkdir()
        try:
            d.prepare({'fixture':{'kind':kind,'unitPriceMinor':127,'purchasedQuantity':3,'requestedQuantity':1}})
            snapshot=d.snapshot();save(folder/'SQL.json',snapshot)
            stock=snapshot['inventory_stock'][0]
            if kind=='payment_failed':
                assert snapshot['customer_order'][0]['status']=='PENDING_PAYMENT'
                assert snapshot['payment_record'][0]['status']=='FAILED' and len(snapshot['payment_notification'])==1
                assert (stock['available_quantity'],stock['reserved_quantity'],stock['sold_quantity'])==(97,3,0)
            else:
                assert snapshot['customer_order'][0]['status']=='EXPIRED' and not snapshot['payment_record']
                assert (stock['available_quantity'],stock['reserved_quantity'],stock['sold_quantity'])==(100,0,0)
            assert not snapshot['support_order_receipt'] and not snapshot['support_case']
            save(folder/'VERDICT.json',{'verdict':'PASS','scope':'Actual backend state fixture; no model quality claim'})
            print(kind,'PASS',flush=True)
        except Exception as error:
            save(folder/'FAILURE.json',{'type':type(error).__name__});save(folder/'failure-state.json',d.snapshot());raise
        finally:save(folder/'driver.json',d.driver_evidence);d.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();main(args.runtime,args.output)
