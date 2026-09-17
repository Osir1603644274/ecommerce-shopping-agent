"""Real MySQL shared-pool regression, same binary with admission off/on. No live data."""
import asyncio,json,time,uuid,secrets,subprocess,hashlib,statistics
from pathlib import Path
import aiohttp
ROOT=Path(__file__).resolve().parents[2]
DEST=ROOT/'.runtime/pool-protection-20260911-v3'
DBCOMPOSE=['docker','compose','-f',str(ROOT/'.runtime/reliability-lab-20260910/compose.yaml')]
def sql(query):
    cid=subprocess.check_output(DBCOMPOSE+['ps','-q','mysql'],text=True).strip()
    label=subprocess.check_output(['docker','inspect','--format','{{ index .Config.Labels "com.docker.compose.project" }}',cid],text=True).strip()
    assert label=='reliability-lab-20260910'
    return subprocess.run(['docker','exec','-i','-e','MYSQL_PWD=isolated-experiment-only',cid,'mysql','-uroot','--batch','--skip-column-names','experiment'],input=query,encoding='utf-8',capture_output=True,check=True).stdout.strip()
async def run():
    assert not (DEST/'results.json').exists()
    results={'scope':'same patched lab.jar, guard off/on; reused isolated synthetic database; no production fault injection','rounds':[],'status':'RUNNING'}
    def save(): (DEST/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as c:
        async def request(port,path,method='GET',**kwargs):
            t=time.perf_counter()
            async with c.request(method,f'http://127.0.0.1:{port}'+path,**kwargs) as r:
                text=await r.text()
                try:body=json.loads(text)
                except ValueError:body={'text':text[:300]}
                return {'status':r.status,'ms':(time.perf_counter()-t)*1000,'body':body}
        for port in [38512,38513]:
            for attempt in range(150):
                try:
                    r=await request(port,'/api/health')
                    if r['status']==200:break
                except (aiohttp.ClientError,asyncio.TimeoutError): pass
                await asyncio.sleep(1)
            else:raise RuntimeError('Lab startup timed out')
        assert int(sql("SELECT COUNT(*) FROM product WHERE source='lab';"))==256
        registration=await request(38512,'/api/auth/register','POST',json={'username':'pool-'+uuid.uuid4().hex[:10],'password':secrets.token_urlsafe(24)})
        assert registration['status']==201
        token=registration['body']['data']['accessToken']
        async def order(port):
            key='pool-'+uuid.uuid4().hex
            r=await request(port,'/api/orders','POST',headers={'Authorization':'Bearer '+token,'Idempotency-Key':key},json={'itemType':'PRODUCT','itemId':920249,'quantity':1})
            count=int(sql(f"SELECT COUNT(*) FROM customer_order WHERE idempotency_key='{key}'"))
            assert r['status']==201 and count==1,r
            return {'status':r['status'],'ms':r['ms'],'orderCount':count}
        # Warm normal catalog and order paths before fault rounds; no result is treated as a fresh-user baseline.
        results['normalOrders']={}
        for port in [38512,38513]:
            for _ in range(5): assert (await request(port,'/api/products/920000/purchase-view'))['status']==200
            results['normalOrders'][str(port)]=await order(port)
        for trial in range(3):
            for name,port,expected in ([('baseline',38512,8),('fixed',38513,8)] if trial%2==0 else [('fixed',38513,8),('baseline',38512,8)]):
                tasks=[asyncio.create_task(request(port,'/api/products/pool-lab/slow')) for _ in range(8)]
                try:
                    active=0
                    for _ in range(20):
                        await asyncio.sleep(.05)
                        active=int(await asyncio.to_thread(sql,"SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE INFO='SELECT SLEEP(6)';"))
                        if active==expected:break
                    assert active==expected,(name,active)
                    created=await order(port)
                    slow=await asyncio.gather(*tasks)
                finally:
                    await asyncio.gather(*tasks,return_exceptions=True)
                statuses={str(code):sum(r['status']==code for r in slow) for code in {r['status'] for r in slow}}
                row={'trial':trial+1,'variant':name,'observedActiveSlowSql':active,'order':created,'slowStatuses':statuses,'slowRequestMs':[r['ms'] for r in slow],
                     'busy':sum('繁忙' in str(r['body']) for r in slow),'timedOut':sum('超时' in str(r['body']) for r in slow)}
                results['rounds'].append(row);save();print(json.dumps(row,ensure_ascii=False),flush=True)
                if name=='fixed': assert statuses=={'503':8} and row['busy']==0 and row['timedOut']==8 and created['ms']<2000,row
                else:assert statuses=={'200':8} and created['ms']>4000,row
                assert int(sql("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE INFO='SELECT SLEEP(6)';"))==0
        # Exercise the real JdbcTemplate offer path too, not only the synthetic MyBatis mapper.
        locker=asyncio.create_task(asyncio.to_thread(sql,"LOCK TABLES product_local_offer WRITE; SELECT SLEEP(5); UNLOCK TABLES;"))
        try:
            active=0
            for _ in range(20):
                await asyncio.sleep(.05)
                active=int(await asyncio.to_thread(sql,"SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE INFO='SELECT SLEEP(5)';"))
                if active==1:break
            assert active==1
            blocked=await request(38513,'/api/products/920000/purchase-view')
            results['realOfferJdbcTimeout']={'status':blocked['status'],'ms':blocked['ms'],'timeoutMessage':'超时' in str(blocked['body'])}
            save()
            assert blocked['status']==503 and results['realOfferJdbcTimeout']['timeoutMessage'] and blocked['ms']<4000,blocked
        finally:await locker
        # After timeout/rejection: permits and JDBC connections must be reusable.
        rows=[]
        for _ in range(25):rows+=await asyncio.gather(*(request(38513,'/api/products/920000/purchase-view') for _ in range(4)))
        results['normalAfterFault']={'requests':len(rows),'errors':sum(r['status']!=200 for r in rows),'medianMs':statistics.median(r['ms'] for r in rows)}
        assert results['normalAfterFault']['errors']==0
        results['status']='PASS';save()
asyncio.run(run())
