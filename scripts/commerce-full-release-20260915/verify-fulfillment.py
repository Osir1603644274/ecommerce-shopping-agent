"""Read real isolated Kafka receipts, durable warehouse request and SQL outcome."""
import json,sqlite3
import pymysql
from prepare import OUT,write

secret=json.loads((OUT/'stage-connection.private.json').read_text());assert secret['database']=='commerce_acceptance'
trade=json.loads((OUT/'ACCEPTANCE-TRADE.json').read_text(encoding='utf8'))
order=trade['steps'][0]['result']['id']
db=pymysql.connect(**secret,cursorclass=pymysql.cursors.DictCursor,autocommit=True)
with db.cursor() as c:
    c.execute('SELECT * FROM fulfillment_task WHERE order_id=%s',(order,));task=c.fetchone()
    c.execute('SELECT * FROM order_line_allocation WHERE order_id=%s',(order,));lines=c.fetchall()
    c.execute('SELECT id,event_type,status FROM outbox_event WHERE aggregate_id=%s',(order,));events=c.fetchall()
    ids=[e['id'] for e in events]
    c.execute('SELECT consumer_name,event_id,status FROM inbox_event WHERE event_id IN ('+','.join(['%s']*len(ids))+')',ids);receipts=c.fetchall()
db.close()
with sqlite3.connect('file:'+str(OUT/'stage-warehouse/warehouse.sqlite3')+'?mode=ro',uri=True) as warehouse:
    rows=warehouse.execute('SELECT request_key,order_id,command_hash,tracking_no,command_json FROM shipment WHERE order_id=?',(order,)).fetchall()
report=dict(orderId=order,task=task,lines=lines,events=events,receipts=receipts,warehouse=rows)
write('ACCEPTANCE-FULFILLMENT.json',report)
assert task['status']=='SHIPPED',task['status']
assert len(rows)==1
assert all(e['status']=='PUBLISHED' for e in events),events
assert len([r for r in receipts if r['consumer_name']=='fulfillment-v1' and r['status']=='PROCESSED'])==len(events)
assert lines[0]['refunded_quantity']==1,lines
command=json.loads(task['command_json']);assert command['items'][0]['quantity']==1
assert json.loads(rows[0][4])==command
receipt=json.loads(task['receipt_json']);assert receipt['commandHash']==rows[0][2] and receipt['trackingNo']==rows[0][3]
report['status']='PASS';write('ACCEPTANCE-FULFILLMENT.json',report)
print('Kafka Outbox/Inbox + warehouse receipt PASS; refunded one unit, shipped the remaining one.')
