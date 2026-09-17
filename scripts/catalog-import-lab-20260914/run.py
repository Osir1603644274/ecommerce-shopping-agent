"""Owned, isolated MySQL 8.4 import rehearsal. Never writes to the application DB.

Run without arguments to create attempt001; --worker is an internal subprocess.
Input and target guard are bound to every checkpoint. No production apply mode.
"""
import collections
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pymysql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/catalog-preflight-20260914'))
from rules import VERSION, candidate, decode, validate
from scan import dump, sha, snapshot, monitor

OUT = Path('D:/agent-datasets/commerce-import-lab-20260914-attempt001')
PRIOR = Path('D:/agent-datasets/commerce-preflight-20260914-attempt001')
MANIFEST = Path('D:/agent-datasets/integration-repair-20260913-v1/metadata/MANIFEST.json')
NAME = 'commerce-import-lab-20260914-a1'
VOLUME = NAME + '-data'
DATABASE = 'commerce_import_lab'
TABLES = ('product', 'product_local_offer', 'inventory_stock')
BATCH = 250


def docker(*args, **kwargs):
    p = subprocess.run(['docker', *args], capture_output=True, **kwargs)
    if p.returncode:
        # Do not expose command/environment credentials through CalledProcessError.
        raise RuntimeError('Docker operation failed (details withheld): ' + args[0])
    return p.stdout


def connect(database=DATABASE):
    port = int(os.environ['CATALOG_LAB_PORT'])
    if port in (3306, 13306) or not database.startswith('commerce_import_'):
        raise ValueError('non-lab connection refused')
    return pymysql.connect(host='127.0.0.1', port=port, user='root',
                           password=os.environ['CATALOG_LAB_SECRET'], database=database,
                           charset='utf8mb4', autocommit=False, connect_timeout=5,
                           read_timeout=30, cursorclass=pymysql.cursors.DictCursor)


def guarded(db):
    with db.cursor() as c:
        c.execute('SELECT DATABASE() AS db')
        if c.fetchone()['db'] != DATABASE:
            raise ValueError('wrong database')
        c.execute('SELECT token FROM lab_guard WHERE id=1')
        if c.fetchone()['token'] != os.environ['CATALOG_LAB_GUARD']:
            raise ValueError('wrong ownership guard')
    db.rollback()


def business_state(db):
    result = {}
    with db.cursor() as c:
        for t in TABLES:
            c.execute('SELECT * FROM ' + t + ' ORDER BY ' + ('product_id' if t == 'product_local_offer' else 'id'))
            result[t] = c.fetchall()
    db.rollback()
    return result


