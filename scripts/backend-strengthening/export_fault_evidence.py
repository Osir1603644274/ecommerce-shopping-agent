"""Read-only consistent snapshots of dedicated V3/V4 trade fixtures; excludes credentials."""
import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path
import pymysql


def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    config=json.loads((a.runtime/'compose.validation.json').read_text())
    assert config['name'] in ('backend-strengthening-v3','backend-strengthening-v4')
    secret=dict(line.split('=',1) for line in (a.runtime/'compose.env').read_text().splitlines() if '=' in line)
    a.output.mkdir(parents=True,exist_ok=False)
    tables=['flyway_schema_history','customer_order','order_item','inventory_stock','inventory_reservation',
        'payment_record','payment_notification','refund_record','fulfillment_task','fulfillment_attempt',
        'outbox_event','inbox_event','dead_letter_event','order_line_allocation','fulfillment_command_version',
        'partial_refund','partial_refund_item','local_refund_receipt','user_coupon','coupon_template']
    db=pymysql.connect(host='127.0.0.1',port=33316,user='root',password=secret['BENCH_DB_PASSWORD'],
        database='backend_strengthening',cursorclass=pymysql.cursors.DictCursor)
    counts={};started=time.time()
    try:
        with db.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY');cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            cursor.execute('SELECT @@server_uuid AS serverUuid,@@version AS mysqlVersion,@@transaction_isolation AS isolation')
            identity=cursor.fetchone()
            cursor.execute('SHOW TABLES');available={next(iter(row.values())) for row in cursor.fetchall()}
            for table in tables:
                if table not in available:continue
                cursor.execute('SELECT * FROM '+table);rows=cursor.fetchall();counts[table]=len(rows)
                (a.output/(table+'.json')).write_text(json.dumps(rows,indent=2,default=str),encoding='utf-8')
            schema={}
            for table in counts:
                cursor.execute('SHOW CREATE TABLE '+table)
                schema[table]=cursor.fetchone()
            (a.output/'schema.json').write_text(json.dumps(schema,indent=2,default=str),encoding='utf-8')
        db.rollback()
    finally:db.close()
    with sqlite3.connect((a.runtime/'warehouse.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as source:
        with sqlite3.connect(a.output/'warehouse-final-snapshot.sqlite') as destination:source.backup(destination)
    files=[{'path':x.name,'bytes':x.stat().st_size,'sha256':hashlib.sha256(x.read_bytes()).hexdigest()}
           for x in sorted(a.output.iterdir()) if x.is_file()]
    (a.output/'manifest.json').write_text(json.dumps({'project':config['name'],'startedUnix':started,
        'finishedUnix':time.time(),'database':identity,'rowCounts':counts,'files':files,
        'excluded':'No account/password/JWT/refresh-token/config secrets'},indent=2),encoding='utf-8')
    print(json.dumps({'project':config['name'],'counts':counts,'files':len(files)}))


if __name__=='__main__':main()
