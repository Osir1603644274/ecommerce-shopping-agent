"""Actual receipt-worker exhaustion and manual application of the unchanged receipt."""
import uuid
from live_evaluation import LiveDriver,save
from progress_oracle import grade_progress
from trace_metering import meter_requests


def run_receipt_retry(runtime,bff,fault,output):
    driver=LiveDriver(runtime,bff.origin);driver.inventory_fault=fault
    scenario={'fixture':{'kind':'return_inspection','unitPriceMinor':101,'purchasedQuantity':3,'requestedQuantity':1},
              'expected':{'routeOrOutcome':'after_sale'}}
    results=[]
    try:
        driver.prepare(scenario)
        driver.drive('inspect_sellable',None)
        driver.drive('lose_inventory_ack',None)
        driver.drive('exhaust_receipt_retries',None)
        for stage,message in (('exhausted','请查询这笔退货退款，现在是否已经到账？'),('recovered','再查一下这笔售后现在是否已经完成。')):
            if stage=='recovered':driver.drive('manual_original_retry',None)
            before=driver.snapshot();save(output/(stage+'-before.json'),before)
            observed=driver.chat(message,uuid.uuid4().hex);save(output/(stage+'-http.json'),observed)
            after=driver.snapshot();save(output/(stage+'-after.json'),after)
            grade=grade_progress(scenario,observed,before,after);save(output/(stage+'-VERDICT.json'),grade)
            results.append({'stage':stage,'grade':grade});print(stage+': '+grade['verdict'],flush=True)
        final=driver.snapshot()
        assert final['order_line_allocation'][0]['refunded_minor']==101
        assert final['support_case'][0]['phase']=='COMPLETED'
        save(output/'METERING.json',meter_requests(driver.chat_observations))
        save(output/'RESULT.json',{'status':'PASS' if all(r['grade']['verdict']=='PASS' for r in results) else 'FAIL',
             'scope':'additional actual receipt recovery Agent probe; not formal240','results':results})
    finally:
        try:
            save(output/'driver-evidence.json',driver.driver_evidence)
            if driver.order:save(output/'last-snapshot.json',driver.snapshot())
        finally:driver.close()