def state_hash(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def counts(db):
    with db.cursor() as c:
        result = {}
        for t in TABLES:
            c.execute('SELECT COUNT(*) AS n FROM ' + t)
            result[t] = c.fetchone()['n']
    db.rollback()
    return result


def insert_rows(c, table, rows):
    if not rows:
        return
    columns = tuple(rows[0])
    c.executemany('INSERT INTO ' + table + ' (' + ','.join('`'+k+'`' for k in columns) + ') VALUES (' +
                  ','.join(['%s'] * len(columns)) + ')', [tuple(r[k] for k in columns) for r in rows])


def products(rows):
    mapping = {'sourceItemId':'source_item_id', 'categoryL1':'category_l1', 'categoryL2':'category_l2',
               'categoryL3':'category_l3', 'snapshotPriceMinor':'snapshot_price_minor', 'priceStatus':'price_status',
               'dataNature':'data_nature', 'datasetRevision':'dataset_revision', 'sourceLicense':'source_license',
               'provenanceUrl':'provenance_url'}
    keys = ('id','source','sourceItemId','title','brand','seller','categoryL1','categoryL2','categoryL3',
            'snapshotPriceMinor','currency','priceStatus','dataNature','datasetRevision','sourceLicense','provenanceUrl')
    # ACTIVE is confined to the isolated lab. No application network or search projection is connected.
    return [{**{mapping.get(k,k):r[k] for k in keys}, 'lifecycle_status':'ACTIVE', 'entity_version':1} for r in rows]


def import_worker(path, run_id, fault):
    started = time.monotonic()
    digest = sha(path)
    rows = [json.loads(x) for x in path.read_text(encoding='utf8').splitlines()]
    if not rows or len(rows) > 10000:
        raise ValueError('bounded input required')
    keys = [(r['source'],r['sourceItemId']) for r in rows]
    if len(set(keys)) != len(keys) or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('duplicate input identity')
    for r in rows:
        if validate(r):
            raise ValueError('invalid candidate')
    db = connect()
    try:
        guarded(db)
        with db.cursor() as c:
            c.execute('INSERT IGNORE INTO import_checkpoint(run_id,input_sha,rule_version,position) VALUES(%s,%s,%s,0)',
                      (run_id,digest,VERSION))
            c.execute('SELECT * FROM import_checkpoint WHERE run_id=%s FOR UPDATE',(run_id,))
            saved = c.fetchone()
            if (saved['input_sha'],saved['rule_version']) != (digest,VERSION):
                raise ValueError('checkpoint input/version mismatch')
        db.commit()
        added = skipped = 0
        while True:
            with db.cursor() as c:
                c.execute('SELECT * FROM import_checkpoint WHERE run_id=%s FOR UPDATE',(run_id,))
                saved = c.fetchone()
                start = saved['position']
                batch = rows[start:start+BATCH]
                if not batch:
                    db.commit()
                    break
                # The checkpoint serializes workers of the same run; unique keys arbitrate different runs.
                c.execute('SELECT id,source,source_item_id FROM product WHERE (source,source_item_id) IN (' +
                          ','.join(['(%s,%s)']*len(batch)) + ')',
                          tuple(v for r in batch for v in (r['source'],r['sourceItemId'])))
                present = {(r['source'],r['source_item_id']) for r in c.fetchall()}
                fresh = [r for r in batch if (r['source'],r['sourceItemId']) not in present]
                if fresh:
                    c.execute('SELECT id FROM product WHERE id IN (' + ','.join(['%s']*len(fresh)) + ')',
                              tuple(r['id'] for r in fresh))
                    if c.fetchall():
                        raise ValueError('primary key collision with another identity')
                insert_rows(c,'product',products(fresh))
                if fault == 'mid-transaction' and start == 1000:
                    os._exit(77)  # Real worker exit; socket closes with uncommitted product rows.
                insert_rows(c,'product_local_offer',[
                    dict(product_id=r['id'],price_minor=r['localOffer']['priceMinor'],currency='CNY',
                         price_kind='local_simulated',source_revision=VERSION,version=1) for r in fresh])
                insert_rows(c,'inventory_stock',[
                    dict(item_type='PRODUCT',item_id=r['id'],total_quantity=10,available_quantity=10,
                         reserved_quantity=0,sold_quantity=0,version=0) for r in fresh])
                insert_rows(c,'import_ledger',[
                    dict(product_id=r['id'],source=r['source'],native_id=r['sourceItemId'],
                         source_line=r['provenance']['line'],raw_sha=r['provenance']['rawSha256'],
                         field_states=json.dumps(r['fieldStates'],ensure_ascii=False),
                         candidate_sha=hashlib.sha256(json.dumps(r,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
                    for r in fresh])
                c.execute('UPDATE import_checkpoint SET position=%s WHERE run_id=%s',(start+len(batch),run_id))
            db.commit()
            added += len(fresh)
            skipped += len(batch)-len(fresh)
            if fault == 'after-commit' and start+len(batch) == 5000:
                os._exit(78)  # Durable commit, but the caller receives no successful receipt.
            time.sleep(.01)
        print(json.dumps(dict(run=run_id,added=added,skipped=skipped,position=len(rows),
                              seconds=round(time.monotonic()-started,3))),flush=True)
    finally:
        db.rollback()
        db.close()


def production_schema():
    info=json.loads(docker('inspect','local-life-mysql'))[0]
    env=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
    db=pymysql.connect(host='127.0.0.1',port=13306,user=env['MYSQL_USER'],password=env['MYSQL_PASSWORD'],
                       database=env['MYSQL_DATABASE'],connect_timeout=5,read_timeout=15)
    try:
        with db.cursor() as c:
            c.execute('SET TRANSACTION READ ONLY')
            result={}
            for t in TABLES:
                c.execute('SHOW CREATE TABLE '+t)
                result[t]=c.fetchone()[1]
        db.rollback()
        return result
    finally:
        db.close()


def freeze_sample(before):
    manifest=json.loads(MANIFEST.read_text(encoding='utf8'))
    prior=json.loads((PRIOR/'RESULT.json').read_text(encoding='utf8'))
    if sha(MANIFEST)!=prior['manifestSha256']:
        raise ValueError('source manifest changed since preflight')
    if sha(MANIFEST.parent/'metadata.sqlite')!=manifest['artifacts']['metadata.sqlite']['sha256']:
        raise ValueError('metadata version changed since preflight')
    meta=sqlite3.connect((MANIFEST.parent/'metadata.sqlite').as_uri()+'?mode=ro',uri=True)
    meta.execute('PRAGMA query_only=ON')
    existing={(r['source'],r['source_item_id']) for r in before['product']}
    rows=[]; selection={}
    try:
        for source in ('kuaisearch','multicpr'):
            source_rows=[]; offset=0; examined=0
            info=manifest['sources'][source]
            if sha(Path(info['path']))!=info['sha256']:
                raise ValueError('source version changed since preflight')
            cursor=iter(meta.execute('SELECT docid,source_line,byte_offset,byte_length,record_sha256 FROM records WHERE source=? ORDER BY rowid',(source,)))
            with Path(info['path']).open('rb') as f:
                for line,raw in enumerate(f,1):
                    bound=next(cursor)
                    raw_sha=hashlib.sha256(raw).hexdigest()
                    if (line,offset,len(raw),raw_sha)!=tuple(bound[1:]):
                        raise ValueError('selected raw row differs from audited metadata')
                    offset+=len(raw);examined+=1
                    try:
                        ident,fields=decode(source,raw)
                        if bound[0]!=source+':'+ident:
                            raise RuntimeError('metadata identity mismatch')
                        row=candidate(source,ident,fields,info['sha256'])
                        if validate(row) or (source,ident) in existing:
                            continue
                    except (ValueError,TypeError,KeyError):
                        continue
                    row['provenance']=dict(line=line,byteOffset=bound[2],rawSha256=raw_sha)
                    source_rows.append(row)
                    if len(source_rows)==5000:
                        break
            if len(source_rows)!=5000:
                raise ValueError('insufficient eligible rows')
            rows.extend(source_rows)
            selection[source]=dict(selected=len(source_rows),examined=examined,
                                   categories=dict(collections.Counter(r['categoryL3'] for r in source_rows)))
    finally:
        meta.close()
    path=OUT/'sample.jsonl'
    with path.open('x',encoding='utf8') as f:
        for r in rows:
            f.write(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n')
    dump(OUT/'SELECTION.json',dict(method='first 5000 valid NEW identities per source; not a random representative sample',
                                   sources=selection,inputSha256=sha(path)))
    return rows,path


def measure(db):
    with db.cursor() as c:
        c.execute('ANALYZE TABLE '+','.join(TABLES)+',import_ledger,import_checkpoint')
        c.fetchall()
        c.execute('SET SESSION information_schema_stats_expiry=0')
        c.execute('SELECT table_name,table_rows,data_length,index_length,data_free FROM information_schema.tables WHERE table_schema=%s',(DATABASE,))
        tables=c.fetchall()
        c.execute('SHOW BINARY LOGS')
        logs=c.fetchall()
        c.execute("SHOW VARIABLES WHERE Variable_name IN ('innodb_buffer_pool_size','innodb_redo_log_capacity','max_connections')")
        variables=c.fetchall()
    db.rollback()
    sizes=docker('exec',NAME,'du','-B1','-s','/var/lib/mysql/'+DATABASE,'/var/lib/mysql/#innodb_redo','/var/lib/mysql/undo_001','/var/lib/mysql/undo_002').decode()
    return dict(counts=counts(db),tables=tables,binlogs=logs,physicalAllocatedBytes=sizes,variables=variables,
                hostFreeBytes={d:shutil.disk_usage(d+':/').free for d in 'DEF'})


def invoke(path,run_id,fault='none',expected=0):
    start=time.monotonic()
    p=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker',str(path),run_id,fault],
                     capture_output=True,text=True,encoding='utf8',timeout=180)
    result=dict(run=run_id,fault=fault,exitCode=p.returncode,seconds=round(time.monotonic()-start,3),
                stdout=p.stdout,stderr=p.stderr)
    with (OUT/'workers.jsonl').open('a',encoding='utf8') as f:
        f.write(json.dumps(result,ensure_ascii=False)+'\n')
    if p.returncode!=expected:
        raise RuntimeError('unexpected worker result: '+run_id+'; see workers.jsonl')
    print(json.dumps({k:result[k] for k in ('run','fault','exitCode','seconds')}),flush=True)
    return result


def main():
    if OUT.exists():
        raise ValueError('attempt exists; do not overwrite evidence')
    for d in ('D','E','F'):
        if shutil.disk_usage(d+':/').free<5*1024**3:
            raise ValueError('insufficient host headroom: '+d)
    if NAME in docker('ps','-a','--format','{{.Names}}').decode().splitlines() or VOLUME in docker('volume','ls','--format','{{.Name}}').decode().splitlines():
        raise ValueError('owned lab name/volume already exists')
    OUT.mkdir(parents=True)
    stop=threading.Event(); monitor_thread=threading.Thread(target=monitor,args=(stop,OUT/'health.jsonl'),daemon=True)
    monitor_thread.start()
    created=False;db=None;report={'scope':'ISOLATED_DATABASE_IMPORT_ONLY_NO_APPLICATION_CUTOVER','status':'RUNNING'}
    try:
        # Recheck sealed audit evidence and the exact rule implementation.
        for name,item in json.loads((PRIOR/'EVIDENCE-MANIFEST.json').read_text(encoding='utf8')).items():
            if sha(PRIOR/name)!=item['sha256']:
                raise ValueError('preflight evidence changed: '+name)
        provenance=json.loads((PRIOR/'PROVENANCE.json').read_text(encoding='utf8'))['files']
        rule_path=ROOT/'scripts/catalog-preflight-20260914/rules.py'
        if sha(rule_path)!=provenance[str(rule_path)]:
            raise ValueError('mapping rules changed since preflight')
        before=snapshot();dump(OUT/'production-before.json',before)
        schema=production_schema();dump(OUT/'SCHEMA.json',schema)
        rows,path=freeze_sample(before)
        print('Frozen 10000 source-bound NEW records',flush=True)
        os.environ['CATALOG_LAB_SECRET']=secrets.token_hex(24)
        os.environ['CATALOG_LAB_GUARD']=secrets.token_hex(24)
        image=json.loads(docker('image','inspect','mysql:8.4'))[0]['Id']
        report.update(image=image,container=NAME,volume=VOLUME,ruleVersion=VERSION,inputSha256=sha(path))
        docker('run','-d','--pull=never','--name',NAME,'--label','catalog.lab=20260914-attempt001',
               '--cpus=1','--memory=1g','--pids-limit=512','-p','127.0.0.1::3306',
               '-e','MYSQL_ROOT_PASSWORD='+os.environ['CATALOG_LAB_SECRET'],'-e','MYSQL_ROOT_HOST=%',
               '-e','MYSQL_DATABASE='+DATABASE,'-v',VOLUME+':/var/lib/mysql',image,
               '--innodb-buffer-pool-size=134217728','--innodb-redo-log-capacity=67108864','--max-connections=40')
        created=True
        inspect=json.loads(docker('inspect',NAME))[0]
        os.environ['CATALOG_LAB_PORT']=inspect['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort']
        report['port']=int(os.environ['CATALOG_LAB_PORT'])
        for _ in range(90):
            try:
                db=connect();break
            except pymysql.MySQLError:
                time.sleep(2)
        if db is None:
            raise RuntimeError('lab MySQL not ready')
        with db.cursor() as c:
            for ddl in schema.values():
                c.execute(ddl)
            c.execute('CREATE TABLE lab_guard(id INT PRIMARY KEY, token VARCHAR(64) NOT NULL)')
            c.execute('INSERT INTO lab_guard VALUES(1,%s)',(os.environ['CATALOG_LAB_GUARD'],))
            c.execute('CREATE TABLE import_checkpoint(run_id VARCHAR(64) PRIMARY KEY,input_sha CHAR(64) NOT NULL,rule_version VARCHAR(128) NOT NULL,position INT NOT NULL) ENGINE=InnoDB')
            c.execute('CREATE TABLE import_ledger(product_id BIGINT PRIMARY KEY,source VARCHAR(64) NOT NULL,native_id VARCHAR(128) NOT NULL,source_line BIGINT NOT NULL,raw_sha CHAR(64) NOT NULL,field_states JSON NOT NULL,candidate_sha CHAR(64) NOT NULL,UNIQUE(source,native_id)) ENGINE=InnoDB')
            for t in TABLES:
                insert_rows(c,t,before[t])
        db.commit()
        seeded=business_state(db);base=counts(db)
        report['baseline']=measure(db)
        invoke(path,'main','mid-transaction',77)
        time.sleep(.5)
        assert counts(db)=={t:base[t]+1000 for t in TABLES}, 'mid-transaction orphan rows'
        with db.cursor() as c:
            c.execute('SELECT position FROM import_checkpoint WHERE run_id="main"')
            assert c.fetchone()['position']==1000
        db.rollback()
        report['midTransactionCrash']='1000 complete triples; 250 uncommitted products rolled back; checkpoint=1000'
        invoke(path,'main','after-commit',78)
        assert counts(db)=={t:base[t]+5000 for t in TABLES}
        invoke(path,'main')
        assert counts(db)=={t:base[t]+10000 for t in TABLES}
        report['afterCommitCrash']='5000 complete triples survived loss of receipt; resumed to 10000'
        final_state=business_state(db)
        for t in TABLES:
            pk='product_id' if t=='product_local_offer' else 'id'
            old_ids={r[pk] for r in seeded[t]}
            assert [r for r in final_state[t] if r[pk] in old_ids]==seeded[t], 'existing rows changed'
        replay=invoke(path,'replay')
        assert json.loads(replay['stdout'])['skipped']==10000
        assert state_hash(business_state(db))==state_hash(final_state)
        report['replay']='fresh checkpoint processed all 10000; skipped all; catalog byte-equivalent'
        pid=rows[0]['id']
        with db.cursor() as c:
            c.execute('UPDATE product SET lifecycle_status="ARCHIVED",entity_version=entity_version+1 WHERE id=%s',(pid,))
            c.execute('UPDATE product_local_offer SET price_minor=price_minor+100,version=version+1 WHERE product_id=%s',(pid,))
            c.execute('UPDATE inventory_stock SET available_quantity=9,reserved_quantity=1,version=version+1 WHERE item_type="PRODUCT" AND item_id=%s',(pid,))
        db.commit(); changed=business_state(db)
        invoke(path,'replay-after-business-change')
        assert state_hash(business_state(db))==state_hash(changed)
        report['stockPreservation']='changed offer, ARCHIVED lifecycle, available=9 reserved=1 preserved exactly'
        altered=OUT/'altered-input.jsonl'
        # A valid altered candidate file is needed to test checkpoint binding rather than JSON parsing.
        altered_rows=[dict(r) for r in rows];altered_rows[0]['title']+=' test'
        with altered.open('w',encoding='utf8') as f:
            for r in altered_rows:
                f.write(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n')
        mismatch=invoke(altered,'main',expected=1)
        assert 'checkpoint input/version mismatch' in mismatch['stderr']
        assert state_hash(business_state(db))==state_hash(changed)
        conflict=dict(rows[0]);conflict['sourceItemId']='999999999999';conflict['provenanceUrl']='urn:catalog:kuaisearch:999999999999'
        conflict_path=OUT/'collision-probe.jsonl'
        conflict_path.write_text(json.dumps(conflict,ensure_ascii=False)+'\n',encoding='utf8')
        collision=invoke(conflict_path,'collision-probe',expected=1)
        assert 'primary key collision' in collision['stderr']
        assert state_hash(business_state(db))==state_hash(changed)
        report['inputMismatchAndCollision']='both rejected with no business mutations'
        report['after']=measure(db)
        dump(OUT/'lab-final-state.json',changed)
        # Backup/restore checks a different database in the same owned lab, never the application DB.
        sql=docker('exec','-e','MYSQL_PWD='+os.environ['CATALOG_LAB_SECRET'],NAME,'mysqldump',
                   '-uroot','--single-transaction','--no-tablespaces','--set-gtid-purged=OFF',DATABASE)
        with gzip.open(OUT/'lab-backup.sql.gz','wb') as f:
            f.write(sql)
        restore='commerce_import_restore_lab'
        with db.cursor() as c:
            c.execute('CREATE DATABASE '+restore+' CHARACTER SET utf8mb4')
        db.commit()
        docker('exec','-i','-e','MYSQL_PWD='+os.environ['CATALOG_LAB_SECRET'],NAME,'mysql','-uroot',restore,input=sql)
        restored=connect(restore)
        try:
            assert state_hash(business_state(restored))==state_hash(changed)
        finally:
            restored.close()
        report['backup']=dict(sqlBytes=len(sql),gzipBytes=(OUT/'lab-backup.sql.gz').stat().st_size,
                              restoredBusinessStateSha256=state_hash(changed))
        report['status']='PASS_BOUNDED_10000_DATABASE_REHEARSAL'
    except Exception as e:
        report.update(status='FAILED',error=type(e).__name__+': '+str(e))
        raise
    finally:
        if db:
            db.close()
        if created:
            docker('stop','-t','15',NAME)
        try:
            after=snapshot();dump(OUT/'production-after.json',after)
            report['productionCatalogUnchanged']=all(before[t]==after[t] for t in TABLES)
        finally:
            stop.set();monitor_thread.join(6)
            health=[json.loads(x) for x in (OUT/'health.jsonl').read_text().splitlines()]
            report['frontendHealth']=dict(probes=len(health),all200=all(x.get('status')==200 for x in health),
                                          maxMs=max(x['ms'] for x in health))
            report['sourceCodeSha256']={str(p.relative_to(ROOT)):sha(p) for p in
                                      (Path(__file__).resolve(),ROOT/'scripts/catalog-preflight-20260914/rules.py')}
            report['retention']='owned container stopped; volume and backup retained; no production import, ES, Outbox or cache mutations'
            dump(OUT/'RESULT.json',report)
            dump(OUT/'EVIDENCE-MANIFEST.json',{p.name:dict(bytes=p.stat().st_size,sha256=sha(p)) for p in OUT.iterdir() if p.is_file() and p.name!='EVIDENCE-MANIFEST.json'})
            print(json.dumps(dict(status=report['status'],output=str(OUT),health=report['frontendHealth'])),flush=True)


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker':
        import_worker(Path(sys.argv[2]),sys.argv[3],sys.argv[4])
    else:
        main()
