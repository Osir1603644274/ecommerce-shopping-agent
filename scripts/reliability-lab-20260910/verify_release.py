"""Read-only HTTP acceptance of release image against synthetic lab database."""
import asyncio,json,subprocess,time
import aiohttp,yaml
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
LAB=ROOT/'.runtime/reliability-lab-20260910'
config=yaml.safe_load((LAB/'compose.yaml').read_text())
app=json.loads(json.dumps(config['services']['app1']))
app.update(image='agent-backend:product-read-20260910',volumes=[],working_dir='/app',command=['java','-Xmx384m','-jar','/app/app.jar'],ports=['127.0.0.1:38486:8080'])
config['services']['canary']=app
path=LAB/'canary.compose.yaml';path.write_text(yaml.safe_dump(config,sort_keys=False),encoding='utf-8')
cmd=['docker','compose','-f',str(path)]
subprocess.run(cmd+['up','-d','--no-deps','canary'],check=True)
async def run():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as c:
        for _ in range(180):
            try:
                async with c.get('http://127.0.0.1:38486/api/health') as r:
                    if r.status==200:break
            except Exception:pass
            await asyncio.sleep(1)
        else:raise RuntimeError('Release canary not ready')
        records=[]
        for pid in (920000,920240):
            async with c.get(f'http://127.0.0.1:38486/api/products/{pid}/purchase-view') as r:
                data=await r.json();assert r.status==200,data
            result=data['data'];assert result['product']['id']==pid
            assert result['offer']['priceMinor']==60000
            if pid==920240:assert result['offer']['available']==0 and result['offer']['canPurchase'] is False
            records.append({'productId':pid,'offer':result['offer']})
        async with c.get('http://127.0.0.1:38486/api/products/99999999/purchase-view') as r:assert r.status==404
        async with c.get('http://127.0.0.1:38486/api/cache-lab/two/920000') as r:assert r.status==404
        (LAB/'release-acceptance.json').write_text(json.dumps({'status':'PASS','checks':records,'missingProduct404':True,'experimentEndpointAbsent':True}),encoding='utf-8')
        print('Release canary passed',flush=True)
try:asyncio.run(run())
finally:subprocess.run(cmd+['stop','canary'],check=True)
