"""No fixes: genuine isolated HTTP/MySQL/Redis/JVM faults, transport-failure simulation."""
import asyncio, json, secrets, time, uuid, subprocess
from pathlib import Path
import aiohttp
from aiohttp import web
from run import ROOT, COMPOSE, command, sql, req
RESULTS=[]
def save():
    (ROOT/'fault-results.json').write_text(json.dumps(RESULTS,indent=2,ensure_ascii=False),encoding='utf-8')
def record(name,passed,**details):
    row={'name':name,'status':'PASS' if passed else 'FINDING',**details};RESULTS.append(row);save();print(json.dumps(row,ensure_ascii=False),flush=True)
async def ready(c,port):
    for _ in range(180):
        try:
            await req(c,'/api/health',port);return
        except Exception:await asyncio.sleep(1)
    raise RuntimeError('Lab JVM did not recover')
async def run():
    assert not (ROOT/'fault-results.json').exists()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),connector=aiohttp.TCPConnector(limit=128)) as c:
        await ready(c,38482);await ready(c,38483)
        user='lab-'+uuid.uuid4().hex[:12];password=secrets.token_urlsafe(24)
        async with c.post('http://127.0.0.1:38482/api/auth/register',json={'username':user,'password':password}) as r:
            body=await r.json();assert r.status==201,body;token=body['data']['accessToken'];owner=body['data']['user']['id']
        headers={'Authorization':'Bearer '+token}
        async def create(key,pid=920250,port=38482):
            async with c.post(f'http://127.0.0.1:{port}/api/orders',json={'itemType':'PRODUCT','itemId':pid,'quantity':1},headers={**headers,'Idempotency-Key':key}) as r:
                return r.status,await r.json()
        # Actual authority commits; intermediary drops the response before the client receives it.
        async def lose(request):
            status,body=await create(request.match_info['key'])
            assert status==201,body
            request.transport.close()
            return web.Response(status=502)
        app=web.Application();app.router.add_post('/{key}',lose);runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,'127.0.0.1',38485).start()
        outcomes=[]
        try:
            for i in range(10):
                key='lost-'+uuid.uuid4().hex
                try:
                    async with c.post('http://127.0.0.1:38485/'+key) as r:await r.read()
                except aiohttp.ClientError:pass
                status,again=await create(key,port=38483)
                async with c.get('http://127.0.0.1:38483/api/orders/by-idempotency-key/'+key,headers=headers) as r:found=await r.json()
                count=int(sql(f"SELECT COUNT(*) FROM customer_order WHERE user_id='{owner}' AND idempotency_key='{key}';").strip())
                outcomes.append({'sameOrder':again.get('data',{}).get('id')==found.get('data',{}).get('id'),'count':count,'retryStatus':status})
            record('commit_then_response_lost',all(x['sameOrder'] and x['count']==1 for x in outcomes),rounds=outcomes,scope='Java HTTP through dropping proxy, not Agent confirmation')
        finally:await runner.cleanup()
        # Competing unique orders across two actual Java processes.
        for trial in range(3):
            pid=920240+trial
            sql(f"UPDATE inventory_stock SET total_quantity=1,available_quantity=1 WHERE item_id={pid} AND item_type='PRODUCT';")
            responses=await asyncio.gather(*(create('race-'+uuid.uuid4().hex,pid,38482+i%2) for i in range(64)))
            stock=sql(f"SELECT available_quantity,reserved_quantity,sold_quantity FROM inventory_stock WHERE item_id={pid} AND item_type='PRODUCT';").strip()
            statuses={str(s):sum(a==s for a,b in responses) for s,b in responses}
            record('last_stock_two_instances_'+str(trial),sum(s==201 for s,b in responses)==1 and stock=='0\t1\t0',statuses=statuses,stock=stock,requests=64)
        # Failed outbox insert must roll back order and stock in the same transaction.
        key='rollback-'+uuid.uuid4().hex
        before=sql("SELECT available_quantity,reserved_quantity FROM inventory_stock WHERE item_id=920250;")
        sql("CREATE TRIGGER lab_outbox_failure BEFORE INSERT ON outbox_event FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='lab injection';")
        try:status,_=await create(key)
        finally:sql('DROP TRIGGER lab_outbox_failure;')
        count=int(sql(f"SELECT COUNT(*) FROM customer_order WHERE idempotency_key='{key}';"))
        after=sql("SELECT available_quantity,reserved_quantity FROM inventory_stock WHERE item_id=920250;")
        record('outbox_insert_failure_atomic_rollback',status>=400 and count==0 and before==after,httpStatus=status,orders=count,stockUnchanged=before==after)
        # Real SQL Outbox retry state; explicit synthetic transport failure, not broker outage.
        event=sql("SELECT id FROM outbox_event WHERE status='PENDING' ORDER BY created_at LIMIT 1;").strip()
        await req(c,f'/api/message-lab/outbox/{event}?mode=fail')
        pending=sql(f"SELECT status,attempts FROM outbox_event WHERE id='{event}';").strip()
        await asyncio.sleep(3)
        await req(c,f'/api/message-lab/outbox/{event}?mode=success')
        final=sql(f"SELECT status FROM outbox_event WHERE id='{event}';").strip()
        record('outbox_transport_failure_retry_contract',pending=='PENDING\t1' and final=='PUBLISHED',afterFailure=pending,afterRecovery=final,scope='real MySQL claim/failure/receipt; injected transport exception, no Kafka send')
        start=time.perf_counter();baseline,_=await create('normal-'+uuid.uuid4().hex);base_ms=(time.perf_counter()-start)*1000
        slow=[asyncio.create_task(req(c,'/api/cache-lab/slow')) for _ in range(8)]
        await asyncio.sleep(.5)
        start=time.perf_counter();status,_=await create('slow-'+uuid.uuid4().hex);affected_ms=(time.perf_counter()-start)*1000
        await asyncio.gather(*slow)
        record('shared_db_pool_slow_dependency',status==201 and affected_ms<2000,baselineMs=base_ms,affectedMs=affected_ms,httpStatus=status,
               scope='8 injected SELECT SLEEP(6) share actual Hikari pool; 2s is this lab responsiveness target, not production SLA')
        # Redis lost invalidation: SQL update plus DEL but missed Pub/Sub notification.
        pid=920230
        old=await req(c,f'/api/cache-lab/two/{pid}',38483)
        sql(f"UPDATE product SET title='updated-lab' WHERE id={pid};")
        command('exec','-T','redis','redis-cli','DEL',f'local-life:product:detail:v1:{pid}')
        stale=await req(c,f'/api/cache-lab/two/{pid}',38483)
        await asyncio.sleep(32)
        fresh=await req(c,f'/api/cache-lab/two/{pid}',38483)
        record('missed_invalidation_with_redis_delete',fresh['title']=='updated-lab',immediatelyStale=stale['title']==old['title'],after32Seconds=fresh['title'],expectedBound='L1 TTL 30s plus reload')
        # Lost whole invalidation (including Redis DEL) distinguishes L1 TTL from total stale bound.
        pid=920231
        await req(c,f'/api/cache-lab/two/{pid}',38483)
        sql(f"UPDATE product SET title='whole-invalidation-lost' WHERE id={pid};")
        await asyncio.sleep(32)
        stale=await req(c,f'/api/cache-lab/two/{pid}',38483)
        ttl=command('exec','-T','redis','redis-cli','TTL',f'local-life:product:detail:v1:{pid}').strip()
        record('whole_invalidation_lost',stale['title']=='whole-invalidation-lost',after32Seconds=stale['title'],redisRemainingTTL=ttl,scope='omitted whole invalidation; no claim this occurs on every normal update')
        # Actual Redis outage; hot L1 versus cold request (no production configuration change).
        await req(c,'/api/cache-lab/two/920229')
        command('stop','redis')
        try:
            start=time.perf_counter();hot=await req(c,'/api/cache-lab/two/920229');hotms=(time.perf_counter()-start)*1000
            # Pick a never-read-by-this-process product after cache campaign? force local expiry.
            await asyncio.sleep(31)
            start=time.perf_counter()
            try:
                cold=await req(c,'/api/cache-lab/two/920228');coldms=(time.perf_counter()-start)*1000;ok=cold['id']==920228
            except Exception as e:coldms=(time.perf_counter()-start)*1000;ok=False
            record('redis_outage_fallback',ok,hotMs=hotms,coldMs=coldms,scope='one cold request after expiry; not capacity proof')
        finally:command('start','redis')
        # Actual kill at two Inbox windows, then recovery through the other instance.
        for mode in ('before-handler','after-handler'):
            event=str(uuid.uuid4())
            try:await req(c,f'/api/message-lab/inbox/{event}?mode={mode}')
            except Exception:pass
            processing=sql(f"SELECT status FROM inbox_event WHERE consumer_name='lab-audit' AND event_id='{event}';").strip()
            await asyncio.sleep(2)
            await req(c,f'/api/message-lab/inbox/{event}',38483)
            for _ in range(10):await req(c,f'/api/message-lab/inbox/{event}',38483)
            receipt=sql(f"SELECT status,attempts FROM inbox_event WHERE consumer_name='lab-audit' AND event_id='{event}';").strip()
            record('inbox_crash_'+mode,processing=='PROCESSING' and receipt=='PROCESSED\t2',before=processing,after=receipt,duplicateRedeliveries=10,scope='real JVM termination, real Inbox and audit handler; no Kafka offset claim')
            command('start','app1');await ready(c,38482)
        record('final_order_stock_invariants',int(sql('SELECT COUNT(*) FROM inventory_stock WHERE available_quantity<0 OR reserved_quantity<0 OR sold_quantity<0 OR available_quantity+reserved_quantity+sold_quantity<>total_quantity;'))==0)

if __name__=='__main__':
    try:asyncio.run(run())
    except Exception as e:
        RESULTS.append({'name':'campaign_error','status':'INCOMPLETE','error':repr(e)});save();raise
