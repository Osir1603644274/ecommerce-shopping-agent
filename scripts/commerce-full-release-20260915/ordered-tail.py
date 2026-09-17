"""Externally sort the remaining frozen source; commit rows and ordered cursor together.

The original raw byte offset stays frozen during ORDERED_IMPORT. source_line is
the processed-record count in this phase, not a raw seek position. At EOF both
fields return to the original full-source meaning. Raw line numbers remain on
every receipt; the additional release-owned cursor makes crash recovery exact.
"""
import hashlib,json,shutil,sqlite3,sys,time
import pymysql
from prepare import OUT,ROOT,cmd,write
from import_catalog import decode,candidate,validate,VERSION,lab,sha

SOURCE='kuaisearch'
ARTIFACT=OUT/'kuai-ordered-tail.sqlite3'
MANIFEST=OUT/'ORDERED-TAIL-MANIFEST.json'
BASELINE=OUT/'ORDERED-TAIL-BASELINE.json'
origin=json.loads((OUT.parent/'integration-repair-20260913-v1/metadata/MANIFEST.json').read_text())['sources'][SOURCE]
secret=json.loads((OUT/'connection.private.json').read_text())
assert secret['database']=='commerce_candidate'
assert json.loads(cmd('inspect',secret['container']))[0]['Config']['Labels']['commerce.release']=='20260915-a1'
rule_path=ROOT/'scripts/catalog-preflight-20260914/rules.py'
rule_sha=sha(rule_path)
assert rule_sha==json.loads((OUT.parent/'commerce-preflight-20260914-attempt001/PROVENANCE.json').read_text())['files'][str(rule_path)]

def connect():
    return pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},
        charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,autocommit=False,read_timeout=180,write_timeout=180)

def frozen_baseline(row):return json.loads(json.dumps(row,default=str))

def prepare():
    assert not ARTIFACT.exists() and not MANIFEST.exists() and not BASELINE.exists(),'preserve prior artifacts; inspect before resuming'
    assert shutil.disk_usage('D:/').free>10*1024**3
    assert sha(__import__('pathlib').Path(origin['path']))==origin['sha256']
    db=connect()
    with db.cursor() as c:
        c.execute('SELECT * FROM external_catalog_import_checkpoint WHERE source=%s FOR UPDATE',(SOURCE,));base=c.fetchone()
        assert base['status']=='IMPORTING' and base['input_sha']==origin['sha256'] and base['rule_version']==VERSION
    db.rollback();db.close();base=frozen_baseline(base);write(BASELINE.name,base)
    stage=sqlite3.connect(ARTIFACT)
    stage.execute('PRAGMA journal_mode=WAL');stage.execute('PRAGMA synchronous=FULL')
    stage.execute('PRAGMA cache_size=-65536');stage.execute('PRAGMA temp_store=FILE')
    stage.execute('CREATE TABLE records(product_id INTEGER PRIMARY KEY,source_line INTEGER NOT NULL,raw_sha BLOB NOT NULL,raw BLOB NOT NULL)')
    stage.execute('CREATE TABLE quarantine(source_line INTEGER PRIMARY KEY,raw_sha BLOB NOT NULL,reason TEXT NOT NULL)')
    stage.commit();valid=bad=0;batch=[];invalid=[];started=time.monotonic()
    with open(origin['path'],'rb') as source:
        source.seek(base['byte_offset']);line=base['source_line']
        for raw in source:
            line+=1;digest=hashlib.sha256(raw).digest()
            try:
                native,fields=decode(SOURCE,raw);row=candidate(SOURCE,native,fields,origin['sha256'])
                errors=validate(row)
                if errors:raise ValueError(','.join(errors))
                batch.append((int(row['id']),line,digest,raw));valid+=1
            except (ValueError,TypeError,KeyError,UnicodeError) as exc:
                invalid.append((line,digest,str(exc)[:1024]));bad+=1
            if len(batch)+len(invalid)>=5000:
                stage.executemany('INSERT INTO records VALUES(?,?,?,?)',batch)
                stage.executemany('INSERT INTO quarantine VALUES(?,?,?)',invalid);stage.commit();batch=[];invalid=[]
            if (line-base['source_line'])%100000==0:print('Sorted staging prepared',line-base['source_line'],'records',flush=True)
        stage.executemany('INSERT INTO records VALUES(?,?,?,?)',batch)
        stage.executemany('INSERT INTO quarantine VALUES(?,?,?)',invalid);stage.commit();end=source.tell()
    assert line==6634118 and bad+base['quarantined_count']==65
    assert stage.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert stage.execute('SELECT COUNT(*) FROM records').fetchone()[0]==valid
    stage.execute('PRAGMA wal_checkpoint(TRUNCATE)');stage.execute('PRAGMA journal_mode=DELETE');stage.close()
    db=connect()
    with db.cursor() as c:
        c.execute('SELECT * FROM external_catalog_import_checkpoint WHERE source=%s',(SOURCE,));assert frozen_baseline(c.fetchone())==base,'raw-order importer was not stopped'
    db.rollback();db.close()
    write(MANIFEST.name,dict(status='SEALED',baseline=base,sourceSha256=origin['sha256'],ruleSha256=rule_sha,
        artifactSha256=sha(ARTIFACT),validRows=valid,quarantinedRows=bad,finalSourceLine=line,finalByteOffset=end,
        seconds=round(time.monotonic()-started,2)))
    print('Ordered tail sealed:',valid,'valid,',bad,'quarantined',flush=True)

