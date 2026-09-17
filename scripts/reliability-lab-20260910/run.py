"""Bounded cache trials. Fault trials added separately; all targets assert lab ownership."""
import asyncio, collections, hashlib, json, math, random, statistics, subprocess, time, uuid, secrets
from pathlib import Path
import aiohttp
ROOT=Path(__file__).resolve().parent
COMPOSE=['docker','compose','-f',str(ROOT/'compose.yaml')]
PROJECT='reliability-lab-20260910'
ROWS=[]
def command(*args):
    return subprocess.run(COMPOSE+list(args),check=True,capture_output=True,text=True,encoding='utf-8').stdout
def sql(query):
    cid=command('ps','-q','mysql').strip()
    label=subprocess.run(['docker','inspect','--format','{{ index .Config.Labels "com.docker.compose.project" }}',cid],check=True,capture_output=True,text=True).stdout.strip()
    assert label==PROJECT
    return subprocess.run(['docker','exec','-i','-e','MYSQL_PWD=isolated-experiment-only',cid,'mysql','-uroot','--batch','--skip-column-names','experiment'],input=query,encoding='utf-8',check=True,capture_output=True).stdout
def seed():
    statements=[]
    for i in range(256):
        p=920000+i
        statements.extend([f"INSERT INTO product(id,source,source_item_id,title,brand,seller,category_l1,category_l2,category_l3,snapshot_price_minor,currency,price_status,lifecycle_status,entity_version,attribute_text,data_nature,dataset_revision,source_license,provenance_url) VALUES({p},'lab','{p}','Synthetic {i}','lab','fixture','digital','phone','二手手机',50000,'CNY','verified','ACTIVE',1,'synthetic','synthetic','lab','test','about:blank');",
          f"INSERT INTO product_local_offer(product_id,price_minor,currency,price_kind,source_revision) VALUES({p},60000,'CNY','local_simulated','lab');",
          f"INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity) VALUES('PRODUCT',{p},1000,1000);"])
        statements.extend(f"INSERT INTO product_attribute(product_id,attribute_key,value_type,raw_value,normalized_text,evidence_field,extraction_method,confidence) VALUES({p},'a{a}','text','synthetic','synthetic','fixture','fixture',1);" for a in range(8))
    sql('\n'.join(statements))
async def req(c,path,port=38482,method='GET',**kw):
    async with c.request(method,f'http://127.0.0.1:{port}'+path,**kw) as r:
        body=await r.json(); assert r.status==200,(r.status,body);return body
def percentile(xs,q):
    xs=sorted(xs);p=(len(xs)-1)*q;a=int(p);return xs[a]+(xs[math.ceil(p)]-xs[a])*(p-a)
async def run():
    assert not (ROOT/'cache-samples.json').exists()
    existing=int(sql("SELECT COUNT(*) FROM product WHERE source='lab';"))
    if existing==0:seed()
    else:assert existing==256, 'Partial fixture requires investigation, not automatic overwrite'
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),connector=aiohttp.TCPConnector(limit=500)) as c:
        async with c.post('http://127.0.0.1:38482/api/auth/register',json={'username':'cache-'+uuid.uuid4().hex[:10],'password':secrets.token_urlsafe(24)}) as response:
            registered=await response.json();assert response.status==201
            reset_headers={'Authorization':'Bearer '+registered['data']['accessToken']}
        for port in (38482,38483):
            for tier in ('l1','l2','two'):
                for p in range(920000,920032): await req(c,f'/api/cache-lab/{tier}/{p}',port)
        for state,concurrency in [('hot',16),('spread',16),('cold',16),('hot',128),('hot',500)]:
            for rnd in range(3):
                tiers=['l1','l2','two'];random.Random(57+rnd).shuffle(tiers)
                for tier in tiers:
                    for port in (38482,38483):await req(c,f'/api/cache-lab/reset/{tier}',port,method='POST',headers=reset_headers)
                    if state!='cold':
                        for p in range(920000,920256):await req(c,f'/api/cache-lab/{tier}/{p}')
                    n=256 if state=='cold' else max(512,2*concurrency)
                    queue=iter(range(n));batch=[]
                    async def worker():
                        for i in queue:
                            p=920000+(i% (4 if state=='hot' else 256));start=time.perf_counter();error=None
                            try:
                                value=await req(c,f'/api/cache-lab/{tier}/{p}',38482+i%2)
                                assert value['id']==p
                            except Exception as e:error=type(e).__name__+':'+str(e)
                            row={'tier':tier,'state':state,'concurrency':concurrency,'round':rnd,'ms':(time.perf_counter()-start)*1000,'error':error}
                            ROWS.append(row);batch.append(row)
                    await asyncio.gather(*(worker() for _ in range(concurrency)))
                    print(json.dumps({'tier':tier,'state':state,'concurrency':concurrency,'round':rnd,'p95':percentile([r['ms'] for r in batch],.95),'errors':sum(bool(r['error']) for r in batch)}),flush=True)
                    if sum(bool(r['error']) for r in batch)>len(batch)*.01 or percentile([r['ms'] for r in batch],.95)>5000:
                        raise RuntimeError('Safety threshold; preserve partial results')
    
if __name__=='__main__':
    status='COMPLETE';error=None
    try:asyncio.run(run())
    except Exception as e:status='INCOMPLETE';error=repr(e);raise
    finally:
        (ROOT/'cache-samples.json').write_text(json.dumps(ROWS),encoding='utf-8')
        groups=collections.defaultdict(list)
        for row in ROWS:groups[(row['tier'],row['state'],row['concurrency'])].append(row)
        summary=[dict(zip(('tier','state','concurrency'),key),samples=len(rows),errors=sum(bool(r['error']) for r in rows),p50=percentile([r['ms'] for r in rows],.5),p95=percentile([r['ms'] for r in rows],.95),p99=percentile([r['ms'] for r in rows],.99)) for key,rows in groups.items()]
        (ROOT/'cache-results.json').write_text(json.dumps({'status':status,'error':error,'groups':summary},indent=2),encoding='utf-8')
