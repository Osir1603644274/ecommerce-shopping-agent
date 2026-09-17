"""Owned-schema, real-MySQL / two-JVM protocol tests. Never mutate live stock."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import time
import uuid
import httpx
import pymysql

ROOT=Path(__file__).resolve().parents[2]
OUT=Path('D:/agent-experiments/microservices-release-20260915-attempt001')
DB='micro_inventory_lab_20260915'
NAMES=['micro-inventory-lab-20260915-a','micro-inventory-lab-20260915-b']

def docker(*args):
    p=subprocess.run(['docker',*args],capture_output=True)
    if p.returncode:raise RuntimeError('docker operation failed: '+args[0]+' (arguments withheld)')
    return p.stdout.decode()

def write(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8')

def prepare():
    assert (OUT/'BASELINE.json').exists()
    private=OUT/'inventory-lab.private.json'
    if private.exists():return json.loads(private.read_text())
    original=json.loads(Path('D:/agent-datasets/commerce-full-release-20260915-attempt001/connection.private.json').read_text())
    info=json.loads(docker('inspect',original['container']))[0]
    assert info['Config']['Labels'].get('commerce.release')=='20260915-a1'
    binding=info['NetworkSettings']['Ports']['3306/tcp'][0]
    assert binding['HostIp']=='127.0.0.1'
    root=pymysql.connect(host='127.0.0.1',port=int(binding['HostPort']),user=original['user'],password=original['password'],autocommit=True)
    config={'host':'127.0.0.1','port':int(binding['HostPort']),'user':'micro_inventory_lab','password':secrets.token_hex(24),
            'database':DB,'readToken':secrets.token_hex(32),'writeToken':secrets.token_hex(32)}
    with root.cursor() as c:
        c.execute('SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s',(DB,))
        assert c.fetchone() is None,'Refuse to reuse an unowned schema'
        c.execute('CREATE DATABASE '+DB+' CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
        c.execute("CREATE USER 'micro_inventory_lab'@'%%' IDENTIFIED BY %s",(config['password'],))
        c.execute("GRANT ALL PRIVILEGES ON "+DB+".* TO 'micro_inventory_lab'@'%'")
    root.close();write(private.name,config)
    env={'INVENTORY_DB_URL':'jdbc:mysql://commerce-release-mysql:3306/'+DB+'?serverTimezone=UTC',
         'INVENTORY_DB_USER':config['user'],'INVENTORY_DB_PASSWORD':config['password'],
         'INVENTORY_READ_TOKEN':config['readToken'],'INVENTORY_WRITE_TOKEN':config['writeToken'],
         'INVENTORY_MIGRATE':'true','JAVA_TOOL_OPTIONS':'-Xms48m -Xmx160m -XX:MaxDirectMemorySize=32m -XX:ActiveProcessorCount=2'}
    (OUT/'inventory-lab.env.private').write_text(''.join(k+'='+v+'\n' for k,v in env.items()),encoding='utf8')
    return config

def connect(config):
    return pymysql.connect(**{k:config[k] for k in ('host','port','user','password','database')},autocommit=True,cursorclass=pymysql.cursors.DictCursor)

def start():
    config=prepare()
    jar=ROOT/'backend-inventory/target/inventory-service-0.1.0-SNAPSHOT.jar'
    assert jar.exists()
    docker('build','--pull=false','-t','agent-inventory:micro-20260915',str(ROOT/'backend-inventory'))
    urls=[]
    for name in NAMES:
        assert name not in docker('ps','-a','--format','{{.Names}}').splitlines(),'Existing lab container: inspect before reuse'
        docker('run','-d','--pull=never','--name',name,'--label','microservices.attempt=20260915-a1',
               '--network','agent_default','--memory=320m','--cpus=1','--pids-limit=160',
               '--env-file',str(OUT/'inventory-lab.env.private'),'-p','127.0.0.1::8083','agent-inventory:micro-20260915')
        info=json.loads(docker('inspect',name))[0]
        url='http://127.0.0.1:'+info['NetworkSettings']['Ports']['8083/tcp'][0]['HostPort']
        for _ in range(90):
            try:
                if httpx.get(url+'/actuator/health',timeout=2,trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(1)
        else:raise RuntimeError('inventory lab did not start: '+name)
        urls.append(url)
    write('inventory-lab-endpoints.json',{'urls':urls,'containers':NAMES,'jarSha256':hashlib.sha256(jar.read_bytes()).hexdigest()})
    return config,urls

def test(config,urls):
    attempt=uuid.uuid4().hex[:10];report={'attempt':attempt,'tests':[],'scope':'real MySQL, two independent JVMs; isolated schema only'}
    with connect(config) as db,db.cursor() as c:
        c.execute('SELECT COALESCE(MAX(item_id),0)+100 FROM inventory_stock');base=int(next(iter(c.fetchone().values())))
        for n in range(8):c.execute("INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity) VALUES('PRODUCT',%s,%s,%s)",(base+n,1 if n==6 else 10,1 if n==6 else 10))
    headers={'X-Inventory-Service-Token':config['writeToken']}
    def call(command,instance=0):
        r=httpx.post(urls[instance]+'/internal/inventory/commands',json=command,headers=headers,timeout=15,trust_env=False)
        return r.status_code,r.json()
    def command(kind,order,items=(),key=None):
        return dict(commandId=key or uuid.uuid4().hex,orderId=order,kind=kind,items=[{'itemType':'PRODUCT','itemId':i,'quantity':q} for i,q in items],expiresAt='2026-09-16T12:00:00' if kind=='RESERVE' else None)
    def check(name,condition,evidence):
        report['tests'].append({'name':name,'passed':bool(condition),'evidence':evidence})
        write('INVENTORY-PROTOCOL-'+attempt+'.json',report)
        assert condition,name
    order=uuid.uuid4().hex
    reserve=command('RESERVE',order,[(base,3),(base+1,2)])
    first=call(reserve)
    check('reserve two SKU atomically',first[0]==200 and first[1]['status']=='APPLIED',first)
    with ThreadPoolExecutor(max_workers=8) as pool:
        repeats=list(pool.map(lambda n:call(reserve,n%2),range(20)))
    check('same key twenty deliveries across two JVMs returns identical receipt',all(r==first for r in repeats),{'deliveries':len(repeats),'receipt':first[1]})
    changed={**reserve,'items':[{'itemType':'PRODUCT','itemId':base,'quantity':4}]}
    check('same key changed payload rejects',call(changed)[0]==409,{'status':409})
    confirmed=call(command('CONFIRM',order))
    check('confirm reserved inventory',confirmed[1]['status']=='APPLIED',confirmed)
    refund=command('REFUND',order,[(base,1)])
    refunded=call(refund);retry=call(refund,1)
    check('partial refund replay does not restore twice',refunded==retry and refunded[1]['status']=='APPLIED',refunded)
    too_many=call(command('REFUND',order,[(base,3)]))
    check('refund cannot exceed remaining sold quantity',too_many[1]['status']=='REJECTED',too_many)
    before=uuid.uuid4().hex
    released=call(command('RELEASE',before))
    late=call(command('RESERVE',before,[(base+2,1)]),1)
    check('release before delayed reserve fences resurrection',released[1]['status']=='APPLIED' and late[1]['status']=='REJECTED',late)
    failed=call(command('RESERVE',uuid.uuid4().hex,[(base+3,1),(base+4,11)]))
    with connect(config) as db,db.cursor() as c:
        c.execute('SELECT available_quantity,reserved_quantity FROM inventory_stock WHERE item_id=%s',(base+3,));unchanged=c.fetchone()
    check('second SKU insufficient rolls back first SKU',failed[1]['status']=='REJECTED' and unchanged=={'available_quantity':10,'reserved_quantity':0},unchanged)
    with ThreadPoolExecutor(max_workers=8) as pool:
        raced=list(pool.map(lambda n:call(command('RESERVE',uuid.uuid4().hex,[(base+6,1)]),n%2),range(20)))
    winners=[r for r in raced if r[0]==200 and r[1].get('status')=='APPLIED']
    check('twenty orders compete for last unit',len(winners)==1 and all(r[0]==200 for r in raced),{'applied':len(winners),'rejected':len(raced)-len(winners)})
    no_auth=httpx.get(urls[0]+'/internal/inventory/stocks/PRODUCT/'+str(base),trust_env=False).status_code
    reader={'X-Inventory-Service-Token':config['readToken']}
    read=httpx.get(urls[0]+'/internal/inventory/stocks/PRODUCT/'+str(base),headers=reader,trust_env=False).status_code
    denied=httpx.post(urls[0]+'/internal/inventory/commands',json=command('RELEASE',uuid.uuid4().hex),headers=reader,trust_env=False).status_code
    check('catalog read identity cannot mutate inventory',no_auth==403 and read==200 and denied==403,{'anonymous':no_auth,'catalogRead':read,'catalogWrite':denied})
    with connect(config) as db,db.cursor() as c:
        c.execute('SELECT COUNT(*) AS n FROM inventory_stock WHERE total_quantity<>available_quantity+reserved_quantity+sold_quantity OR LEAST(available_quantity,reserved_quantity,sold_quantity)<0');invalid=c.fetchone()['n']
        c.execute('SELECT item_id,available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_id BETWEEN %s AND %s ORDER BY item_id',(base,base+7));stocks=c.fetchall()
    check('independent SQL conservation',invalid==0 and stocks[0]['sold_quantity']==2,stocks)
    # A committed reply can be recovered after its service process disappears.
    docker('kill',NAMES[0])
    recovered=httpx.get(urls[1]+'/internal/inventory/commands/'+reserve['commandId'],headers=headers,timeout=5,trust_env=False).json()
    check('receipt survives JVM kill and is readable on second instance',recovered==first[1],recovered)
    docker('start',NAMES[0])
    report['status']='PASS';write('INVENTORY-PROTOCOL-'+attempt+'.json',report)
    print(json.dumps({'status':'PASS','checks':len(report['tests']),'evidence':'INVENTORY-PROTOCOL-'+attempt+'.json'}))

if __name__=='__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='test':
        test(prepare(),json.loads((OUT/'inventory-lab-endpoints.json').read_text())['urls'])
    else:
        config,urls=start();test(config,urls)
