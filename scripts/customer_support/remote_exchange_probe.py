"""Additional real exchange fault probes; independent SQL/receipts and model grading."""
import uuid
from live_evaluation import LiveDriver, save
from progress_oracle import grade_progress
from trace_metering import meter_requests


def run_exchange(runtime,bff,fault,output):
    summaries=[]
    for mode in ('reserve','dispatch','release'):
        folder=output/mode;folder.mkdir()
        driver=LiveDriver(runtime,bff.origin)
        driver.inventory_fault=fault
        scenario={'fixture':{'kind':'exchange_waiting','unitPriceMinor':101,'purchasedQuantity':3,'requestedQuantity':1},
                  'expected':{'routeOrOutcome':'after_sale'}}
        try:
            driver.prepare(scenario)
            case=driver.case['id']
            driver.admin('process',case)
            assert driver.admin('inventory-retry',case,{'commandId':'return:'+case})['status']=='ACK'
            driver.admin('process',case)
            replacement=driver.sql('SELECT id FROM support_replacement WHERE case_id=%s',(case,))[0]['id']
            reserve='replacement-reserve:'+replacement
            if mode!='reserve':
                assert driver.admin('inventory-retry',case,{'commandId':reserve})['status']=='ACK'
                driver.admin('process',case)
                assert driver.case_read()['phase']=='REPLACEMENT_READY'
            if mode=='release':
                driver.admin('clock-advance',driver.order,{'expectedVersion':0,'seconds':2592001})
            driver.drive('lose_'+mode+'_ack',None)
            command=driver.remote_pending['commandId'];receipt=driver.remote_pending['receiptId']
            if mode=='dispatch':
                driver.drive('attempt_expiry',None)
            before=driver.snapshot();save(folder/'before.json',before)
            driver.drive('attempt_conversion',None)
            assert driver.snapshot()==before
            observed=driver.chat('请查询这笔换货目前的办理进度。',uuid.uuid4().hex)
            save(folder/'http-and-trace.json',observed)
            after=driver.snapshot();save(folder/'after-chat.json',after)
            grade=grade_progress(scenario,observed,before,after);save(folder/'VERDICT.json',grade)
            save(folder/'METERING.json',meter_requests([observed]))
            driver.drive('recover_inventory',None)
            expected={'reserve':'REPLACEMENT_READY','dispatch':'REPLACEMENT_SHIPPED','release':'WAITING_CHOICE'}[mode]
            assert driver.case_read()['phase']==expected
            final=driver.snapshot();save(folder/'final.json',final)
            assert final['inventory_stock']==before['inventory_stock']
            assert final['order_line_allocation']==before['order_line_allocation']
            assert not final['support_refund_command']
            driver.admin('inventory-retry',case,{'commandId':command})
            if receipt:driver.admin('receipt-apply',receipt)
            assert driver.snapshot()==final
            if mode=='release':
                preview=driver.api('POST','/api/after-sales/'+case+'/conversion-preview')
                assert preview['amountMinor']==101
                save(folder/'recovered-conversion-preview.json',preview)
            summary={'mode':mode,'status':grade['verdict'],'grade':grade,'recoveredPhase':expected,
                     'conversionBlockedWithoutAck':True,'noDuplicateInventoryEffect':True}
            save(folder/'RESULT.json',summary);summaries.append(summary)
            print(mode+': '+grade['verdict'],flush=True)
        finally:
            save(folder/'driver-evidence.json',driver.driver_evidence)
            try:
                if driver.order:save(folder/'last-snapshot.json',driver.snapshot())
            finally:driver.close()
    save(output/'RESULT.json',{'status':'PASS' if all(x['status']=='PASS' for x in summaries) else 'FAIL',
         'scope':'three additional remote exchange Agent probes; not formal240','probes':summaries})
