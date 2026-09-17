"""Verify deployed production jars through real HTTP, MySQL, Redis, RocketMQ and RabbitMQ."""
import datetime,json,pathlib,secrets,subprocess,time,urllib.request,urllib.error,uuid
import session as s
RUN=s.OUT/('verification-'+datetime.datetime.now().strftime('%H%M%S'));RUN.mkdir()
PRIVATE=pathlib.Path(json.loads((s.OUT/'backup.json').read_text())['path'])
token=None; results={}
def record(kind,value):
    with (RUN/'raw.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'kind':kind,'value':value},ensure_ascii=False)+'\n')
def api(path,body=None,method=None,b=False,headers=None):
    url=f'http://127.0.0.1:{28083 if b else 8080}'+path
    h={'Content-Type':'application/json'}
    if token:h['Authorization']='Bearer '+token
    h.update(headers or {})
    req=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),headers=h,method=method)
    try:
        with urllib.request.urlopen(req,timeout=10) as r: value=json.load(r)
    except urllib.error.HTTPError as e:
        record('http-error',{'path':path,'code':e.code,'response':e.read().decode()});raise
    record('http',{'path':path,'instance':'b' if b else 'a','response':{'auth':'redacted'} if '/auth/' in path else value})
    return value.get('data',value)
def until(fn,seconds=60):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            value=fn()
            if value:return value
        except (urllib.error.URLError,OSError):pass
        time.sleep(.5)
    raise AssertionError('Condition did not converge')
