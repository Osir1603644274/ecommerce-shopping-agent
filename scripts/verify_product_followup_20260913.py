"""Real workspace replay, new anonymous owner, no order/payment writes."""
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
    ('phone','推荐一部 2000 元左右的二手手机'),
    ('ambiguous','第一个苹果手机 有它的充电器吗？'),
    ('included','我是问是否附赠充电器'),
    ('accessory','那就另买一个，帮我找适配的'),
]


async def main(out):
    out.mkdir(parents=True,exist_ok=False)
    def save(name,value):
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    save('CONTRACT.json',{'cases':CASES,'scope':'bounded product followup replay; not generalization benchmark',
                        'orders':False,'payments':False})
    r=redis.from_url('redis://[::1]:6379/0',decode_responses=True)
    results=[]
    try:
        async with httpx.AsyncClient(base_url=BASE,timeout=45,headers={'Origin':BASE,'Sec-Fetch-Site':'same-origin',
                'X-Conversation-Source':'automated_test'}) as c:
            response=await c.get(PREFIX);response.raise_for_status()
            c.headers['X-CSRF-Token']=response.json()['csrfToken']
            cookie=c.cookies.get('commerce_browser')
            key='commerce:workspace:merged439-v1:'+hashlib.sha256(cookie.encode()).hexdigest()+':guest'
            original=None
            for cid,message in CASES:
                rid=str(uuid.uuid4());began=time.perf_counter()
                response=await c.post(PREFIX+'/run',json={'message':message,'requestId':rid,'mode':'continuous'})
                response.raise_for_status()
                while time.perf_counter()-began<180:
                    response=await c.get(PREFIX);response.raise_for_status();value=response.json()
                    if value['run']['status'] in {'completed','failed','interrupted','clarification','ended'}:break
                    await asyncio.sleep(1)
                private=json.loads(await r.get(key));run=json.loads(await r.get(key+':run'))
                answer=next((m for m in value['messages'] if m['requestId']==rid and m['role']=='assistant'),{})
                row={'case':cid,'status':run['status'],'seconds':round(time.perf_counter()-began,2),
                     'plan':run.get('catalogPlan'),'answer':answer.get('content'),'answerCardCount':len(answer.get('cards',[])),
                     'currentCardIds':[p['id'] for p in private.get('cards',[])],
                     'scope':(private.get('catalogSearch') or {}).get('scope')}
                save(cid+'.json',row);save(cid+'-run.json',run)
                results.append(row)
                print(json.dumps({k:v for k,v in row.items() if k!='scope'},ensure_ascii=False),flush=True)
                assert run['status']=='completed',run.get('catalogError')
                if cid=='phone':
                    original=private
                    assert original['cards'] and 'iphone11' in original['cards'][0]['title'].lower()
                elif cid in {'ambiguous','included'}:
                    assert private['cards']==original['cards'] and private['reference']==original['reference']
                    assert not answer.get('cards') and run['catalogPlan']['route']=='product'
                    assert run['catalogPlan']['productContext']['productId']==str(original['cards'][0]['id'])
                    assert not any(n.get('label')=='search_products' for n in run['nodes'])
                    if cid=='ambiguous':assert '随附' in answer['content'] and '另外找' in answer['content']
                    else:assert '暂时不能确认' in answer['content'] or '商品记录写明' in answer['content']
                else:
                    plan=run['catalogPlan']
                    assert plan['route']=='catalog' and plan['action']=='new'
                    assert 'iphone11' in plan['retrievalQuery'].replace(' ','').lower() and '充电器' in plan['retrievalQuery']
                    assert not any(q['facet']=='预算' for q in plan['requirements'])
                    assert not private['cards'] and private['reference'] is None
                    assert row['scope']['groups']
                    assert all('充电' in g['title'] for g in row['scope']['groups'])
    finally:
        await r.aclose()
        save('RESULTS.json',{'completedCases':len(results),'results':[{k:v for k,v in row.items() if k!='scope'} for row in results]})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    asyncio.run(main(parser.parse_args().output))
