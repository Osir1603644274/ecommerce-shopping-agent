"""Real confirmation protocol probes; no model or heldout conversation labels."""
import argparse
from pathlib import Path
from live_evaluation import LiveDriver,save
from business_oracle import grade_final

FLOWS={'superseded':['make_new_preview','confirm_old_preview'],
       'expired':['advance_preview_ttl','confirm_preview'],
       'changed_key_body':['confirm_preview','reuse_key_changed_body'],
       'competing_types':['concurrent_confirm_types']}

def main(runtime,output):
    output.mkdir(parents=True,exist_ok=False)
    for name,actions in FLOWS.items():
        d=LiveDriver(runtime,'http://127.0.0.1:18000');folder=output/name;folder.mkdir()
        scenario={'fixture':{'kind':'received','unitPriceMinor':107,'purchasedQuantity':4,'requestedQuantity':2},'steps':[{'message':'protocol probe'}]+[{'action':a} for a in actions]}
        try:
            d.prepare(scenario);before=d.snapshot();save(folder/'before.json',before)
            preview=d.api('POST','/api/after-sales/preview',{'orderId':d.order,'itemId':d.item,'quantity':2,'type':'REFUND_ONLY','reason':'独立确认协议检查'})
            save(folder/'preview.json',preview)
            for action in actions:d.drive(action,{'preview':preview})
            final=d.snapshot();save(folder/'final.json',final)
            verdict=grade_final(scenario,before,final);save(folder/'VERDICT.json',verdict)
            if verdict['verdict']!='PASS':raise AssertionError(verdict)
            print(name,'PASS',flush=True)
        finally:save(folder/'driver.json',d.driver_evidence);d.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();main(args.runtime,args.output)
