"""Live HTTP acceptance: BGE/BM25, concurrency, interruption and recovery."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
from app.product_knowledge.client import search_evidence
from mcp import Client

async def main(out: Path):
    out.mkdir(parents=True,exist_ok=False)
    url='http://127.0.0.1:18793/mcp'
    env={**os.environ,'PYTHONPATH':str(ROOT/'agent')}
    log=(out/'server.log').open('w',encoding='utf8')
    proc=None
    def start():
        return subprocess.Popen([sys.executable,'-B','-m','app.product_knowledge.server','--port','18793'],
            cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    async def ready():
        for _ in range(100):
            if proc.poll() is not None: raise RuntimeError('server_exited')
            try:
                async with Client(url) as client:
                    return await client.list_tools()
            except Exception: await asyncio.sleep(.2)
        raise TimeoutError('server_start_timeout')
    async def query():
        start_time=time.perf_counter()
        r=await search_evidence(url,'芯片和续航',['1275270'],timeout=20)
        return dict(seconds=round(time.perf_counter()-start_time,4),result=r)
    try:
        proc=start();listed=await ready()
        first=await query()
        assert first['result']['retrieval']['channels']=={'bm25':'OK','bge':'OK'}
        async with Client(url) as client:
            raw=await client.call_tool('get_product_evidence',{'evidence_id':first['result']['evidence'][0]['evidenceId']})
            retrieved=raw.model_dump(by_alias=True).get('structuredContent')
            if retrieved is None: retrieved=json.loads(next(c.text for c in raw.content if c.type=='text'))
        assert retrieved['evidence']==first['result']['evidence'][0]
        concurrent=await asyncio.gather(*(query() for _ in range(8)))
        assert all(r['result']==first['result'] for r in concurrent)
        proc.terminate();proc.wait(timeout=10)
        disconnected=False
        try: await search_evidence(url,'芯片',['1275270'],timeout=.5)
        except Exception: disconnected=True
        assert disconnected
        proc=start();await ready();recovered=await query()
        assert recovered['result']==first['result']
        result=dict(status='PASS',knowledgeVersion=first['result']['knowledgeVersion'],
            tools=[t.name for t in listed.tools],first=first,getById=retrieved,concurrent=concurrent,
            disconnected=disconnected,recovered=recovered,kind='LIVE_MCP_COMPONENT_ACCEPTANCE_NOT_AGENT_AB')
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print(json.dumps({k:result[k] for k in ('status','knowledgeVersion','tools','disconnected','kind')},ensure_ascii=False))
    finally:
        if proc and proc.poll() is None: proc.terminate();proc.wait(timeout=10)
        log.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    asyncio.run(main(p.parse_args().output))
