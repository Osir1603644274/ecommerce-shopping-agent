"""Preserve failed JSON extraction; resume via hex-safe SQL and warm-producer outage."""
import asyncio,json,secrets,subprocess,time,uuid
import aiohttp
from run import ROOT,sql
CMD=['docker','compose','-f',str(ROOT/'broker.compose.yaml')]
rows=[];topic='reliability-lab-20260910.events'
def command(*args,stdin=None):
    return subprocess.run(CMD+list(args),input=stdin,capture_output=True,text=True,encoding='utf-8',check=True).stdout
async def run():
    prior=json.loads((ROOT/'broker-results.json').read_text())[0]
    order=prior['orderId'];event=sql(f"SELECT id FROM outbox_event WHERE aggregate_id='{order}' AND event_type='order.created.v1';").strip()
    raw=sql(f"SELECT HEX(JSON_OBJECT('id',id,'aggregateType',aggregate_type,'aggregateId',aggregate_id,'eventType',event_type,'payloadJson',CAST(payload_json AS CHAR),'occurredAt',DATE_FORMAT(occurred_at,'%Y-%m-%dT%H:%i:%s'))) FROM outbox_event WHERE id='{event}';").strip()
    value=json.loads(bytes.fromhex(raw))
    command('exec','-T','kafka','/opt/kafka/bin/kafka-console-producer.sh','--bootstrap-server','localhost:9092','--topic',topic,stdin=(json.dumps(value)+'\n')*20)
    lag='';parsed=[]
    for _ in range(30):
        lag=command('exec','-T','kafka','/opt/kafka/bin/kafka-consumer-groups.sh','--bootstrap-server','localhost:9092','--describe','--group','lab-recovery')
        parsed=[line.split() for line in lag.splitlines() if topic in line]
        if parsed and all(len(r)>5 and r[5]=='0' for r in parsed):break
        await asyncio.sleep(2)
    (ROOT/'kafka-offsets.txt').write_text(lag,encoding='utf-8')
    receipt=sql(f"SELECT COUNT(*),MAX(attempts) FROM inbox_event WHERE event_id='{event}' AND consumer_name='lab-kafka';").strip()
    rows.append({'name':'real_kafka_duplicate_delivery','status':'PASS' if receipt=='1\t1' and parsed and all(r[5]=='0' for r in parsed) else 'FINDING','deliveries':20,'receiptCountAndAttempts':receipt,'lagZero':bool(parsed) and all(r[5]=='0' for r in parsed)})
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as c:
        async with c.post('http://127.0.0.1:38483/api/auth/register',json={'username':'warm-'+uuid.uuid4().hex[:10],'password':secrets.token_urlsafe(24)}) as r:
            data=await r.json();assert r.status==201;token=data['data']['accessToken']
        assert int(sql("SELECT COUNT(*) FROM outbox_event WHERE status IN ('PENDING','PROCESSING');"))==0
        command('stop','kafka')
        try:
            async with c.post('http://127.0.0.1:38483/api/orders',headers={'Authorization':'Bearer '+token,'Idempotency-Key':str(uuid.uuid4())},json={'itemType':'PRODUCT','itemId':920248,'quantity':1}) as r:
                data=await r.json();assert r.status==201,data;oid=data['data']['id']
            eid=sql(f"SELECT id FROM outbox_event WHERE aggregate_id='{oid}' AND event_type='order.created.v1';").strip()
            await asyncio.sleep(25)
            failed=sql(f"SELECT status,attempts FROM outbox_event WHERE id='{eid}';").strip()
        finally:command('start','kafka')
        start=time.monotonic();end=''
        for _ in range(90):
            end=sql(f"SELECT status FROM inbox_event WHERE event_id='{eid}' AND consumer_name='lab-kafka';").strip()
            if end=='PROCESSED':break
            await asyncio.sleep(2)
        final=sql(f"SELECT status,attempts FROM outbox_event WHERE id='{eid}';").strip()
        attempts=int(failed.split('\t')[1])
        rows.append({'name':'real_kafka_warm_producer_failure','status':'PASS' if attempts>0 and end=='PROCESSED' and final.startswith('PUBLISHED') else 'FINDING','duringOutage':failed,'afterRecovery':final,'inbox':end,'recoverySeconds':time.monotonic()-start})
        print(json.dumps(rows),flush=True)
try:asyncio.run(run())
finally:(ROOT/'broker-continuation-results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
