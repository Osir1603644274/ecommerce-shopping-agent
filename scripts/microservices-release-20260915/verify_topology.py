"""HTTP + independent SQL, actual service boundaries. No live user credentials."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import time
import uuid
import httpx
import pymysql
from topology_lab import prepare,root_connection,SCHEMAS,OUT,docker,write

def main():
    config=prepare();urls=json.loads((OUT/'TOPOLOGY-LAB-ENDPOINTS.json').read_text())['urls']
    attempt=uuid.uuid4().hex[:10];report={'attempt':attempt,'scope':'isolated schemas, actual HTTP services, actual MySQL','steps':[]}
    def evidence(name,value):
        report['steps'].append({'name':name,'value':value});write('TOPOLOGY-ACCEPTANCE-'+attempt+'.json',report)
    def connection(role):
        account=config['accounts'][role]
        return pymysql.connect(host='127.0.0.1',port=root_connection()['port'],user=account['user'],password=account['password'],
            database=SCHEMAS[role],autocommit=True,cursorclass=pymysql.cursors.DictCursor)
    client=httpx.Client(base_url=urls.get('gateway',urls['trade']),timeout=15,trust_env=False)
    def call(method,path,body=None,key=None,base=None):
        headers={'Idempotency-Key':key} if key else {}
        response=client.request(method,(base+path) if base else path,json=body,headers=headers)
        if response.status_code>=400:
            evidence('unexpected-http',{'path':path,'status':response.status_code,'body':response.text[:1000]})
        response.raise_for_status();result=response.json();assert result['success'];return result['data']
    account={'username':'micro-e2e-'+attempt,'password':secrets.token_urlsafe(24)}
    auth=call('POST','/api/auth/register',account);client.headers['Authorization']='Bearer '+auth['accessToken']
    write('topology-session-'+attempt+'.private.json',{'account':account,'auth':auth})
    evidence('register',{'username':account['username']})
    ids=config['products']
    for ident in ids:
        detail=call('GET','/api/products/'+str(ident),base=urls['catalog'])
        offer=call('GET','/api/products/'+str(ident)+'/offer',base=urls['catalog'])
        assert str(detail['id'])==str(ident) and offer['canPurchase'];evidence('catalog-plus-remote-inventory',{'id':ident,'offer':offer})
    call('PUT','/api/product-favorites/'+str(ids[0]),base=urls['catalog'])
    assert len(call('GET','/api/product-favorites',base=urls['catalog']))==1
    request={'items':[{'itemType':'PRODUCT','itemId':ids[0],'quantity':2,'expectedUnitPriceMinor':100000},
                      {'itemType':'PRODUCT','itemId':ids[1],'quantity':1,'expectedUnitPriceMinor':200000}],'expectedPayableMinor':400000}
    key='cart-'+attempt
    order=call('POST','/api/orders/cart',request,key);evidence('multi-item-order',order)
    repeat=call('POST','/api/orders/cart',request,key);assert repeat['id']==order['id']
    payment=call('POST','/api/payments/orders/'+order['id']);evidence('payment',{'id':payment['id'],'status':payment['status']})
    paid=call('POST','/api/payments/'+payment['id']+'/simulate-success');assert paid['status']=='SUCCESS'
    # Wait for independent inventory confirmation; HTTP payment success is not stock proof.
    for _ in range(60):
        with connection('trade') as db,db.cursor() as c:
            c.execute('SELECT kind,status,command_id FROM inventory_command_journal WHERE order_id=%s ORDER BY sequence_id',(order['id'],));journal=c.fetchall()
        if journal and all(row['status']=='ACK' for row in journal):break
        time.sleep(1)
    else:raise AssertionError('inventory journal did not settle: '+str(journal))
    evidence('confirmed-command-journal',journal)
    refund=call('POST','/api/payments/orders/'+order['id']+'/partial-refunds',{'items':[{'itemId':ids[0],'quantity':1}],'reason':'microservices isolated acceptance'},'refund-'+attempt)
    refunded=call('POST','/api/payments/partial-refunds/'+refund['id']+'/simulate-success');assert refunded['status']=='SUCCESS'
    replay=call('POST','/api/payments/partial-refunds/'+refund['id']+'/simulate-success');assert replay==refunded
    for _ in range(60):
        with connection('inventory') as db,db.cursor() as c:
            c.execute('SELECT stock_id,quantity,refunded_quantity,status FROM inventory_reservation WHERE order_id=%s ORDER BY stock_id',(order['id'],));reservations=c.fetchall()
        if sum(row['refunded_quantity'] for row in reservations)==1:break
        time.sleep(1)
    else:raise AssertionError('remote refund did not settle')
    evidence('partial-refund-stock',{'refundId':refund['id'],'amountMinor':refunded.get('amountMinor'),'reservations':reservations})
    # Business rejection after the first remote reserve must compensate that reserve.
    failure_key='fail-'+attempt
    with connection('trade') as db,db.cursor() as c:
        c.execute('SELECT COALESCE(MAX(sequence_id),0) AS n FROM inventory_command_journal');before_failure=c.fetchone()['n']
    bad={'items':[{'itemType':'PRODUCT','itemId':ids[0],'quantity':1},{'itemType':'PRODUCT','itemId':ids[1],'quantity':1000}]}
    response=client.post('/api/orders/cart',json=bad,headers={'Idempotency-Key':failure_key})
    assert response.status_code==409,(response.status_code,response.text)
    with connection('trade') as db,db.cursor() as c:
        c.execute("SELECT DISTINCT order_id FROM inventory_command_journal WHERE sequence_id>%s ORDER BY order_id",(before_failure,));orphans=[r['order_id'] for r in c.fetchall()]
        c.execute('SELECT COUNT(*) AS n FROM customer_order WHERE idempotency_key=%s',(failure_key,));assert c.fetchone()['n']==0
    assert orphans,'expected durable intents even after order rollback'
    for _ in range(60):
        with connection('inventory') as db,db.cursor() as c:
            c.execute("SELECT order_id,status FROM inventory_reservation WHERE order_id IN ("+','.join(['%s']*len(orphans))+")",orphans);orphan_rows=c.fetchall()
        if orphan_rows and all(r['status']=='RELEASED' for r in orphan_rows):break
        time.sleep(1)
    else:raise AssertionError('rollback reserve not compensated')
    evidence('order-rollback-compensated',orphan_rows)
    denials=[]
    for role,other,table in [('trade','inventory','inventory_stock'),('trade','catalog','product'),('catalog','trade','customer_order'),('inventory','trade','customer_order')]:
        with connection(role) as db,db.cursor() as c:
            try:c.execute('SELECT * FROM '+SCHEMAS[other]+'.'+table+' LIMIT 1')
            except pymysql.err.OperationalError as error:
                assert error.args[0]==1142;denials.append({'role':role,'forbiddenOwner':other,'mysqlError':1142})
            else:raise AssertionError('cross-owner SQL unexpectedly allowed')
    evidence('database-ownership-denials',denials)
    anon=httpx.get(urls['trade']+'/api/orders/'+order['id'],trust_env=False).status_code;assert anon==401
    other=httpx.post(urls['trade']+'/api/auth/register',json={'username':'other-'+attempt,'password':secrets.token_urlsafe(24)},trust_env=False).json()['data']
    forbidden=httpx.get(urls['trade']+'/api/orders/'+order['id'],headers={'Authorization':'Bearer '+other['accessToken']},trust_env=False).status_code
    assert forbidden==403
    evidence('order-ownership',{'anonymous':anon,'otherUser':forbidden})
    with connection('inventory') as db,db.cursor() as c:
        c.execute('SELECT COUNT(*) AS n FROM inventory_stock WHERE total_quantity<>available_quantity+reserved_quantity+sold_quantity');assert c.fetchone()['n']==0
    assert httpx.get('http://127.0.0.1:5173/',trust_env=False).status_code==200
    report['status']='PASS';write('TOPOLOGY-ACCEPTANCE-'+attempt+'.json',report)
    print(json.dumps({'status':'PASS','evidence':'TOPOLOGY-ACCEPTANCE-'+attempt+'.json','orderId':order['id']}))

if __name__=='__main__':main()
