"""Independent MySQL/Redis assertions, without using the application's state endpoint."""
import datetime,json,pathlib,subprocess
ROOT=pathlib.Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/messaging-migration/evidence'/('sql-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
OUT.mkdir()
C=['docker','compose','-f',str(ROOT/'scripts/messaging-migration/compose.yml')]
def sql(query,name):
    p=subprocess.run(C+['exec','-T','mysql','mysql','-uroot','-pisolated-migration-only','-D','migration_lab','--batch','--raw','-e',query],capture_output=True,text=True,encoding='utf-8',check=True)
    (OUT/(name+'.tsv')).write_text(p.stdout,encoding='utf-8')
    lines=p.stdout.strip().splitlines()
    if not lines:return []
    return [dict(zip(lines[0].split('\t'),line.split('\t'))) for line in lines[1:]]
rows=sql('''SELECT c.id,c.total_stock,c.available_stock,
 (SELECT COUNT(*) FROM flash_sale_order o WHERE o.campaign_id=c.id) AS orders,
 (SELECT COUNT(*) FROM flash_sale_request r WHERE r.campaign_id=c.id) AS accepted,
 (SELECT COUNT(*) FROM flash_sale_request r WHERE r.campaign_id=c.id AND r.status='COMPLETED') AS completed,
 (SELECT COUNT(*) FROM flash_sale_request r WHERE r.campaign_id=c.id AND r.status='DEAD') AS dead,
 (SELECT COUNT(*) FROM flash_sale_request r WHERE r.campaign_id=c.id AND r.status IN ('PENDING','COMPENSATING')) AS pending
 FROM flash_sale_campaign c WHERE c.title='lab campaign' ORDER BY c.id''','per-campaign')
errors=[]
for r in rows:
    if int(r['total_stock'])-int(r['available_stock'])!=int(r['orders']):errors.append(r)
    if int(r['orders'])!=int(r['completed']) or int(r['pending']) or int(r['accepted'])!=int(r['completed'])+int(r['dead']):errors.append(r)
    p=subprocess.run(C+['exec','-T','redis','redis-cli','GET',f"flash:{{flash-sale}}:campaign:{r['id']}:stock"],capture_output=True,text=True,check=True)
    r['redis_stock']=p.stdout.strip()
    if int(r['redis_stock'])!=int(r['available_stock']):errors.append(r)
requests=sql('SELECT * FROM flash_sale_request ORDER BY campaign_id,id','requests')
linked=sql('''SELECT r.id,r.status,r.completed_order_id,o.id AS order_id,
 r.campaign_id AS request_campaign,o.campaign_id AS order_campaign,
 r.user_id AS request_user,o.user_id AS order_user,r.amount_minor AS request_amount,o.amount_minor AS order_amount
 FROM flash_sale_request r LEFT JOIN flash_sale_order o ON o.id=r.completed_order_id ORDER BY r.campaign_id,r.id''','per-request-linkage')
for r in linked:
    if r['status']=='COMPLETED':
        if not (r['id']==r['completed_order_id']==r['order_id'] and r['request_campaign']==r['order_campaign'] and r['request_user']==r['order_user'] and r['request_amount']==r['order_amount']): errors.append(r)
    elif r['status']=='DEAD':
        if r['order_id']!='NULL':errors.append(r)
    else:errors.append(r)
orphans=sql('SELECT o.id FROM flash_sale_order o LEFT JOIN flash_sale_request r ON o.id=r.completed_order_id WHERE r.id IS NULL','unaccepted-orders')
errors.extend(orphans)
sql('SELECT * FROM flash_sale_order ORDER BY campaign_id,id','orders')
sql("SELECT * FROM dead_letter_event WHERE source='FLASH_SALE' ORDER BY id",'dead-letters')
sql('SELECT * FROM cache_invalidation_outbox ORDER BY created_at,id','cache-outbox')
sql("SELECT id,title,entity_version FROM product WHERE source='messaging-lab' ORDER BY id",'products')
sql('SELECT * FROM lab_fault ORDER BY id','faults')
result={'pass':not errors,'campaigns':len(rows),'rows':rows,'errors':errors}
(OUT/'assertions.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result));print(OUT)
if errors:raise SystemExit(1)
