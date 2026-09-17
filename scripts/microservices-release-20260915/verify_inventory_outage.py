"""Bounded outage of attempt-owned inventory; never stop shared/live services."""
import json
import secrets
import time
import uuid
import httpx
import pymysql
from topology_lab import prepare,root_connection,SCHEMAS,OUT,docker,write,start_container

def main():
    config=prepare();urls=json.loads((OUT/'TOPOLOGY-LAB-ENDPOINTS.json').read_text())['urls']
    attempt=uuid.uuid4().hex[:10];report={'attempt':attempt,'checks':[]}
    def evidence(name,value):
        report['checks'].append({'name':name,'value':value});write('INVENTORY-OUTAGE-'+attempt+'.json',report)
    def query(role,sql,args=()):
        account=config['accounts'][role]
        with pymysql.connect(host='127.0.0.1',port=root_connection()['port'],user=account['user'],password=account['password'],
                database=SCHEMAS[role],autocommit=True,cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
            c.execute(sql,args);return c.fetchall()
    client=httpx.Client(base_url=urls['gateway'],timeout=15,trust_env=False)
    auth=client.post('/api/auth/register',json={'username':'outage-'+attempt,'password':secrets.token_urlsafe(24)})
    auth.raise_for_status();client.headers['Authorization']='Bearer '+auth.json()['data']['accessToken']
    body={'items':[{'itemType':'PRODUCT','itemId':config['products'][0],'quantity':1}]}
    response=client.post('/api/orders/cart',json=body,headers={'Idempotency-Key':'outage-'+attempt});response.raise_for_status()
    order=response.json()['data'];response=client.post('/api/payments/orders/'+order['id']);response.raise_for_status();payment=response.json()['data']
    name='micro-e2e-inventory-20260915';info=json.loads(docker('inspect',name))[0]
    assert info['Config']['Labels'].get('microservices.attempt')=='20260915-a1'
    stopped=False
    try:
        docker('stop','-t','3',name);stopped=True
        result=client.post('/api/payments/'+payment['id']+'/simulate-success');result.raise_for_status()
        assert result.json()['data']['status']=='SUCCESS'
        time.sleep(5)
        journal=query('trade','SELECT kind,status,attempts FROM inventory_command_journal WHERE order_id=%s ORDER BY sequence_id',(order['id'],))
        assert any(r['kind']=='CONFIRM' and r['status']=='PENDING' for r in journal),journal
        start=time.monotonic();read=client.get('/api/orders/'+order['id']);read.raise_for_status()
        assert read.json()['data']['status']=='PAID'
        evidence('payment-durable-while-inventory-down',{'orderId':order['id'],'journal':journal,'orderReadMs':round((time.monotonic()-start)*1000,2)})
        # Internal APIs must not become public through the Gateway.
        denied={path:client.get(path).status_code for path in ['/internal/catalog/manifest','/internal/inventory/stocks/PRODUCT/1','/actuator/env']}
        assert all(status in (401,403,404) for status in denied.values()),denied
        evidence('public-internal-boundary',denied)
    finally:
        if stopped:
            env=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
            urls['inventory']=start_container(name,info['Image'],env,8083,'320m')
            data=json.loads((OUT/'TOPOLOGY-LAB-ENDPOINTS.json').read_text());data['urls']=urls;write('TOPOLOGY-LAB-ENDPOINTS.json',data)
    for _ in range(90):
        journal=query('trade','SELECT kind,status,attempts FROM inventory_command_journal WHERE order_id=%s ORDER BY sequence_id',(order['id'],))
        if all(r['status']=='ACK' for r in journal):break
        time.sleep(1)
    else:raise AssertionError('inventory did not catch up after restart')
    rows=query('inventory','SELECT status,quantity FROM inventory_reservation WHERE order_id=%s',(order['id'],))
    assert rows and all(r['status']=='CONFIRMED' for r in rows),rows
    evidence('restart-replays-durable-confirm',{'journal':journal,'reservations':rows})
    # New inventory creation is idempotent and has a separate read credential.
    ident=8100000000000000+int(attempt[:8],16);stock={'itemType':'PRODUCT','itemId':ident,'quantity':3}
    read_header={'X-Inventory-Service-Token':config['inventoryReadToken']};write_header={'X-Inventory-Service-Token':config['inventoryWriteToken']}
    assert httpx.post(urls['inventory']+'/internal/inventory/stocks',json=stock,headers=read_header,trust_env=False).status_code==403
    first=httpx.post(urls['inventory']+'/internal/inventory/stocks',json=stock,headers=write_header,trust_env=False);first.raise_for_status()
    again=httpx.post(urls['inventory']+'/internal/inventory/stocks',json=stock,headers=write_header,trust_env=False);again.raise_for_status();assert first.json()==again.json()
    conflict=httpx.post(urls['inventory']+'/internal/inventory/stocks',json={**stock,'quantity':4},headers=write_header,trust_env=False)
    assert conflict.status_code==409
    evidence('stock-creation-identity',{'itemId':ident,'readTokenWriteStatus':403,'sameQuantitySameResult':True,'changedQuantityStatus':409})
    assert not query('inventory','SELECT id FROM inventory_stock WHERE total_quantity<>available_quantity+reserved_quantity+sold_quantity')
    assert httpx.get('http://127.0.0.1:5173/',trust_env=False).status_code==200
    report['status']='PASS';write('INVENTORY-OUTAGE-'+attempt+'.json',report);print(json.dumps({'status':'PASS','artifact':'INVENTORY-OUTAGE-'+attempt+'.json'}))

if __name__=='__main__':main()
