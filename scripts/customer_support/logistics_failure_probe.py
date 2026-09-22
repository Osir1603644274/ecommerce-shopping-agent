"""Real worker outage fixtures with independent model/SQL checks."""
import uuid
from live_evaluation import LiveDriver,save
from progress_oracle import grade_progress
from trace_metering import meter_requests


def run_logistics(runtime,bff,warehouse,output):
    results=[]
    for kind in ('dispatch_unknown','dispatch_review'):
        folder=output/kind;folder.mkdir();driver=LiveDriver(runtime,bff.origin)
        scenario={'fixture':{'kind':kind,'unitPriceMinor':101,'purchasedQuantity':3,'requestedQuantity':1},
                  'expected':{'routeOrOutcome':'logistics:original_order'}}
        try:
            count=len(warehouse.events);driver.prepare(scenario)
            before=driver.snapshot();save(folder/'before.json',before)
            assert len(warehouse.events)>count and all(e['statusCode']==503 for e in warehouse.events[count:])
            assert not before['support_order_receipt']
            assert before['order_line_allocation'][0]['refunded_minor']==0
            observed=driver.chat('帮我查一下这笔订单的当前物流状态。',uuid.uuid4().hex)
            save(folder/'http-and-trace.json',observed)
            after=driver.snapshot();save(folder/'after.json',after)
            grade=grade_progress(scenario,observed,before,after)
            save(folder/'VERDICT.json',grade);save(folder/'METERING.json',meter_requests([observed]))
            results.append({'fixture':kind,'grade':grade,'attempts':before['fulfillment_task'][0]['attempts']})
            print(kind+': '+grade['verdict'],flush=True)
        finally:
            save(folder/'driver-evidence.json',driver.driver_evidence)
            driver.close()
    save(output/'RESULT.json',{'status':'PASS' if all(r['grade']['verdict']=='PASS' for r in results) else 'FAIL',
        'scope':'two additional live logistics Agent fixtures, not formal240','results':results})
