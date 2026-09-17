"""Additional actual Kafka outage/recovery and repeated delivery, isolated project only."""
import asyncio,json,secrets,subprocess,time,uuid
from pathlib import Path
import aiohttp,yaml
from run import ROOT,sql
from faults import ready
RESULT=[]
config=yaml.safe_load((ROOT/'compose.yaml').read_text())
assert config['name']=='reliability-lab-20260910'
config['services']['kafka']={'image':'apache/kafka:3.9.1','cpus':1,'mem_limit':'512m','environment':{
    'KAFKA_NODE_ID':1,'KAFKA_PROCESS_ROLES':'broker,controller','KAFKA_LISTENERS':'INTERNAL://:9092,CONTROLLER://:9093',
    'KAFKA_ADVERTISED_LISTENERS':'INTERNAL://kafka:9092','KAFKA_LISTENER_SECURITY_PROTOCOL_MAP':'INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT',
    'KAFKA_INTER_BROKER_LISTENER_NAME':'INTERNAL','KAFKA_CONTROLLER_LISTENER_NAMES':'CONTROLLER',
    'KAFKA_CONTROLLER_QUORUM_VOTERS':'1@kafka:9093','KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR':1,
    'KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR':1,'KAFKA_TRANSACTION_STATE_LOG_MIN_ISR':1,
    'KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS':0,'KAFKA_HEAP_OPTS':'-Xms128m -Xmx256m'}}
topic='reliability-lab-20260910.events'
config['services']['app2']['environment'].update(MESSAGING_ENABLED='true',KAFKA_BOOTSTRAP_SERVERS='kafka:9092',
    DOMAIN_EVENT_TOPIC=topic,DOMAIN_EVENT_CONSUMER_GROUP='lab-recovery',DOMAIN_EVENT_CONSUMER_ID='lab-kafka',MESSAGE_STALE_CLAIM_AFTER='PT30S')
config['services']['app2']['command']=['java','-Xmx384m','-XX:ActiveProcessorCount=1','-jar','/lab/base.jar']
path=ROOT/'broker.compose.yaml'
assert not path.exists()
path.write_text(yaml.safe_dump(config,sort_keys=False),encoding='utf-8')
COMPOSE=['docker','compose','-f',str(path)]
def command(*args,stdin=None):
    return subprocess.run(COMPOSE+list(args),input=stdin,check=True,capture_output=True,text=True,encoding='utf-8').stdout
async def broker_ready():
    for _ in range(40):
        try:
            command('exec','-T','kafka','/opt/kafka/bin/kafka-topics.sh','--bootstrap-server','localhost:9092','--list');return
        except Exception:await asyncio.sleep(2)
    raise RuntimeError('Broker not ready')
async def run():
    command('up','-d','kafka');await broker_ready()
    command('exec','-T','kafka','/opt/kafka/bin/kafka-topics.sh','--bootstrap-server','localhost:9092','--create','--if-not-exists','--topic',topic,'--partitions','1','--replication-factor','1')
    command('up','-d','--no-deps','app2')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as c:
        await ready(c,38483)
        async with c.post('http://127.0.0.1:38483/api/auth/register',json={'username':'broker-'+uuid.uuid4().hex[:10],'password':secrets.token_urlsafe(24)}) as r:
            data=await r.json();assert r.status==201;token=data['data']['accessToken']
        command('stop','kafka')
        key='broker-'+uuid.uuid4().hex
        try:
            async with c.post('http://127.0.0.1:38483/api/orders',headers={'Authorization':'Bearer '+token,'Idempotency-Key':key},json={'itemType':'PRODUCT','itemId':920249,'quantity':1}) as r:
                data=await r.json();assert r.status==201,data;order=data['data']['id']
            await asyncio.sleep(12)
            event=sql(f"SELECT id FROM outbox_event WHERE aggregate_id='{order}' AND event_type='order.created.v1';").strip()
            before=sql(f"SELECT status,attempts FROM outbox_event WHERE id='{event}';").strip()
        finally:command('start','kafka')
        await broker_ready()
        start=time.monotonic()
        after=''
        for _ in range(90):
            after=sql(f"SELECT status FROM inbox_event WHERE event_id='{event}' AND consumer_name='lab-kafka';").strip()
            if after=='PROCESSED':break
            await asyncio.sleep(2)
        published=sql(f"SELECT status,attempts FROM outbox_event WHERE id='{event}';").strip()
        RESULT.append({'name':'real_kafka_outage_recovery','status':'PASS' if after=='PROCESSED' and published.startswith('PUBLISHED') and not before.startswith('PUBLISHED') else 'FINDING',
            'outboxDuringOutage':before,'outboxAfterRecovery':published,'inbox':after,'recoverySeconds':time.monotonic()-start,'orderId':order})
        # Preserve exact event ID and payload across 20 deliveries.
        value=json.loads(bytes.fromhex(sql(f"SELECT HEX(JSON_OBJECT('id',id,'aggregateType',aggregate_type,'aggregateId',aggregate_id,'eventType',event_type,'payloadJson',CAST(payload_json AS CHAR),'occurredAt',DATE_FORMAT(occurred_at,'%Y-%m-%dT%H:%i:%s'))) FROM outbox_event WHERE id='{event}';").strip()))
        command('exec','-T','kafka','/opt/kafka/bin/kafka-console-producer.sh','--bootstrap-server','localhost:9092','--topic',topic,stdin=(json.dumps(value)+'\n')*20)
        # Wait until lag is zero; receipt count alone does not prove deliveries were consumed.
        lag=''
        for _ in range(30):
            lag=command('exec','-T','kafka','/opt/kafka/bin/kafka-consumer-groups.sh','--bootstrap-server','localhost:9092','--describe','--group','lab-recovery')
            rows=[line.split() for line in lag.splitlines() if topic in line]
            if rows and all(len(row)>5 and row[5]=='0' for row in rows):break
            await asyncio.sleep(2)
        (ROOT/'kafka-offsets.txt').write_text(lag,encoding='utf-8')
        receipt=sql(f"SELECT COUNT(*),MAX(attempts) FROM inbox_event WHERE event_id='{event}' AND consumer_name='lab-kafka';").strip()
        RESULT.append({'name':'real_kafka_duplicate_delivery','status':'PASS' if receipt=='1\t1' and rows and all(row[5]=='0' for row in rows) else 'FINDING',
            'deliveries':20,'receiptCountAndAttempts':receipt,'scope':'audit handler + Inbox, not all fulfillment effects'})
        print(json.dumps(RESULT),flush=True)
if __name__=='__main__':
    try:asyncio.run(run())
    except Exception as e:RESULT.append({'name':'broker_campaign_error','status':'INCOMPLETE','error':repr(e)});raise
    finally:(ROOT/'broker-results.json').write_text(json.dumps(RESULT,indent=2),encoding='utf-8')
