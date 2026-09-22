"""Real BFF-off probe on a new synthetic order in an explicitly owned runtime."""
import argparse
import json
from pathlib import Path
import uuid

from live_evaluation import LiveDriver, save
from managed_bff import ManagedBff


def run(runtime, output):
    output.mkdir(parents=True, exist_ok=False)
    config=json.loads((runtime/'private.json').read_text())
    with ManagedBff(18002, authority=config['authority'], support_agent_enabled=False) as bff:
        save(output/'BFF_SOURCE_HASHES.json',bff.source_hashes)
        driver=LiveDriver(runtime,bff.origin)
        try:
            driver.prepare({'fixture':{'kind':'refund_pending','unitPriceMinor':101,'purchasedQuantity':3,'requestedQuantity':1},
                            'expected':{'routeOrOutcome':'after_sale'}})
            before=driver.snapshot();save(output/'before.json',before)
            observed=driver.chat('查询退款进度',uuid.uuid4().hex)
            save(output/'disabled-chat.json',observed)
            assert observed['statusCode']==503 and observed['trace'] is None
            assert before==driver.snapshot()
            headers={'Origin':bff.origin}
            # Use the driver's same authenticated client and its rotated CSRF.
            order=driver.browser.get('/api/commerce-demo/orders/page',headers=headers)
            cases=driver.browser.get('/api/commerce-demo/workspace/support/orders/'+driver.order+'/cases',headers=headers)
            tickets=driver.browser.get('/api/commerce-demo/workspace/support/tickets',headers=headers)
            assert order.status_code==cases.status_code==tickets.status_code==200
            save(output/'existing-reads.json',{'orders':order.json(),'cases':cases.json(),'tickets':tickets.json()})
            case=driver.case_read()
            assert case['phase']=='REFUND_PENDING'
            receipt=driver.admin('refund-success',case['id'])
            pending=driver.snapshot();save(output/'pending-receipt.json',pending)
            assert pending['support_case'][0]['phase']=='REFUND_PENDING'
            driver.admin('receipt-apply',receipt['id'])
            final=driver.snapshot();save(output/'final.json',final)
            assert final['support_case'][0]['phase']=='COMPLETED'
            assert final['order_line_allocation'][0]['refunded_minor']==101
            driver.admin('receipt-apply',receipt['id'])
            assert final==driver.snapshot()
            save(output/'RESULT.json',{'status':'PASS','scope':'BFF Agent switch off; Java support remains enabled',
                'disabledChatStatus':503,'modelCalls':0,'ownedReadsStatus':200,'existingRefundRecovered':True,
                'sameReceiptReplayUnchanged':True,'bffSourceHashes':'BFF_SOURCE_HASHES.json'})
            print('PASS: disabled Agent rejected; existing order/case/ticket reads and original receipt recovery preserved.',flush=True)
        finally:
            driver.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();run(args.runtime,args.output)
