"""Observe active import and run read-only full verification only at both EOFs."""
import json,subprocess,sys,time
import pymysql
from prepare import OUT,ROOT,write
secret=json.loads((OUT/'connection.private.json').read_text());assert secret['database']=='commerce_candidate'
while True:
    db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},
        cursorclass=pymysql.cursors.DictCursor,autocommit=True,connect_timeout=10)
    with db.cursor() as c:
        c.execute('SELECT * FROM external_catalog_import_checkpoint ORDER BY source');rows=c.fetchall()
    db.close()
    status=dict(checkpoints=rows,processed=sum(r['source_line'] for r in rows),total=7636940,
        inserted=sum(r['inserted_count'] for r in rows),preserved=sum(r['preserved_count'] for r in rows),
        quarantined=sum(r['quarantined_count'] for r in rows),observedAtUnix=time.time())
    write('CURRENT-IMPORT-STATUS.json',status)
    print(json.dumps({k:v for k,v in status.items() if k!='checkpoints'}),flush=True)
    if len(rows)==2 and all(r['status']=='COMPLETE' for r in rows):break
    time.sleep(45)
print('All sources reached EOF; starting full read-only SQL invariants.',flush=True)
with (OUT/'verify-final.log').open('x',encoding='utf8') as log:
    result=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/commerce-full-release-20260915/verify-final.py')],stdout=log,stderr=subprocess.STDOUT)
if result.returncode:raise RuntimeError('Final invariants failed; inspect verify-final.log')
print('Full import and SQL invariants PASS; backup and cutover gates still required.',flush=True)
