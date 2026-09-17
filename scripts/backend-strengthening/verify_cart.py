"""V4 real-MySQL cart/refund validation. New attempts only; local simulator contract."""
import argparse
import concurrent.futures
import hashlib
import http.client
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from collections import Counter
from verify_faults import FaultVerification


class CartVerification(FaultVerification):
    def __init__(self,runtime,output):
        super().__init__(runtime,output,expected_project='backend-strengthening-v4')
        self.entry_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        (self.output/'executed-verify_cart.py').write_bytes(Path(__file__).read_bytes())

    def request(self,method,path,body=None,expected=200,token=None,headers=None,port=38080):
        # Keep expected rejection/fault probes separate from unexpected HTTP failures.
        started=time.time();began=time.perf_counter();status=0;content=b'';failure=None
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=12)
        try:
            hdr={'Content-Type':'application/json',**(headers or {})}
            if token or self.token:hdr['Authorization']='Bearer '+(token or self.token)
            conn.request(method,path,json.dumps(body) if body is not None else None,hdr)
            response=conn.getresponse();status=response.status;content=response.read()
        except OSError as error:failure=str(error)
        finally:conn.close()
        intended=200 if expected is None else expected
        row={'startedUnix':started,'port':port,'method':method,'path':path,'status':status,
             'phase':getattr(self,'phase','setup'),'milliseconds':(time.perf_counter()-began)*1000,
             'transportError':failure,'expectedStatus':intended,'responseAssertionDeferred':expected is None,
             'expectedNegativeProbe':intended>=400,'expectationMet':status==intended and failure is None}
        with self.lock:
            self.http.append(row)
            with (self.output/'http.jsonl').open('a',encoding='utf-8') as log:log.write(json.dumps(row)+'\n')
        if expected is not None:assert row['expectationMet'],(method,path,status,content.decode(errors='replace')[:400],failure)
        data=json.loads(content).get('data') if content else None
        return (status,data) if expected is None else data

    def http_attribution(self):
        rows=list(self.http)
        result={'requests':len(rows),'statusCounts':dict(Counter(str(r['status']) for r in rows)),
            'expectedNegativeProbes':[r for r in rows if r['expectedNegativeProbe']],
            'unexpectedFailures':[r for r in rows if not r['expectationMet']],
            'phases':{phase:{'requests':sum(r['phase']==phase for r in rows),
                'unexpectedFailures':sum(r['phase']==phase and not r['expectationMet'] for r in rows)}
                for phase in sorted({r['phase'] for r in rows})}}
        (self.output/'http-attribution.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        return result

    def deadlock_snapshot(self):
        row=self.sql("SELECT NAME,COUNT,STATUS,TYPE FROM information_schema.INNODB_METRICS WHERE NAME='lock_deadlocks'")
        assert len(row)==1 and row[0]['STATUS']=='enabled' and row[0]['TYPE']=='counter',row
        return {'observedUnix':time.time(),'metric':'information_schema.INNODB_METRICS.lock_deadlocks',
                'count':int(row[0]['COUNT']),'enabled':True}

    def record_deadlock_delta(self,label,before):
        after=self.deadlock_snapshot()
        result={'scope':'dedicated MySQL server, interval counter; not a per-request metric',
            'before':before,'after':after,'delta':after['count']-before['count']}
        (self.output/(label+'-deadlocks.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
        return result

    def event_convergence(self):
        """Bind every fixture order event to both durable consumers, including refund events."""
        assert self.orders
        identities=sorted(set(self.orders));marks=','.join(['%s']*len(identities))
        latest={}
        def observed():
            nonlocal latest
            events=self.sql('SELECT * FROM outbox_event WHERE aggregate_type=\'ORDER\' AND aggregate_id IN ('+marks+') ORDER BY aggregate_id,id',tuple(identities))
            inbox=self.sql('''SELECT i.* FROM inbox_event i JOIN outbox_event o ON o.id=i.event_id
                WHERE o.aggregate_type='ORDER' AND o.aggregate_id IN ('''+marks+') ORDER BY i.event_id,i.consumer_name',tuple(identities))
            dead=self.sql('''SELECT d.* FROM dead_letter_event d JOIN outbox_event o ON o.id=d.event_id
                WHERE o.aggregate_type='ORDER' AND o.aggregate_id IN ('''+marks+') ORDER BY d.id',tuple(identities))
            issues=[]
            if {e['aggregate_id'] for e in events}!=set(identities):issues.append('fixture order lacks Outbox evidence')
            # Expected business effects must themselves have an event, not just whatever rows happened to exist.
            for identity in identities:
                types=Counter(e['event_type'] for e in events if e['aggregate_id']==identity)
                state=self.sql('SELECT status FROM customer_order WHERE id=%s',(identity,))[0]['status']
                expected=Counter({'order.created.v1':1})
                if state in ('PAID','REFUNDED'):expected['order.paid.v1']=1
                if state=='CANCELLED':expected['order.cancelled.v1']=1
                refunds=self.sql("SELECT id,amount_minor FROM partial_refund WHERE order_id=%s AND status='SUCCESS'",(identity,))
                if state=='REFUNDED':expected['order.refunded.v1']=1
                expected['order.partial-refunded.v2']=len(refunds)-(1 if state=='REFUNDED' else 0)
                if +types!=+expected:issues.append({'orderId':identity,'actualEventTypes':dict(types),'expectedEventTypes':dict(expected)})
                refund_events=[e for e in events if e['aggregate_id']==identity and e['event_type'] in ('order.partial-refunded.v2','order.refunded.v1')]
                payloads=[json.loads(e['payload_json']) for e in refund_events]
                if Counter((p.get('refundId'),p.get('amountMinor'),p.get('orderId'),p.get('currency')) for p in payloads)!=Counter((r['id'],r['amount_minor'],identity,'CNY') for r in refunds):
                    issues.append({'orderId':identity,'refundEventBinding':'mismatch'})
            hashes=[]
            for event in events:
                if event['status']!='PUBLISHED':issues.append({'eventId':event['id'],'outboxStatus':event['status']})
                for consumer in ('fulfillment-v1','domain-event-projection'):
                    raw=event['payload_json']
                    if consumer=='fulfillment-v1':raw='\n'.join((event['aggregate_type'],event['aggregate_id'],event['event_type'],raw))
                    digest=hashlib.sha256(raw.encode('utf-8')).hexdigest()
                    receipts=[r for r in inbox if r['event_id']==event['id'] and r['consumer_name']==consumer]
                    hashes.append({'eventId':event['id'],'consumer':consumer,'expectedPayloadHash':digest})
                    if len(receipts)!=1 or receipts[0]['status']!='PROCESSED' or receipts[0]['payload_hash']!=digest or receipts[0]['event_type']!=event['event_type']:
                        issues.append({'eventId':event['id'],'consumer':consumer,'receiptMismatch':receipts})
            if dead:issues.append({'fixtureDeadLetters':len(dead)})
            latest={'observedUnix':time.time(),'orderIds':identities,'events':events,'inbox':inbox,
                'expectedHashes':hashes,'deadLetters':dead,'issues':issues,'pass':not issues}
            # Persist even when waiting eventually fails; old attempts are never overwritten.
            (self.output/'event-final.json').write_text(json.dumps(latest,indent=2,default=str),encoding='utf-8')
            return not issues
        self.wait('fixture Outbox and both consumers converge without own DLQ',observed,100)
        self.accept('fixture_events_published_and_both_consumers_processed',orders=len(identities),
            events=len(latest['events']),consumerReceipts=len(latest['inbox']),deadLetters=0)
        assert not self.http_attribution()['unexpectedFailures']

    def fixture(self):
        super().fixture()
        self.second=self.product+1
        self.sql("UPDATE product SET snapshot_price_minor=101,dataset_revision='backend-strengthening-v4' WHERE id=%s",(self.product,))
        self.sql('''INSERT INTO product(id,source,source_item_id,title,brand,seller,category_l1,category_l2,category_l3,
            snapshot_price_minor,currency,price_status,attribute_text,data_nature,dataset_revision,source_license,provenance_url)
            SELECT %s,source,%s,'Cart second SKU',brand,seller,category_l1,category_l2,category_l3,
            202,currency,price_status,attribute_text,data_nature,'backend-strengthening-v4',source_license,provenance_url
            FROM product WHERE id=%s''',(self.second,str(self.second),self.product))
        self.sql("INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity) VALUES('PRODUCT',%s,100000,100000)",(self.second,))

    def coupon(self,discount=5):
        template=int(time.time()*1000000);identity=str(uuid.uuid4())
        self.sql("""INSERT INTO coupon_template(id,name,threshold_minor,discount_minor,total_quantity,claimed_quantity,
            valid_from,valid_until,status,version) VALUES(%s,'V4 exact cents',0,%s,100,1,
            CURRENT_TIMESTAMP-INTERVAL 1 DAY,CURRENT_TIMESTAMP+INTERVAL 1 DAY,'ACTIVE',0)""",(template,discount))
        self.sql("INSERT INTO user_coupon(id,template_id,user_id,status,version) VALUES(%s,%s,%s,'AVAILABLE',0)",(identity,template,self.user))
        return identity

    def cart(self,items=None,coupon=None,key=None,port=38080,expected=201):
        key=key or 'cart-v4-'+uuid.uuid4().hex
        body={'items':items or [{'itemType':'PRODUCT','itemId':self.product,'quantity':3},
                              {'itemType':'PRODUCT','itemId':self.second,'quantity':2}]}
        if coupon:body['userCouponId']=coupon
        intent={'key':key,'outcome':'cart','body':body}
        with self.lock:
            self.intents.append(intent)
            with (self.output/'order-intents.jsonl').open('a',encoding='utf-8') as log:
                log.write(json.dumps(intent)+'\n')
        order=self.request('POST','/api/orders/cart',body,expected,headers={'Idempotency-Key':key},port=port)
        with self.lock:
            if expected==201 and order['id'] not in self.orders:self.orders.append(order['id'])
        return order

    def pay(self,order,port=38080):
        payment=self.request('POST','/api/payments/orders/'+order,{},201,port=port)
        self.callback(payment,port)
        return payment

    def refund(self,order,items,key=None,expected=201,port=38080):
        return self.request('POST','/api/payments/orders/'+order+'/partial-refunds',
            {'items':items,'reason':'V4 quantity return'},expected,
            headers={'Idempotency-Key':key or 'refund-v4-'+uuid.uuid4().hex},port=port)

    def balances(self,identity):
        return self.request('GET','/api/payments/orders/'+identity+'/refund-balance')

    def record_order(self,identity,label):
        data={'order':self.sql('SELECT * FROM customer_order WHERE id=%s',(identity,)),
              'items':self.sql('SELECT * FROM order_item WHERE order_id=%s ORDER BY item_id',(identity,)),
              'payments':self.sql('SELECT * FROM payment_record WHERE order_id=%s',(identity,)),
              'allocations':self.sql('SELECT * FROM order_line_allocation WHERE order_id=%s ORDER BY item_id',(identity,)),
              'reservations':self.sql('SELECT * FROM inventory_reservation WHERE order_id=%s ORDER BY stock_id',(identity,)),
              'refunds':self.sql('SELECT * FROM partial_refund WHERE order_id=%s ORDER BY id',(identity,)),
              'refundItems':self.sql('''SELECT i.* FROM partial_refund_item i JOIN partial_refund r ON r.id=i.refund_id
                  WHERE r.order_id=%s ORDER BY i.refund_id,i.item_id''',(identity,)),
              'receipts':self.sql('''SELECT p.* FROM local_refund_receipt p JOIN partial_refund r ON r.id=p.refund_id
                  WHERE r.order_id=%s ORDER BY p.refund_id''',(identity,)),
              'task':self.task(identity),
              'commands':self.sql('SELECT * FROM fulfillment_command_version WHERE order_id=%s ORDER BY revision',(identity,)),
              'stocks':self.stock_snapshot()}
        (self.output/(label+'.json')).write_text(json.dumps(data,indent=2,default=str),encoding='utf-8')
        return data

    def stock_snapshot(self):
        return self.sql('''SELECT s.item_id,s.total_quantity,s.available_quantity,s.reserved_quantity,s.sold_quantity,
            COALESCE(SUM(CASE WHEN r.status='RESERVED' THEN r.quantity ELSE 0 END),0) AS ledger_reserved,
            COALESCE(SUM(CASE WHEN r.status='CONFIRMED' THEN r.quantity-r.refunded_quantity ELSE 0 END),0) AS ledger_sold
            FROM inventory_stock s LEFT JOIN inventory_reservation r ON r.stock_id=s.id
            WHERE s.item_type='PRODUCT' AND s.item_id IN (%s,%s)
            GROUP BY s.id,s.item_id,s.total_quantity,s.available_quantity,s.reserved_quantity,s.sold_quantity
            ORDER BY s.item_id''',(self.product,self.second))

    def assert_stock_ledger(self,stocks):
        assert {row['item_id'] for row in stocks}=={self.product,self.second},stocks
        for row in stocks:
            assert row['reserved_quantity']==row['ledger_reserved'] and row['sold_quantity']==row['ledger_sold'],row
            assert row['available_quantity']==row['total_quantity']-row['ledger_reserved']-row['ledger_sold'],row

    def assert_financial(self,state):
        """Compare original HTTP intent, order lines, refund money, receipts and reservation quantities."""
        order=state['order'][0]
        items={row['item_id']:row for row in state['items']}
        allocations={row['item_id']:row for row in state['allocations']}
        assert items and len(items)==len(state['items']) and set(items)==set(allocations),state
        intent=next(i for i in self.intents if i['key']==order['idempotency_key'])
        expected=sorted((i['itemType'],i['itemId'],i['quantity']) for i in intent['body']['items'])
        actual=sorted((i['item_type'],i['item_id'],i['quantity']) for i in state['items'])
        assert actual==expected,(actual,expected)
        assert order['total_minor']==sum(i['subtotal_minor'] for i in state['items'])
        assert order['discount_minor']==sum(a['discount_minor'] for a in state['allocations'])
        assert order['payable_minor']==sum(a['paid_minor'] for a in state['allocations'])
        assert order['total_minor']==order['discount_minor']+order['payable_minor']
        reservations={row['stock_id']:row for row in state['reservations']}
        assert len(reservations)==len(allocations)==len(state['reservations'])
        payments=state['payments']
        if order['status'] in ('PAID','REFUNDED'):
            assert len(payments)==1 and payments[0]['status']=='SUCCESS',payments
        else:
            assert order['status']=='CANCELLED' and all(p['status'] in ('CREATED','FAILED') for p in payments),order
        for payment in payments:
            assert payment['amount_minor']==order['payable_minor'] and payment['currency']==order['currency'],payment
        quantities={item:0 for item in items};amounts={item:0 for item in items}
        receipts={row['refund_id']:row for row in state['receipts']}
        successful_total=0
        for refund in state['refunds']:
            lines=[i for i in state['refundItems'] if i['refund_id']==refund['id']]
            assert lines and sum(i['amount_minor'] for i in lines)==refund['amount_minor'],refund
            assert refund['currency']==order['currency'] and len(payments)==1 and refund['payment_id']==payments[0]['id'],refund
            receipt=receipts.get(refund['id'])
            if receipt:
                assert all(receipt[k]==refund[k] for k in ('payment_id','request_hash','amount_minor','currency')),(receipt,refund)
            if refund['status']=='SUCCESS':
                assert receipt and receipt['provider_refund_no']==refund['provider_refund_no'],refund
                successful_total+=refund['amount_minor']
                for line in lines:
                    assert line['item_id'] in items and line['quantity']>0 and line['amount_minor']>=0,line
                    quantities[line['item_id']]+=line['quantity'];amounts[line['item_id']]+=line['amount_minor']
            else:
                assert refund['status']=='PROCESSING' and state['task']['status']=='REFUND_HOLD',refund
        assert successful_total==sum(a['refunded_minor'] for a in state['allocations'])<=order['payable_minor']
        for item,allocation in allocations.items():
            line=items[item];reservation=reservations[allocation['stock_id']]
            assert line['unit_price_minor']*line['quantity']==line['subtotal_minor']==allocation['subtotal_minor']
            assert line['quantity']==allocation['quantity']==reservation['quantity']
            assert allocation['subtotal_minor']==allocation['discount_minor']+allocation['paid_minor']
            assert 0<=allocation['discount_minor']<=allocation['subtotal_minor']
            assert quantities[item]==allocation['refunded_quantity']==reservation['refunded_quantity']<=line['quantity']
            assert 0<=amounts[item]==allocation['refunded_minor']<=allocation['paid_minor']
            if order['status']=='REFUNDED':assert quantities[item]==line['quantity'] and reservation['status']=='REFUNDED'
            elif order['status']=='CANCELLED':assert reservation['status']=='RELEASED' and quantities[item]==0
            else:assert reservation['status']==('REFUNDED' if quantities[item]==line['quantity'] else 'CONFIRMED')
        if order['status']=='REFUNDED':assert successful_total==order['payable_minor']
        self.assert_stock_ledger(state['stocks'])

    def receipt_before_business_failure(self,identity,refund):
        before=self.record_order(identity,'before-receipt-business-failure')
        self.sql("CREATE TRIGGER v4_refund_outbox_failure BEFORE INSERT ON outbox_event FOR EACH ROW BEGIN "
            "IF NEW.aggregate_id='"+identity+"' AND NEW.event_type IN ('order.partial-refunded.v2','order.refunded.v1') THEN "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='V4 refund apply failure'; END IF; END")
        try:
            self.request('POST','/api/payments/partial-refunds/'+refund+'/simulate-success',{},500)
        finally:self.sql('DROP TRIGGER v4_refund_outbox_failure')
        assert self.sql('SELECT COUNT(*) AS n FROM local_refund_receipt WHERE refund_id=%s',(refund,))[0]['n']==1
        state=self.record_order(identity,'receipt-committed-business-rolled-back')
        assert state['refunds'][0]['status']=='PROCESSING'
        assert all(row['refunded_quantity']==0 and row['refunded_minor']==0 for row in state['allocations'])
        assert state['task']['status']=='REFUND_HOLD'
        assert state['stocks']==before['stocks'] and state['reservations']==before['reservations']
        assert state['commands']==before['commands'] and state['task']['command_json']==before['task']['command_json']
        assert state['task']['request_key']==before['task']['request_key']
        self.assert_financial(state)

    def concurrent_reconcile(self,identity,refund):
        before=self.record_order(identity,'before-concurrent-reconcile')
        deadlocks_before=self.deadlock_snapshot();previous_phase=getattr(self,'phase','setup')
        self.phase='concurrent-reconcile'
        blocker=self.connect();blocker.begin();gate=threading.Event()
        try:
            with blocker.cursor() as cursor:
                cursor.execute('SELECT CONNECTION_ID() AS id');blocker_id=cursor.fetchone()['id']
                cursor.execute('SELECT id FROM customer_order WHERE id=%s FOR UPDATE',(identity,));cursor.fetchall()
            observed=[]
            def target_waiters():
                nonlocal observed
                rows=self.sql('''SELECT w.REQUESTING_ENGINE_TRANSACTION_ID AS requestingTransaction,
                    b.OBJECT_SCHEMA AS objectSchema,b.OBJECT_NAME AS objectName,b.INDEX_NAME AS indexName,
                    b.LOCK_DATA AS blockingLockData,t.PROCESSLIST_ID AS blockingConnection
                    FROM performance_schema.data_lock_waits w
                    JOIN performance_schema.data_locks b ON b.ENGINE=w.ENGINE AND b.ENGINE_LOCK_ID=w.BLOCKING_ENGINE_LOCK_ID
                    JOIN performance_schema.threads t ON t.THREAD_ID=b.THREAD_ID
                    WHERE t.PROCESSLIST_ID=%s AND b.OBJECT_SCHEMA=DATABASE()
                      AND b.OBJECT_NAME='customer_order' AND b.INDEX_NAME='PRIMARY' ''',(blocker_id,))
                observed=[r for r in rows if identity in str(r['blockingLockData'])]
                return len({r['requestingTransaction'] for r in observed})>=2
            def invoke(number):
                gate.wait()
                return self.request('POST','/api/payments/partial-refunds/'+refund+'/reconcile',{},None,port=38080+number%2)
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                futures=[pool.submit(invoke,i) for i in range(12)];gate.set()
                self.wait('at least two transactions wait on this blocker and order record',target_waiters,8)
                wait_count=len({r['requestingTransaction'] for r in observed})
                (self.output/'concurrent-reconcile-locks.json').write_text(json.dumps({'orderId':identity,
                    'blockerConnection':blocker_id,'observedUnix':time.time(),'locks':observed},indent=2,default=str),encoding='utf-8')
                blocker.rollback()
                results=[f.result() for f in futures]
        finally:
            blocker.rollback();blocker.close();self.phase=previous_phase
            deadlocks=self.record_deadlock_delta('concurrent-reconcile',deadlocks_before)
        (self.output/'concurrent-reconcile.json').write_text(json.dumps({'observedLockWaits':wait_count,
            'results':results},indent=2),encoding='utf-8')
        state=self.record_order(identity,'after-concurrent-reconcile')
        self.assert_financial(state)
        before_stock={r['item_id']:r for r in before['stocks']}
        for stock in state['stocks']:
            delta=1 if stock['item_id']==self.product else 0
            assert stock['available_quantity']==before_stock[stock['item_id']]['available_quantity']+delta
            assert stock['sold_quantity']==before_stock[stock['item_id']]['sold_quantity']-delta
            assert stock['reserved_quantity']==before_stock[stock['item_id']]['reserved_quantity']
        assert state['commands'][:len(before['commands'])]==before['commands']
        assert all(code==200 and value['status']=='SUCCESS' for code,value in results),[code for code,_ in results]
        assert sum(row['refunded_quantity'] for row in state['allocations'])==1
        assert sum(row['refunded_minor'] for row in state['allocations'])==101
        assert len(state['commands'])==2
        assert deadlocks['delta']==0,deadlocks
        self.accept('mysql_concurrent_reconcile_exactly_once',calls=12,observedRowWaits=wait_count,refundedQuantity=1,refundedMinor=101,commandVersions=2)

    def rr_probe(self):
        self.apps(False,True);self.fixture()
        self.schema_version(16)
        identity=self.cart(coupon=self.coupon())['id'];self.pay(identity)
        refund=self.refund(identity,[{'itemId':self.product,'quantity':1}])['id']
        self.receipt_before_business_failure(identity,refund)
        self.concurrent_reconcile(identity,refund)
        self.event_convergence()

    def schema_version(self,expected):
        history=self.sql('SELECT installed_rank,version,description,script,checksum,success FROM flyway_schema_history ORDER BY installed_rank')
        assert history and all(row['success']==1 for row in history),history
        actual=max(int(row['version']) for row in history if row['version'] is not None)
        assert actual==expected,(actual,expected)
        (self.output/('flyway-version-'+str(expected)+'.json')).write_text(json.dumps(history,indent=2,default=str),encoding='utf-8')
        return {'version':actual,'successfulMigrations':len(history),'failedMigrations':0}

    def legacy_seed(self):
        self.apps(False,True)
        schema=self.schema_version(15)
        # Run against the V15 payment-boundary-fixed JAR before replacing the V4 mount.
        FaultVerification.fixture(self)
        paid=self.new_order('paid');pending=self.new_order('pending')
        commands={identity:self.task(identity)['command_json'] for identity in (paid,pending)}
        hashes={identity:hashlib.sha256(raw.encode()).hexdigest() for identity,raw in commands.items()}
        (self.output/'legacy-command-before.json').write_text(json.dumps({'schema':schema,'commands':commands,
            'sha256':hashes},indent=2),encoding='utf-8')
        (self.runtime/'legacy-private.json').write_text(json.dumps({'token':self.token,'refresh':self.refresh,
            'user':self.user,'product':self.product,'paid':paid,'pending':pending,
            'commandBeforeSha256':hashes}),encoding='utf-8')
        self.accept('v15_legacy_seed',orders=[paid,pending],schema=schema,commandBeforeSha256=hashes)

    def legacy_check(self):
        self.apps(False,True)
        schema=self.schema_version(16)
        saved=json.loads((self.runtime/'legacy-private.json').read_text())
        issued=self.request('POST','/api/auth/refresh',{'refreshToken':saved['refresh']})
        assert issued['user']['id']==saved['user']
        self.token,self.user=issued['accessToken'],saved['user']
        saved.update(token=self.token,refresh=issued['refreshToken'])
        (self.runtime/'legacy-private.json').write_text(json.dumps(saved),encoding='utf-8')
        self.request('POST','/api/orders/'+saved['pending']+'/cancel',{})
        refund=self.request('POST','/api/payments/orders/'+saved['paid']+'/refunds',{'reason':'V15 upgrade compatibility'},201)
        self.request('POST','/api/payments/refunds/'+refund['id']+'/simulate-success',{})
        self.request('POST','/api/payments/refunds/'+refund['id']+'/simulate-success',{})
        stock=self.sql('SELECT available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_id=%s',(saved['product'],))[0]
        assert stock=={'available_quantity':100000,'reserved_quantity':0,'sold_quantity':0},stock
        commands={identity:self.task(identity)['command_json'] for identity in (saved['paid'],saved['pending'])}
        hashes={identity:hashlib.sha256(raw.encode()).hexdigest() for identity,raw in commands.items()}
        assert all('"itemType":"PRODUCT"' in raw and 'warehouse.cart.v2' not in raw for raw in commands.values())
        before=saved.get('commandBeforeSha256')
        if before is not None:assert before==hashes,(before,hashes)
        states=self.sql('''SELECT o.id,o.status,p.status AS payment,r.status AS refund,f.status AS fulfillment
            FROM customer_order o LEFT JOIN payment_record p ON p.order_id=o.id
            LEFT JOIN refund_record r ON r.order_id=o.id LEFT JOIN fulfillment_task f ON f.order_id=o.id
            WHERE o.id IN (%s,%s) ORDER BY o.id''',(saved['paid'],saved['pending']))
        by_id={row['id']:row for row in states}
        assert by_id[saved['paid']]['status']=='REFUNDED' and by_id[saved['paid']]['payment']=='SUCCESS' and by_id[saved['paid']]['refund']=='SUCCESS'
        assert by_id[saved['pending']]['status']=='CANCELLED' and by_id[saved['pending']]['payment'] is None
        evidence={'schema':schema,'orders':states,'stock':stock,'commandAfterSha256':hashes,
            'oldWarehouseCommandShapePreserved':True,'commandByteEqualityVerified':before is not None,
            'beforeEvidenceAvailable':before is not None}
        (self.output/'legacy-after.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        self.accept('v15_existing_orders_upgrade_and_refund',**evidence)

    def legacy_readonly(self):
        """Current post-upgrade facts only; never fabricates a pre-upgrade command baseline."""
        schema=self.schema_version(16)
        saved=json.loads((self.runtime/'legacy-private.json').read_text())
        rows=self.sql('''SELECT o.id,o.status,p.status AS payment,r.status AS refund,f.command_json
            FROM customer_order o LEFT JOIN payment_record p ON p.order_id=o.id
            LEFT JOIN refund_record r ON r.order_id=o.id LEFT JOIN fulfillment_task f ON f.order_id=o.id
            WHERE o.id IN (%s,%s) ORDER BY o.id''',(saved['paid'],saved['pending']))
        assert {row['id'] for row in rows}=={saved['paid'],saved['pending']}
        by_id={row['id']:row for row in rows}
        assert by_id[saved['paid']]['status']=='REFUNDED' and by_id[saved['paid']]['payment']=='SUCCESS' and by_id[saved['paid']]['refund']=='SUCCESS'
        assert by_id[saved['pending']]['status']=='CANCELLED' and by_id[saved['pending']]['payment'] is None
        hashes={row['id']:hashlib.sha256(row['command_json'].encode()).hexdigest() for row in rows}
        before=saved.get('commandBeforeSha256')
        if before is not None:assert before==hashes
        assert all('"itemType":"PRODUCT"' in row['command_json'] and 'warehouse.cart.v2' not in row['command_json'] for row in rows)
        stock=self.sql('SELECT available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_id=%s',(saved['product'],))[0]
        assert stock=={'available_quantity':100000,'reserved_quantity':0,'sold_quantity':0},stock
        result={'schema':schema,'orders':rows,'stock':stock,'commandAfterSha256':hashes,
            'commandByteEqualityVerified':before is not None,'beforeEvidenceAvailable':before is not None,
            'oldWarehouseCommandShapePreserved':True,'readOnlyFollowup':True}
        (self.output/'legacy-readonly.json').write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')
        self.accept('legacy_upgrade_current_state_readonly',**result)

    def default_disabled(self):
        config=json.loads((self.runtime/'compose.validation.json').read_text())
        app=config['services']['app1']['environment']
        app['SPRING_APPLICATION_JSON']=app['SPRING_APPLICATION_JSON'].replace('"enabled":true','"enabled":false')
        control=self.output/'compose.disabled.json'
        control.write_text(json.dumps(config,indent=2),encoding='utf-8')
        original=self.compose;self.compose=original[:-1]+[str(control.resolve())]
        try:
            self.apps(False,False,names=('app1',));self.fixture()
            self.cart(expected=409)
            assert self.sql('SELECT COUNT(*) AS n FROM customer_order WHERE user_id=%s',(self.user,))[0]['n']==0
            identity=self.new_order('cancelled')
            stock=self.sql('SELECT available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_id=%s',(self.product,))[0]
            assert stock=={'available_quantity':100000,'reserved_quantity':0,'sold_quantity':0}
            self.accept('disabled_cart_gate_and_legacy_compatibility',cartHttpStatus=409,cartOrders=0,legacyCancelled=identity,stock=stock)
        finally:
            self.compose=original;self.apps(False,True,names=('app1',))

    def business(self):
        rows=self.sql('''SELECT o.id,o.status,i.item_id,i.quantity,r.status AS reservation,r.refunded_quantity,
            f.status AS fulfillment,f.command_json FROM customer_order o JOIN order_item i ON i.order_id=o.id
            LEFT JOIN inventory_reservation r ON r.order_id=o.id AND r.stock_id=(SELECT id FROM inventory_stock WHERE item_type=i.item_type AND item_id=i.item_id)
            LEFT JOIN fulfillment_task f ON f.order_id=o.id WHERE o.user_id=%s ORDER BY o.id,i.item_id''',(self.user,))
        assert {row['id'] for row in rows}==set(self.orders)
        financial=[]
        for identity in sorted(set(self.orders)):
            state=self.record_order(identity,'financial-order-'+identity)
            self.assert_financial(state)
            financial.append({'orderId':identity,'status':state['order'][0]['status'],
                'successfulRefundMinor':sum(r['amount_minor'] for r in state['refunds'] if r['status']=='SUCCESS')})
        sold={self.product:0,self.second:0}
        for row in rows:
            if row['status']=='PAID':
                assert row['fulfillment']=='SHIPPED' and self.shipment_count(row['id'])==1,row
                assert row['reservation'] in ('CONFIRMED','REFUNDED'),row
                remaining=row['quantity']-row['refunded_quantity'];sold[row['item_id']]+=remaining
                command=json.loads(row['command_json'])
                actual=sum(item['quantity'] for item in command['items'] if item['itemId']==row['item_id'])
                assert actual==remaining,(row,command)
            elif row['status']=='REFUNDED':
                assert row['fulfillment']=='CANCELLED' and row['reservation']=='REFUNDED' and row['refunded_quantity']==row['quantity'],row
                assert self.shipment_count(row['id'])==0
            elif row['status']=='CANCELLED':
                assert row['fulfillment']=='CANCELLED' and row['reservation']=='RELEASED' and self.shipment_count(row['id'])==0,row
            else:raise AssertionError(row)
        stocks=self.sql('SELECT item_id,total_quantity,available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_id IN (%s,%s) ORDER BY item_id',(self.product,self.second))
        for stock in stocks:
            assert stock['reserved_quantity']==0 and stock['sold_quantity']==sold[stock['item_id']] and stock['available_quantity']==100000-sold[stock['item_id']],stock
        # Compare immutable bytes to the independent durable warehouse receipt database.
        with sqlite3.connect(self.runtime/'warehouse.sqlite') as warehouse:
            for identity in set(row['id'] for row in rows if row['status']=='PAID'):
                task=self.task(identity)
                shipment=warehouse.execute('SELECT request_key,command_hash,command_json FROM shipment WHERE order_id=?',(identity,)).fetchone()
                assert shipment==(task['request_key'],hashlib.sha256(task['command_json'].encode()).hexdigest(),task['command_json']),identity
        data={'orders':rows,'stocks':stocks,'uniqueOrders':len(self.orders),'financialInvariants':financial}
        (self.output/'business-final.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
        return data

    def full(self):
        self.apps(False,True);self.fixture()
        self.schema_version(16)
        migration=self.sql("SELECT version,success FROM flyway_schema_history WHERE version='16'")[0]
        assert migration['success']==1
        # Insufficient second SKU rolls back first SKU, coupon, order and line allocation.
        coupon=self.coupon();before=self.sql('SELECT COUNT(*) AS n FROM customer_order WHERE user_id=%s',(self.user,))[0]['n']
        self.sql('UPDATE inventory_stock SET total_quantity=1,available_quantity=1 WHERE item_id=%s',(self.second,))
        self.cart([{'itemType':'PRODUCT','itemId':self.product,'quantity':1},
            {'itemType':'PRODUCT','itemId':self.second,'quantity':2}],coupon=coupon,expected=409)
        assert self.sql('SELECT COUNT(*) AS n FROM customer_order WHERE user_id=%s',(self.user,))[0]['n']==before
        assert self.sql('SELECT status FROM user_coupon WHERE id=%s',(coupon,))[0]['status']=='AVAILABLE'
        assert all(row['reserved_quantity']==0 for row in self.sql('SELECT reserved_quantity FROM inventory_stock WHERE item_id IN (%s,%s)',(self.product,self.second)))
        assert self.sql('SELECT available_quantity FROM inventory_stock WHERE item_id=%s',(self.second,))[0]['available_quantity']==1
        self.assert_stock_ledger(self.stock_snapshot())
        self.sql('UPDATE inventory_stock SET total_quantity=100000,available_quantity=100000 WHERE item_id=%s',(self.second,))
        self.accept('mysql_v16_and_cart_atomic_rollback',migration=migration,ordersLeft=0,coupon='AVAILABLE')
        key='stable-cart-'+uuid.uuid4().hex;order=self.cart(coupon=coupon,key=key)
        reverse=self.cart([{'itemType':'PRODUCT','itemId':self.second,'quantity':2},{'itemType':'PRODUCT','itemId':self.product,'quantity':3}],coupon=coupon,key=key)
        assert reverse['id']==order['id'] and order['totalMinor']==707 and order['discountMinor']==5 and order['payableMinor']==702
        identity=order['id'];self.pay(identity)
        balance=self.balances(identity)
        assert [row['discountMinor'] for row in balance]==[2,3] and [row['paidMinor'] for row in balance]==[301,401]
        self.accept('cart_idempotency_and_exact_discount',orderId=identity,total=707,discount=5,paid=702,allocations=balance)
        # Old full-refund endpoint cannot bypass the new ledger.
        self.request('POST','/api/payments/orders/'+identity+'/refunds',{'reason':'reject mixed refund paths'},409)
        refund_key='stable-refund-'+uuid.uuid4().hex
        first=self.refund(identity,[{'itemId':self.product,'quantity':1}],key=refund_key)
        assert self.refund(identity,[{'itemId':self.product,'quantity':1}],key=refund_key)['id']==first['id']
        self.refund(identity,[{'itemId':self.product,'quantity':2}],key=refund_key,expected=409)
        no_receipt=self.request('POST','/api/payments/partial-refunds/'+first['id']+'/reconcile',{})
        assert no_receipt['status']=='PROCESSING' and self.task(identity)['status']=='REFUND_HOLD'
        outsider=self.request('POST','/api/auth/register',{'username':'cart-other-'+uuid.uuid4().hex[:12],'password':uuid.uuid4().hex},201)
        self.request('GET','/api/payments/partial-refunds/'+first['id'],expected=403,token=outsider['accessToken'])
        self.receipt_before_business_failure(identity,first['id'])
        self.concurrent_reconcile(identity,first['id'])
        second=self.refund(identity,[{'itemId':self.product,'quantity':1}])
        self.request('POST','/api/payments/partial-refunds/'+second['id']+'/simulate-success',{})
        third=self.refund(identity,[{'itemId':self.product,'quantity':1},{'itemId':self.second,'quantity':2}])
        self.request('POST','/api/payments/partial-refunds/'+third['id']+'/simulate-success',{})
        self.request('POST','/api/payments/partial-refunds/'+third['id']+'/simulate-success',{})
        assert [first['amountMinor'],second['amountMinor'],third['amountMinor']]==[101,100,501]
        state=self.record_order(identity,'full-refund-exact-cents')
        assert sum(row['refunded_minor'] for row in state['allocations'])==702 and state['order'][0]['status']=='REFUNDED'
        self.refund(identity,[{'itemId':self.product,'quantity':1}],expected=409)
        self.accept('partial_refund_exact_cents_and_no_overrefund',refunds=[101,100,501],total=702)
        shipped=self.cart()['id'];self.pay(shipped)
        partial=self.refund(shipped,[{'itemId':self.product,'quantity':1}])
        self.request('POST','/api/payments/partial-refunds/'+partial['id']+'/simulate-success',{})
        # Reverse item order across concurrent transactions while locking by physical stock id.
        def create_cancel(number):
            items=[{'itemType':'PRODUCT','itemId':self.product,'quantity':1},{'itemType':'PRODUCT','itemId':self.second,'quantity':1}]
            if number%2:items.reverse()
            current=self.cart(items,port=38080+number%2)['id']
            self.request('POST','/api/orders/'+current+'/cancel',{},port=38080+number%2)
            return current
        deadlocks_before=self.deadlock_snapshot();previous_phase=getattr(self,'phase','setup')
        self.phase='reverse-sku-concurrency';concurrent_results=[]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                futures=[pool.submit(create_cancel,n) for n in range(12)]
                for number,future in enumerate(futures):
                    try:concurrent_results.append({'number':number,'orderId':future.result(),'success':True})
                    except Exception as error:concurrent_results.append({'number':number,'success':False,'error':str(error)})
        finally:
            self.phase=previous_phase
            deadlocks=self.record_deadlock_delta('reverse-sku-concurrency',deadlocks_before)
            (self.output/'reverse-sku-concurrency.json').write_text(json.dumps(concurrent_results,indent=2),encoding='utf-8')
            self.http_attribution()
        concurrent_ids=[r['orderId'] for r in concurrent_results if r['success']]
        assert len(concurrent_ids)==12 and deadlocks['delta']==0,(concurrent_results,deadlocks)
        self.accept('mysql_reverse_sku_concurrency',orders=len(concurrent_ids),errors=0,deadlockDelta=deadlocks['delta'])
        self.apps(True,True);self.state(shipped,'SHIPPED')
        self.refund(shipped,[{'itemId':self.product,'quantity':1}],expected=409)
        self.wait('cancelled events consumed',lambda:all(self.task(x)['status']=='CANCELLED' for x in concurrent_ids))
        final=self.business()
        self.accept('remaining_items_ship_and_whole_fixture_conserves',orders=final['uniqueOrders'],stocks=final['stocks'],remainingShipment={'firstSku':2,'secondSku':2})
        self.event_convergence()

    def save(self,status,error=None):
        self.http_attribution()
        super().save(status,error)
        path=self.output/'result.json';result=json.loads(path.read_text())
        result['entryScriptSha256']=self.entry_hash
        path.write_text(json.dumps(result,indent=2),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--case',choices=['legacy_seed','legacy_check','legacy_readonly','rr_probe','full','default_disabled'],required=True);a=p.parse_args()
    runner=CartVerification(a.runtime,a.output)
    try:getattr(runner,a.case)();runner.save('BOUNDED_ACCEPT')
    except Exception as error:runner.save('FAILED',str(error));raise
    finally:
        if a.case!='legacy_readonly':runner.fault('normal')
        runner.command(['logs','--no-color','app1','app2'])
