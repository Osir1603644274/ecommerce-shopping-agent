"""Full candidate invariants; refuses unfinished import and never changes data."""
import json,hashlib,shutil
import pymysql
from prepare import OUT,write

EXPECTED={'kuaisearch':(6634118,6634053,65),'multicpr':(1002822,1002796,26)}
secret=json.loads((OUT/'connection.private.json').read_text());assert secret['database']=='commerce_candidate'
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},
    charset='utf8mb4',read_timeout=1800,cursorclass=pymysql.cursors.DictCursor)
baseline=json.loads((OUT/'original-counts.json').read_text())
old=json.loads((OUT/'original-catalog.json').read_text(encoding='utf8'))
report={'status':'VERIFYING','checks':{}}
def checked(key,value):
    report['checks'][key]=value;write('FINAL-INVARIANTS.json',report);print(key+' verified',flush=True)
def stable(rows):return sorted(json.dumps(r,sort_keys=True,default=str,ensure_ascii=False) for r in rows)
with db.cursor() as c:
    c.execute('SET TRANSACTION READ ONLY')
    c.execute('SELECT * FROM external_catalog_import_checkpoint ORDER BY source');cp=c.fetchall()
    assert {r['source'] for r in cp}==set(EXPECTED)
    for r in cp:
        total,valid,bad=EXPECTED[r['source']]
        assert r['status']=='COMPLETE' and r['source_line']==total
        assert r['inserted_count']+r['preserved_count']==valid and r['quarantined_count']==bad
    checked('completedCheckpoints',cp)
    c.execute("SHOW TABLES LIKE 'commerce_release_ordered_import'")
    if c.fetchone():
        ordered=json.loads((OUT/'ORDERED-TAIL-MANIFEST.json').read_text())
        c.execute('SELECT * FROM commerce_release_ordered_import');ordered_rows=c.fetchall()
        assert len(ordered_rows)==1
        receipt=ordered_rows[0]
        assert receipt['source']=='kuaisearch' and receipt['status']=='COMPLETE'
        assert receipt['artifact_sha']==ordered['artifactSha256']
        assert receipt['processed_count']==ordered['validRows']
        assert receipt['quarantined_count']==ordered['quarantinedRows']
        assert receipt['inserted_count']+receipt['preserved_count']==receipt['processed_count']
        assert json.loads(receipt['baseline_json'])==ordered['baseline']
        checked('orderedTailCheckpoint',ordered_rows)
    new=sum(r['inserted_count'] for r in cp);kept=sum(r['preserved_count'] for r in cp)
    assert kept==1752 and new==7635097
    for table,expected in [('product',baseline['product']+new),('product_local_offer',baseline['product_local_offer']+new),
                           ('inventory_stock',baseline['inventory_stock']+new),('external_catalog_identity',7636849),
                           ('external_catalog_quarantine',91),('catalog_version_member',439)]:
        c.execute('SELECT COUNT(*) n FROM `'+table+'`');actual=c.fetchone()['n'];assert actual==expected,(table,actual,expected)
        checked(table+'Count',actual)
    # Every fresh identity must have exact initial price/stock and deterministic product ID.
    c.execute("""SELECT COUNT(*) bad FROM external_catalog_identity i
        LEFT JOIN product p ON p.id=i.product_id
        LEFT JOIN product_local_offer o ON o.product_id=p.id
        LEFT JOIN inventory_stock s ON s.item_type='PRODUCT' AND s.item_id=p.id
        WHERE NOT i.preserved_existing AND (
          p.id IS NULL OR o.product_id IS NULL OR s.id IS NULL
          OR p.id <> 4000000000000000 + IF(p.source='multicpr',1000000000000,0)+CAST(p.source_item_id AS UNSIGNED)
          OR p.source NOT IN ('kuaisearch','multicpr') OR p.lifecycle_status<>'ACTIVE'
          OR p.snapshot_price_minor IS NOT NULL OR p.price_status<>'missing'
          OR o.price_kind<>'local_simulated' OR o.source_revision<>'external-simulated-catalog-v1'
          OR o.currency<>'CNY' OR p.currency<>'CNY'
          OR o.price_minor <> (10+MOD(CAST(CONV(SUBSTRING(SHA2(CONCAT('external-simulated-catalog-v1|price|',p.source,':',p.source_item_id),256),1,16),16,10) AS DECIMAL(20,0)),9990))*100
          OR s.total_quantity<>10 OR s.available_quantity<>10 OR s.reserved_quantity<>0 OR s.sold_quantity<>0
          OR BINARY p.dataset_revision<>BINARY i.source_revision OR i.import_version<>'external-simulated-catalog-v1')""")
    assert c.fetchone()['bad']==0;checked('everyNewProductOfferStockInvariant',{'rows':new,'violations':0})
    # Preserve prior catalog/offer/inventory exactly, not only row counts.
    for table,rows in old.items():
        if table in ('product','inventory_stock'):key='id'
        elif table=='product_local_offer':key='product_id'
        else:
            c.execute('SELECT * FROM `'+table+'`');assert stable(c.fetchall())==stable(rows),table
            checked(table+'OriginalRowsExact',len(rows));continue
        ids=[r[key] for r in rows]
        c.execute('SELECT * FROM `'+table+'` WHERE `'+key+'` IN ('+','.join(['%s']*len(ids))+')',ids)
        assert stable(c.fetchall())==stable(rows),table
        checked(table+'OriginalRowsExact',len(rows))
    for table,count in baseline.items():
        if table in ('product','product_local_offer','inventory_stock'):continue
        c.execute('SELECT COUNT(*) n FROM `'+table+'`');assert c.fetchone()['n']==count,table
    checked('originalBusinessTableCountsUnchanged',True)
db.rollback();db.close()
report.update(status='PASS',newProducts=new,preservedSourceIdentities=kept,
    hostFreeBytes={d:shutil.disk_usage(d+':/').free for d in 'DEF'})
write('FINAL-INVARIANTS.json',report)
print('Full SQL invariants PASS.',flush=True)
