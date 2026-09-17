"""Final read-only deployment/evidence audit; --publish selects verified status only after all gates."""
import hashlib,json,sys,time
from pathlib import Path
import httpx,pymysql
from live_release import STATE,PRIVATE,SCHEMAS,NAMES,PORTS,owned,verify_inventory_write_binding,save
from topology_lab import ROOT,OUT,root_connection,write,docker

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()

gates={
 'inventory-protocol':'INVENTORY-PROTOCOL-445d902a12.json',
 'isolated-full-transaction':'TOPOLOGY-ACCEPTANCE-9de289e251.json',
 'process-death-and-lost-reply':'DISTRIBUTED-FAULTS-ecc9c24b99.json',
 'payment-during-inventory-outage':'INVENTORY-OUTAGE-bd32dc4138.json',
 'search-isolation':'SEARCH-OUTAGE-ACCEPTANCE.json',
 'frozen-backup-restore':'FROZEN-LIVE-BACKUP.json',
 'database-authority':'LIVE-OWNERSHIP-ACCEPTANCE.json',
 'public-full-transaction':'PUBLIC-MICROSERVICES-TRADE-063450b4ce.json',
 'real-warehouse-receipt':'PUBLIC-MICROSERVICES-FULFILLMENT.json',
 'legacy-multicart-and-payment-cancel-race':'LIVE-PHONES-RACE-fd277fd438.json',
 'live-owner-restarts':'LIVE-RESTARTS-31f8cdad67.json',
 'migration-fk-regression':'FK-MIGRATION-REGRESSION-ce777d93.json'}
search=max(OUT.glob('INDEPENDENT-SEARCH-ACCEPTANCE-*.json'),key=lambda p:p.stat().st_mtime)
gates['post-agent-restart-search']=search.name
report={'status':'AUDITING','gates':{},'limits':['same host','one physical MySQL','single search owner','single inventory URL','local payment and warehouse simulators']}
for name,file in gates.items():
    path=OUT/file;data=json.loads(path.read_text(encoding='utf8'));assert data['status']=='PASS',(name,data.get('status'))
    report['gates'][name]={'artifact':file,'sha256':sha(path)}
ui=OUT/'public-ui-results-003.json';stats=json.loads(ui.read_text(encoding='utf8'))['stats']
assert stats['expected']==1 and stats['unexpected']==0 and stats['skipped']==0,stats
report['gates']['public-browser']={'artifact':ui.name,'stats':stats,'sha256':sha(ui)}
log=OUT/'trade-linux-full-006.log';assert 'Tests run: 310, Failures: 0, Errors: 0, Skipped: 0' in log.read_text(encoding='utf8')
report['gates']['java-full-suite']={'artifact':log.name,'sha256':sha(log),'tests':310}
state=json.loads(STATE.read_text(encoding='utf8'));assert state['status'] in ('SERVICES_READY','LIVE_VERIFIED')
assert not (ROOT/'.runtime/merged-commerce/microservices-maintenance.json').exists()
config=json.loads(PRIVATE.read_text(encoding='utf8'));assert config['images']==state['images']
report['images']=state['images'];report['services']={}
for role in NAMES:
    info=owned(NAMES[role]);expected=state['images']['backend' if role.startswith(('catalog','trade')) else role]
    assert info['Image']==expected and info['State']['Running']
    r=httpx.get('http://127.0.0.1:'+str(PORTS[role])+'/actuator/health',timeout=4,trust_env=False);assert r.status_code==200
    report['services'][role]={'image':info['Image'],'startedAt':info['State']['StartedAt'],'healthy':True}
assert not json.loads(docker('inspect',state['oldContainer']))[0]['State']['Running']
for port in (5173,8000,18110):
    r=httpx.get('http://127.0.0.1:'+str(port)+('/' if port==5173 else '/health'),timeout=4,trust_env=False);assert r.status_code==200
root=root_connection();verify_inventory_write_binding(root)
with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},autocommit=True,cursorclass=pymysql.cursors.DictCursor,read_timeout=180) as db,db.cursor() as c:
    c.execute('SELECT COUNT(*) AS n FROM '+SCHEMAS['catalog']+'.product');count=c.fetchone()['n'];assert count==7637043;report['productCount']=count
    c.execute('SELECT COUNT(*) AS n FROM '+SCHEMAS['inventory']+'.inventory_stock WHERE total_quantity<>available_quantity+reserved_quantity+sold_quantity OR available_quantity<0 OR reserved_quantity<0 OR sold_quantity<0');assert c.fetchone()['n']==0
    c.execute('SELECT COUNT(*) AS n FROM '+SCHEMAS['inventory']+'.inventory_reservation r LEFT JOIN '+SCHEMAS['inventory']+'.inventory_stock s ON s.id=r.stock_id WHERE s.id IS NULL');assert c.fetchone()['n']==0
    c.execute('SELECT table_schema,table_name,referenced_table_schema FROM information_schema.key_column_usage WHERE table_schema IN (%s,%s,%s) AND referenced_table_schema IS NOT NULL AND table_schema<>referenced_table_schema',tuple(SCHEMAS.values()));assert not c.fetchall()
    c.execute("SELECT account_locked FROM mysql.user WHERE user='commerce_live' AND host='%'");assert c.fetchone()['account_locked']=='Y'
    c.execute('SELECT kind,status,COUNT(*) AS n FROM '+SCHEMAS['trade']+'.inventory_command_journal GROUP BY kind,status');report['inventoryJournal']=c.fetchall()
    assert all(r['status'] in ('ACK','CANCELLED') for r in report['inventoryJournal']),report['inventoryJournal']
report['originalDataMigrationPreserved']=state['allOriginalTableCountsPreserved'];assert report['originalDataMigrationPreserved']
backup=json.loads((OUT/gates['frozen-backup-restore']).read_text(encoding='utf8'));assert backup['restoredAllCountsMatch'] and backup['restoredSmallTableContentsMatch']
paths=set()
for folder in ('backend/src','backend-inventory/src','backend-gateway/src','scripts/microservices-release-20260915'):
    paths.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
for name in ('README.md','docs/PUBLIC_REPOSITORY_GUIDE.md','docs/diagrams/current-architecture/README.md','scripts/merged-commerce.ps1','scripts/unified-commerce.ps1','agent/app/catalog_search_server.py','agent/app/catalog_remote.py','agent/app/catalog_service.py','agent/app/catalog_index.py','agent/app/backend_observer.py','frontend/tests/microservices-public.spec.ts'):
    paths.add(ROOT/name)
paths.update((ROOT/'docs/architecture/microservices-release-20260915').glob('*.md'))
manifest={p.relative_to(ROOT).as_posix():sha(p) for p in sorted(paths)}
write('FINAL-SOURCE-MANIFEST.json',manifest)
report['manifestSha256']=sha(OUT/'FINAL-SOURCE-MANIFEST.json')
report.update(status='PASS',auditedAtUnix=time.time())
write('FINAL-RELEASE-AUDIT.json',report)
if '--publish' in sys.argv:
    state.update(status='LIVE_VERIFIED',verifiedAtUnix=report['auditedAtUnix'],finalAudit='FINAL-RELEASE-AUDIT.json',sourceManifest='FINAL-SOURCE-MANIFEST.json')
    save(state)
print(json.dumps({'status':'PASS','published':'--publish' in sys.argv,'gates':len(report['gates']),'products':count}))
