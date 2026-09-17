"""Guarded same-host service/data ownership cutover. No destructive rollback.

prepare: freezes deployment identities, does not touch live tables.
migrate: requires stopped live Java and a fresh frozen backup/restore PASS.
ensure-backend: only starts the explicitly selected independent services.
"""
import hashlib
import json
from pathlib import Path
import secrets
import socket
import sys
import time
import uuid
import httpx
import pymysql
from topology_lab import ROOT,OUT,CATALOG,SHARED,root_connection,docker,write,sql_file

STATE=ROOT/'.runtime/merged-commerce/microservices-release.json'
PRIVATE=OUT/'live-topology.private.json'
SCHEMAS={'trade':'commerce_candidate','catalog':'commerce_catalog_live_20260915','inventory':'commerce_inventory_live_20260915'}
NAMES={r:'micro-live-'+r+'-20260915' for r in ('inventory','catalog-a','catalog-b','trade-a','trade-b','gateway')}
PORTS={'inventory':18203,'catalog-a':18201,'catalog-b':18202,'trade-a':18211,'trade-b':18212,'gateway':8080}
CATALOG_EVENTS="(event_type LIKE 'product.search.%' OR event_type LIKE 'shop.%' OR event_type LIKE 'review.vector.%')"

def save(state):
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    write('LIVE-RELEASE-STATE.json',state)

def rebind_inventory_fk(cursor):
    """Refresh engine FK binding after cross-schema RENAME; keep checks enabled.

    Metadata alone was insufficient in the first live cutover: SHOW CREATE
    pointed to the new schema while a live INSERT still reported the old one.
    Rebuild only the small reservation table's constraint, never the stock table.
    """
    schema=SCHEMAS['inventory']
    cursor.execute('SELECT COUNT(*) AS n FROM '+schema+'.inventory_reservation r LEFT JOIN '+schema+'.inventory_stock s ON s.id=r.stock_id WHERE s.id IS NULL')
    assert cursor.fetchone()['n']==0,'orphan reservation blocks FK rebind'
    cursor.execute('ALTER TABLE '+schema+'.inventory_reservation '
                   'DROP FOREIGN KEY fk_inventory_reservation_stock, '
                   'ADD CONSTRAINT fk_owned_reservation_stock FOREIGN KEY(stock_id) REFERENCES '+schema+'.inventory_stock(id), ALGORITHM=COPY')

def repair_inventory_fk():
    state=json.loads(STATE.read_text(encoding='utf8'))
    assert state['status']=='SERVICES_READY' and state['schemas']==SCHEMAS
    assert json.loads((OUT/'FROZEN-LIVE-BACKUP.json').read_text(encoding='utf8'))['status']=='PASS'
    assert owned(NAMES['inventory'])['Image']==state['images']['inventory']
    root=root_connection()
    with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},autocommit=True,
                         cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
        c.execute('SET SESSION lock_wait_timeout=5')
        c.execute('SHOW CREATE TABLE '+SCHEMAS['inventory']+'.inventory_reservation');before=c.fetchone()
        rebind_inventory_fk(c)
        c.execute('SHOW CREATE TABLE '+SCHEMAS['inventory']+'.inventory_reservation');after=c.fetchone()
    write('LIVE-FK-REBIND.json',{'status':'APPLIED_REQUIRES_WRITE_PROBE','before':before,'after':after,
          'note':'Atomic constraint replacement with checks enabled; no business row deletion or stock adjustment.'})
    print('Inventory FK rebound; run rolled-back insertion checks next.')

def verify_inventory_write_binding(root):
    """Readiness must test engine enforcement, not just SELECT or metadata."""
    with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},database=SCHEMAS['inventory']) as db,db.cursor() as c:
        c.execute('SELECT id FROM inventory_stock ORDER BY id LIMIT 1');row=c.fetchone()
        assert row,'selected inventory must contain migrated stock'
        for stock,expected in ((row[0],True),(-987654321,False)):
            try:
                c.execute("INSERT INTO inventory_reservation(id,order_id,stock_id,quantity,status,expires_at) VALUES(%s,%s,%s,1,'RESERVED',DATE_ADD(NOW(),INTERVAL 5 MINUTE))",
                          (str(uuid.uuid4()),str(uuid.uuid4()),stock))
                assert expected,'missing stock unexpectedly accepted'
            except pymysql.IntegrityError as error:
                assert not expected and error.args[0]==1452,'live FK binding failed for an existing stock'
            finally:db.rollback()

