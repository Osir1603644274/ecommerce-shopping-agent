"""Controlled selected-owner restart and reserve-before-effect outage acceptance."""
import json,time,uuid
import httpx,pymysql
from live_release import SCHEMAS,PRIVATE,NAMES,PORTS,owned,ensure_backend
from topology_lab import OUT,root_connection,write,docker

attempt=uuid.uuid4().hex[:10];report={'attempt':attempt,'steps':{}};config=json.loads(PRIVATE.read_text(encoding='utf8'))
saved=json.loads(max(OUT.glob('live-phones-session-*.private.json'),key=lambda p:p.stat().st_mtime).read_text(encoding='utf8'))
assert saved['account']['username'].startswith('micro-phones-')
client=httpx.Client(base_url='http://127.0.0.1:8080',timeout=20,trust_env=False)
auth=client.post('/api/auth/login',json=saved['account']);auth.raise_for_status();client.headers['Authorization']='Bearer '+auth.json()['data']['accessToken']
def record(name,value):report['steps'][name]=value;write('LIVE-RESTARTS-'+attempt+'.json',report)
def query(role,sql,args=()):
    root=root_connection();account=config['accounts'][role]
    with pymysql.connect(host=root['host'],port=root['port'],user=account['user'],password=account['password'],database=SCHEMAS[role],autocommit=True,
                         cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
        c.execute(sql,args);return c.fetchall()
def ready(role):
    for _ in range(90):
        try:
            if httpx.get('http://127.0.0.1:'+str(PORTS[role])+'/actuator/health',timeout=2,trust_env=False).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(1)
    raise AssertionError(role+' did not restart')
key='before-stock-'+attempt;body={'items':[{'itemType':'PRODUCT','itemId':320633,'quantity':1}]}
before=query('inventory',"SELECT * FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=320633")[0]
assert before['available_quantity']>0
name=NAMES['inventory'];owned(name);docker('stop','-t','15',name)
try:
    assert client.get('/api/orders/page').status_code==200
    response=client.post('/api/orders/cart',json=body,headers={'Idempotency-Key':key})
    record('reserveWhileOwnerDown',{'http':response.status_code,'body':response.json()})
    assert response.status_code in (500,502,503,504),'cannot report order success while inventory is unreachable'
    assert not query('trade','SELECT id FROM customer_order WHERE idempotency_key=%s',(key,))
finally:
    docker('start',name);ready('inventory')
retry=client.post('/api/orders/cart',json=body,headers={'Idempotency-Key':key});retry.raise_for_status();order=retry.json()['data']
repeat=client.post('/api/orders/cart',json=body,headers={'Idempotency-Key':key});repeat.raise_for_status();assert repeat.json()['data']['id']==order['id']
cancel=client.post('/api/orders/'+order['id']+'/cancel');cancel.raise_for_status()
for _ in range(60):
    stock=query('inventory',"SELECT * FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=320633")[0]
    if all(stock[k]==before[k] for k in ('available_quantity','reserved_quantity','sold_quantity')):break
    time.sleep(1)
else:raise AssertionError('stock not restored after original-key retry and cancel')
record('sameKeyRetryAfterInventoryRestart',{'orderId':order['id'],'stockBefore':before,'stockAfter':stock})
for role in ('trade-b','catalog-a'):
    previous=owned(NAMES[role])['State']['StartedAt'];docker('restart','-t','20',NAMES[role]);ready(role)
    assert owned(NAMES[role])['State']['StartedAt']!=previous
    result=client.get('/api/orders/'+order['id']);result.raise_for_status();assert result.json()['data']['status']=='CANCELLED'
    record('restart-'+role,{'before':previous,'after':owned(NAMES[role])['State']['StartedAt'],'orderReadable':True})
ensure_backend() # Actual selected-release launch path, no monolith fallback.
assert httpx.get('http://127.0.0.1:5173',trust_env=False).status_code==200
record('selectedLauncher','PASS')
report['status']='PASS';write('LIVE-RESTARTS-'+attempt+'.json',report);print(json.dumps({'status':'PASS','artifact':'LIVE-RESTARTS-'+attempt+'.json'}))
