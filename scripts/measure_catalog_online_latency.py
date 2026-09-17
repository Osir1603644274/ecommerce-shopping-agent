"""New-query timings through the actual front-end proxy; own guest only."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import statistics
import time
import uuid
import httpx
import redis.asyncio as redis

ROOT=Path('D:/agent-datasets/catalog-latency-20260913-v1')
BASE='http://127.0.0.1:5173';API='/api/commerce-demo/workspace'
QUERIES=['找抽屉式透明桌面收纳盒','找可折叠笔记本电脑支架','找木质晾衣夹','找桌面手机支架',
         '找儿童水彩笔','找女士帆布包','找透明鞋盒','找硅胶锅铲','找便携雨伞','找玻璃水杯']


def write(path,value):
    with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


async def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default='online-latency001')
    p.add_argument('--index-version',type=int,choices=[1,2,3],default=1)
    p.add_argument('--pooled-client',action='store_true');a=p.parse_args()
    out=ROOT/a.out;out.mkdir(parents=True,exist_ok=False)
    selected=json.loads((ROOT/('selected.json' if a.index_version==1 else f'selected-v{a.index_version}.json')).read_text('utf-8'))
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from agent.app.catalog_service import workflow_code_binding,settings
    settings.catalog_workspace_fast_enabled=True
    settings.catalog_workspace_fast_index_version=a.index_version
    settings.catalog_workspace_reuse_model_client=a.pooled_client
    settings.catalog_workspace_model_path=selected['modelPath']
    expected_binding=workflow_code_binding()
    write(out/'CONTRACT.json',{'queries':QUERIES,'newDemandEveryTurn':True,'queryResultCache':False,
        'scope':'10 sequential new queries, actual frontend HTTP, model-warmed service; not concurrent production p95',
        'thresholdP95Seconds':10,'modelAndConfiguration':selected,
        'pooledModelClient':a.pooled_client,'expectedWorkflowBinding':expected_binding})
    r=redis.from_url('redis://[::1]:6379/0',decode_responses=True);rows=[]
    try:
        async with httpx.AsyncClient(base_url=BASE,headers={'Origin':BASE,'Sec-Fetch-Site':'same-origin','X-Conversation-Source':'automated_test'},timeout=110) as c:
            response=await c.get(API);response.raise_for_status();c.headers['X-CSRF-Token']=response.json()['csrfToken']
            key='commerce:workspace:merged439-v1:'+hashlib.sha256(c.cookies.get('commerce_browser').encode()).hexdigest()+':guest'
            for i,q in enumerate(QUERIES,1):
                request_id=str(uuid.uuid4());began=time.perf_counter()
                result=await c.post(API+'/run',json={'message':'新需求，'+q,'requestId':request_id,'mode':'continuous'})
                result.raise_for_status();ack=time.perf_counter()-began
                while time.perf_counter()-began<90:
                    value=(await c.get(API)).json();run=value['run']
                    if run['status'] in {'completed','interrupted','failed','clarification'}:break
                    await asyncio.sleep(.15)
                elapsed=time.perf_counter()-began
                raw=json.loads(await r.get(key+':run'))
                assert raw.get('workflow')=='catalog_workspace_v1'
                assert raw['catalogCodeBinding']==expected_binding,'live code or client configuration differs from contract'
                answer=next((m['content'] for m in reversed(value['messages']) if m['requestId']==request_id and m['role']=='assistant'),None)
                scope=raw.get('catalogNext',{}).get('scope') or {}
                for source in scope.get('sources',[]):
                    actual=next(iter(source['timings'][0]['recall']['dense'].values()))
                    expected=selected['parameters'][source['source']]
                    assert actual['nprobe']==expected['nprobe'] and actual['candidate_limit']==expected['candidates'],'live configuration differs from contract'
                    assert source['timings'][0]['ce']['pair_count']==300,'CE candidate count changed'
                    if a.index_version==3:assert source['timings'][0]['recall']['scheduling']=='two_sql_readers_overlap_dense'
                row={'number':i,'input':q,'actualQuery':raw.get('catalogNext',{}).get('query'),'seconds':elapsed,'ackSeconds':ack,
                    'status':run['status'],'cards':len(value['cards']),'answer':answer,
                    'sourceSeconds':{s['source']:s['seconds'] for s in scope.get('sources',[])},
                    'phases':{n['detail']['phase']:n['durationMs']/1000 for n in raw['nodes']},
                    'modelCalls':{k:raw.get(k) for k in ['catalogRouteCall','catalogAnswerCall']}}
                rows.append(row);write(out/f'{i:02d}-run.json',raw);write(out/f'{i:02d}-result.json',row)
                print(json.dumps({k:row[k] for k in ['number','input','seconds','status','sourceSeconds']},ensure_ascii=False),flush=True)
                if run['status']!='completed' or value['cards']:break
        ordered=sorted(x['seconds'] for x in rows);p95=ordered[min(len(ordered)-1,__import__('math').ceil(.95*len(ordered))-1)] if ordered else None
        write(out/'RESULTS.json',{'status':'PASS' if len(rows)==10 and all(x['status']=='completed' for x in rows) and p95<=10 else 'REVIEW_REQUIRED',
            'count':len(rows),'p50Seconds':statistics.median(ordered) if ordered else None,'p95NearestRankSeconds':p95,
            'maxSeconds':max(ordered) if ordered else None,'rows':rows,'caveat':'10 samples only; sequential model-warmed service, not production capacity'})
    finally:await r.aclose()


if __name__=='__main__':asyncio.run(main())