def owned(name):
    info=json.loads(docker('inspect',name))[0]
    assert info['Config']['Labels'].get('microservices.attempt')=='20260915-a1',name
    return info

def prepare():
    if PRIVATE.exists():return json.loads(PRIVATE.read_text())
    for file in ('DISTRIBUTED-FAULTS-ecc9c24b99.json','TOPOLOGY-ACCEPTANCE-9de289e251.json','INVENTORY-OUTAGE-bd32dc4138.json','INDEPENDENT-SEARCH-ACCEPTANCE-682655385c.json'):
        assert json.loads((OUT/file).read_text())['status']=='PASS',file
    for role,port in PORTS.items():
        if role=='gateway':continue # The explicitly selected monolith still owns 8080.
        with socket.socket() as s:assert s.connect_ex(('127.0.0.1',port))!=0,'port already in use: '+str(port)
    live=json.loads(docker('inspect','commerce-full-live-java-20260915'))[0]
    assert live['Config']['Labels'].get('commerce.release')=='20260915-a1'
    base=dict(v.split('=',1) for v in live['Config']['Env'] if '=' in v)
    config={'schemas':SCHEMAS,'accounts':{role:{'user':'micro_live_'+role,'password':secrets.token_hex(24)} for role in SCHEMAS},
            'internalToken':secrets.token_hex(32),'inventoryReadToken':secrets.token_hex(32),'inventoryWriteToken':secrets.token_hex(32),
            'baseEnvironment':base,'oldContainer':live['Name'].lstrip('/'),'oldImage':live['Image'],
            'images':{'backend':json.loads(docker('image','inspect','agent-backend:micro-20260915-006'))[0]['Id'],
                      'inventory':json.loads(docker('image','inspect','agent-inventory:micro-e2e-20260915-003'))[0]['Id'],
                      'gateway':json.loads(docker('image','inspect','agent-gateway:micro-lab-20260915'))[0]['Id']}}
    assert len(base.get('JWT_SECRET',''))>=32
    write(PRIVATE.name,config)
    token=ROOT/'.runtime/merged-commerce/secrets/catalog-internal.token';assert not token.exists()
    token.write_text(config['internalToken']+'\n',encoding='ascii')
    write('LIVE-TOPOLOGY-PREPARED.json',{'status':'PREPARED_NOT_MIGRATED','schemas':SCHEMAS,'containers':NAMES,'ports':PORTS,'images':config['images']})
    return config

