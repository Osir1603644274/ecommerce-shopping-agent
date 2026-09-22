"""Read-only order/payment assertions against separately captured SQL."""
from business_oracle import _result

ORDER_TERMS={'PENDING_PAYMENT':'等待支付','PAID':'已支付','COMPLETED':'订单已完成','CANCELLED':'订单已取消','EXPIRED':'订单已过期','REFUNDING':'退款处理中','REFUNDED':'订单已退款'}
PAYMENT_TERMS={'CREATED':'尚未支付成功','PENDING':'等待支付','SUCCESS':'支付成功','FAILED':'支付失败','CLOSED':'支付已关闭','CANCELLED':'支付已取消','REFUNDED':'已退款'}

def grade_query(scenario,observed,before,after):
    expected=scenario['expected']['routeOrOutcome'];trace=observed.get('trace') or {};result=trace.get('result') or {}
    answer=result.get('answer','');kind=expected
    checks=[('completed',trace.get('status')=='COMPLETED'),('read_only',before==after),
            ('no_write_draft',not any(result.get(k) for k in ('preview','ticketDraft','actionDraft','ticketReplyDraft')))]
    if expected=='payment_absent':
        order=before['customer_order'][0]
        facts=[c.get('fields',{}) for c in result.get('citations',[]) if c.get('kind')=='payment_absent']
        tools=[t for attempt in trace.get('attempts',[]) for t in attempt.get('toolReceipts',[])]
        checks += [('sql_payment_absent',not before['payment_record']),
                   ('absence_evidence',facts==[{'orderId':order['id'],'recordExists':False,'sourceStatusCode':404}]),
                   ('actual_not_found',any(t.get('method')=='GET' and t.get('path')=='/api/payments/orders/'+order['id'] and t.get('statusCode')==404 for t in tools)),
                   ('absence_explained','尚无支付记录' in answer and '不表示支付失败' in answer and '不能据此认定已经付款或退款到账' in answer)]
        return _result(checks,[] if before==after else ['unconfirmed_business_write'])
    citations=[c.get('fields',{}) for c in result.get('citations',[]) if c.get('kind')==kind]
    rows=before['customer_order'] if kind=='order' else before['payment_record']
    checks.append(('authoritative_record_exists',bool(rows)))
    if rows:
        row=rows[-1];amount_key='payable_minor' if kind=='order' else 'amount_minor'
        expected_fields={'id':row['id'],'status':row['status'],'currency':row['currency'],
                         'payableMinor' if kind=='order' else 'amountMinor':row[amount_key]}
        if kind=='payment':expected_fields.update(orderId=row['order_id'],version=row['version'])
        checks.append(('sql_facts_match',len(citations)==1 and all(citations[0].get(k)==v for k,v in expected_fields.items())))
        amount=row[amount_key];money=f"{amount//100}.{amount%100:02d} {row['currency']}"
        checks += [('answer_money',money in answer),('answer_status',(ORDER_TERMS if kind=='order' else PAYMENT_TERMS)[row['status']] in answer)]
        if kind=='payment':checks.append(('payment_not_refund','不代表售后退款已经到账' in answer))
    return _result(checks,[] if before==after else ['unconfirmed_business_write'])
