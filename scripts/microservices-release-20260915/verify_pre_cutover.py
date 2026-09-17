"""Read-only final checks before write freeze."""
import json
import pymysql
import httpx
from live_release import SCHEMAS,PRIVATE,NAMES
from topology_lab import OUT,root_connection,docker,write

config=json.loads(PRIVATE.read_text(encoding='utf8'));root=root_connection()
with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},database=root['database'],autocommit=True) as db,db.cursor() as c:
    c.execute('SELECT schema_name FROM information_schema.schemata WHERE schema_name IN (%s,%s)',(SCHEMAS['catalog'],SCHEMAS['inventory']))
    assert not c.fetchall(),'destination schema must be new'
    for account in config['accounts'].values():
        c.execute('SELECT user FROM mysql.user WHERE user=%s',(account['user'],));assert not c.fetchall(),'service principal must be new'
    c.execute('SELECT event_type,COUNT(*) FROM outbox_event WHERE published_at IS NULL GROUP BY event_type');pending=c.fetchall()
    assert not pending,'pending Outbox must drain first'
    c.execute("SELECT COUNT(*) FROM inbox_event WHERE status='PROCESSING'");assert c.fetchone()[0]==0
    c.execute('SELECT status,COUNT(*) FROM partial_refund GROUP BY status');refunds=c.fetchall()
assert not set(NAMES.values()).intersection(docker('ps','-a','--format','{{.Names}}').splitlines())
for port,path in [(5173,'/'),(8000,'/health'),(18110,'/health'),(8080,'/actuator/health')]:
    assert httpx.get('http://127.0.0.1:'+str(port)+path,timeout=10,trust_env=False).status_code==200
assert json.loads((OUT/'SEARCH-OUTAGE-ACCEPTANCE.json').read_text())['status']=='PASS'
write('LIVE-PRE-CUTOVER.json',{'status':'PASS','newSchemasAbsent':True,'newPrincipalsAbsent':True,'pendingOutbox':pending,
    'partialRefundStatesPreservedWithoutCompletingThem':refunds,'allCurrentEndpointsAvailable':True})
print('Pre-cutover ownership, drained events and live endpoints PASS; existing refund states remain untouched.')
