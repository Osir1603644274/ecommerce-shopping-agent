"""Build a fresh empty-data topology from current schema, not from live business rows."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import hashlib
import httpx
import pymysql
from inventory_lab import ROOT,OUT,docker,write,NAMES

SCHEMAS={'trade':'micro_trade_e2e_20260915','catalog':'micro_catalog_e2e_20260915','inventory':'micro_inventory_e2e_20260915'}
CATALOG={'product','product_attribute','product_favorite','product_local_offer','product_search_projection_cursor',
         'shop','shop_type','review','review_projection_head','user_behavior','catalog_state','catalog_version_member',
         'external_catalog_identity','external_catalog_import_checkpoint','external_catalog_quarantine','cache_invalidation_outbox'}
SHARED={'outbox_event','inbox_event','dead_letter_event'}

def root_connection():
    saved=json.loads(Path('D:/agent-datasets/commerce-full-release-20260915-attempt001/connection.private.json').read_text())
    info=json.loads(docker('inspect',saved['container']))[0]
    assert info['Config']['Labels'].get('commerce.release')=='20260915-a1'
    saved['port']=int(info['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort'])
    return saved

def sql_file(c,path):
    # Migrations selected below contain plain DDL only, no delimiter/procedural SQL.
    text='\n'.join(s for s in path.read_text(encoding='utf8').splitlines() if not s.lstrip().startswith('--'))
    for sql in text.split(';'):
        if sql.strip():c.execute(sql)

def prepare():
    import sys
    private=OUT/'topology-lab.private.json'
    existing=json.loads(private.read_text()) if private.exists() else None
    if existing and existing.get('phase')=='READY':return existing
    root=root_connection()
    config={'schemas':SCHEMAS,'accounts':{r:{'user':'micro_e2e_'+r,'password':secrets.token_hex(24)} for r in SCHEMAS},
            'jwtSecret':secrets.token_hex(32),'internalToken':secrets.token_hex(32),'inventoryReadToken':secrets.token_hex(32),
            'inventoryWriteToken':secrets.token_hex(32),'paymentSecret':secrets.token_hex(32),'rootPort':root['port']}
    if existing:config=existing
    db=pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},autocommit=True,cursorclass=pymysql.cursors.DictCursor)
    resume=bool(existing) or '--resume-schema' in sys.argv
    if '--resume-schema' in sys.argv and not existing:
        assert (OUT/'lab-baseline-schema.sql').exists()
        with db.cursor() as c:
            for table in ('customer_order','user_account','inventory_command_journal'):
                c.execute('SELECT COUNT(*) AS n FROM '+SCHEMAS['trade']+'.'+table);assert c.fetchone()['n']==0
            for role in ('catalog','inventory'):
                c.execute('SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=%s',(SCHEMAS[role],));assert c.fetchone()['n']==0
        config['phase']='SCHEMA_RESTORED';write(private.name,config)
    if not resume:
      with db.cursor() as c:
        for schema in SCHEMAS.values():
            c.execute('SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s',(schema,))
            assert c.fetchone() is None,'Refuse unowned existing schema'
            c.execute('CREATE DATABASE '+schema+' CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
      # Dump structure only: no personal accounts, orders, payments or historic jobs copied.
      process=subprocess.run(['docker','exec','-e','MYSQL_PWD='+root['password'],root['container'],'mysqldump','-uroot',
                            '--no-data','--skip-lock-tables','--no-tablespaces','--set-gtid-purged=OFF',root['database']],capture_output=True)
      if process.returncode:raise RuntimeError('schema-only dump failed')
      (OUT/'lab-baseline-schema.sql').write_bytes(process.stdout)
      restored=subprocess.run(['docker','exec','-i','-e','MYSQL_PWD='+root['password'],root['container'],'mysql','-uroot',SCHEMAS['trade']],input=process.stdout,capture_output=True)
      if restored.returncode:raise RuntimeError('owned schema restore failed')
      config['phase']='SCHEMA_RESTORED';write(private.name,config)
    db.select_db(SCHEMAS['trade'])
    with db.cursor() as c:
        c.execute("SHOW TABLES LIKE 'inventory_command_journal'")
        if not c.fetchone():sql_file(c,ROOT/'backend/src/main/resources/db/migration/V22__distributed_inventory_journal.sql')
        for table,reference in [('order_line_allocation','inventory_stock'),('review','user_account')]:
            c.execute("SELECT constraint_name AS name FROM information_schema.key_column_usage WHERE table_schema=%s AND table_name=%s AND referenced_table_name=%s",(SCHEMAS['trade'],table,reference))
            for row in c.fetchall():c.execute('ALTER TABLE '+table+' DROP FOREIGN KEY `'+row['name']+'`')
        c.execute('SHOW TABLES');tables={next(iter(row.values())) for row in c.fetchall()}
        for table in sorted(CATALOG):
            if table in tables:c.execute('RENAME TABLE '+SCHEMAS['trade']+'.'+table+' TO '+SCHEMAS['catalog']+'.'+table)
        for table in SHARED:c.execute('CREATE TABLE IF NOT EXISTS '+SCHEMAS['catalog']+'.'+table+' LIKE '+SCHEMAS['trade']+'.'+table)
        for table in ('inventory_stock','inventory_reservation'):
            if table in tables:c.execute('RENAME TABLE '+SCHEMAS['trade']+'.'+table+' TO '+SCHEMAS['inventory']+'.'+table)
    db.select_db(SCHEMAS['inventory'])
    ddl=(ROOT/'backend-inventory/src/main/resources/db/migration/V1__owned_inventory.sql').read_text()
    with db.cursor() as c:
        for table in ('inventory_order_guard','inventory_command_receipt'):
            c.execute('SHOW TABLES LIKE %s',(table,))
            if not c.fetchone():
                sql=ddl[ddl.index('CREATE TABLE '+table):].split(';',1)[0];c.execute(sql)
        c.execute("SELECT constraint_name FROM information_schema.table_constraints WHERE table_schema=%s AND constraint_name='ck_owned_stock_balance'",(SCHEMAS['inventory'],))
        if not c.fetchone():c.execute('ALTER TABLE inventory_stock ADD CONSTRAINT ck_owned_stock_balance CHECK(total_quantity=available_quantity+reserved_quantity+sold_quantity)')
    # Seed two explicit local fixtures, keeping original product provenance out of mock facts.
    ids=[8000000000001001,8000000000001002]
    db.select_db(SCHEMAS['catalog'])
    with db.cursor() as c:
        for n,ident in enumerate(ids):
            c.execute('SELECT id FROM product WHERE id=%s',(ident,))
            if c.fetchone():continue
            c.execute("""INSERT INTO product(id,source,source_item_id,title,brand,seller,category_l1,category_l2,category_l3,
                snapshot_price_minor,currency,price_status,lifecycle_status,entity_version,attribute_text,data_nature,dataset_revision,source_license,provenance_url)
                VALUES(%s,'microservice_lab',%s,%s,'实验品牌','实验商户','电子','手机','二手手机',NULL,'CNY','unknown','ACTIVE',1,'isolated fixture','synthetic','20260915','local-test','')""",(ident,str(ident),'微服务隔离手机 '+str(n+1)))
            c.execute("INSERT INTO product_local_offer(product_id,price_minor,currency,price_kind,source_revision,version) VALUES(%s,%s,'CNY','local_simulated','microservices-lab',1)",(ident,100000 if n==0 else 200000))
            c.execute('INSERT INTO '+SCHEMAS['inventory']+".inventory_stock(item_type,item_id,total_quantity,available_quantity) VALUES('PRODUCT',%s,100,100)",(ident,))
        for role,account in config['accounts'].items():
            c.execute('CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s',(account['user'],'%',account['password']))
            c.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON '+SCHEMAS[role]+'.* TO %s@%s',(account['user'],'%'))
    db.close();config['products']=ids;config['phase']='READY'
    write(private.name,config)
    write('TOPOLOGY-LAB-SCHEMAS.json',{'schemas':SCHEMAS,'catalogTables':sorted(CATALOG),'sharedTableStructures':sorted(SHARED),
          'fixtureProductIds':ids,'sourceBusinessRowsCopied':0,'crossServiceForeignKeysRemoved':['order_line_allocation.stock_id','review.owner_user_id']})
    return config

def start_container(name,image,env,port,memory='512m'):
    names=docker('ps','-a','--format','{{.Names}}').splitlines()
    if name in names:
        info=json.loads(docker('inspect',name))[0]
        assert info['Config']['Labels'].get('microservices.attempt')=='20260915-a1'
        if not info['State']['Running']:docker('start',name)
    else:
        path=OUT/(name+'.env.private');path.write_text(''.join(k+'='+v+'\n' for k,v in env.items()),encoding='utf8')
        docker('run','-d','--pull=never','--name',name,'--label','microservices.attempt=20260915-a1','--network','agent_default',
               '--memory',memory,'--cpus=1','--pids-limit=256','--env-file',str(path),'-p','127.0.0.1::'+str(port),image)
    info=json.loads(docker('inspect',name))[0]
    url='http://127.0.0.1:'+info['NetworkSettings']['Ports'][str(port)+'/tcp'][0]['HostPort']
    for _ in range(120):
        try:
            if httpx.get(url+'/actuator/health',timeout=2,trust_env=False).status_code==200:return url
        except httpx.HTTPError:pass
        info=json.loads(docker('inspect',name))[0]
        if not info['State']['Running']:raise RuntimeError('service exited: '+name)
        time.sleep(1)
    raise RuntimeError('service not healthy: '+name)

def start():
    config=prepare()
    # Finished first-protocol lab remains recoverable; stop only these owned JVMs.
    for name in NAMES:
        info=json.loads(docker('inspect',name))[0]
        assert info['Config']['Labels'].get('microservices.attempt')=='20260915-a1'
        if info['State']['Running']:docker('stop','-t','10',name)
    jar=ROOT/'backend/target/local-life-backend-0.1.0-SNAPSHOT.jar'
    context=OUT/'trade-image';context.mkdir(exist_ok=True);shutil.copy2(jar,context/'app.jar')
    docker('build','--pull=false','-t','agent-backend:micro-20260915','-f',str(ROOT/'scripts/commerce-full-release-20260915/Dockerfile'),str(context))
    docker('build','--pull=false','-t','agent-inventory:micro-e2e-20260915',str(ROOT/'backend-inventory'))
    endpoints={}
    inventory_env={'INVENTORY_DB_URL':'jdbc:mysql://commerce-release-mysql:3306/'+SCHEMAS['inventory']+'?serverTimezone=UTC',
        'INVENTORY_DB_USER':config['accounts']['inventory']['user'],'INVENTORY_DB_PASSWORD':config['accounts']['inventory']['password'],
        'INVENTORY_READ_TOKEN':config['inventoryReadToken'],'INVENTORY_WRITE_TOKEN':config['inventoryWriteToken'],
        'INVENTORY_MIGRATE':'false','JAVA_TOOL_OPTIONS':'-Xms48m -Xmx160m -XX:MaxDirectMemorySize=32m -XX:ActiveProcessorCount=2'}
    endpoints['inventory']=start_container('micro-e2e-inventory-20260915','agent-inventory:micro-e2e-20260915',inventory_env,8083,'320m')
    redis_name='micro-e2e-redis-20260915'
    if redis_name not in docker('ps','-a','--format','{{.Names}}').splitlines():
        docker('run','-d','--pull=never','--name',redis_name,'--label','microservices.attempt=20260915-a1','--network','agent_default','--memory','96m','redis:7-alpine')
    base=dict(v.split('=',1) for v in json.loads(docker('inspect','commerce-full-live-java-20260915'))[0]['Config']['Env'] if '=' in v)
    base.update(DB_HOST='commerce-release-mysql',DB_PORT='3306',DB_POOL_MAX_SIZE='4',SPRING_DATASOURCE_HIKARI_MINIMUM_IDLE='1',
        SPRING_DATASOURCE_HIKARI_CONNECTION_TIMEOUT='2000',
        REDIS_HOST=redis_name,JWT_SECRET=config['jwtSecret'],INTERNAL_SERVICE_TOKEN=config['internalToken'],
        PAYMENT_CALLBACK_SECRET=config['paymentSecret'],PAYMENT_SIMULATOR_ENABLED='true',LOCAL_LIFE_DEMO_COMMERCE_ENABLED='true',
        FLASH_SALE_ENABLED='false',SEARCH_ENABLED='false',LOCAL_LIFE_ORDER_EXPIRY_ENABLED='false',
        LOCAL_LIFE_FULFILLMENT_ENABLED='true',LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED='false',LOCAL_LIFE_FULFILLMENT_KAFKA_ENABLED='false',
        MESSAGING_ENABLED='true',DOMAIN_EVENT_TOPIC='microservices-lab-events-20260915',
        MICRO_CATALOG_CONSUMER_GROUP='micro-lab-catalog-20260915',MICRO_TRADE_CONSUMER_GROUP='micro-lab-trade-20260915',
        LOCAL_LIFE_INVENTORY_SERVICE_URL='http://micro-e2e-inventory-20260915:8083',
        CATALOG_INSTANCE_A='http://micro-e2e-catalog-a-20260915:8080',CATALOG_INSTANCE_B='http://micro-e2e-catalog-b-20260915:8080',
        TZ='UTC',JAVA_TOOL_OPTIONS='-Xms64m -Xmx256m -XX:MaxDirectMemorySize=48m -XX:ActiveProcessorCount=2')
    for role in ('catalog','trade'):
        env={**base,'DB_NAME':SCHEMAS[role],'DB_USER':config['accounts'][role]['user'],'DB_PASSWORD':config['accounts'][role]['password'],
             'SPRING_DATASOURCE_URL':'jdbc:mysql://commerce-release-mysql:3306/'+SCHEMAS[role]+'?serverTimezone=UTC&sessionVariables=innodb_lock_wait_timeout=3',
             'SPRING_PROFILES_ACTIVE':'micro-catalog' if role=='catalog' else 'micro-trade,cloud-trade',
             'LOCAL_LIFE_DEPLOYMENT_ROLE':role,'LOCAL_LIFE_MESSAGING_CONSUMER_GROUP':'micro-e2e-'+role+'-20260915',
             'LOCAL_LIFE_INVENTORY_RECOVERY_ENABLED':'true' if role=='trade' else 'false',
             'LOCAL_LIFE_INVENTORY_SERVICE_TOKEN':config['inventoryReadToken' if role=='catalog' else 'inventoryWriteToken'],
             'SERVICE_INSTANCE_ID':role+'-a'}
        endpoints[role]=start_container('micro-e2e-'+role+'-a-20260915','agent-backend:micro-20260915',env,8080)
    write('TOPOLOGY-LAB-ENDPOINTS.json',{'urls':endpoints,'tradeJarSha256':hashlib.sha256(jar.read_bytes()).hexdigest()})
    print('Independent catalog/trade/inventory lab started; live 5173 unchanged.')

if __name__=='__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='prepare':prepare();print('Owned schemas prepared')
    else:start()
