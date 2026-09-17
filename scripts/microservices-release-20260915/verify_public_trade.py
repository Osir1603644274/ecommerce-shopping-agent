"""Public 5173 -> Agent -> Gateway -> owners, dedicated local-simulator account only."""
import json
import time
import uuid
import httpx
import pymysql
from live_release import SCHEMAS,PRIVATE
from topology_lab import ROOT,OUT,root_connection,write

def main():
    attempt=uuid.uuid4().hex[:10];report={'attempt':attempt,'steps':{}};steps=report['steps']
    latest=max(OUT.glob('micro-live-session-*.private.json'),key=lambda p:p.stat().st_mtime)
    saved=json.loads(latest.read_text(encoding='utf8'));assert saved['account']['username'].startswith('micro-live-')
    base='http://127.0.0.1:5173';workspace='/api/commerce-demo/workspace'
    client=httpx.Client(base_url=base,timeout=30,trust_env=False,headers={'Origin':base,'X-Conversation-Source':'automated_test','X-Backend-Observe':'1'})
    response=client.post('/api/commerce-demo/login',json=saved['account']);response.raise_for_status()
    me=response.json();client.headers['X-CSRF-Token']=me['csrfToken']
    saved.update(cookies=dict(client.cookies),csrf=me['csrfToken']);write('public-trade-session-'+attempt+'.private.json',saved)
    config=json.loads(PRIVATE.read_text(encoding='utf8'))
    def query(role,sql,args=()):
        account=config['accounts'][role];root=root_connection()
        with pymysql.connect(host=root['host'],port=root['port'],user=account['user'],password=account['password'],database=SCHEMAS[role],
                            autocommit=True,cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
            c.execute(sql,args);return c.fetchall()
    def record(name,value):steps[name]=value;write('PUBLIC-MICROSERVICES-TRADE-'+attempt+'.json',report)
    def call(method,path,body=None):
        response=client.request(method,path,json=body)
        if response.status_code>=400:record('unexpected-http',{'path':path,'status':response.status_code,'body':response.text[:1500]})
        response.raise_for_status()
        ticket=response.headers.get('X-Backend-Trace-Ticket')
        if ticket:
            trace=client.get('/api/commerce-demo/backend-traces/'+ticket);trace.raise_for_status()
            write('PUBLIC-TRACE-'+attempt+'-'+uuid.uuid4().hex[:6]+'.json',trace.json())
            calls=trace.json()['calls']
            if calls:
                assert all(c.get('serviceInstance') in ('catalog-a','catalog-b','trade-a','trade-b') for c in calls),calls
                assert all(c.get('detail') is not None for c in calls if c.get('traceId')),'issuing instance trace unavailable'
        return response.json()
    current=call('GET',workspace)
    product=next(c for c in current['cards'] if str(c['id'])=='4000000005633437' and c.get('purchasable'))
    ident=product['id'];before=query('inventory',"SELECT * FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=%s",(ident,))[0]
    assert before['available_quantity']>=3
    record('stockBefore',before)
    call('PUT',workspace+'/favorites/'+str(ident));record('favorite',call('GET',workspace+'/favorites'))
    call('POST',workspace+'/selection',{'productId':ident,'quantity':2})
    preview=call('POST',workspace+'/preview',{'productId':ident,'quantity':2});proposal=preview['checkout']['proposal'];record('orderProposal',proposal)
    result=call('POST',workspace+'/confirm',{'confirmationId':proposal['confirmationId']});record('orderOutcome',result['checkout']['outcome'])
    order=result['checkout']['outcome']['result'];assert order['status']=='PENDING_PAYMENT';record('order',order)
    replay=call('POST',workspace+'/confirm',{'confirmationId':proposal['confirmationId']});assert replay['checkout']['outcome']['result']['id']==order['id']
    pay=call('POST',workspace+'/payment-preview',{'orderId':order['id']})['checkout']['proposal']
    payment=call('POST',workspace+'/confirm',{'confirmationId':pay['confirmationId']})['checkout']['outcome']['result'];record('payment',payment)
    paid=call('POST','/api/commerce-demo/payments/'+payment['id']+'/simulate-success');record('paymentCallback',paid);assert paid['success'] and paid['data']['status']=='SUCCESS'
    for _ in range(45):
        journal=query('trade','SELECT kind,status FROM inventory_command_journal WHERE order_id=%s',(order['id'],))
        if journal and all(r['status']=='ACK' for r in journal):break
        time.sleep(1)
    else:raise AssertionError('payment inventory confirmation unresolved')
    record('confirmedJournal',journal)
    refund_preview=call('POST',workspace+'/refund-preview',{'orderId':order['id'],'items':[{'itemId':ident,'quantity':1}],'reason':'独立服务正式入口验证'})
    refund_proposal=refund_preview['checkout']['proposal'];refund=call('POST',workspace+'/confirm',{'confirmationId':refund_proposal['confirmationId']})['checkout']['outcome']['result'];record('refund',refund)
    result=call('POST',workspace+'/refunds/'+refund['id']+'/simulate-success');assert result['status']=='SUCCESS'
    assert call('POST',workspace+'/refunds/'+refund['id']+'/simulate-success')==result
    for _ in range(45):
        rows=query('inventory','SELECT status,quantity,refunded_quantity FROM inventory_reservation WHERE order_id=%s',(order['id'],))
        if rows and rows[0]['refunded_quantity']==1:break
        time.sleep(1)
    else:raise AssertionError('partial refund not applied to independent stock')
    stock=query('inventory',"SELECT * FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=%s",(ident,))[0]
    assert stock['available_quantity']==before['available_quantity']-1 and stock['sold_quantity']==before['sold_quantity']+1
    assert stock['reserved_quantity']==before['reserved_quantity'];record('partialRefundStock',{'before':before,'after':stock,'reservations':rows})
    record('afterSales',call('GET',workspace+'/orders/'+order['id']+'/after-sales'))
    page=call('GET','/api/commerce-demo/orders/page');record('orderPage',page)
    # A distinct second purchase is cancelled before payment; never cancel a real user's order.
    call('POST',workspace+'/selection',{'productId':ident,'quantity':1})
    proposal=call('POST',workspace+'/preview',{'productId':ident,'quantity':1})['checkout']['proposal']
    cancelled=call('POST',workspace+'/confirm',{'confirmationId':proposal['confirmationId']})['checkout']['outcome']['result']
    proposal=call('POST',workspace+'/cancel-preview',{'orderId':cancelled['id']})['checkout']['proposal']
    result=call('POST',workspace+'/confirm',{'confirmationId':proposal['confirmationId']})['checkout']['outcome']['result'];assert result['status']=='CANCELLED'
    for _ in range(45):
        rows=query('inventory','SELECT status FROM inventory_reservation WHERE order_id=%s',(cancelled['id'],))
        if rows and all(r['status']=='RELEASED' for r in rows):break
        time.sleep(1)
    else:raise AssertionError('cancelled order stock not released')
    record('cancelledOrder',{'orderId':cancelled['id'],'reservations':rows})
    report['status']='PASS';write('PUBLIC-MICROSERVICES-TRADE-'+attempt+'.json',report)
    write('PUBLIC-MICROSERVICES-LATEST.json',{'artifact':'PUBLIC-MICROSERVICES-TRADE-'+attempt+'.json','session':'public-trade-session-'+attempt+'.private.json'})
    print(json.dumps({'status':'PASS','artifact':'PUBLIC-MICROSERVICES-TRADE-'+attempt+'.json','orderId':order['id']}))

if __name__=='__main__':main()
