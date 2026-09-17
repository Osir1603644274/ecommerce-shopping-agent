"""Exercise live FK enforcement inside rolled-back transactions; no stock mutations."""
import json
import uuid
import pymysql
from topology_lab import root_connection, write
from live_release import SCHEMAS

r=root_connection()
report={'status':'RUNNING','checks':{}}
with pymysql.connect(**{k:r[k] for k in ('host','port','user','password')},database=SCHEMAS['inventory']) as db,db.cursor() as c:
    c.execute('SELECT id FROM inventory_stock WHERE item_id=%s',(4000000005633437,))
    stock=c.fetchone()[0]
    for label,sid in [('existing',stock),('absent',-987654321)]:
        try:
            c.execute("INSERT INTO inventory_reservation(id,order_id,stock_id,quantity,status,expires_at) VALUES(%s,%s,%s,1,'RESERVED',DATE_ADD(NOW(),INTERVAL 5 MINUTE))",
                      (str(uuid.uuid4()),str(uuid.uuid4()),sid))
            report['checks'][label]={'accepted':True}
        except pymysql.MySQLError as exc:
            report['checks'][label]={'accepted':False,'error':str(exc)}
        finally:db.rollback()
report['status']='PASS' if report['checks']['existing']['accepted'] and not report['checks']['absent']['accepted'] else 'FAIL'
write('LIVE-INVENTORY-FK-PROBE-'+uuid.uuid4().hex[:8]+'.json',report)
print(json.dumps(report,ensure_ascii=False))