def cmd(args):
    p=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
    record('command',{'argv':args,'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    if p.returncode:raise RuntimeError(p.stderr)
    return p.stdout
def q(sql):
    r=s.sql(sql);record('sql',{'query':sql,'rows':r});return r
def flash_state(cid):
    rows=q(f'SELECT * FROM flash_sale_request WHERE campaign_id={cid}')
    return rows[0] if rows and rows[0]['status']=='COMPLETED' else False
def check_flash(cid,user):
    request=until(lambda:flash_state(cid),90)
    orders=q(f'SELECT * FROM flash_sale_order WHERE campaign_id={cid}')
    c=q(f'SELECT total_stock,available_stock FROM flash_sale_campaign WHERE id={cid}')[0]
    assert len(orders)==1 and orders[0]['id']==request['id'] and orders[0]['user_id']==user and orders[0]['amount_minor']=='1234'
    assert request['mq_published_at']!='NULL' and not orders[0]['stream_message_id'].startswith('recovery:')
    assert c['available_stock']=='2' and s.redis('GET',f'flash:{{flash-sale}}:campaign:{cid}:stock')=='2'
    return {'request':request,'order':orders[0],'stock':c}
def campaign(product):
    now=datetime.datetime.now(datetime.timezone.utc)
    c=api('/api/flash-sales',{'itemType':'PRODUCT','itemId':product,'title':'MQ main integration '+str(uuid.uuid4())[:8],
      'salePriceMinor':1234,'totalStock':3,'startsAt':(now-datetime.timedelta(minutes=1)).isoformat(),'endsAt':(now+datetime.timedelta(hours=1)).isoformat()})
    api(f"/api/flash-sales/{c['id']}/activate",{})
    return c['id']
try:
    until(lambda:api('/actuator/health/readiness')['status']=='UP')
    username='mqmain-'+str(int(time.time()));password=secrets.token_urlsafe(24)
    registered=api('/api/auth/register',{'username':username,'password':password});user=registered['user']['id'];uuid.UUID(user)
    q(f"INSERT INTO user_role(user_id,role_name) VALUES('{user}','ADMIN')")
    token=api('/api/auth/login',{'username':username,'password':password})['accessToken']
    (PRIVATE/'verification-auth.json').write_text(json.dumps({'username':username,'password':password,'userId':user,'token':token}),encoding='utf-8')
    product=int(time.time()*1000)
    results.update(user=user,product=product)
    api('/api/admin/products',{'id':product,'source':'mq-main-integration','sourceItemId':str(product),'title':'mq-cache-v1','brand':'integration','seller':'local-fixture',
        'categoryL1':'test','categoryL2':'test','categoryL3':'test','snapshotPriceMinor':1234,'currency':'CNY','priceStatus':'verified',
        'attributeText':'local isolated verification','dataNature':'synthetic','datasetRevision':'mq-main-20260914','sourceLicense':'test','provenanceUrl':'https://example.test/local-fixture'})
    cid=campaign(product);r=api(f'/api/flash-sales/{cid}/purchase',{});results['normal']=check_flash(cid,user)
    again=api(f'/api/flash-sales/{cid}/purchase',{});assert again['orderId']==r['orderId']
    command={'orderId':r['orderId'],'campaignId':cid,'userId':user,'amountMinor':1234}
    cmd(['docker','exec','agent-rocketmq-1','sh','mqadmin','sendMessage','-n','nameserver:9876','-t','flash-sale-orders-v1','-p',json.dumps(command),'-k',r['orderId']])
    time.sleep(2);results['duplicate_and_one_per_user']=check_flash(cid,user)
    assert s.redis('SISMEMBER',f'flash:{{flash-sale}}:campaign:{cid}:buyers',user)==1
    assert s.redis('HGET',f'flash:{{flash-sale}}:campaign:{cid}:reservations',user)==r['orderId']
    print('PASS main RocketMQ normal / duplicate / Redis reservation',flush=True)

    cid2=campaign(product);cmd(['docker','stop','-t','2','agent-rocketmq-1'])
    accepted=api(f'/api/flash-sales/{cid2}/purchase',{});time.sleep(5)
    pending=q(f'SELECT * FROM flash_sale_request WHERE campaign_id={cid2}')[0]
    assert pending['status']=='PENDING' and pending['mq_published_at']=='NULL'
    cmd(['docker','start','agent-rocketmq-1']);results['broker_recovery']=check_flash(cid2,user)
    print('PASS main RocketMQ outage recovery',flush=True)

    d=json.loads(s.run(['docker','inspect','local-life-backend'],text=True))[0]
    env=dict(x.split('=',1) for x in d['Config']['Env'])
    env.update(FLASH_SALE_ENABLED='false',MESSAGING_ENABLED='false',SEARCH_ENABLED='false',LOCAL_LIFE_ORDER_EXPIRY_ENABLED='false',
       LOCAL_LIFE_FULFILLMENT_ENABLED='false',LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED='false',LOCAL_LIFE_FULFILLMENT_KAFKA_ENABLED='false',JAVA_TOOL_OPTIONS='-Xmx384m -XX:ActiveProcessorCount=2')
    envfile=PRIVATE/'cache-replica.env';envfile.write_text('\n'.join(k+'='+v for k,v in env.items()),encoding='utf-8')
    cmd(['docker','run','-d','--name','mq-main-cache-probe','--network','agent_default','--env-file',str(envfile),'-p','127.0.0.1:28083:8080',d['Image']])
    until(lambda:api('/actuator/health/readiness',b=True)['status']=='UP',90)
    until(lambda:all(x['published_at']!='NULL' for x in q(f'SELECT published_at FROM cache_invalidation_outbox WHERE entity_id={product}')))
    a=api(f'/api/products/{product}');b=api(f'/api/products/{product}',b=True);assert a['title']==b['title']=='mq-cache-v1'
    queues=cmd(['docker','exec','agent-rabbitmq-1','rabbitmqctl','list_queues','name','consumers'])
    assert sum(x.endswith('\t1') for x in queues.splitlines())==2
    start=time.monotonic();api(f'/api/admin/products/{product}',{'expectedVersion':1,'title':'mq-cache-v2'},method='PATCH')
    until(lambda:api(f'/api/products/{product}',b=True)['title']=='mq-cache-v2',10)
    assert api(f'/api/products/{product}')['title']=='mq-cache-v2' and time.monotonic()-start<30
    results['fanout']={'two_independent_queues':True,'seconds':time.monotonic()-start}
    cmd(['docker','stop','-t','2','agent-rabbitmq-1'])
    api(f'/api/admin/products/{product}',{'expectedVersion':2,'title':'mq-cache-v3'},method='PATCH');time.sleep(3)
    assert q(f'SELECT COUNT(*) AS n FROM cache_invalidation_outbox WHERE entity_id={product} AND published_at IS NULL')[0]['n']!='0'
    cmd(['docker','start','agent-rabbitmq-1'])
    until(lambda:api(f'/api/products/{product}',b=True)['title']=='mq-cache-v3',25)
    assert api(f'/api/products/{product}')['title']=='mq-cache-v3'
    results['rabbit_recovery']=True
    print('PASS main RabbitMQ two production jars / fanout / outage recovery',flush=True)
    cmd(['docker','stop','-t','2','mq-main-cache-probe'])

    q(f"INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity,reserved_quantity,sold_quantity,version) VALUES('PRODUCT',{product},5,5,0,0,0)")
    order=api('/api/orders',{'itemType':'PRODUCT','itemId':product,'quantity':1},headers={'Idempotency-Key':'mq-main-'+str(uuid.uuid4())})
    oid=order['id'];payment=api(f'/api/payments/orders/{oid}',{})
    api(f"/api/payments/{payment['id']}/simulate-success",{})
    fulfillment=until(lambda:(rows[0] if rows and rows[0]['status']=='SHIPPED' else False) if (rows:=q(f"SELECT * FROM fulfillment_task WHERE order_id='{oid}'")) else False,180)
    assert q(f"SELECT status FROM customer_order WHERE id='{oid}'")[0]['status']=='PAID'
    results['kafka_fulfillment']={'order':oid,'fulfillment':fulfillment}
    print('PASS main Kafka / payment simulator / warehouse fulfillment',flush=True)
    api(f'/api/admin/products/{product}?expectedVersion=3',method='DELETE')
    q(f"DELETE FROM user_role WHERE user_id='{user}' AND role_name='ADMIN'")
    q(f"UPDATE user_account SET enabled=false,token_version=token_version+1 WHERE id='{user}'")
    assert s.redis('XLEN','stream.flash-sale-orders')==452
    results['legacy_stream_preserved']=True;results['pass']=True
finally:
    (RUN/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Evidence:',RUN,flush=True)
