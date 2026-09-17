"""Small real-MySQL reproduction of cross-schema FK relocation; fixtures retained."""
import json
import uuid
import pymysql
import live_release
from topology_lab import root_connection,write

attempt=uuid.uuid4().hex[:8];source='micro_fk_'+attempt+'_a';target='micro_fk_'+attempt+'_b'
root=root_connection();report={'attempt':attempt,'schemas':[source,target]}
with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},autocommit=True,
                     cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
    for schema in (source,target):c.execute('CREATE DATABASE '+schema)
    db.select_db(source)
    c.execute('CREATE TABLE inventory_stock(id BIGINT PRIMARY KEY) ENGINE=InnoDB')
    c.execute('CREATE TABLE inventory_reservation(id BIGINT PRIMARY KEY,stock_id BIGINT,CONSTRAINT fk_inventory_reservation_stock FOREIGN KEY(stock_id) REFERENCES inventory_stock(id)) ENGINE=InnoDB')
    c.execute('INSERT INTO inventory_stock VALUES(1)')
    c.execute('RENAME TABLE '+source+'.inventory_reservation TO '+target+'.inventory_reservation, '+source+'.inventory_stock TO '+target+'.inventory_stock')
    def probe():
        outcomes={}
        for label,sid in [('existing',1),('absent',-1)]:
            db.begin()
            try:
                c.execute('INSERT INTO '+target+'.inventory_reservation VALUES(2,%s)',(sid,));outcomes[label]={'accepted':True}
            except pymysql.MySQLError as exc:outcomes[label]={'accepted':False,'error':str(exc)}
            finally:db.rollback()
        return outcomes
    report['beforeRebind']=probe()
    live_release.SCHEMAS={**live_release.SCHEMAS,'inventory':target}
    live_release.rebind_inventory_fk(c)
    report['afterRebind']=probe()
    assert report['afterRebind']['existing']['accepted']
    assert not report['afterRebind']['absent']['accepted']
    report['status']='PASS'
write('FK-MIGRATION-REGRESSION-'+attempt+'.json',report)
print(json.dumps(report))
