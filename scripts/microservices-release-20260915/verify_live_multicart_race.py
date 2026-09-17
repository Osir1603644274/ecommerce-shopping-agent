"""Dedicated test account, real legacy phones, multi-item refund and payment/cancel race."""
from concurrent.futures import ThreadPoolExecutor
import json,secrets,time,uuid
import httpx,pymysql
from live_release import SCHEMAS,PRIVATE
from topology_lab import OUT,root_connection,write

attempt=uuid.uuid4().hex[:10];report={'attempt':attempt,'scope':'same-host live owners; test account only','races':[]}
config=json.loads(PRIVATE.read_text(encoding='utf8'));base='http://127.0.0.1:8080'
client=httpx.Client(base_url=base,timeout=20,trust_env=False)
def call(method,path,body=None,key=None):
    r=client.request(method,path,json=body,headers={'Idempotency-Key':key} if key else {})
    r.raise_for_status();assert r.json()['success'];return r.json()['data']
def query(role,sql,args=()):
    root=root_connection();account=config['accounts'][role]
    with pymysql.connect(host=root['host'],port=root['port'],user=account['user'],password=account['password'],database=SCHEMAS[role],autocommit=True,
                         cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
        c.execute(sql,args);return c.fetchall()
def settled(order):
    for _ in range(60):
        j=query('trade','SELECT kind,status FROM inventory_command_journal WHERE order_id=%s',(order,))
        if j and all(r['status']=='ACK' for r in j):return j
        time.sleep(1)
    raise AssertionError(j)
account={'username':'micro-phones-'+attempt,'password':secrets.token_urlsafe(24)}
auth=call('POST','/api/auth/register',account);client.headers['Authorization']='Bearer '+auth['accessToken']
write('live-phones-session-'+attempt+'.private.json',{'account':account,'auth':auth})
phones=[]
for ident in (1710698,320633):
    data=call('GET','/api/products/'+str(ident)+'/purchase-view')
    assert data['offer']['canPurchase'] and data['offer']['available']>=5,data
    phones.append({'itemType':'PRODUCT','itemId':ident,'quantity':1,'expectedUnitPriceMinor':data['offer']['priceMinor']})
request={'items':phones,'expectedPayableMinor':sum(p['expectedUnitPriceMinor'] for p in phones)}
order=call('POST','/api/orders/cart',request,'phones-'+attempt)
assert len(order['items'])==2
assert call('POST','/api/orders/cart',request,'phones-'+attempt)['id']==order['id']
payment=call('POST','/api/payments/orders/'+order['id'])
assert call('POST','/api/payments/'+payment['id']+'/simulate-success')['status']=='SUCCESS'
settled(order['id'])
refund=call('POST','/api/payments/orders/'+order['id']+'/partial-refunds',{'items':[{'itemId':phones[0]['itemId'],'quantity':1}],'reason':'真实二手手机多商品回归'},'phone-refund-'+attempt)
done=call('POST','/api/payments/partial-refunds/'+refund['id']+'/simulate-success')
assert done['status']=='SUCCESS' and done['amountMinor']==phones[0]['expectedUnitPriceMinor']
assert call('POST','/api/payments/partial-refunds/'+refund['id']+'/simulate-success')==done
settled(order['id'])
report['multicart']={'order':order,'refund':done,'reservations':query('inventory','SELECT stock_id,quantity,refunded_quantity,status FROM inventory_reservation WHERE order_id=%s',(order['id'],))}
write('LIVE-PHONES-RACE-'+attempt+'.json',report)
for n in range(3):
    o=call('POST','/api/orders/cart',{'items':[phones[1]]},'race-'+attempt+'-'+str(n))
    p=call('POST','/api/payments/orders/'+o['id'])
    def operation(pay):
        origin='http://127.0.0.1:'+('18211' if pay else '18212')
        path='/api/payments/'+p['id']+'/simulate-success' if pay else '/api/orders/'+o['id']+'/cancel'
        r=httpx.post(origin+path,headers={'Authorization':client.headers['Authorization']},trust_env=False,timeout=20)
        return {'action':'pay' if pay else 'cancel','http':r.status_code,'body':r.json()}
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(operation,[True,False]))
    journal=settled(o['id']);current=call('GET','/api/orders/'+o['id'])
    reservations=query('inventory','SELECT status FROM inventory_reservation WHERE order_id=%s',(o['id'],))
    payments=query('trade','SELECT status FROM payment_record WHERE order_id=%s',(o['id'],))
    assert current['status'] in ('PAID','CANCELLED'),current
    if current['status']=='PAID':assert all(r['status']=='CONFIRMED' for r in reservations) and payments[0]['status']=='SUCCESS'
    else:assert all(r['status']=='RELEASED' for r in reservations) and payments[0]['status']!='SUCCESS'
    report['races'].append({'orderId':o['id'],'results':results,'orderStatus':current['status'],'payments':payments,'reservations':reservations,'journal':journal})
    write('LIVE-PHONES-RACE-'+attempt+'.json',report)
    # InvalidBusinessStateException is mapped to 422, conflict exceptions to 409.
    assert sum(r['http']==200 for r in results)==1 and all(r['http'] in (200,409,422) for r in results),results
assert query('inventory','SELECT COUNT(*) AS n FROM inventory_stock WHERE total_quantity<>available_quantity+reserved_quantity+sold_quantity')[0]['n']==0
report['status']='PASS';write('LIVE-PHONES-RACE-'+attempt+'.json',report)
print(json.dumps({'status':'PASS','artifact':'LIVE-PHONES-RACE-'+attempt+'.json'}))
