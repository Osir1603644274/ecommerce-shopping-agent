"""Scenario actions that use real remote commands and an independently owned fault proxy."""
import time
REMOTE_ACTIONS={'lose_inventory_ack','lose_reserve_ack','lose_dispatch_ack','lose_release_ack',
                'attempt_refund','attempt_conversion','attempt_expiry','recover_inventory',
                'exhaust_receipt_retries','manual_original_retry'}


def settle_return(driver):
    case=driver.case['id']
    driver.admin('process',case)
    assert driver.admin('inventory-retry',case,{'commandId':'return:'+case})['status']=='ACK'
    driver.admin('process',case)


def drive_remote(driver,action):
    fault=driver.inventory_fault
    if not driver.inventory_config or fault is None:
        raise RuntimeError('remote fault action requires real remote inventory and owned proxy')
    case=driver.case['id']
    if action.startswith('lose_'):
        if driver.remote_pending:raise RuntimeError('recover existing fault before injecting another')
        receipt=None
        if action=='lose_inventory_ack':
            driver.admin('process',case);command='return:'+case
        else:
            if action=='lose_reserve_ack':settle_return(driver)
            replacement=driver.sql('SELECT id FROM support_replacement WHERE case_id=%s',(case,))[0]['id']
            if action=='lose_dispatch_ack':
                receipt=driver.admin('replacement-dispatch',case,{'trackingNo':'REMOTE-'+replacement})['id']
                driver.admin('receipt-apply',receipt)
            if action=='lose_release_ack':driver.admin('process',case)
            prefix={'lose_reserve_ack':'replacement-reserve:','lose_dispatch_ack':'replacement-dispatch:',
                    'lose_release_ack':'replacement-release:'}[action]
            command=prefix+replacement
        fault.arm(command,block_lookup=True)
        pending=driver.admin('inventory-retry',case,{'commandId':command})
        if pending['status']!='PENDING':raise AssertionError('fault did not leave original command unresolved')
        driver.remote_pending={'action':action,'commandId':command,'receiptId':receipt}
        driver.driver_evidence.append({'action':action,'command':pending,'source':'real upstream commit; POST and receipt lookup unavailable'})
        return
    state=driver.remote_pending
    if not state:raise RuntimeError('no unresolved inventory command')
    if action=='exhaust_receipt_retries':
        if not driver.config.get('supportRecoveryActive') or state['action']!='lose_inventory_ack':
            raise RuntimeError('retry exhaustion requires real receipt worker and unacknowledged return inventory')
        if not state['receiptId']:state['receiptId']=driver.admin('refund-success',case)['id']
        receipt_id=state['receiptId']
        original=driver.sql('SELECT * FROM support_receipt WHERE id=%s',(receipt_id,))[0]
        baseline=driver.snapshot();history=[];deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            row=driver.sql('SELECT * FROM support_receipt WHERE id=%s',(receipt_id,))[0]
            effect=driver.sql('SELECT status,attempts FROM support_stock_effect WHERE effect_id=%s',(state['commandId'],))[0]
            point={'receiptStatus':row['status'],'receiptAttempts':row['attempts'],'effectStatus':effect['status'],'effectAttempts':effect['attempts']}
            if not history or history[-1]!=point:
                history.append(point);print('receipt recovery: '+str(point),flush=True)
            if row['status']=='APPLIED':raise AssertionError('receipt applied without return inventory ACK')
            if row['status']=='NEEDS_REVIEW' and effect['status']=='NEEDS_REVIEW':
                if row['attempts']!=8 or effect['attempts']!=8:raise AssertionError('retry threshold not eight')
                for key in ('payload_json','request_hash','receipt_key','expected_version'):
                    if original[key]!=row[key]:raise AssertionError('worker changed original receipt '+key)
                current=driver.snapshot()
                for key in ('inventory_stock','order_line_allocation','support_order_claim'):
                    if current[key]!=baseline[key]:raise AssertionError('failed worker changed '+key)
                state['exhaustedReceipt']=row
                driver.driver_evidence.append({'action':action,'receiptId':receipt_id,'history':history,
                    'source':'real SupportReceiptRecovery; only this synthetic receipt/effect retry deadlines accelerated',
                    'originalPayloadPreserved':True,'financialStateUnchanged':True})
                return
            driver.sql("UPDATE support_receipt SET next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=%s AND status='PENDING'",(receipt_id,))
            driver.sql("UPDATE support_stock_effect SET next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE effect_id=%s AND status='PENDING'",(state['commandId'],))
            time.sleep(.25)
        raise TimeoutError('real receipt worker did not exhaust retries')
    if action=='manual_original_retry':
        original=state.get('exhaustedReceipt')
        if not original:raise RuntimeError('original receipt has not reached manual review')
        drive_remote(driver,'recover_inventory')
        row=driver.sql('SELECT * FROM support_receipt WHERE id=%s',(original['id'],))[0]
        for key in ('id','payload_json','request_hash','receipt_key','expected_version','attempts'):
            if row[key]!=original[key]:raise AssertionError('manual retry replaced original receipt '+key)
        if row['status']!='APPLIED':raise AssertionError('manual original retry not applied')
        driver.driver_evidence.append({'action':action,'receiptId':row['id'],'attempts':row['attempts'],'originalPayloadPreserved':True})
        return
    before=driver.snapshot()
    if action=='attempt_conversion':
        response=driver.java.post('/api/after-sales/'+case+'/conversion-preview')
        if response.status_code!=409 or driver.snapshot()!=before:
            raise AssertionError('conversion was not a nonmutating conflict')
        driver.driver_evidence.append({'action':action,'statusCode':409,'unchanged':True})
    elif action=='attempt_refund':
        receipt=driver.admin('refund-success',case)['id'];state['receiptId']=receipt
        try:driver.admin('receipt-apply',receipt)
        except RuntimeError as error:
            if '(409)' not in str(error):raise
        else:raise AssertionError('refund succeeded without inventory ACK')
        after=driver.snapshot()
        for key in ('inventory_stock','order_line_allocation','support_order_claim'):
            if before[key]!=after[key]:raise AssertionError('blocked refund changed '+key)
        driver.driver_evidence.append({'action':action,'receiptId':receipt,'statusCode':409,'financialStateUnchanged':True})
    elif action=='attempt_expiry':
        clock=driver.sql('SELECT version FROM support_scenario_clock WHERE order_id=%s',(driver.order,))[0]
        driver.admin('clock-advance',driver.order,{'expectedVersion':clock['version'],'seconds':2592001})
        driver.admin('process',case)
        if driver.case_read()['phase']!='REPLACEMENT_READY':raise AssertionError('unconfirmed dispatch reservation released')
        replacement=before['support_replacement'][0]['id']
        if driver.sql('SELECT command_id FROM inventory_command_journal WHERE command_id=%s',('replacement-release:'+replacement,)):
            raise AssertionError('release command created despite dispatch evidence')
        after=driver.snapshot()
        for key in ('inventory_stock','order_line_allocation','support_order_claim'):
            if before[key]!=after[key]:raise AssertionError('expiry changed protected '+key)
        driver.driver_evidence.append({'action':action,'dispatchEvidenceProtectedReservation':True})
    elif action=='recover_inventory':
        fault.recover(state['commandId'])
        if driver.admin('inventory-retry',case,{'commandId':state['commandId']})['status']!='ACK':
            raise AssertionError('original inventory command did not recover')
        driver.admin('process',case)
        if state['receiptId']:driver.admin('receipt-apply',state['receiptId'])
        driver.case_read();after=driver.snapshot()
        expected={'lose_reserve_ack':'REPLACEMENT_READY','lose_dispatch_ack':'REPLACEMENT_SHIPPED',
                  'lose_release_ack':'WAITING_CHOICE','lose_inventory_ack':'COMPLETED' if state['receiptId'] else 'REFUND_PENDING'}[state['action']]
        persisted=[row for row in after['support_case'] if row['id']==case]
        if len(persisted)!=1 or persisted[0]['phase']!=expected:raise AssertionError('recovery reached unexpected persisted phase')
        if after['inventory_stock']!=before['inventory_stock']:raise AssertionError('recovery duplicated inventory effect')
        driver.admin('inventory-retry',case,{'commandId':state['commandId']})
        if state['receiptId']:driver.admin('receipt-apply',state['receiptId'])
        if driver.snapshot()!=after:raise AssertionError('recovery replay mutated business state')
        driver.driver_evidence.append({'action':action,'commandId':state['commandId'],'phase':driver.case['phase'],'replayUnchanged':True})
        driver.remote_pending=None
