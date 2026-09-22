"""Check denied conversion while actual remote reserve is committed but unacknowledged."""
import uuid
from live_evaluation import LiveDriver,save
from action_oracle import grade_action
from trace_metering import meter_requests


def run_unknown_reserve(runtime,bff,fault,output):
    results=[]
    for number,(price,purchased,quantity) in enumerate(((101,3,1),(199,5,2)),1):
        folder=output/str(number);folder.mkdir();driver=LiveDriver(runtime,bff.origin);driver.inventory_fault=fault
        scenario={'fixture':{'kind':'exchange_reserve_unknown','unitPriceMinor':price,'purchasedQuantity':purchased,'requestedQuantity':quantity},
                  'expected':{'routeOrOutcome':'action:conversion_preview'}}
        try:
            driver.prepare(scenario);before=driver.snapshot();save(folder/'before.json',before)
            command=driver.remote_pending['commandId']
            assert next(r for r in before['inventory_command_journal'] if r['command_id']==command)['status']=='PENDING'
            assert next(r for r in before['inventory_command_receipt'] if r['command_id']==command)['status']=='APPLIED'
            observed=driver.chat('我想把这笔换货改成退货退款，现在能办理吗？',uuid.uuid4().hex)
            save(folder/'http-and-trace.json',observed);after=driver.snapshot();save(folder/'after.json',after)
            grade=grade_action(scenario,observed,before,after)
            answer=(observed.get('trace') or {}).get('result',{}).get('answer','')
            if '已确认缺货' in answer:
                grade['verdict']='FAIL';grade['reasons'].append('unknown_reserve_reported_as_shortage')
            save(folder/'VERDICT.json',grade);save(folder/'METERING.json',meter_requests([observed]))
            driver.drive('attempt_conversion',None)
            driver.drive('recover_inventory',None)
            final=driver.snapshot();save(folder/'final.json',final)
            assert final['order_line_allocation']==before['order_line_allocation']
            assert not final['support_refund_command'] and final['support_case'][0]['phase']=='REPLACEMENT_READY'
            results.append({'variant':number,'grade':grade,'quantity':quantity})
            print('unknown-reserve-'+str(number)+': '+grade['verdict'],flush=True)
        finally:
            save(folder/'driver-evidence.json',driver.driver_evidence);driver.close()
    save(output/'RESULT.json',{'status':'PASS' if all(r['grade']['verdict']=='PASS' for r in results) else 'FAIL',
        'scope':'additional unknown-reserve Agent variants; not formal heldout or240','results':results})
