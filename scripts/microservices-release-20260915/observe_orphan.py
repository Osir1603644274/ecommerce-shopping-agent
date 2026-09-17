"""Read-only evidence for the real compensation failure and its later automatic recovery."""
import argparse
import json
import pymysql
from topology_lab import OUT,SCHEMAS,prepare,root_connection,write

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['before-fix','after-fix']);args=parser.parse_args()
    config=prepare();root=root_connection();order='ffbd24d6-efbd-4a86-b1c7-997e0b0a6598';result={'orderId':order,'phase':args.phase}
    for role in ('trade','inventory'):
        a=config['accounts'][role]
        with pymysql.connect(host=root['host'],port=root['port'],user=a['user'],password=a['password'],database=SCHEMAS[role],
                            cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
            if role=='trade':
                c.execute('SELECT command_id,kind,status,last_error FROM inventory_command_journal WHERE order_id=%s ORDER BY sequence_id',(order,));result['journal']=c.fetchall()
                c.execute('SELECT id FROM customer_order WHERE id=%s',(order,));result['orders']=c.fetchall()
                c.execute('SELECT payload_json FROM outbox_event WHERE aggregate_id=%s',(order,));result['storedOutboxJson']=c.fetchall()
            else:
                c.execute('SELECT stock_id,quantity,status FROM inventory_reservation WHERE order_id=%s',(order,));result['reservations']=c.fetchall()
                c.execute('SELECT id,total_quantity,available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_id=%s',(config['products'][0],));result['stock']=c.fetchall()
    if args.phase=='after-fix':
        assert not result['orders'] and result['reservations'] and all(r['status']=='RELEASED' for r in result['reservations'])
        assert all(r['status'] in {'CANCELLED','ACK'} for r in result['journal'])
    write('ORPHAN-RECOVERY-'+args.phase+'.json',result);print(json.dumps(result,default=str))

if __name__=='__main__':main()
