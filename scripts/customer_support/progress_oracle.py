"""Compare customer-visible progress with SQL and independent simulator receipts."""
import json
from business_oracle import _result

LOGISTICS={'READY':'尚未确认出库','SHIPPED':'尚未签收','RECEIVED':'已签收','UNKNOWN':'尚待核实','NEEDS_REVIEW':'人工核实','REFUND_HOLD':'暂停出库','WAITING_PAYMENT':'等待支付','DISPATCHING':'出库处理中','CANCELLED':'已取消'}
PHASES={'AWAITING_REVIEW':'等待审核','AWAITING_RETURN':'等待登记寄回','RETURN_IN_TRANSIT':'等待仓库收货','AWAITING_INSPECTION':'等待验收','REFUND_PENDING':'尚未确认到账','WAITING_STOCK':'等待确认换货库存','WAITING_CHOICE':'待选择','REPLACEMENT_READY':'尚未确认出库','REPLACEMENT_SHIPPED':'尚未签收','COMPLETED':'完成','REJECTED':'未通过','CANCELLED':'已撤销','NEEDS_REVIEW':'人工核实','REPLACEMENT_RELEASING':'核实库存释放'}

def grade_progress(scenario,observed,before,after):
    expected=scenario['expected']['routeOrOutcome'];trace=observed.get('trace') or {};result=trace.get('result') or {};answer=result.get('answer','')
    citations=result.get('citations',[]);order=before['customer_order'][0]
    checks=[('completed',trace.get('status')=='COMPLETED'),('read_only',before==after),('no_write_draft',not any(result.get(k) for k in ('preview','ticketDraft','actionDraft','ticketReplyDraft')))]
    if expected=='logistics:original_order':
        rows=before.get('fulfillment_task',[]);checks.append(('fulfillment_exists',len(rows)==1))
        if rows:
            row=rows[0];facts=[c.get('fields',{}) for c in citations if c.get('kind')=='logistics']
            fields={'orderId':order['id'],'status':row['status'],'trackingNo':row['tracking_no']}
            checks.append(('logistics_sql',len(facts)==1 and all(facts[0].get(k)==v for k,v in fields.items())))
            checks.append(('logistics_answer',LOGISTICS[row['status']] in answer))
            if row['tracking_no']:checks.append(('tracking_answer',row['tracking_no'] in answer))
            if row['status'] in {'SHIPPED','RECEIVED'}:
                event='RECEIVED' if row['status']=='RECEIVED' else 'DISPATCH'
                checks.append(('independent_logistics_receipt',any(r['status']=='APPLIED' and r['event_type']==event and json.loads(r['payload_json']).get('trackingNo')==row['tracking_no'] for r in before['support_order_receipt'])))
            checks.append(('no_promised_eta','没有承诺送达时间' in answer))
    else:
        replacement=expected=='logistics:replacement'
        cases=[c for c in before['support_case'] if not replacement or c['current_type']=='EXCHANGE']
        facts=[c.get('fields',{}) for c in citations if c.get('kind')=='after_sale']
        fields=[{'id':c['id'],'type':c['current_type'],'phase':c['phase'],'itemId':c['item_id'],'quantity':c['quantity'],'amountMinor':c['amount_minor'],'currency':c['currency'],'version':c['version']} for c in cases]
        checks.append(('aftersale_sql',len(facts)==1 and facts[0].get('orderId')==order['id'] and sorted(facts[0].get('cases',[]),key=lambda c:c['id'])==sorted(fields,key=lambda c:c['id'])))
        if not cases:checks.append(('absence_answer','没有查到' in answer))
        for case in cases:
            checks.append(('phase_answer',PHASES[case['phase']] in answer))
            checks.append(('quantity_answer',f"申请数量 {case['quantity']} 件" in answer))
            if case['current_type']!='EXCHANGE':
                amount=case['amount_minor'];money=f"{amount//100}.{amount%100:02d} {case['currency']}"
                checks.append(('refund_money_answer',('已退款 ' if case['phase']=='COMPLETED' else '申请金额 ')+money in answer))
                if case['phase']!='COMPLETED':checks.append(('not_paid','不代表已经到账' in answer))
            elif replacement and case['phase'] in {'REPLACEMENT_SHIPPED','COMPLETED'}:
                event='REPLACEMENT_RECEIPT_CONFIRMED' if case['phase']=='COMPLETED' else 'REPLACEMENT_DISPATCH_CONFIRMED'
                receipts=[r for r in before['support_receipt'] if r['case_id']==case['id'] and r['status']=='APPLIED' and r['event_type']==event]
                checks.append(('replacement_receipt',bool(receipts)))
                if receipts:
                    tracking=json.loads(receipts[-1]['payload_json']).get('trackingNo')
                    checks.append(('replacement_tracking',bool(tracking) and tracking in answer and any(c.get('kind')=='replacement_logistics' and c['fields'].get('caseId')==case['id'] and c['fields'].get('trackingNo')==tracking for c in citations)))
    return _result(checks,[] if before==after else ['unconfirmed_business_write'])