def run(max_batches=0):
    manifest=json.loads(MANIFEST.read_text());base=manifest['baseline']
    assert manifest['status']=='SEALED' and manifest['sourceSha256']==origin['sha256'] and manifest['ruleSha256']==rule_sha
    assert sha(ARTIFACT)==manifest['artifactSha256']
    assert sha(__import__('pathlib').Path(origin['path']))==origin['sha256']
    stage=sqlite3.connect('file:'+ARTIFACT.as_posix()+'?mode=ro&immutable=1',uri=True)
    stage.row_factory=sqlite3.Row
    db=connect();started=time.monotonic();batches=0
    try:
        with db.cursor() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS commerce_release_ordered_import (
                source VARCHAR(64) PRIMARY KEY, artifact_sha CHAR(64) NOT NULL, baseline_json LONGTEXT NOT NULL,
                last_product_id BIGINT NOT NULL DEFAULT 0, processed_count BIGINT NOT NULL DEFAULT 0,
                inserted_count BIGINT NOT NULL DEFAULT 0,preserved_count BIGINT NOT NULL DEFAULT 0,
                quarantined_count BIGINT NOT NULL DEFAULT 0,status VARCHAR(32) NOT NULL)''')
        db.commit()
        while True:
            assert shutil.disk_usage('E:/').free>10*1024**3
            with db.cursor() as c:
                c.execute('SELECT * FROM external_catalog_import_checkpoint WHERE source=%s FOR UPDATE',(SOURCE,));cp=c.fetchone()
                c.execute('SELECT * FROM commerce_release_ordered_import WHERE source=%s FOR UPDATE',(SOURCE,));cursor=c.fetchone()
                if cursor is None:
                    assert frozen_baseline(cp)==base
                    bad=[dict(source=SOURCE,source_line=r['source_line'],raw_sha=r['raw_sha'],reason=r['reason']) for r in stage.execute('SELECT * FROM quarantine ORDER BY source_line')]
                    lab.insert_rows(c,'external_catalog_quarantine',bad)
                    c.execute('INSERT INTO commerce_release_ordered_import(source,artifact_sha,baseline_json,quarantined_count,status) VALUES(%s,%s,%s,%s,%s)',
                        (SOURCE,manifest['artifactSha256'],json.dumps(base),len(bad),'ORDERED_IMPORT'))
                    c.execute("UPDATE external_catalog_import_checkpoint SET status='ORDERED_IMPORT',source_line=source_line+%s,quarantined_count=quarantined_count+%s WHERE source=%s",(len(bad),len(bad),SOURCE))
                    db.commit();continue
                assert cursor['artifact_sha']==manifest['artifactSha256'] and json.loads(cursor['baseline_json'])==base
                assert cp['input_sha']==origin['sha256'] and cp['rule_version']==VERSION
                assert cp['inserted_count']==base['inserted_count']+cursor['inserted_count']
                assert cp['preserved_count']==base['preserved_count']+cursor['preserved_count']
                assert cp['quarantined_count']==base['quarantined_count']+cursor['quarantined_count']
                if cursor['status']=='COMPLETE':assert cp['status']=='COMPLETE';db.commit();break
                assert cp['status']=='ORDERED_IMPORT' and cp['source_line']==base['source_line']+cursor['processed_count']+cursor['quarantined_count']
                batch=stage.execute('SELECT * FROM records WHERE product_id>? ORDER BY product_id LIMIT 20000',(cursor['last_product_id'],)).fetchall()
                if not batch:
                    assert cursor['processed_count']==manifest['validRows'] and cp['source_line']==manifest['finalSourceLine']
                    c.execute("UPDATE external_catalog_import_checkpoint SET status='COMPLETE',byte_offset=%s WHERE source=%s",(manifest['finalByteOffset'],SOURCE))
                    c.execute("UPDATE commerce_release_ordered_import SET status='COMPLETE' WHERE source=%s",(SOURCE,))
                    db.commit();break
                valid=[]
                for saved in batch:
                    assert hashlib.sha256(saved['raw']).digest()==saved['raw_sha']
                    native,fields=decode(SOURCE,saved['raw']);row=candidate(SOURCE,native,fields,origin['sha256'])
                    assert int(row['id'])==saved['product_id'] and not validate(row)
                    valid.append((row,saved['source_line'],saved['raw_sha']))
                # Keep each indexed lookup small. A 20k IN list can exceed
                # MySQL's range-optimizer memory budget and scan a whole source.
                existing={}
                for offset in range(0,len(valid),1000):
                    part=valid[offset:offset+1000]
                    c.execute('SELECT id,source_item_id FROM product WHERE source=%s AND source_item_id IN ('+','.join(['%s']*len(part))+')',
                        (SOURCE,*(r['sourceItemId'] for r,_,_ in part)))
                    existing.update((r['source_item_id'],r['id']) for r in c.fetchall())
                fresh=[r for r,_,_ in valid if r['sourceItemId'] not in existing]
                lab.insert_rows(c,'product',lab.products(fresh))
                lab.insert_rows(c,'product_local_offer',[dict(product_id=r['id'],price_minor=r['localOffer']['priceMinor'],currency='CNY',price_kind='local_simulated',source_revision=VERSION,version=1) for r in fresh])
                lab.insert_rows(c,'inventory_stock',[dict(item_type='PRODUCT',item_id=r['id'],total_quantity=10,available_quantity=10,reserved_quantity=0,sold_quantity=0,version=0) for r in fresh])
                identities=[dict(product_id=existing.get(r['sourceItemId'],r['id']),raw_sha=digest,source_line=line,source_revision=origin['sha256'],import_version=VERSION,preserved_existing=r['sourceItemId'] in existing) for r,line,digest in valid]
                lab.insert_rows(c,'external_catalog_identity',sorted(identities,key=lambda r:int(r['product_id'])))
                kept=len(valid)-len(fresh)
                c.execute('UPDATE external_catalog_import_checkpoint SET source_line=source_line+%s,inserted_count=inserted_count+%s,preserved_count=preserved_count+%s WHERE source=%s',(len(valid),len(fresh),kept,SOURCE))
                c.execute('UPDATE commerce_release_ordered_import SET last_product_id=%s,processed_count=processed_count+%s,inserted_count=inserted_count+%s,preserved_count=preserved_count+%s WHERE source=%s',(batch[-1]['product_id'],len(valid),len(fresh),kept,SOURCE))
            db.commit();batches+=1
            progress=dict(status='ORDERED_IMPORT',batchesThisRun=batches,seconds=round(time.monotonic()-started,2),lastProductId=batch[-1]['product_id'],
                logicalSourceRecords=base['source_line']+cursor['processed_count']+len(valid)+cursor['quarantined_count'])
            write('ORDERED-IMPORT-PROGRESS.json',progress);print(json.dumps(progress),flush=True)
            if max_batches and batches>=max_batches:return
        print('ORDERED_KUAISEARCH_COMPLETE',flush=True)
    finally:db.rollback();db.close();stage.close()

if __name__=='__main__':
    if sys.argv[1:]==['prepare']:prepare()
    elif sys.argv[1:]==['run']:run()
    elif sys.argv[1:]==['one-batch']:run(1)
    else:raise ValueError('explicit prepare, one-batch or run required')
