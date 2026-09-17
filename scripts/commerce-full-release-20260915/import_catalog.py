"""Resumable, version-bound streaming import into the owned candidate only."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import pymysql

ROOT=Path(__file__).resolve().parents[2]
OUT=Path('D:/agent-datasets/commerce-full-release-20260915-attempt001')
NAME='commerce-full-candidate-20260915'
sys.path.insert(0,str(ROOT/'scripts/catalog-preflight-20260914'))
from rules import decode,candidate,validate,VERSION
from scan import sha
spec=importlib.util.spec_from_file_location('lab_import',ROOT/'scripts/catalog-import-lab-20260914/run.py')
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)


def main(max_new,only_source=None,batch_size=1000):
    assert only_source in (None,'kuaisearch','multicpr') and 100<=batch_size<=20000
    info=json.loads(subprocess.check_output(['docker','inspect',NAME]))[0]
    assert info['Config']['Labels'].get('commerce.release')=='20260915-a1'
    secret=json.loads((OUT/'connection.private.json').read_text())
    assert secret['database']=='commerce_candidate' and secret['host']=='127.0.0.1'
    assert secret['port'] not in (3306,13306)
    assert str(secret['port'])==info['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort']
    db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},charset='utf8mb4',
        autocommit=False,read_timeout=180,write_timeout=180,cursorclass=pymysql.cursors.DictCursor)
    manifest_path=Path('D:/agent-datasets/integration-repair-20260913-v1/metadata/MANIFEST.json')
    manifest=json.loads(manifest_path.read_text())
    audit=json.loads(Path('D:/agent-datasets/commerce-preflight-20260914-attempt001/RESULT.json').read_text(encoding='utf8'))
    assert sha(manifest_path)==audit['manifestSha256']
    rules_expected=json.loads(Path('D:/agent-datasets/commerce-preflight-20260914-attempt001/PROVENANCE.json').read_text())['files']
    assert sha(ROOT/'scripts/catalog-preflight-20260914/rules.py')==rules_expected[str(ROOT/'scripts/catalog-preflight-20260914/rules.py')]
    # Idempotent DDL applied on candidate before import; Flyway records V21 when staged Java starts.
    ddl=(ROOT/'backend/src/main/resources/db/migration/V21__external_commerce_catalog.sql').read_text(encoding='utf8')
    cleaned='\n'.join(line for line in ddl.splitlines() if not line.lstrip().startswith('--'))
    with db.cursor() as c:
        for sql in cleaned.split(';'):
            if sql.strip():c.execute(sql)
        c.execute('SELECT COUNT(*) AS n FROM catalog_version_member')
        assert c.fetchone()['n']==439,'legacy retrieval membership must stay 439'
    db.commit()
    total_new=0;started=time.monotonic()
    try:
        for source in ((only_source,) if only_source else ('kuaisearch','multicpr')):
            origin=manifest['sources'][source];path=Path(origin['path'])
            print('Verifying frozen source '+source,flush=True)
            assert sha(path)==origin['sha256']
            with db.cursor() as c:
                c.execute('INSERT IGNORE INTO external_catalog_import_checkpoint(source,input_sha,rule_version) VALUES(%s,%s,%s)',(source,origin['sha256'],VERSION))
            db.commit()
            with path.open('rb') as f:
                while True:
                    if shutil.disk_usage('E:/').free<10*1024**3:
                        raise RuntimeError('HOST_SPACE_GUARD: keep 10 GiB free on Docker host volume')
                    with db.cursor() as c:
                        c.execute('SELECT * FROM external_catalog_import_checkpoint WHERE source=%s FOR UPDATE',(source,))
                        cp=c.fetchone()
                        assert (cp['input_sha'],cp['rule_version'])==(origin['sha256'],VERSION)
                        if cp['status']=='COMPLETE':db.commit();break
                        if cp['status']!='IMPORTING':raise ValueError('checkpoint uses another resumable import protocol; do not restart the raw-order worker')
                        f.seek(cp['byte_offset']);line=cp['source_line'];valid=[];bad=[]
                        for _ in range(batch_size):
                            raw=f.readline()
                            if not raw:break
                            line+=1;raw_sha=hashlib.sha256(raw).digest()
                            try:
                                ident,fields=decode(source,raw)
                                row=candidate(source,ident,fields,origin['sha256'])
                                errors=validate(row)
                                if errors:raise ValueError(','.join(errors))
                                valid.append((row,line,raw_sha))
                            except (ValueError,TypeError,KeyError,UnicodeError) as e:
                                bad.append(dict(source=source,source_line=line,raw_sha=raw_sha,reason=str(e)[:1024]))
                        if not valid and not bad:
                            c.execute("UPDATE external_catalog_import_checkpoint SET status='COMPLETE' WHERE source=%s",(source,))
                            db.commit();break
                        existing={}
                        if valid:
                            for offset in range(0,len(valid),1000):
                                part=valid[offset:offset+1000]
                                c.execute('SELECT id,source_item_id FROM product WHERE source=%s AND source_item_id IN ('+','.join(['%s']*len(part))+')',
                                    (source,*(v[0]['sourceItemId'] for v in part)))
                                existing.update((r['source_item_id'],r['id']) for r in c.fetchall())
                        fresh=sorted((r for r,_,_ in valid if r['sourceItemId'] not in existing),key=lambda r:int(r['id']))
                        # INSERT's existing PK/source-identity unique constraints reject
                        # collisions atomically. A separate probe only repeats random
                        # primary-page reads; it cannot replace those constraints.
                        try:
                            lab.insert_rows(c,'product',lab.products(fresh))
                        except pymysql.IntegrityError as exc:
                            if exc.args[0]==1062:raise ValueError('PRODUCT_IDENTITY_COLLISION; batch not committed') from None
                            raise
                        lab.insert_rows(c,'product_local_offer',[dict(product_id=r['id'],price_minor=r['localOffer']['priceMinor'],
                            currency='CNY',price_kind='local_simulated',source_revision=VERSION,version=1) for r in fresh])
                        lab.insert_rows(c,'inventory_stock',[dict(item_type='PRODUCT',item_id=r['id'],total_quantity=10,
                            available_quantity=10,reserved_quantity=0,sold_quantity=0,version=0) for r in fresh])
                        identities=[dict(product_id=existing.get(r['sourceItemId'],r['id']),
                            raw_sha=raw_sha,source_line=line_no,source_revision=origin['sha256'],import_version=VERSION,
                            preserved_existing=r['sourceItemId'] in existing) for r,line_no,raw_sha in valid]
                        lab.insert_rows(c,'external_catalog_identity',sorted(identities,key=lambda row:int(row['product_id'])))
                        lab.insert_rows(c,'external_catalog_quarantine',bad)
                        c.execute('UPDATE external_catalog_import_checkpoint SET byte_offset=%s,source_line=%s,inserted_count=inserted_count+%s,'
                            'preserved_count=preserved_count+%s,quarantined_count=quarantined_count+%s WHERE source=%s',
                            (f.tell(),line,len(fresh),len(valid)-len(fresh),len(bad),source))
                    db.commit();total_new+=len(fresh)
                    if only_source or line%10000==0 or max_new and total_new>=max_new:
                        with db.cursor() as c:
                            c.execute('SELECT * FROM external_catalog_import_checkpoint ORDER BY source');checkpoints=c.fetchall()
                        db.rollback()
                        progress=dict(checkpoints=checkpoints,newThisRun=total_new,seconds=round(time.monotonic()-started,2),
                            hostFreeBytes={d:shutil.disk_usage(d+':/').free for d in 'DEF'})
                        progress_name='IMPORT-PROGRESS.json' if only_source is None else 'IMPORT-PROGRESS-'+only_source+'.json'
                        (OUT/progress_name).write_text(json.dumps(progress,indent=2,default=str)+'\n',encoding='utf8')
                        print(json.dumps(progress,default=str),flush=True)
                    if max_new and total_new>=max_new:return
        print('ALL_SOURCES_IMPORTED' if only_source is None else only_source.upper()+'_IMPORTED',flush=True)
    finally:
        db.rollback();db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--max-new',type=int,default=100000)
    parser.add_argument('--source',choices=['kuaisearch','multicpr'])
    parser.add_argument('--batch-size',type=int,default=1000)
    args=parser.parse_args();main(args.max_new,args.source,args.batch_size)
