"""V3 sustained offered load, explicit fault timeline, and cross-instance failover."""
import argparse
import concurrent.futures
import hashlib
import http.client
import json
import threading
import time
from pathlib import Path
from verify_faults import FaultVerification


class LoadFaultVerification(FaultVerification):
    def __init__(self,runtime,output):
        super().__init__(runtime,output)
        self.entry_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        (self.output/'executed-verify_load_faults.py').write_bytes(Path(__file__).read_bytes())

    def monitor(self, stop):
        connection = self.connect()
        try:
            with (self.output/'observations.jsonl').open('w',encoding='utf-8') as log:
                while not stop.is_set():
                    row={'unix':time.time(),'instances':{}}
                    for port in (38080,38081):
                        client=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
                        try:
                            client.request('GET','/actuator/prometheus');response=client.getresponse()
                            row['instances'][str(port)]={'status':response.status,'raw':response.read().decode()}
                        except OSError as error:
                            row['instances'][str(port)]={'error':str(error)}
                        finally:
                            client.close()
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT COUNT(*) AS n FROM performance_schema.data_lock_waits')
                        row['dataLockWaits']=cursor.fetchone()['n']
                        for table in ('fulfillment_task','outbox_event'):
                            field='order_id' if table=='fulfillment_task' else 'aggregate_id'
                            cursor.execute('SELECT e.status,COUNT(*) AS n FROM '+table+' e JOIN customer_order o ON o.id=e.'+field+' WHERE o.user_id=%s GROUP BY e.status',(self.user,))
                            row[table]=cursor.fetchall()
                        cursor.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Innodb_row_lock_waits','Innodb_row_lock_time')")
                        row['mysqlCounters']=cursor.fetchall()
                    row['finishedUnix']=time.time()
                    log.write(json.dumps(row)+'\n');log.flush();stop.wait(.4)
        finally:
            connection.close()

    def inject(self, kind):
        timeline={'kind':kind,'requestedUnix':time.time()}
        path=self.output/'fault-timeline.json'
        def persist():path.write_text(json.dumps(timeline,indent=2),encoding='utf-8')
        if kind=='database':
            connection=self.connect();connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT * FROM inventory_stock WHERE item_id=%s FOR UPDATE',(self.product,))
                    cursor.fetchall()
                timeline['activeUnix']=time.time();persist();time.sleep(3)
            finally:
                connection.rollback();connection.close();timeline['releasedUnix']=time.time();persist()
        elif kind in ('redis','kafka'):
            self.command(['stop','-t','1',kind]);timeline['activeUnix']=time.time();persist()
            try:time.sleep(12)
            finally:
                self.command(['start',kind]);timeline['releasedUnix']=time.time();persist()
            timeline['releaseDefinition']='restart command returned; readiness recorded separately'
            if kind=='redis':
                self.wait('Redis responds PONG',lambda:'PONG' in self.command(['exec','-T','redis','redis-cli','PING']),45)
            else:
                self.command(['exec','-T','kafka','/opt/kafka/bin/kafka-topics.sh','--bootstrap-server','localhost:9092','--list'])
            timeline['dependencyReadyUnix']=time.time();persist()
        elif kind=='warehouse':
            self.fault('commit_then_delay');timeline['activeUnix']=time.time();persist()
            try:time.sleep(15)
            finally:
                self.fault('normal');timeline['releasedUnix']=time.time();persist()
        return timeline

    def sustained(self, kind, duration=50):
        self.apps(True,True);self.fixture()
        # Offered arrivals are fixed; bounded executor admissions and drops are recorded.
        # Each write flow makes three real HTTP requests, not a fixed HTTP read/write ratio.
        stop=threading.Event();self.monitor_stop=stop;start=time.time();fault=None;errors=[];arrivals=[]
        self.phase='load'
        monitor_pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        monitoring=monitor_pool.submit(self.monitor,stop)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            pending=[]
            for tick in range(duration*4):
                due=start+tick*.25
                if due>time.time():time.sleep(due-time.time())
                if tick==40:fault=pool.submit(self.inject,kind)
                pending=[task for task in pending if not task.done()]
                is_write=tick%4==0;port=38080+(tick//4)%2
                row={'flowId':str(tick),'scheduledUnix':due,'submittedUnix':time.time(),'type':'write-flow' if is_write else 'page',
                     'port':port,'admitted':len(pending)<24}
                arrivals.append(row)
                if not row['admitted']:continue
                def operation(write=is_write,target=port,arrival=row):
                    arrival['executeStartUnix']=time.time()
                    try:
                        if write:arrival['orderId']=self.new_order('paid',target)
                        else:self.request('GET','/api/orders/page?size=20',port=target)
                        arrival['success']=True
                    except Exception as error:
                        arrival['success']=False;arrival['error']=str(error)
                        with self.lock:errors.append(str(error))
                    finally:
                        arrival['finishedUnix']=time.time()
                pending.append(pool.submit(operation))
            for task in pending:task.result()
            timeline=fault.result()
        load_end=time.time();self.fault('normal')
        self.phase='repair'
        # Resolve completed server-side creates whose client flow did not reach payment.
        unfinished=self.sql("SELECT id FROM customer_order WHERE user_id=%s AND status='PENDING_PAYMENT'",(self.user,))
        natural=self.sql('''SELECT o.id,o.status,p.status AS payment,f.status AS fulfillment FROM customer_order o
            LEFT JOIN payment_record p ON p.order_id=o.id LEFT JOIN fulfillment_task f ON f.order_id=o.id WHERE o.user_id=%s''',(self.user,))
        (self.output/'natural-load-end.json').write_text(json.dumps({'observedUnix':time.time(),'orders':natural,'repairOrderIds':[r['id'] for r in unfinished]},indent=2),encoding='utf-8')
        for row in unfinished:
            payment=self.request('POST','/api/payments/orders/'+row['id'],{},201)
            self.callback(payment)
        self.phase='recovery'
        business=self.reconcile(150)
        self.wait('all outbox published',lambda:self.sql('''SELECT COUNT(*) AS n FROM outbox_event e JOIN customer_order o
            ON o.id=e.aggregate_id WHERE o.user_id=%s AND e.status<>'PUBLISHED' ''',(self.user,))[0]['n']==0,150)
        self.wait('all original events consumed',lambda:self.sql('''SELECT COUNT(*) AS n FROM outbox_event e JOIN customer_order o
            ON o.id=e.aggregate_id LEFT JOIN inbox_event i ON i.event_id=e.id AND i.consumer_name='fulfillment-v1'
            WHERE o.user_id=%s AND i.event_id IS NULL''',(self.user,))[0]['n']==0,90)
        all_recovered=time.time();stop.set();monitoring.result();monitor_pool.shutdown()
        self.offsets('final')
        (self.output/'arrivals.json').write_text(json.dumps(arrivals,indent=2),encoding='utf-8')
        metrics=[]
        for line in (self.output/'observations.jsonl').read_text(encoding='utf-8').splitlines():
            metrics.append(json.loads(line))
        from analyze_mixed_workload import metric_values
        instance_metrics={}
        for port in (38080,38081):
            per_instance={}
            for name in ('hikaricp_connections_active','hikaricp_connections_pending','hikaricp_connections_timeout_total',
                'local_life_fulfillment_worker_active','local_life_fulfillment_worker_queued','local_life_fulfillment_queue_rejected_total'):
                values=[v for row in metrics for v in metric_values(row['instances'][str(port)].get('raw',''),name)]
                if name.startswith('hikaricp'):assert values,(port,name,'missing required metrics')
                per_instance[name]={'first':values[0],'last':values[-1],'max':max(values),'delta':values[-1]-values[0]} if values else None
            for window in ('before','after'):
                samples=[row for row in metrics if (row['unix']<timeline['activeUnix'] if window=='before' else row['unix']>timeline['releasedUnix'])]
                assert any(row['instances'][str(port)].get('status')==200 for row in samples),(port,window,'no successful monitoring')
                for metric in ('hikaricp_connections_active','hikaricp_connections_pending','hikaricp_connections_timeout_total'):
                    assert any(metric_values(row['instances'][str(port)].get('raw',''),metric) for row in samples),(port,window,metric)
                writes=[a for a in arrivals if a['port']==port and a['type']=='write-flow' and a.get('success') and
                        (a['executeStartUnix']<timeline['activeUnix'] if window=='before' else a['executeStartUnix']>timeline['releasedUnix'])]
                assert writes,(port,window,'no successful write flow')
            instance_metrics[str(port)]=per_instance
        peaks={name:max((v for row in metrics for app in row['instances'].values()
            for v in metric_values(app.get('raw',''),name)),default=None) for name in (
            'hikaricp_connections_active','hikaricp_connections_pending','hikaricp_connections_timeout_total',
            'local_life_fulfillment_worker_active','local_life_fulfillment_worker_queued',
            'local_life_fulfillment_queue_rejected_total')}
        during_fault=[row for row in metrics if timeline['activeUnix']<=row['unix']<=timeline['releasedUnix']]
        assert during_fault,'No fault-window observations'
        if kind=='database':assert max(row['dataLockWaits'] for row in during_fault)>0
        if kind=='warehouse':
            assert peaks['local_life_fulfillment_worker_queued']==2
            assert (peaks['local_life_fulfillment_queue_rejected_total'] or 0)>0
        own_backlog=max(sum(x['n'] for x in row['outbox_event'] if x['status']!='PUBLISHED') for row in metrics)
        fault_backlog=max(sum(x['n'] for x in row['outbox_event'] if x['status']!='PUBLISHED') for row in during_fault)
        if kind=='kafka':assert fault_backlog>0
        requests=[row for row in self.http if row['startedUnix']>=start and row['startedUnix']<=load_end]
        windows={}
        from mixed_workload import percentile
        completed=[a for a in arrivals if 'finishedUnix' in a]
        queue_ms=[(a['executeStartUnix']-a['submittedUnix'])*1000 for a in completed]
        flow_ms=[(a['finishedUnix']-a['scheduledUnix'])*1000 for a in completed]
        overlap=[row for row in requests if row['startedUnix']<timeline['releasedUnix'] and
                 row['startedUnix']+row['milliseconds']/1000>timeline['activeUnix']]
        for name in ('before','during','after'):
            values=[row for row in requests if ('before' if row['startedUnix']<timeline['activeUnix'] else
                ('during' if row['startedUnix']<timeline['releasedUnix'] else 'after'))==name]
            ms=[row['milliseconds'] for row in values]
            windows[name]={'requests':len(values),'httpErrors':sum(r['status']>=400 or r['status']==0 for r in values),
                'p95Ms':percentile(ms,.95),'p99Ms':percentile(ms,.99),'maxMs':max(ms,default=None)}
        self.accept('sustained_'+kind,offeredArrivals=len(arrivals),admitted=sum(x['admitted'] for x in arrivals),
            loadSeconds=load_end-start,flowErrors=errors,unfinishedFlowsReconciled=len(unfinished),
            plannedDurationSeconds=duration,postScheduleDrainSeconds=max(0,load_end-(start+duration)),
            queueWaitP95Ms=percentile(queue_ms,.95),offeredToFlowDoneP95Ms=percentile(flow_ms,.95),
            offeredToFlowDoneMaxMs=max(flow_ms),noHttpFlowErrors=not errors,
            admissionDrops=sum(not a['admitted'] for a in arrivals),
            offeredWorkloadSuccess=all(a['admitted'] and a.get('success') for a in arrivals),
            compensationRequired=bool(unfinished),instanceMetrics=instance_metrics,
            maxOwnOutboxUnpublished=own_backlog,maxFaultWindowOutboxUnpublished=fault_backlog,
            maxFaultWindowDataLockWaits=max(row['dataLockWaits'] for row in during_fault),
            allBusinessAndOwnEventsRecoveredUnix=all_recovered,
            faultReleaseToFinalConvergenceCheckSeconds=all_recovered-timeline['releasedUnix'],
            firstSuccessfulWriteAfterReleaseSeconds=min(a['finishedUnix'] for a in completed if a['type']=='write-flow' and a.get('success') and a['executeStartUnix']>=timeline['releasedUnix'])-timeline['releasedUnix'],
            overlappingFaultRequests=len(overlap),overlappingFaultMaxMs=max((r['milliseconds'] for r in overlap),default=None),
            windows=windows,peaks=peaks,maxDataLockWaits=max(row['dataLockWaits'] for row in metrics),
            faultTimeline=timeline,allOwnOutboxPublishedAndConsumed=True,**business)

    def pause_recovery(self):
        self.apps(True,True);self.fixture();self.fault('commit_then_delay')
        paused=[]
        try:
            self.command(['pause','app1']);paused.append('app1')
            identity=self.new_order('paid',38081)
            self.wait('external commit before local acknowledgement',lambda:self.shipment_count(identity)==1
                and self.task(identity)['status']=='DISPATCHING',90)
            old=self.task(identity)
            self.command(['pause','app2']);paused.append('app2')
            self.command(['unpause','app1']);paused.remove('app1')
            self.fault('normal')
            self.wait('another owner with higher fence ships',lambda:self.task(identity)['status']=='SHIPPED'
                and self.task(identity)['fence']>old['fence'],90)
            current=self.task(identity)
            self.command(['unpause','app2']);paused.remove('app2')
            def old_worker_finished():
                client=http.client.HTTPConnection('127.0.0.1',38081,timeout=2)
                try:
                    client.request('GET','/actuator/prometheus');response=client.getresponse();raw=response.read().decode()
                finally:client.close()
                from analyze_mixed_workload import metric_values
                (self.output/'old-worker-after-resume.prom').write_text(raw,encoding='utf-8')
                active=metric_values(raw,'local_life_fulfillment_worker_active')
                counts=metric_values(raw,'local_life_fulfillment_dispatch_seconds_count')
                return active and max(active)==0 and counts and sum(counts)>=1
            self.wait('resumed worker completed attempt',old_worker_finished,45)
            after=self.task(identity)
            assert current['fence']==after['fence'] and after['status']=='SHIPPED'
            audits=self.sql('SELECT * FROM fulfillment_attempt WHERE order_id=%s ORDER BY id',(identity,))
            assert sum(row['outcome']=='SHIPPED' for row in audits)==1
            (self.output/'claims.json').write_text(json.dumps({'before':old,'after':after,'audit':audits},indent=2,default=str),encoding='utf-8')
            self.accept('paused_owner_recovers_without_duplicate_effect',oldFence=old['fence'],newFence=after['fence'],
                resumedWorkerCompleted=True,directStaleUpdateReturnObserved=False,
                **self.reconcile())
        finally:
            self.fault('normal')
            for name in paused:self.command(['unpause',name])

    def payment_race(self):
        self.apps(False,True);self.fixture();evidence=[]
        for number in range(24):
            identity=self.new_order('pending',38080+number%2)
            payment=self.request('POST','/api/payments/orders/'+identity,{},201)
            self.sql('UPDATE customer_order SET expires_at=CURRENT_TIMESTAMP(6) WHERE id=%s',(identity,))
            # Inject only the fixture deadline; the actual expiry job performs business transitions.
            delay=(number%3)*.35;time.sleep(delay)
            status,_=self.callback(payment,38080+(number+1)%2,expected=None)
            self.wait('paid or expired',lambda:self.sql('SELECT status FROM customer_order WHERE id=%s',(identity,))[0]['status'] in ('PAID','EXPIRED'))
            state=self.sql('''SELECT o.status,p.status AS payment,r.status AS reservation FROM customer_order o
                JOIN payment_record p ON p.order_id=o.id JOIN inventory_reservation r ON r.order_id=o.id WHERE o.id=%s''',(identity,))[0]
            assert state in ({'status':'PAID','payment':'SUCCESS','reservation':'CONFIRMED'},
                             {'status':'EXPIRED','payment':'CREATED','reservation':'EXPIRED'}),state
            if state['status']=='EXPIRED':assert status==422
            else:assert status==200
            evidence.append({'id':identity,'callbackDelaySeconds':delay,'callbackHttpStatus':status,**state})
        assert {row['status'] for row in evidence}=={'PAID','EXPIRED'}
        (self.output/'race.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        self.apps(True,True)
        self.accept('signed_callback_vs_automatic_expiry',outcomes=evidence,**self.reconcile())

    def payment_boundary(self):
        self.apps(False,True);self.fixture();evidence=[]
        for case in ('expired','outbox_failure'):
            identity=self.new_order('pending')
            payment=self.request('POST','/api/payments/orders/'+identity,{},201)
            if case=='expired':
                self.sql('UPDATE customer_order SET expires_at=CURRENT_TIMESTAMP(6) WHERE id=%s',(identity,))
                self.wait('fixture automatically expired',lambda:self.sql('SELECT status FROM customer_order WHERE id=%s',(identity,))[0]['status']=='EXPIRED')
            else:
                # Only this fresh fixture order is affected; trigger dropped even on failure.
                self.sql("CREATE TRIGGER v3_reject_paid BEFORE INSERT ON outbox_event FOR EACH ROW BEGIN "
                    "IF NEW.aggregate_id='"+identity+"' AND NEW.event_type='order.paid.v1' THEN "
                    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='V3 audit paid outbox failure'; END IF; END")
            try:
                status,_=self.request('POST','/api/payments/'+payment['id']+'/simulate-success',{},None)
                state=self.sql('''SELECT o.status,p.status AS payment,r.status AS reservation,
                    (SELECT COUNT(*) FROM payment_notification n WHERE n.payment_no=p.payment_no) AS notifications
                    FROM customer_order o JOIN payment_record p ON p.order_id=o.id
                    JOIN inventory_reservation r ON r.order_id=o.id WHERE o.id=%s''',(identity,))[0]
                assert state['payment']=='CREATED' and state['notifications']==0,state
                assert state['status']==('EXPIRED' if case=='expired' else 'PENDING_PAYMENT'),state
                assert state['reservation']==('EXPIRED' if case=='expired' else 'RESERVED'),state
                assert status==(422 if case=='expired' else 500),status
                evidence.append({'case':case,'httpStatus':status,**state})
            finally:
                if case=='outbox_failure':self.sql('DROP TRIGGER v3_reject_paid')
            if case=='outbox_failure':
                self.request('POST','/api/payments/'+payment['id']+'/simulate-success',{})
                self.request('POST','/api/payments/'+payment['id']+'/simulate-success',{})
        self.apps(True,True)
        self.accept('http_simulator_atomic_boundary_fixed',controls=evidence,**self.reconcile())

    def save(self,status,error=None):
        super().save(status,error)
        path=self.output/'result.json';result=json.loads(path.read_text())
        result['entryScriptSha256']=self.entry_hash
        path.write_text(json.dumps(result,indent=2),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--case',choices=['redis','kafka','database','warehouse','pause_recovery','payment_race','payment_boundary'],required=True)
    a=p.parse_args();runner=LoadFaultVerification(a.runtime,a.output)
    try:
        if a.case in ('pause_recovery','payment_race','payment_boundary'):getattr(runner,a.case)()
        else:runner.sustained(a.case)
        runner.save('BOUNDED_ACCEPT')
    except Exception as error:
        runner.save('FAILED',str(error));raise
    finally:
        if hasattr(runner,'monitor_stop'):runner.monitor_stop.set()
        runner.fault('normal');runner.command(['logs','--no-color','app1','app2'])
