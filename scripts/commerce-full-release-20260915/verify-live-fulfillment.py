"""Wait for the owned public test order's normal local fulfillment; never force dispatch."""
import json,sqlite3,time
import pymysql
from prepare import OUT,ROOT,DB,write,refresh_connections

refresh_connections()
secret=json.loads((OUT/'live-connection.private.json').read_text());assert secret['database']==DB
trade=json.loads((OUT/'LIVE-TRADE.json').read_text(encoding='utf8'));assert trade['status']=='PASS'
order=trade['steps']['order']['id'];deadline=time.monotonic()+360
while True:
    db=pymysql.connect(**secret,cursorclass=pymysql.cursors.DictCursor,autocommit=True)
    with db.cursor() as c:
        c.execute('SELECT * FROM fulfillment_task WHERE order_id=%s',(order,));task=c.fetchone()
        c.execute('SELECT id,event_type,status FROM outbox_event WHERE aggregate_id=%s',(order,));events=c.fetchall()
        receipts=[]
        if events:
            c.execute('SELECT consumer_name,event_id,status FROM inbox_event WHERE event_id IN ('+','.join(['%s']*len(events))+')',[e['id'] for e in events]);receipts=c.fetchall()
    db.close()
    if task and task['status']=='SHIPPED' and events and all(e['status']=='PUBLISHED' for e in events):break
    print('Public test fulfillment:',task['status'] if task else 'awaiting event',flush=True)
    if time.monotonic()>=deadline:raise TimeoutError('normal fulfillment did not finish; inspect task/outbox without forcing state')
    time.sleep(10)
path=ROOT/'.runtime/merged-commerce/warehouse/warehouse.sqlite3'
with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as warehouse:
    rows=warehouse.execute('SELECT request_key,order_id,command_hash,tracking_no,command_json FROM shipment WHERE order_id=?',(order,)).fetchall()
assert len(rows)==1
assert len([r for r in receipts if r['consumer_name']=='fulfillment-v1' and r['status']=='PROCESSED'])==len(events)
command=json.loads(task['command_json']);assert len(command['items'])==1 and command['items'][0]['quantity']==1
assert json.loads(rows[0][4])==command
receipt=json.loads(task['receipt_json']);assert receipt['commandHash']==rows[0][2] and receipt['trackingNo']==rows[0][3]
write('LIVE-FULFILLMENT.json',dict(status='PASS',orderId=order,task=task,events=events,receipts=receipts,warehouse=rows))
print('Public test order: durable Kafka processing and exactly one matching warehouse command/receipt PASS.',flush=True)
