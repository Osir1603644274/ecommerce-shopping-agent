"""Read-only live ownership/cutover audit. This script never migrates live data."""
import json
import uuid
import pymysql
from topology_lab import ROOT,OUT,CATALOG,SHARED,root_connection,write

def main():
    root=root_connection();schema=root['database']
    with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},database=schema,
        autocommit=True,cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
        c.execute('SELECT table_name AS name,table_rows AS estimatedRows,data_length AS bytes,index_length AS indexBytes FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name',(schema,));tables=c.fetchall()
        def owner(table):return 'catalog' if table in CATALOG else 'inventory' if table in {'inventory_stock','inventory_reservation'} else 'trade'
        c.execute('''SELECT table_name AS child,column_name AS childColumn,constraint_name AS constraintName,referenced_table_name AS parent,referenced_column_name AS parentColumn
            FROM information_schema.key_column_usage WHERE table_schema=%s AND referenced_table_name IS NOT NULL ORDER BY table_name,constraint_name''',(schema,));fks=c.fetchall()
        cross=[{**row,'childOwner':owner(row['child']),'parentOwner':owner(row['parent'])} for row in fks if owner(row['child'])!=owner(row['parent'])]
        c.execute('SELECT status,COUNT(*) AS n FROM customer_order GROUP BY status');orders=c.fetchall()
        c.execute('SELECT status,COUNT(*) AS n FROM inventory_reservation GROUP BY status');reservations=c.fetchall()
        c.execute('SELECT event_type,COUNT(*) AS n FROM outbox_event WHERE published_at IS NULL GROUP BY event_type');pending=c.fetchall()
        c.execute('SELECT status,COUNT(*) AS n FROM partial_refund GROUP BY status');refunds=c.fetchall()
        c.execute('SELECT status,COUNT(*) AS n FROM inbox_event GROUP BY status');inbox=c.fetchall()
    result={'scope':'READ_ONLY_LIVE_AUDIT','sourceSchema':schema,'tables':[dict(t,owner=owner(t['name'])) for t in tables],
        'crossOwnerForeignKeys':cross,'ordersByStatus':orders,'reservationsByStatus':reservations,'refundsByStatus':refunds,
        'pendingOutboxByType':pending,'inboxByStatus':inbox,'sharedStructuresToOwnSeparately':sorted(SHARED),
        'migrationPerformed':False,'requiredBeforeCutover':['fresh backup and restore proof','write freeze and drain current writers',
            'move owned tables without changing IDs','partition event ownership without dropping pending events','reconcile old reservations with new command guards',
            'least-privilege accounts','all entrypoints and restart helper use new release marker']}
    name='LIVE-CUTOVER-AUDIT-'+uuid.uuid4().hex[:10]+'.json';write(name,result)
    print(json.dumps({'artifact':name,'crossOwnerForeignKeys':cross,'pendingOutbox':pending,'migrationPerformed':False}))

if __name__=='__main__':main()
