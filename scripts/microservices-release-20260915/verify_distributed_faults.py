"""Real JVM termination and dropped HTTP receipts, only in attempt-owned lab."""
from concurrent.futures import ThreadPoolExecutor
import json
import secrets
import time
import uuid
import httpx
import pymysql
from topology_lab import OUT,SCHEMAS,prepare,root_connection,docker,write,start_container

def main():
    config=prepare();urls=json.loads((OUT/'TOPOLOGY-LAB-ENDPOINTS.json').read_text())['urls']
    attempt=uuid.uuid4().hex[:10];report={'attempt':attempt,'scope':'real HTTP loss, actual JVM kill, independent SQL verification','checks':[]}
    def evidence(name,result):
        report['checks'].append({'name':name,'result':result});write('DISTRIBUTED-FAULTS-'+attempt+'.json',report)
    def query(role,sql,args=()):
        account=config['accounts'][role]
        with pymysql.connect(host='127.0.0.1',port=root_connection()['port'],user=account['user'],password=account['password'],
                database=SCHEMAS[role],autocommit=True,cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
            c.execute(sql,args);return c.fetchall()
    client=httpx.Client(timeout=15,trust_env=False)
    response=client.post(urls['tradeB']+'/api/auth/register',json={'username':'fault-'+attempt,'password':secrets.token_urlsafe(24)})
    response.raise_for_status();client.headers['Authorization']='Bearer '+response.json()['data']['accessToken']
    body={'items':[{'itemType':'PRODUCT','itemId':config['products'][0],'quantity':1}]}
    control=OUT/'fault-control'
    def arm(mode,kind='RESERVE'):
        assert not (control/'rule.json').exists(),'another armed experiment must finish first'
        key=attempt+'-'+mode
        (control/'rule.json').write_text(json.dumps({'id':key,'kind':kind,'mode':mode}))
        return key
    # Stock service commits; response is physically dropped; trade reads the exact receipt.
    fault=arm('drop');key='drop-'+attempt
    response=client.post(urls['tradeB']+'/api/orders/cart',json=body,headers={'Idempotency-Key':key})
    response.raise_for_status();order=response.json()['data']
    event=json.loads((control/('fired-'+fault+'.json')).read_text());assert event['upstreamStatus']==200
    assert event['orderId']==order['id']
    receipt=query('inventory','SELECT status,request_hash FROM inventory_command_receipt WHERE command_id=%s',(event['commandId'],))
    assert len(receipt)==1 and receipt[0]['status']=='APPLIED'
    repeat=client.post(urls['trade']+'/api/orders/cart',json=body,headers={'Idempotency-Key':key});repeat.raise_for_status()
    assert repeat.json()['data']['id']==order['id']
    evidence('reserve-committed-response-lost',{'orderId':order['id'],'sameKeyOtherInstanceSameOrder':True,'receipt':receipt,'fault':event})
    # Hold only after the stock commit, then terminate trade before it can persist the reply.
    fault=arm('hold');key='crash-'+attempt;name='micro-e2e-trade-b-20260915'
    info=json.loads(docker('inspect',name))[0];assert info['Config']['Labels'].get('microservices.attempt')=='20260915-a1'
    def request():
        try:return client.post(urls['tradeB']+'/api/orders/cart',json=body,headers={'Idempotency-Key':key}).status_code
        except httpx.HTTPError:return 'connection_lost'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(request);deadline=time.monotonic()+20;fired=control/('fired-'+fault+'.json')
        while not fired.exists() and time.monotonic()<deadline:time.sleep(.03)
        assert fired.exists(),'fault did not fire'
        event=json.loads(fired.read_text());docker('kill',name)
        client_result=future.result(timeout=20)
    orphan=event['orderId']
    assert not query('trade','SELECT id FROM customer_order WHERE id=%s',(orphan,)),'JVM must be killed before order commit'
    for _ in range(60):
        rows=query('inventory','SELECT status,quantity FROM inventory_reservation WHERE order_id=%s',(orphan,))
        if rows and all(r['status']=='RELEASED' for r in rows):break
        time.sleep(1)
    else:raise AssertionError('surviving trade did not compensate orphan reservation')
    evidence('trade-jvm-killed-after-stock-commit',{'orphanOrderId':orphan,'clientResult':client_result,'reservations':rows,
        'journal':query('trade','SELECT kind,status FROM inventory_command_journal WHERE order_id=%s',(orphan,))})
    env=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
    restored=start_container(name,info['Image'],env,8080,'512m')
    # Bound port may change on restart; persist the observed endpoint before further use.
    urls['tradeB']=restored;data=json.loads((OUT/'TOPOLOGY-LAB-ENDPOINTS.json').read_text());data['urls']=urls;write('TOPOLOGY-LAB-ENDPOINTS.json',data)
    response=client.post(restored+'/api/orders/cart',json=body,headers={'Idempotency-Key':key});response.raise_for_status()
    new=response.json()['data'];assert new['id']!=orphan
    evidence('restart-and-original-client-key-retry',{'newOrderId':new['id'],'rolledBackOrderNotReused':True})
    # Two independent JVMs are actually selected by the Gateway.
    headers=set()
    for _ in range(12):
        response=client.get(urls['gateway']+'/api/products/'+str(config['products'][0]));response.raise_for_status()
        headers.add(response.headers.get('X-Service-Instance'))
    assert headers=={'catalog-a','catalog-b'},headers
    evidence('gateway-two-catalog-instances',sorted(headers))
    catalog='micro-e2e-catalog-b-20260915';info=json.loads(docker('inspect',catalog))[0]
    assert info['Config']['Labels'].get('microservices.attempt')=='20260915-a1'
    docker('stop','-t','5',catalog)
    # Bounded discovery convergence is recorded, not hidden as zero failed requests.
    observed=[];deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        response=client.get(urls['gateway']+'/api/products/'+str(config['products'][0]));observed.append(response.status_code)
        if len(observed)>=6 and all(s==200 for s in observed[-6:]):break
        time.sleep(.5)
    assert len(observed)>=6 and all(s==200 for s in observed[-6:]),observed
    assert client.get(urls['gateway']+'/api/orders/'+order['id']).status_code==200
    evidence('catalog-instance-exit',{'observedHttpStatuses':observed,'orderReadStillAvailable':True})
    docker('start',catalog)
    assert not query('inventory','SELECT id FROM inventory_stock WHERE total_quantity<>available_quantity+reserved_quantity+sold_quantity')
    assert httpx.get('http://127.0.0.1:5173/',trust_env=False).status_code==200
    report['status']='PASS';write('DISTRIBUTED-FAULTS-'+attempt+'.json',report);print(json.dumps({'status':'PASS','artifact':'DISTRIBUTED-FAULTS-'+attempt+'.json'}))

if __name__=='__main__':main()
