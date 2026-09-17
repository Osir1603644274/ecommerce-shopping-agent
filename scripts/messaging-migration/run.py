"""Real brokers, two independent Java processes. Run only with this isolated Compose project."""
import argparse, datetime, hashlib, json, pathlib, subprocess, time, urllib.request, urllib.error, uuid, traceback

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'scripts/messaging-migration/compose.yml'
OUT = ROOT / 'docs/messaging-migration/evidence' / datetime.datetime.now().strftime('run-%Y%m%d-%H%M%S')
OUT.mkdir(parents=True)
RESULTS=[]

def record(kind, value):
    with (OUT/'raw.jsonl').open('a',encoding='utf-8') as f:
        f.write(json.dumps({'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'kind':kind,'value':value},ensure_ascii=False,default=str)+'\n')

def docker(*args):
    command=['docker','compose','-f',str(COMPOSE),*args]
    p=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
    record('command',{'argv':command,'code':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    if p.returncode: raise RuntimeError(p.stderr)
    return p.stdout

def api(path, body=None, instance='a'):
    url=f'http://127.0.0.1:{28081 if instance=="a" else 28082}/lab{path}'
    req=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=8) as r: result=json.load(r)
        record('http',{'url':url,'body':body,'response':result})
        return result
    except Exception as e:
        record('http-error',{'url':url,'body':body,'error':repr(e)})
        raise

def until(fn, timeout=60):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        try:
            last=fn()
            if last: return last
        except (OSError,urllib.error.URLError): pass
        time.sleep(0.5)
    raise AssertionError(f'Timed out after {timeout}s; last={last}')

def ready(instance='a'): until(lambda:api('/ready',instance=instance),90)
def seed(): return api('/seed',{})
def state(s): return api('/state/'+str(s['campaign']))
def terminal(s,status='COMPLETED'):
    return until(lambda:(r if r['requests'] and r['requests'][0]['status']==status else False) if (r:=state(s)) else False,90)
def invariant(s, status='COMPLETED'):
    r=terminal(s,status)
    count=1 if status=='COMPLETED' else 0
    assert len(r['requests'])==1 and len(r['orders'])==count, r
    assert r['campaign']['available_stock']==3-count, r
    assert r['requests'][0]['user_id']==s['user'],r
    redis_stock=docker('exec','-T','redis','redis-cli','GET',f"flash:{{flash-sale}}:campaign:{s['campaign']}:stock").strip()
    assert int(redis_stock)==3-count,(r,redis_stock)
    record('invariant',r)
    return r

def normal():
    s=seed(); receipt=api('/purchase',s); r=invariant(s)
    assert r['orders'][0]['id']==receipt['orderId']
    assert not r['orders'][0]['stream_message_id'].startswith('recovery:'),r
    assert r['requests'][0]['mq_published_at'],r
    api('/duplicate/'+receipt['orderId'],{})
    time.sleep(3); invariant(s)
    return {'campaign':s['campaign'],'order':receipt['orderId'],'normal_and_duplicate':True}

def crash(point):
    s=seed(); api('/fault',{'point':point,'mode':'crash'})
    try: api('/purchase',s)
    except (OSError,urllib.error.URLError): pass
    until(lambda:'Exited (86)' in docker('ps','-a','a'),30)
    # SQL is queried externally while the Java process is dead.
    sql=docker('exec','-T','mysql','mysql','-uroot','-pisolated-migration-only','-D','migration_lab','-B','-e',
        f"SELECT * FROM flash_sale_request WHERE campaign_id={s['campaign']}; SELECT * FROM flash_sale_order WHERE campaign_id={s['campaign']}; SELECT * FROM lab_fault ORDER BY id;")
    record('crash-sql',sql)
    before=api('/state/'+str(s['campaign']),instance='b')
    assert len(before['requests'])==1,before
    if point=='before-send':
        assert before['requests'][0]['status']=='PENDING' and before['requests'][0]['mq_published_at'] is None and not before['orders'],before
    if point=='after-commit-before-ack':
        assert before['requests'][0]['status']=='COMPLETED' and len(before['orders'])==1,before
    docker('start','a'); ready(); r=invariant(s)
    assert any(x['point']==point and x['request_id']==r['requests'][0]['id'] for x in r['faults']),r
    if point=='after-commit-before-ack':
        r=until(lambda:(v if len([x for x in v['faults'] if x['point']==point and x['request_id']==v['requests'][0]['id']])>=2 else False) if (v:=state(s)) else False,90)
        invariant(s)
    return {'point':point,'campaign':s['campaign'],'state':r}

def broker_outage():
    s=seed(); docker('stop','-t','1','rocketmq')
    receipt=api('/purchase',s)
    time.sleep(7)
    r=state(s)
    assert r['requests'][0]['status']=='PENDING' and not r['orders'],r
    assert r['requests'][0]['mq_attempts']>0,r
    docker('start','rocketmq'); r=invariant(s)
    return {'campaign':s['campaign'],'state':r}

def dead_letter():
    s=seed(); api('/fault',{'point':'before-consume','mode':'fail'})
    receipt=api('/purchase',s)
    r=invariant(s,'DEAD')
    assert r['deadLetters'],r
    retries=[x for x in r['faults'] if x['request_id']==receipt['orderId'] and x['point']=='before-consume']
    assert len(retries)>=3,retries
    api('/fault',{'point':'clear'})
    api('/duplicate/'+receipt['orderId'],{})
    time.sleep(5); invariant(s,'DEAD')
    return {'campaign':s['campaign'],'attempts':len(retries),'state':r,'late_delivery_cannot_resurrect':True}

def sql_recovery():
    s=seed(); docker('stop','-t','1','rocketmq'); receipt=api('/purchase',s)
    api('/recover/'+receipt['orderId'],{})
    r=invariant(s)
    assert r['orders'][0]['stream_message_id'].startswith('recovery:'),r
    docker('start','rocketmq')
    return {'campaign':s['campaign'],'sql_fallback':True,'state':r}

def cache():
    product_id=int(time.time()*1000)
    api('/product',{'id':product_id,'source':'messaging-lab','sourceItemId':str(product_id),'title':'cache-v1','brand':'lab','seller':'lab',
        'categoryL1':'lab','categoryL2':'lab','categoryL3':'lab','snapshotPriceMinor':1234,'currency':'CNY','priceStatus':'verified',
        'attributeText':'isolated','dataNature':'synthetic','datasetRevision':'lab','sourceLicense':'test','provenanceUrl':'https://example.test/lab'})
    def read(instance): return api('/product/'+str(product_id),instance=instance)
    def update(version,title,rollback=False): return api('/product/'+str(product_id)+('?rollback=true' if rollback else ''),{'expectedVersion':version,'title':title})
    def both(title,timeout=12):
        start=time.monotonic()
        for instance in ('a','b'): until(lambda:read(instance)['title']==title,timeout)
        elapsed=time.monotonic()-start
        assert elapsed<30 # L1 TTL must not be the reason this test passes.
        return elapsed
    # Wait until initial creation notification is delivered before warming both L1s.
    until(lambda:all(r['published_at'] for r in api('/cache-outbox') if r['entity_id']==product_id))
    queues=docker('exec','-T','rabbitmq','rabbitmqctl','list_queues','name','consumers')
    assert sum(line.endswith('\t1') for line in queues.splitlines())==2,queues
    record('two-instance-queues',queues)
    both('cache-v1'); update(1,'cache-v2'); broadcast=both('cache-v2')
    api('/notify/'+str(product_id),{}); api('/notify/'+str(product_id),{}); both('cache-v2')
    before=len(api('/cache-outbox')); update(2,'rolled-back',True)
    after=len(api('/cache-outbox')); assert before==after,(before,after)
    both('cache-v2')
    api('/rabbit/stop',{},instance='b'); update(2,'cache-v3')
    until(lambda:read('a')['title']=='cache-v3',12)
    assert read('b')['title']=='cache-v2'
    api('/rabbit/start',{},instance='b'); reconnect=both('cache-v3')
    docker('stop','-t','1','b'); update(3,'cache-v4'); docker('start','b'); ready('b'); both('cache-v4')
    docker('stop','-t','1','rabbitmq'); update(4,'cache-v5')
    time.sleep(3)
    pending=[r for r in api('/cache-outbox') if r['entity_id']==product_id and r['published_at'] is None]
    assert pending,pending
    docker('start','rabbitmq'); both('cache-v5',25)
    return {'product':product_id,'broadcast_seconds':broadcast,'reconnect_seconds':reconnect,
            'two_process_fanout':True,'duplicate':True,'rollback_no_outbox':True,'consumer_restart':True,'broker_recovery':True}

SCENARIOS={'normal_duplicate':normal,'accept_crash':lambda:crash('after-accept'),
 'send_crash':lambda:crash('before-send'),'commit_ack_crash':lambda:crash('after-commit-before-ack'),
 'broker_outage':broker_outage,'dead_letter':dead_letter,'sql_recovery':sql_recovery,'cache':cache}

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--only',nargs='*',choices=SCENARIOS); args=p.parse_args()
    try:
        sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'backend/src').rglob('*') if p.is_file()}
        (OUT/'source-hashes.json').write_text(json.dumps(sources,indent=2),encoding='utf-8')
        record('containers',docker('ps','--format','json'))
        image=subprocess.run(['docker','image','inspect','local-life-messaging-lab:2103','--format','{{.Id}}'],capture_output=True,text=True,check=True).stdout.strip()
        record('image-id',image)
        ready(); ready('b')
        for name in args.only or SCENARIOS:
            print('RUN',name,flush=True)
            start=time.monotonic()
            try:
                detail=SCENARIOS[name](); RESULTS.append({'scenario':name,'pass':True,'seconds':time.monotonic()-start,'detail':detail})
                print('PASS',name,flush=True)
            except Exception:
                RESULTS.append({'scenario':name,'pass':False,'traceback':traceback.format_exc()})
                raise
            finally: (OUT/'results.json').write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    finally:
        record('container-logs',docker('logs','--no-color','--tail','300','a','b','rabbitmq','rocketmq'))
        print('Evidence:',OUT,flush=True)
