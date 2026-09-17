import asyncio, hashlib, json, sys, time, uuid
from pathlib import Path
import httpx
import redis.asyncio as redis
from run_plans import ROOT, verify, write

BASE='http://127.0.0.1:5173'
PREFIX='/api/commerce-demo/workspace'
async def main():
    verify()
    out=ROOT/'multi-live';out.mkdir(exist_ok=False)
    r=redis.from_url('redis://[::1]:6379/0',decode_responses=True)
    rows=[]
    try:
        for d in json.loads((ROOT/'multi-inputs.json').read_text('utf-8')):
            async with httpx.AsyncClient(base_url=BASE,timeout=110,headers={'Origin':BASE,'Sec-Fetch-Site':'same-origin','X-Conversation-Source':'automated_test'}) as c:
                response=await c.get(PREFIX);response.raise_for_status()
                c.headers['X-CSRF-Token']=response.json()['csrfToken']
                key='commerce:workspace:merged439-v1:'+hashlib.sha256(c.cookies.get('commerce_browser').encode()).hexdigest()+':guest'
                assert await r.get(key)
                for n,t in enumerate(d['turns'],1):
                    cid=f"{d['id']}-T{n}"
                    before=json.loads(await r.get(key))
                    began=time.perf_counter()
                    response=await c.post(PREFIX+'/run',json={'message':t['message'],'requestId':str(uuid.uuid4()),'mode':'continuous'})
                    response.raise_for_status()
                    seen=[]
                    while time.perf_counter()-began<110:
                        response=await c.get(PREFIX);response.raise_for_status();value=response.json();run=value['run']
                        state=[run['status'],run['revision'],run.get('nextStage')]
                        if not seen or seen[-1]!=state:seen.append(state)
                        if run['status'] in {'completed','ended','failed','interrupted','clarification'}:break
                        await asyncio.sleep(.25)
                    raw=json.loads(await r.get(key+':run'))
                    after=json.loads(await r.get(key))
                    current=after.get('catalogSearch') or {}
                    row={'id':cid,'expected':t,'plan':raw.get('catalogPlan'),'query':current.get('query'),
                         'status':run['status'],'seconds':time.perf_counter()-began,'states':seen,
                         'route':'catalog' if raw.get('workflow')=='catalog_workspace_v1' else 'phone',
                         'titles':[{'number':g['number'],'title':g['title']} for g in (current.get('scope') or {}).get('groups',[])],
                         'answer':next((m['content'] for m in reversed(value['messages']) if m['role']=='assistant' and m.get('requestId')==run['requestId']),None)}
                    write(out/(cid+'.json'),{'summary':row,'before':before,'after':after,'rawRun':raw,'publicResponse':value})
                    rows.append(row)
                    print(json.dumps({k:row[k] for k in ['id','status','plan','query','seconds']},ensure_ascii=False),flush=True)
                    if run['status'] not in {'completed','clarification'}:break
    finally:
        await r.aclose()
        write(out/'RESULTS.json',{'turns':rows,'complete':len(rows)==24,'status':'RAW_REQUIRES_REVIEW'})
        verify()
if __name__=='__main__':asyncio.run(main())
