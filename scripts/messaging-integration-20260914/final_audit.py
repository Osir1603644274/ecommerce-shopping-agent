"""Read-only final audit of the deployed main project; emit no secrets."""
import hashlib,json,pathlib,urllib.request,zipfile,xml.etree.ElementTree as ET
import session as s

def read(name):return json.loads((s.OUT/name).read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def http(url):
    with urllib.request.urlopen(url,timeout=15) as r:return json.load(r)
checks={}; evidence={}
for name,key,query in [
 ('product-before.json','id','SELECT id,title,entity_version,lifecycle_status FROM product ORDER BY id'),
 ('orders-before-corrected.json','id','SELECT id,user_id,status,total_minor,payable_minor,updated_at FROM customer_order'),
 ('fulfillment-before-corrected.json','order_id','SELECT order_id,status,updated_at FROM fulfillment_task'),
 ('flash-orders-before.json','id','SELECT id,campaign_id,user_id,amount_minor,status FROM flash_sale_order ORDER BY id')]:
    old=read(name);current={r[key]:r for r in s.sql(query)}
    differences=[{'before':r,'after':current.get(r[key])} for r in old if current.get(r[key])!=r]
    evidence[name]={'original_rows':len(old),'differences':differences,'current_rows':len(current)}
    checks[name]=not differences
evidence['legacy_stream']=s.redis('XRANGE','stream.flash-sale-orders','-','+')
evidence['legacy_groups']=s.redis('XINFO','GROUPS','stream.flash-sale-orders')
checks['legacy_stream_unchanged']=evidence['legacy_stream']==read('legacy-stream-before.json')
checks['legacy_group_unchanged']=evidence['legacy_groups']==read('legacy-groups-before.json')
py=s.hashes(s.ROOT/'agent/app')|s.hashes(s.ROOT/'agent/tests');before=read('python-before.json')
evidence['python']={'before_count':len(before),'current_count':len(py),'changed':[p for p in before if before[p]!=py.get(p)],'added':sorted(set(py)-set(before))}
checks['python_preserved']=py==before
evidence['catalog']=http('http://127.0.0.1:8080/internal/catalog/manifest')['data']
checks['catalog_preserved']=evidence['catalog']==read('catalog-manifest-before.json')['data']
evidence['backend_health']=http('http://127.0.0.1:8080/actuator/health/readiness')
checks['backend_up']=evidence['backend_health']['status']=='UP'
ah=http('http://127.0.0.1:8000/health')
evidence['agent_health']={k:ah.get(k) for k in ['status','backendBaseUrl','catalogVersion']}
checks['agent_up']=ah.get('status') in ['ok','UP','healthy','运行中'] and ah.get('backendBaseUrl')=='http://127.0.0.1:8080'
with urllib.request.urlopen('http://127.0.0.1:5173/',timeout=15) as r:
    evidence['frontend_http']=r.status;checks['frontend_up']=r.status==200
oldinspect=json.loads((pathlib.Path(read('backup.json')['path'])/'container-inspect-private.json').read_text())
current=json.loads(s.run(['docker','inspect']+[r['Name'].lstrip('/') for r in oldinspect],text=True))
evidence['preserved_services']=[]
for old,new in zip(oldinspect,current):
    if old['Name']=='/local-life-backend':
        oldenv=dict(x.split('=',1) for x in old['Config']['Env']);newenv=dict(x.split('=',1) for x in new['Config']['Env'])
        ignored={'JAVA_VERSION','JAVA_HOME','PATH','LANG','LANGUAGE','LC_ALL'}
        changed=[k for k,v in oldenv.items() if k not in ignored and newenv.get(k)!=v]
        checks['existing_backend_environment_preserved']=not changed
        evidence['environment_changed_keys']=changed
        evidence['backend_image']={'name':new['Config']['Image'],'id':new['Image']}
    else:
        item={'name':new['Name'],'same_container':old['Id']==new['Id'],'same_mounts':sorted(old['Mounts'],key=lambda m:m['Destination'])==sorted(new['Mounts'],key=lambda m:m['Destination']),'running':new['State']['Running']}
        evidence['preserved_services'].append(item)
        checks['preserved'+new['Name']]=all(item[k] for k in ['same_container','same_mounts','running'])
jar=s.ROOT/'backend/target/local-life-backend-0.1.0-SNAPSHOT.jar'
runtime_sha=s.run(['docker','exec','local-life-backend','sha256sum','/app/app.jar'],text=True).split()[0]
with zipfile.ZipFile(jar) as z:lab=[p for p in z.namelist() if 'MessagingLab' in p]
evidence['jar']={'local_sha256':sha(jar),'runtime_sha256':runtime_sha,'lab_classes':lab}
checks['runtime_jar_matches']=sha(jar)==runtime_sha
checks['no_lab_classes']=not lab
evidence['flyway']=s.sql("SELECT version,description,success FROM flyway_schema_history WHERE version='20'")
checks['flyway_v20']=len(evidence['flyway'])==1 and evidence['flyway'][0]['success']=='1'
evidence['flash_pending']=s.sql("SELECT id,status FROM flash_sale_request WHERE status IN ('PENDING','COMPENSATING')")
evidence['cache_pending']=s.sql('SELECT id FROM cache_invalidation_outbox WHERE published_at IS NULL')
checks['flash_drained']=not evidence['flash_pending'];checks['cache_drained']=not evidence['cache_pending']
result=read('verification-113312/results.json');order=result['kafka_fulfillment']['order']
evidence['order_event_linkage']=s.sql(f"SELECT o.id AS event_id,o.event_type,o.status AS outbox_status,i.consumer_name,i.status AS inbox_status,i.processed_at FROM outbox_event o LEFT JOIN inbox_event i ON o.id=i.event_id WHERE o.aggregate_id='{order}' ORDER BY o.occurred_at,i.consumer_name")
evidence['fulfillment']=s.sql(f"SELECT order_id,status,tracking_no,receipt_json FROM fulfillment_task WHERE order_id='{order}'")
checks['fulfillment_shipped']=evidence['fulfillment'][0]['status']=='SHIPPED'
checks['event_consumed']=len(evidence['order_event_linkage'])==4 and all(r['outbox_status']=='PUBLISHED' and r['inbox_status']=='PROCESSED' for r in evidence['order_event_linkage']) and any(r['consumer_name']=='fulfillment-v1' and r['event_type']=='order.paid.v1' for r in evidence['order_event_linkage'])
tests=[]
for p in (s.OUT/'surefire-reports').rglob('TEST-*.xml'):
    root=ET.parse(p).getroot();tests.append({k:int(root.attrib.get(k,0)) for k in ['tests','failures','errors','skipped']})
evidence['tests']={k:sum(r[k] for r in tests) for k in ['tests','failures','errors','skipped']}
checks['tests_passed']=evidence['tests']=={'tests':290,'failures':0,'errors':0,'skipped':0}
checks['main_live_verification']=result['pass'] is True
s.write('final-audit.json',{'pass':all(checks.values()),'checks':checks,'evidence':evidence})
print(json.dumps({'checks':checks,'pass':all(checks.values())},ensure_ascii=False))
