"""Real isolated HTTP driver smoke; independent of heldout conversation labels."""
import argparse
from pathlib import Path
from live_evaluation import LiveDriver,save
from business_oracle import grade_final

def main(runtime,output):
    output.mkdir(parents=True,exist_ok=False)
    for action,kind in [('receive_wrong_item','return_transit'),('receive_wrong_quantity','return_transit'),('inspect_disputed','return_inspection')]:
        driver=LiveDriver(runtime,'http://127.0.0.1:18000');folder=output/action;folder.mkdir()
        scenario={'fixture':{'kind':kind,'unitPriceMinor':103,'purchasedQuantity':4,'requestedQuantity':2},'steps':[{'message':'driver smoke'},{'action':action}]}
        try:
            driver.prepare(scenario);before=driver.snapshot();save(folder/'before.json',before)
            driver.drive(action,None);save(folder/'review.json',driver.snapshot())
            driver.drive('resolve_linked_ticket',None);driver.drive('resume_review',None);save(folder/'resumed.json',driver.snapshot())
            driver.drive('fresh_warehouse_receipt',None);final=driver.snapshot();save(folder/'final.json',final)
            verdict=grade_final(scenario,before,final);save(folder/'VERDICT.json',verdict)
            if verdict['verdict']!='PASS':raise AssertionError(verdict)
            print(action,'PASS',flush=True)
        finally:
            save(folder/'driver.json',driver.driver_evidence);driver.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();main(args.runtime,args.output)
