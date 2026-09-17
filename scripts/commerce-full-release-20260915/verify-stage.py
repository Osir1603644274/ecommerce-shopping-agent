"""Check persisted business invariants of the owned acceptance order."""
import json
import pymysql
from prepare import OUT,write
secret=json.loads((OUT/'stage-connection.private.json').read_text())
assert secret['database']=='commerce_acceptance'
flow=json.loads((OUT/'ACCEPTANCE-TRADE.json').read_text(encoding='utf8'))
steps={s['name']:s['result'] for s in flow['steps']}
order_id=steps['create']['id'];product_id=flow['productId']
db=pymysql.connect(**secret,cursorclass=pymysql.cursors.DictCursor)
with db.cursor() as c:
    c.execute('SET TRANSACTION READ ONLY')
    result={}
    for table,field,value in [('customer_order','id',order_id),('order_item','order_id',order_id),
        ('order_line_allocation','order_id',order_id),('inventory_reservation','order_id',order_id),
        ('partial_refund','order_id',order_id),('payment_record','order_id',order_id),('inventory_stock','item_id',product_id)]:
        c.execute('SELECT * FROM '+table+' WHERE '+field+'=%s',(value,));result[table]=c.fetchall()
    assert len(result['customer_order'])==len(result['order_item'])==len(result['partial_refund'])==len(result['payment_record'])==1
    order=result['customer_order'][0];allocation=result['order_line_allocation'][0];stock=result['inventory_stock'][0]
    assert order['status']=='PAID' and order['payable_minor']==432000
    assert allocation['quantity']==2 and allocation['refunded_quantity']==1 and allocation['refunded_minor']==216000
    assert result['partial_refund'][0]['status']=='SUCCESS' and result['partial_refund'][0]['amount_minor']==216000
    assert stock['total_quantity']==10 and stock['available_quantity']==9 and stock['reserved_quantity']==0 and stock['sold_quantity']==1
    assert stock['total_quantity']==stock['available_quantity']+stock['reserved_quantity']+stock['sold_quantity']
db.rollback();db.close()
write('ACCEPTANCE-SQL.json',{'status':'PASS','checks':['one order/payment/refund','integer refund exact','remaining quantity correct','inventory conserved'], 'rows':result})
print('SQL verification PASS: one order, payment, refund; 432000 paid, 216000 refunded; stock 10 = 9 available + 1 sold.')
