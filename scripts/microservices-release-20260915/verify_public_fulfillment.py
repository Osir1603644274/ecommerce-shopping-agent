"""Read-only wait for the dedicated public order's remaining item to ship normally."""
import json
import sqlite3
import time
import pymysql
from live_release import SCHEMAS,PRIVATE
from topology_lab import ROOT,OUT,root_connection,write

latest=json.loads((OUT/'PUBLIC-MICROSERVICES-LATEST.json').read_text(encoding='utf8'))
report=json.loads((OUT/latest['artifact']).read_text(encoding='utf8'));assert report['status']=='PASS'
order=report['steps']['order']['id'];config=json.loads(PRIVATE.read_text(encoding='utf8'));account=config['accounts']['trade']
deadline=time.monotonic()+360
while True:
    root=root_connection()
    with pymysql.connect(host=root['host'],port=root['port'],user=account['user'],password=account['password'],database=SCHEMAS['trade'],autocommit=True,
                         cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
        c.execute('SELECT * FROM fulfillment_task WHERE order_id=%s',(order,));task=c.fetchone()
        c.execute('SELECT kind,status FROM inventory_command_journal WHERE order_id=%s',(order,));journal=c.fetchall()
        c.execute('SELECT event_type,status FROM outbox_event WHERE aggregate_id=%s',(order,));events=c.fetchall()
    if task and task['status']=='SHIPPED' and all(r['status']=='ACK' for r in journal):break
    if time.monotonic()>deadline:
        write('PUBLIC-MICROSERVICES-FULFILLMENT.json',{'status':'FAIL','orderId':order,'task':task,'journal':journal})
        raise AssertionError('normal fulfillment did not settle; no forced status change')
    print('Waiting for normal fulfillment:',task['status'] if task else 'not-enrolled',flush=True);time.sleep(10)
path=ROOT/'.runtime/merged-commerce/warehouse/warehouse.sqlite3'
with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as warehouse:
    rows=warehouse.execute('SELECT request_key,order_id,command_hash,tracking_no,command_json FROM shipment WHERE order_id=?',(order,)).fetchall()
assert len(rows)==1
command=json.loads(task['command_json']);assert sum(i['quantity'] for i in command['items'])==1
assert json.loads(rows[0][4])==command
receipt=json.loads(task['receipt_json']);assert receipt['commandHash']==rows[0][2] and receipt['trackingNo']==rows[0][3]
write('PUBLIC-MICROSERVICES-FULFILLMENT.json',{'status':'PASS','orderId':order,'task':task,'inventoryJournal':journal,'outbox':events,'warehouse':rows})
print('One remaining item shipped with one matching durable warehouse receipt; all inventory commands ACK.')
