"""Actual cold-JVM cache recovery. Global SQL counters only with other app stopped."""
import asyncio,json,time,random
import aiohttp
from run import ROOT,command,sql,req
from faults import ready
async def run():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as c:
        await ready(c,38483)
        for tier in ('l1','l2','two'):
            for pid in range(920200,920212):await req(c,f'/api/cache-lab/{tier}/{pid}',38483)
        # app1 has just been genuinely restarted by the after-handler crash case
        # and has not read any cache-lab products. Reuse that cold JVM, no extra reboot.
        prior=json.loads((ROOT/'inbox-results.json').read_text())
        assert any(r['name']=='inbox_crash_after-handler' and r['status']=='PASS' for r in prior)
        command('stop','app2')
        start=time.monotonic();await ready(c,38482)
        startup=time.monotonic()-start
        rows=[]
        # Same product contents, same JVM. Different namespaces/adapters, Redis survives.
        for i,pid in enumerate(range(920200,920212)):
            tiers=['l1','l2','two'];random.Random(i).shuffle(tiers)
            values=[]
            for tier in tiers:
                before=int(sql("SHOW GLOBAL STATUS LIKE 'Com_select';").strip().split('\t')[1])
                start=time.perf_counter();value=await req(c,f'/api/cache-lab/{tier}/{pid}');ms=(time.perf_counter()-start)*1000
                after=int(sql("SHOW GLOBAL STATUS LIKE 'Com_select';").strip().split('\t')[1])
                rows.append({'tier':tier,'productId':pid,'ms':ms,'globalSelectDelta':after-before});values.append(value)
            assert values[0]==values[1]==values[2], 'Adapters differ in payload'
        (ROOT/'restart-cache-results.json').write_text(json.dumps({'status':'PASS','rows':rows,'readinessWaitSeconds':startup,
            'scope':'12 distinct products per tier, cold JVM from prior verified after-handler crash/restart; Redis survives; other app stopped; global SQL counts may include scheduled reads'},indent=2),encoding='utf-8')
        print('Restart cache observations complete',flush=True)
asyncio.run(run())
