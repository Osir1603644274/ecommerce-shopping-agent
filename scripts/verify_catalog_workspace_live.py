"""Bounded real HTTP workspace acceptance using a new anonymous owner.

No login, order or payment is created. Redis reads are restricted to the exact
workspace key derived from this client's own newly issued visitor cookie.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid

import httpx
import redis.asyncio as redis

OUT=Path('D:/agent-datasets/catalog-main-integration-20260913-v1/live-attempt001')
BASE='http://127.0.0.1:5173'
PREFIX='/api/commerce-demo/workspace'
CASES=[
    ('S1','找抽屉式透明桌面收纳盒','step','catalog','search'),
    ('S2','只看亚克力材质，其他条件保留','continuous','catalog','refine'),
    ('S3','比较第1项和第2项','continuous','catalog','compare'),
    ('S4','撤销刚才的材质要求','continuous','catalog','undo'),
    ('S5','换个需求，找200元以内的可折叠笔记本电脑支架','continuous','catalog','new'),
    ('S6','新任务，找二手iPhone 13，128GB，预算2000元以内，日常拍照使用','continuous','phone',None),
    ('S7','新任务，找透明桌面收纳盒','continuous','catalog',None),
    ('S8','取消当前普通商品搜索','continuous','catalog','cancel'),
]


def write(name,value):
    with (OUT/name).open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2)


async def main():
    OUT.mkdir(parents=True,exist_ok=False)
    import shutil
    root=Path(__file__).resolve().parents[1]
    names=['agent/app/catalog_worker.py','agent/app/catalog_service.py','agent/app/catalog_conversation.py',
        'agent/app/api/catalog_workspace.py','agent/app/api/commerce_controls.py','agent/app/main.py','agent/app/settings.py']
    source_hashes={}
    for name in names:
        path=root/name;target=OUT/'code'/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target);source_hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    write('CONTRACT.json',{'status':'FROZEN_BEFORE_CALLS','cases':CASES,'scope':'real same-owner workspace HTTP; 8 turns, no relevance benchmark',
        'code':source_hashes,
        'pause':'S2 requests cooperative pause then resumes once from checkpoint',
        'max_turns':8,'deadline_each_seconds':360,'training':False,'purchase':False})
    r=redis.from_url('redis://[::1]:6379/0',decode_responses=True)
    observations=[]
    try:
        async with httpx.AsyncClient(base_url=BASE,timeout=110,headers={'Sec-Fetch-Site':'same-origin','Origin':BASE,
                'X-Conversation-Source':'automated_test'}) as c:
            bootstrap=await c.get(PREFIX);bootstrap.raise_for_status()
            c.headers['X-CSRF-Token']=bootstrap.json()['csrfToken']
            cookie=c.cookies.get('commerce_browser')
            key='commerce:workspace:merged439-v1:'+hashlib.sha256(cookie.encode()).hexdigest()+':guest'
            assert await r.get(key),'client owner key not found'
            for cid,message,mode,expected_route,expected_action in CASES:
                began=time.perf_counter()
                print('Starting '+cid,flush=True)
                response=await c.post(PREFIX+'/run',json={'message':message,'requestId':str(uuid.uuid4()),'mode':mode})
                if response.status_code!=200:
                    write(cid+'-failure.json',{'status':response.status_code,'body':response.text});break
                paused=False;resumed=False;seen=[]
                while time.perf_counter()-began<360:
                    snapshot=await c.get(PREFIX);snapshot.raise_for_status();value=snapshot.json();run=value['run']
                    state=(run['status'],run['revision'],run.get('nextStage'))
                    if not seen or seen[-1]!=list(state):seen.append(list(state))
                    if run['status'] in {'completed','ended','failed','interrupted','clarification'}:
                        break
                    operation=None
                    if mode=='step' and run['status']=='waiting':operation='step'
                    if cid=='S2' and not paused and run['status']=='running':operation='pause';paused=True
                    if cid=='S2' and run['status']=='paused' and not resumed:operation='continue';resumed=True
                    if operation:
                        command=await c.post(PREFIX+'/control/'+operation,json={'runId':run['id'],'revision':run['revision']})
                        if command.status_code==409:
                            if operation=='pause':paused=False
                            if operation=='continue':resumed=False
                        else:command.raise_for_status()
                    await asyncio.sleep(1)
                private_run=json.loads(await r.get(key+':run'))
                private_state=json.loads(await r.get(key))
                actual_route='catalog' if private_run.get('workflow')=='catalog_workspace_v1' else 'phone'
                action=(private_run.get('catalogPlan') or {}).get('action')
                row={'case':cid,'message':message,'seconds':time.perf_counter()-began,'status':run['status'],
                    'route':actual_route,'action':action,'expected_route':expected_route,'expected_action':expected_action,
                    'route_pass':actual_route==expected_route,'action_pass':expected_action is None or action==expected_action,
                    'states':seen,'pause_requested':paused,'resumed':resumed,
                    'answer':next((m['content'] for m in reversed(value['messages']) if m['role']=='assistant' and m['requestId']==run['requestId']),None),
                    'cards_count':len(value['cards']),'scopeId':((private_state.get('catalogSearch') or {}).get('scope') or {}).get('scopeId'),
                    'currentQuery':(private_state.get('catalogSearch') or {}).get('query')}
                write(cid+'-result.json',row)
                write(cid+'-run.json',private_run)
                observations.append(row)
                print(json.dumps({k:row[k] for k in ['case','status','route','action','seconds','route_pass','action_pass']}),flush=True)
                if run['status']!='completed' or not row['route_pass'] or not row['action_pass']:
                    break
            if len(observations)>=4:
                assert observations[2]['scopeId']==observations[1]['scopeId'],'comparison changed scope'
                assert observations[3]['scopeId']==observations[0]['scopeId'],'undo did not restore original scope'
    finally:
        await r.aclose()
        write('RUN-RESULTS.json',{'turns':observations,'complete':len(observations)==len(CASES),
            'semantic_review_required':True,'status':'RECORDED_NOT_AUTOMATIC_QUALITY_PASS'})


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=OUT)
    OUT=parser.parse_args().output.resolve()
    asyncio.run(main())
