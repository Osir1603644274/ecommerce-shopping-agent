"""Create owned acceptance database/runtime; never exercise candidate business rows."""
import gzip
import json
import secrets
import subprocess
import time
import pymysql
from import_catalog import ROOT, OUT, NAME, lab, VERSION
from prepare import cmd, write

DB='commerce_acceptance'
NETWORK='commerce-full-stage-20260915'
BACKEND='commerce-full-stage-java-20260915'


def main():
    secret=json.loads((OUT/'connection.private.json').read_text())
    info=json.loads(cmd('inspect',NAME))[0]
    assert info['Config']['Labels'].get('commerce.release')=='20260915-a1'
    db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password')},charset='utf8mb4',autocommit=True)
    with db.cursor() as c:
        c.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',(DB,))
        if c.fetchone():raise ValueError('acceptance DB exists; inspect and resume explicitly')
        c.execute('CREATE DATABASE commerce_acceptance CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    p=subprocess.Popen(['docker','exec','-i','-e','MYSQL_PWD='+secret['password'],NAME,'mysql','-uroot',DB],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    with gzip.open(OUT/'original.sql.gz','rb') as f:
        while b:=f.read(1024*1024):p.stdin.write(b)
    p.stdin.close()
    if p.wait():raise RuntimeError('acceptance restore failed')
    db.select_db(DB);db.autocommit(False)
    ddl=(ROOT/'backend/src/main/resources/db/migration/V21__external_commerce_catalog.sql').read_text(encoding='utf8')
    with db.cursor() as c:
        for sql in '\n'.join(s for s in ddl.splitlines() if not s.lstrip().startswith('--')).split(';'):
            if sql.strip():c.execute(sql)
    db.commit()
    rows=[json.loads(s) for s in (OUT.parent/'commerce-import-lab-20260914-attempt001/sample.jsonl').read_text(encoding='utf8').splitlines()]
    assert len(rows)==10000
    with db.cursor() as c:
        for start in range(0,len(rows),250):
            batch=rows[start:start+250]
            lab.insert_rows(c,'product',lab.products(batch))
            lab.insert_rows(c,'product_local_offer',[dict(product_id=r['id'],price_minor=r['localOffer']['priceMinor'],currency='CNY',price_kind='local_simulated',source_revision=VERSION,version=1) for r in batch])
            lab.insert_rows(c,'inventory_stock',[dict(item_type='PRODUCT',item_id=r['id'],total_quantity=10,available_quantity=10,reserved_quantity=0,sold_quantity=0,version=0) for r in batch])
            lab.insert_rows(c,'external_catalog_identity',[dict(product_id=r['id'],raw_sha=bytes.fromhex(r['provenance']['rawSha256']),source_line=r['provenance']['line'],source_revision=r['datasetRevision'],import_version=VERSION,preserved_existing=False) for r in batch])
            db.commit()
        password=secrets.token_hex(24)
        c.execute('CREATE USER %s@%s IDENTIFIED BY %s',('commerce_stage','%',password))
        c.execute('GRANT ALL PRIVILEGES ON commerce_acceptance.* TO %s@%s',('commerce_stage','%'))
    db.close()
    write('stage-connection.private.json',dict(host=secret['host'],port=secret['port'],user='commerce_stage',password=password,database=DB))
    cmd('network','create','--label','commerce.release=20260915-a1',NETWORK)
    cmd('network','connect','--alias','release-mysql',NETWORK,NAME)
    runtime()


def runtime():
    secret=json.loads((OUT/'stage-connection.private.json').read_text())
    assert secret['database']==DB
    password=secret['password']
    for name,image,port in [('commerce-full-stage-redis-20260915','redis:7-alpine','6379'),('commerce-full-stage-rabbit-20260915','rabbitmq:4.1-management','5672')]:
        if name in cmd('ps','-a','--format','{{.Names}}').decode().splitlines():
            info=json.loads(cmd('inspect',name))[0]
            assert info['Config']['Labels'].get('commerce.release')=='20260915-a1' and info['State']['Running']
            continue
        cmd('run','-d','--pull=never','--name',name,'--label','commerce.release=20260915-a1','--network',NETWORK,'--memory=512m','-p','127.0.0.1::'+port,image)
    redis=json.loads(cmd('inspect','commerce-full-stage-redis-20260915'))[0]
    write('stage-ports.json',dict(redis=int(redis['NetworkSettings']['Ports']['6379/tcp'][0]['HostPort']),java=19380,agent=8001,frontend=5174))
    image='agent-backend:commerce-full-20260915-v3'
    context=OUT/'image';context.mkdir(exist_ok=True)
    cmd('cp','commerce-full-build-20260915:/workspace/target/local-life-backend-0.1.0-SNAPSHOT.jar',str(context/'app.jar'))
    cmd('build','--pull=false','-t',image,'-f',str(ROOT/'scripts/commerce-full-release-20260915/Dockerfile'),str(context))
    original=json.loads(cmd('inspect','local-life-backend'))[0]
    env=dict(v.split('=',1) for v in original['Config']['Env'] if '=' in v)
    env.update(DB_HOST='release-mysql',DB_PORT='3306',DB_NAME=DB,DB_USER='commerce_stage',DB_PASSWORD=password,
        REDIS_HOST='commerce-full-stage-redis-20260915',REDIS_PORT='6379',RABBITMQ_HOST='commerce-full-stage-rabbit-20260915',
        MESSAGING_ENABLED='false',FLASH_SALE_ENABLED='false',SEARCH_ENABLED='false',
        PAYMENT_SIMULATOR_ENABLED='true',LOCAL_LIFE_DEMO_COMMERCE_ENABLED='true',
        LOCAL_LIFE_ORDER_EXPIRY_ENABLED='false',LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED='false',LOCAL_LIFE_FULFILLMENT_KAFKA_ENABLED='false',
        LOCAL_LIFE_SEARCH_RECONCILE_CATALOG_VERSION='merged-used-phone-439-20260909-v1',
        AGENT_BASE_URL='http://host.docker.internal:8001',TZ='UTC')
    if (OUT/'stage-fulfillment.json').exists():
        env.update(MESSAGING_ENABLED='true',KAFKA_BOOTSTRAP_SERVERS='commerce-full-stage-kafka-20260915:9092',
            LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED='true',LOCAL_LIFE_FULFILLMENT_KAFKA_ENABLED='true',
            LOCAL_LIFE_FULFILLMENT_DISPATCH_DELAY_SECONDS='5',
            LOCAL_LIFE_FULFILLMENT_WAREHOUSE_URL='http://commerce-full-stage-warehouse-20260915:19091',
            LOCAL_LIFE_FULFILLMENT_WAREHOUSE_TOKEN=(OUT/'stage-warehouse/warehouse.token').read_text().strip())
    args=['run','-d','--pull=never','--name',BACKEND,'--label','commerce.release=20260915-a1','--network',NETWORK,'--cpus=2','--memory=1536m','-p','127.0.0.1:19380:8080']
    for k,v in env.items():args+=['-e',k+'='+v]
    cmd(*args,image)
    write('STAGE.json',dict(status='STARTING',database=DB,backend=BACKEND,image=image,sampleRows=10000,candidateBusinessUntouched=True))
    print('Acceptance runtime started; private connection material saved locally.')


if __name__=='__main__':
    import sys
    if '--resume-runtime' in sys.argv:runtime()
    else:main()
