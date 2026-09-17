"""Read-only evidence that a new worker advances the same atomic ordered cursor."""
import json,sys
import pymysql
from prepare import OUT,write
secret=json.loads((OUT/'connection.private.json').read_text());assert secret['database']=='commerce_candidate'
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},cursorclass=pymysql.cursors.DictCursor)
with db.cursor() as c:
    c.execute("SELECT * FROM external_catalog_import_checkpoint WHERE source='kuaisearch'");cp=c.fetchone()
    c.execute("SELECT * FROM commerce_release_ordered_import WHERE source='kuaisearch'");ordered=c.fetchone()
db.rollback();db.close()
base=json.loads(ordered['baseline_json'])
assert cp['source_line']==base['source_line']+ordered['processed_count']+ordered['quarantined_count']
assert cp['inserted_count']==base['inserted_count']+ordered['inserted_count']
assert cp['preserved_count']==base['preserved_count']+ordered['preserved_count']
assert cp['quarantined_count']==base['quarantined_count']+ordered['quarantined_count']
evidence=dict(sourceCheckpoint=cp,orderedCheckpoint=ordered)
if sys.argv[1:]==['first']:
    assert ordered['processed_count']==20000 and not (OUT/'ORDERED-FIRST-BATCH.json').exists()
    write('ORDERED-FIRST-BATCH.json',evidence);print('First ordered batch and both SQL cursors consistent.')
elif sys.argv[1:]==['resumed']:
    first=json.loads((OUT/'ORDERED-FIRST-BATCH.json').read_text())['orderedCheckpoint']
    assert ordered['artifact_sha']==first['artifact_sha']
    assert ordered['last_product_id']>first['last_product_id'] and ordered['processed_count']>=40000
    evidence.update(status='PASS',scope='clean process exit and new-worker resume; not a crash-injection claim')
    write('ORDERED-RESUME.json',evidence);print('New worker advanced the sealed cursor; counters remain consistent.')
else:raise ValueError('explicit first or resumed required')