def migrate():
    config=prepare();backup=json.loads((OUT/'FROZEN-LIVE-BACKUP.json').read_text())
    assert backup['status']=='PASS' and backup['restoredAllCountsMatch'] and backup['restoredSmallTableContentsMatch']
    old=json.loads(docker('inspect',config['oldContainer']))[0];assert not old['State']['Running'] and old['Image']==config['oldImage']
    if STATE.exists():
        state=json.loads(STATE.read_text());assert state['status'] in ('MIGRATING','MIGRATED_NOT_STARTED'),state['status']
        if state['status']=='MIGRATED_NOT_STARTED':return
    else:
        state={'status':'MIGRATING','schemas':SCHEMAS,'containers':NAMES,'ports':PORTS,'images':config['images'],
            'backupArtifact':'FROZEN-LIVE-BACKUP.json','evidenceDirectory':str(OUT),'oldContainer':config['oldContainer'],
            'catalogUrls':['http://127.0.0.1:'+str(PORTS[r]) for r in ('catalog-a','catalog-b')],
            'observerInstances':{r:'http://127.0.0.1:'+str(PORTS[r]) for r in ('catalog-a','catalog-b','trade-a','trade-b')}}
        save(state) # Restart helper must refuse the monolith from this point onward.
    root=root_connection()
    with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},database=SCHEMAS['trade'],autocommit=True,read_timeout=1800,
                         cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
        c.execute('SHOW TABLES');existing={next(iter(row.values())) for row in c.fetchall()}
        if 'product' in existing:
            # All business contents were frozen; verify before changing ownership.
            for table,expected in backup['smallTableHashes'].items():
                if table=='inventory_command_journal':continue
                c.execute('SELECT * FROM `'+table+'`')
                rows=sorted(json.dumps(tuple(row.values()),default=str,ensure_ascii=False) for row in c.fetchall())
                assert hashlib.sha256('\n'.join(rows).encode()).hexdigest()==expected,'frozen source changed: '+table
        for role in ('catalog','inventory'):
            schema=SCHEMAS[role]
            c.execute('CREATE DATABASE IF NOT EXISTS '+schema+' CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
            c.execute('SELECT table_name AS name FROM information_schema.tables WHERE table_schema=%s',(schema,))
            permitted=CATALOG|SHARED if role=='catalog' else {'inventory_stock','inventory_reservation','inventory_order_guard','inventory_command_receipt'}
            assert {r['name'] for r in c.fetchall()}<=permitted,'unexpected destination tables'
        if 'inventory_command_journal' not in existing:sql_file(c,ROOT/'backend/src/main/resources/db/migration/V22__distributed_inventory_journal.sql')
        for table,parent in [('order_line_allocation','inventory_stock'),('review','user_account')]:
            if table not in existing:continue
            c.execute('SELECT constraint_name AS name FROM information_schema.key_column_usage WHERE table_schema=%s AND table_name=%s AND referenced_table_name=%s',(SCHEMAS['trade'],table,parent))
            for row in c.fetchall():c.execute('ALTER TABLE `'+table+'` DROP FOREIGN KEY `'+row['name']+'`')
        moves=[]
        for table in sorted(CATALOG|{'inventory_stock','inventory_reservation'}):
            if table in existing:
                role='catalog' if table in CATALOG else 'inventory'
                moves.append(SCHEMAS['trade']+'.`'+table+'` TO '+SCHEMAS[role]+'.`'+table+'`')
        if moves:c.execute('RENAME TABLE '+', '.join(moves)) # One atomic metadata change, IDs unchanged.
        if moves:rebind_inventory_fk(c)
        for table in SHARED:
            c.execute('CREATE TABLE IF NOT EXISTS '+SCHEMAS['catalog']+'.'+table+' LIKE '+SCHEMAS['trade']+'.'+table)
            # Copy only catalog-owned event history and unsettled work. Original audit rows remain preserved.
            c.execute('INSERT IGNORE INTO '+SCHEMAS['catalog']+'.'+table+' SELECT * FROM '+SCHEMAS['trade']+'.'+table+' WHERE '+CATALOG_EVENTS)
            c.execute('SELECT COUNT(*) AS n FROM '+SCHEMAS['trade']+'.'+table+' WHERE '+CATALOG_EVENTS);expected=c.fetchone()['n']
            c.execute('SELECT COUNT(*) AS n FROM '+SCHEMAS['catalog']+'.'+table);assert c.fetchone()['n']==expected,table
        # Historical published catalog Outbox rows remain audit-only in trade. Never duplicate pending publishers.
        c.execute('SELECT COUNT(*) AS n FROM '+SCHEMAS['trade']+'.outbox_event WHERE published_at IS NULL AND '+CATALOG_EVENTS)
        assert c.fetchone()['n']==0,'pending catalog outbox must drain before cutover'
        db.select_db(SCHEMAS['inventory'])
        ddl=(ROOT/'backend-inventory/src/main/resources/db/migration/V1__owned_inventory.sql').read_text()
        for table in ('inventory_order_guard','inventory_command_receipt'):
            c.execute('SHOW TABLES LIKE %s',(table,))
            if not c.fetchone():c.execute(ddl[ddl.index('CREATE TABLE '+table):].split(';',1)[0])
        c.execute("SELECT constraint_name FROM information_schema.table_constraints WHERE table_schema=%s AND constraint_name='ck_owned_stock_balance'",(SCHEMAS['inventory'],))
        if not c.fetchone():c.execute('ALTER TABLE inventory_stock ADD CONSTRAINT ck_owned_stock_balance CHECK(total_quantity=available_quantity+reserved_quantity+sold_quantity)')
        # Create one DML-only principal for each owner. No shared account or cross-schema grants.
        for role,account in config['accounts'].items():
            c.execute('CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s',(account['user'],'%',account['password']))
            c.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON '+SCHEMAS[role]+'.* TO %s@%s',(account['user'],'%'))
        for table,count in backup['tableCounts'].items():
            role='catalog' if table in CATALOG else 'inventory' if table in {'inventory_stock','inventory_reservation'} else 'trade'
            c.execute('SELECT COUNT(*) AS n FROM '+SCHEMAS[role]+'.`'+table+'`');assert c.fetchone()['n']==count,table
    state.update(status='MIGRATED_NOT_STARTED',migratedAtUnix=time.time(),allOriginalTableCountsPreserved=True)
    save(state);print('Owned schemas migrated with original IDs and all original row counts preserved.')

def run(role,image,env,port,memory):
    name=NAMES[role]
    if name not in docker('ps','-a','--format','{{.Names}}').splitlines():
        private=OUT/(name+'.env.private');private.write_text(''.join(k+'='+str(v)+'\n' for k,v in env.items()),encoding='utf8')
        docker('run','-d','--pull=never','--name',name,'--label','microservices.attempt=20260915-a1','--network','agent_default',
               '--memory',memory,'--cpus','1','--pids-limit','256','--env-file',str(private),'-p','127.0.0.1:'+str(PORTS[role])+':'+str(port),image)
    info=owned(name);assert info['Image']==image,'selected image drift'
    if not info['State']['Running']:docker('start',name)
    url='http://127.0.0.1:'+str(PORTS[role])
    for _ in range(180):
        try:
            response=httpx.get(url+'/actuator/health',timeout=2,trust_env=False)
            if response.status_code==200:return
        except httpx.HTTPError:pass
        assert owned(name)['State']['Running'],'service exited: '+name
        time.sleep(1)
    raise RuntimeError('service did not become ready: '+name)

def ensure_backend():
    config=json.loads(PRIVATE.read_text());state=json.loads(STATE.read_text())
    assert state['status'] in ('MIGRATED_NOT_STARTED','SERVICES_READY','LIVE_VERIFIED'),state['status']
    assert state['schemas']==SCHEMAS and state['images']==config['images']
    for name in (config['oldContainer'],'local-life-backend'):
        assert not json.loads(docker('inspect',name))[0]['State']['Running'],'old authority must remain stopped'
    mysql_name='commerce-full-candidate-20260915'
    mysql=json.loads(docker('inspect',mysql_name))[0]
    assert mysql['Config']['Labels'].get('commerce.release')=='20260915-a1'
    if not mysql['State']['Running']:docker('start',mysql_name)
    for _ in range(120):
        try:
            root=root_connection()
            connection=pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},connect_timeout=2)
            connection.close();break
        except (pymysql.MySQLError,KeyError,TypeError):time.sleep(1)
    else:raise RuntimeError('selected MySQL did not recover')
    verify_inventory_write_binding(root)
    # The retired single-owner principal must not silently regain write authority.
    old_user=config['baseEnvironment']['DB_USER']
    assert old_user=='commerce_live' and old_user not in {a['user'] for a in config['accounts'].values()}
    with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},autocommit=True) as db,db.cursor() as c:
        c.execute('ALTER USER %s@%s ACCOUNT LOCK',(old_user,'%'))
    inventory_env={'INVENTORY_DB_URL':'jdbc:mysql://commerce-release-mysql:3306/'+SCHEMAS['inventory']+'?serverTimezone=UTC',
        'INVENTORY_DB_USER':config['accounts']['inventory']['user'],'INVENTORY_DB_PASSWORD':config['accounts']['inventory']['password'],
        'INVENTORY_READ_TOKEN':config['inventoryReadToken'],'INVENTORY_WRITE_TOKEN':config['inventoryWriteToken'],'INVENTORY_MIGRATE':'false',
        'JAVA_TOOL_OPTIONS':'-Xms48m -Xmx160m -XX:MaxDirectMemorySize=32m -XX:ActiveProcessorCount=2'}
    run('inventory',config['images']['inventory'],inventory_env,8083,'320m')
    base={**config['baseEnvironment'],'DB_HOST':'commerce-release-mysql','DB_PORT':'3306','DB_POOL_MAX_SIZE':'6',
        'SPRING_DATASOURCE_HIKARI_MINIMUM_IDLE':'1','SPRING_DATASOURCE_HIKARI_CONNECTION_TIMEOUT':'2000',
        'INTERNAL_SERVICE_TOKEN':config['internalToken'],'REQUIRE_INTERNAL_SERVICE_TOKEN':'true','TZ':'UTC',
        'LOCAL_LIFE_INVENTORY_SERVICE_URL':'http://'+NAMES['inventory']+':8083',
        'CATALOG_INSTANCE_A':'http://'+NAMES['catalog-a']+':8080','CATALOG_INSTANCE_B':'http://'+NAMES['catalog-b']+':8080',
        'JAVA_TOOL_OPTIONS':'-Xms64m -Xmx256m -XX:MaxDirectMemorySize=48m -XX:ActiveProcessorCount=2'}
    for role in ('catalog-a','catalog-b','trade-a','trade-b'):
        owner=role.split('-')[0];account=config['accounts'][owner]
        env={**base,'DB_NAME':SCHEMAS[owner],'DB_USER':account['user'],'DB_PASSWORD':account['password'],
             'SPRING_DATASOURCE_URL':'jdbc:mysql://commerce-release-mysql:3306/'+SCHEMAS[owner]+'?serverTimezone=UTC&sessionVariables=innodb_lock_wait_timeout=3',
             'SPRING_PROFILES_ACTIVE':'micro-catalog' if owner=='catalog' else 'micro-trade,cloud-trade',
             'LOCAL_LIFE_DEPLOYMENT_ROLE':owner,'SERVICE_INSTANCE_ID':role,
             'LOCAL_LIFE_MESSAGING_CONSUMER_GROUP':'commerce-micro-'+owner+'-v1',
             'LOCAL_LIFE_INVENTORY_SERVICE_TOKEN':config['inventoryReadToken' if owner=='catalog' else 'inventoryWriteToken'],
             'LOCAL_LIFE_INVENTORY_REMOTE_ENABLED':'true','LOCAL_LIFE_INVENTORY_RECOVERY_ENABLED':'true' if owner=='trade' else 'false'}
        if owner=='catalog':
            env.update(LOCAL_LIFE_ORDER_EXPIRY_ENABLED='false',FLASH_SALE_ENABLED='false',LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED='false',
                       LOCAL_LIFE_FULFILLMENT_KAFKA_ENABLED='false',LOCAL_LIFE_MEMORY_ENABLED='false')
        else:env.update(LOCAL_LIFE_CACHE_PRODUCT_ENABLED='false',LOCAL_LIFE_CACHE_SHOP_ENABLED='false',SEARCH_ENABLED='false')
        run(role,config['images']['backend'],env,8080,'512m')
    gateway_env={'SPRING_PROFILES_ACTIVE':'microservices','JWT_SECRET':base['JWT_SECRET'],'JWT_ISSUER':base.get('JWT_ISSUER','local-life-backend'),
        'REDIS_HOST':base.get('REDIS_HOST','redis'),'REDIS_PORT':base.get('REDIS_PORT','6379'),
        'CATALOG_INSTANCE_A':base['CATALOG_INSTANCE_A'],'CATALOG_INSTANCE_B':base['CATALOG_INSTANCE_B'],
        'TRADE_INSTANCE_A':'http://'+NAMES['trade-a']+':8080','TRADE_INSTANCE_B':'http://'+NAMES['trade-b']+':8080',
        'JAVA_TOOL_OPTIONS':'-Xms48m -Xmx192m -XX:MaxDirectMemorySize=48m -XX:ActiveProcessorCount=2'}
    run('gateway',config['images']['gateway'],gateway_env,8080,'384m')
    if state['status']!='LIVE_VERIFIED':state.update(status='SERVICES_READY',servicesReadyAtUnix=time.time());save(state)
    print('Independent services and Gateway ready; UI regression is a separate gate.')

if __name__=='__main__':
    action=sys.argv[1:]
    if action==['prepare']:prepare();print('Deployment prepared; no live table changed.')
    elif action==['migrate']:migrate()
    elif action==['ensure-backend']:ensure_backend()
    elif action==['repair-inventory-fk']:repair_inventory_fk()
    else:raise ValueError('explicit prepare, migrate or ensure-backend required')
