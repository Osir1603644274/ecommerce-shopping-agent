"""Read-only large-catalog plans for the explicitly bounded phone and source APIs."""
import json,time
import pymysql
from prepare import OUT,write
secret=json.loads((OUT/'connection.private.json').read_text())
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},
    autocommit=True,cursorclass=pymysql.cursors.DictCursor,read_timeout=30)
queries={
    'legacyPhoneScope':("SELECT id,title FROM product WHERE lifecycle_status='ACTIVE' AND id IN "
        "(SELECT product_id FROM catalog_version_member WHERE catalog_version=%s) "
        "AND (category_l1=%s OR category_l2 LIKE %s OR category_l3 LIKE %s) ORDER BY id LIMIT 1500",
        ('merged-used-phone-439-20260909-v1','手机','%手机%','%手机%')),
    'sourceIdentity':("SELECT p.id FROM product p JOIN external_catalog_identity i ON i.product_id=p.id "
        "WHERE p.source=%s AND p.source_item_id=%s AND i.raw_sha=%s AND p.lifecycle_status='ACTIVE'",
        ('multicpr','1002822',bytes.fromhex('61fe0ce2adabf32d4731651c9b849f7d7255d3a41beb142ecbd8b148ffbbb0da')))
}
checks={}
with db.cursor() as c:
    for name,(sql,args) in queries.items():
        c.execute('EXPLAIN '+sql,args);plan=c.fetchall()
        assert not any(r['table'] in ('product','p') and r['type']=='ALL' for r in plan),plan
        started=time.monotonic();c.execute(sql,args);rows=c.fetchall()
        assert len(rows)==(439 if name=='legacyPhoneScope' else 1)
        checks[name]=dict(plan=plan,rowCount=len(rows),elapsedMs=round((time.monotonic()-started)*1000,2))
    c.execute('SELECT source,source_line,status FROM external_catalog_import_checkpoint');checkpoints=c.fetchall()
db.close()
phase='full' if all(row['status']=='COMPLETE' for row in checkpoints) else 'during-import'
write('CATALOG-READ-PLANS-'+phase+'.json',dict(status='PASS',checks=checks,checkpoints=checkpoints,
    scope='single read-only probes during import; not a performance benchmark'))
print(json.dumps(checks,default=str))
