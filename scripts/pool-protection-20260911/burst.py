"""Expose the admission tradeoff under a 500-request burst; 503 is not counted as success."""
import asyncio,json,time
from pathlib import Path
import aiohttp
DEST=Path(__file__).resolve().parents[2]/'.runtime/pool-protection-20260911-v3'
async def main():
    rows=[]
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=500),timeout=aiohttp.ClientTimeout(total=15)) as c:
        async def request():
            t=time.perf_counter()
            try:
                async with c.get('http://127.0.0.1:38513/api/products/920000/purchase-view') as r:
                    await r.read();rows.append({'status':r.status,'ms':(time.perf_counter()-t)*1000})
            except Exception as e:rows.append({'status':'transport_error','error':type(e).__name__,'ms':(time.perf_counter()-t)*1000})
        await asyncio.gather(*(request() for _ in range(500)))
    counts={str(code):sum(r['status']==code for r in rows) for code in {r['status'] for r in rows}}
    result={'requests':500,'statuses':counts,'rows':rows,'scope':'one burst, admission tradeoff, not capacity benchmark; possible live cutover on same host'}
    (DEST/'burst-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    assert all(r['status']==200 for r in rows),counts
    print(counts)
asyncio.run(main())
