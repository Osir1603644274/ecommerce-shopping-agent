"""Bounded real user-language replay on a fresh anonymous workspace."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid

import httpx
import redis.asyncio as redis

BASE='http://127.0.0.1:5173'
PREFIX='/api/commerce-demo/workspace'
CASES=[
 ('chocolate','巧克力面包'),
 ('change','算了 我怕吃巧克力晚上失眠；我想减肥 有没有减脂类型的面包？'),
 ('taste','哦哦 那我想减肥 还有其他好吃又不变胖的吗 全麦面包太难吃了'),
 ('broaden','不限定面包呀 只要能减脂口感也好 就可以了捏'),
 ('exclude','不限定面包了；我不要面包'),
 ('compare','比较第1项和第2项'),
 ('undo','撤销刚才不要面包的要求'),
]


async def main(out):
 out.mkdir(parents=True,exist_ok=False)
 def save(name,value):
  (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
 save('CONTRACT.json',dict(cases=CASES,scope='exposed failure replay, not a held-out benchmark',
  assertions=['complete','stable record identity','broaden differs from exclude','compare retains candidates','undo restores requirements']))
 r=redis.from_url('redis://localhost:6379/0',decode_responses=True)
 rows=[]; snapshots={}
 try:
  async with httpx.AsyncClient(base_url=BASE,timeout=60,headers={'Origin':BASE,'Sec-Fetch-Site':'same-origin',
       'X-Conversation-Source':'automated_test'}) as client:
   response=await client.get(PREFIX);response.raise_for_status()
   client.headers['X-CSRF-Token']=response.json()['csrfToken']
   key='commerce:workspace:merged439-v1:'+hashlib.sha256(client.cookies.get('commerce_browser').encode()).hexdigest()+':guest'
   for name,message in CASES:
    started=time.perf_counter();rid=str(uuid.uuid4())
    response=await client.post(PREFIX+'/run',json=dict(message=message,requestId=rid,mode='continuous'))
    response.raise_for_status()
    while time.perf_counter()-started<180:
     response=await client.get(PREFIX);response.raise_for_status();value=response.json()
     if value['run']['status'] in {'completed','failed','interrupted','clarification','ended'}:break
     await asyncio.sleep(.5)
    private=json.loads(await r.get(key));run=json.loads(await r.get(key+':run'))
    state=private.get('catalogSearch') or {};scope=state.get('scope') or {};groups=scope.get('groups',[])
    answer=next((m.get('content') for m in value['messages'] if m['requestId']==rid and m['role']=='assistant'),None)
    row=dict(case=name,seconds=round(time.perf_counter()-started,2),requestId=rid,status=run['status'],
      plan=run.get('catalogPlan'),answer=answer,query=state.get('query'),requirements=state.get('requirements'),
      groups=[{'number':g['number'],'title':g['title'],'ids':[m['docid'] for m in g['members']]} for g in groups],
      rejected=scope.get('semanticExcludedGroups',[]),error=run.get('catalogError'))
    save(name+'.json',row);save(name+'-run.json',run);rows.append(row);snapshots[name]=state
    print(json.dumps(row,ensure_ascii=False),flush=True)
    assert run['status']=='completed',run.get('catalogError')
    assert all(len(g['members'])==1 for g in groups)
    assert len({g['members'][0]['docid'] for g in groups})==len(groups)
    if name=='taste':
     assert any(q['mode']=='avoid' and '全麦' in q['value'] for q in state['requirements'])
     assert not any(q['mode']=='exclude' and '全麦' in q['value'] for q in state['requirements'])
     assert any(q['mode']=='prefer' and q['facet']=='口感' for q in state['requirements'])
     assert '不要全麦' not in state['query'] and '全麦除外' not in state['query']
    if name=='broaden':
     assert not any(q['facet']=='商品' and q['value']=='面包' for q in state['requirements'])
     assert '面包' not in state['retrievalQuery']
     assert any(q['mode']=='avoid' and '全麦' in q['value'] for q in state['requirements'])
    if name=='exclude':
     assert any(q['mode']=='exclude' and q['value']=='面包' for q in state['requirements'])
    if name=='compare':assert scope==snapshots['exclude']['scope']
    if name=='undo':
     assert state['requirements']==snapshots['broaden']['requirements']
     assert not any(q['mode']=='exclude' and q['value']=='面包' for q in state['requirements'])
 finally:
  await r.aclose();save('RESULTS.json',dict(completedCases=len(rows),results=rows))


if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
 asyncio.run(main(parser.parse_args().output))
