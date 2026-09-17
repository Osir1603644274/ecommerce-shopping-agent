"""Real owner-scoped checkpoint recovery and reversible launcher flag test.

Uses the task-owned local launcher. Run only after other live acceptance calls
have finished. The launcher checks process identity before each restart.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid
import httpx
import redis.asyncio as redis

ROOT=Path(__file__).resolve().parents[1]
OUT=Path('D:/agent-datasets/catalog-main-integration-20260913-v1/restart-live002')
BASE='http://127.0.0.1:5173'
API='/api/commerce-demo/workspace'


def write(name,value):
    with (OUT/name).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


async def restart(disabled=False):
    command=[shutil.which('pwsh'),'-NoProfile','-File',str(ROOT/'scripts/merged-commerce.ps1'),'-Action','restart-agent']
    if disabled:command.append('-DisableCatalogSearch')
    print('Restarting catalog '+('disabled' if disabled else 'enabled'),flush=True)
    log=OUT/('launcher-'+('disabled' if disabled else 'enabled')+'.log')
    # Windows descendants can inherit a pipe writer and keep communicate()
    # waiting after PowerShell itself has exited. A file plus process.wait()
    # observes launcher completion without depending on descendant pipe EOF.
    with log.open('xb') as stream:
        process=await asyncio.create_subprocess_exec(*command,cwd=ROOT,stdout=stream,stderr=asyncio.subprocess.STDOUT)
        await asyncio.wait_for(process.wait(),180)
    write('restart-'+('disabled' if disabled else 'enabled')+'.json',{'returncode':process.returncode,'log':log.name})
    if process.returncode:raise RuntimeError('launcher_restart_failed')


async def owner(c,r):
    response=await c.get(API);response.raise_for_status()
    c.headers['X-CSRF-Token']=response.json()['csrfToken']
    key='commerce:workspace:merged439-v1:'+hashlib.sha256(c.cookies.get('commerce_browser').encode()).hexdigest()+':guest'
    assert await r.get(key)
    return key


async def main():
    OUT.mkdir(parents=True,exist_ok=False)
    write('CONTRACT.json',{'query':'找透明桌面收纳盒','pauseAfterPhase':'retrieve',
        'checks':['rollback bypasses public workflow','same owner recovers same scope after restart',
                  'completed retrieval not repeated','no public purchase cards'],
        'sourceSha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
            ['agent/app/catalog_conversation.py','agent/app/api/catalog_workspace.py','scripts/merged-commerce.ps1']}})
    r=redis.from_url('redis://[::1]:6379/0',decode_responses=True)
    headers={'Sec-Fetch-Site':'same-origin','Origin':BASE,'X-Conversation-Source':'automated_test'}
    restored=False;disabled=False
    try:
        async with httpx.AsyncClient(base_url=BASE,headers=headers,timeout=110) as c:
            key=await owner(c,r)
            private=OUT/'private';private.mkdir()
            (private/'owner.json').write_text(json.dumps({'cookie':c.cookies.get('commerce_browser'),'key':key}),encoding='utf-8')
            response=await c.post(API+'/run',json={'message':'找透明桌面收纳盒','requestId':str(uuid.uuid4()),'mode':'step'})
            response.raise_for_status()
            for phase in range(2):
                run=(await c.get(API)).json()['run']
                response=await c.post(API+'/control/step',json={'runId':run['id'],'revision':run['revision']})
                response.raise_for_status()
                began=time.monotonic()
                while time.monotonic()-began<300:
                    value=(await c.get(API)).json();run=value['run']
                    if run['status']=='waiting':break
                    if run['status']=='interrupted':raise RuntimeError('checkpoint_interrupted')
                    await asyncio.sleep(1)
                else:raise TimeoutError('checkpoint_deadline')
            before=json.loads(await r.get(key+':run'))
            assert before['catalogPhase']==2
            write('before-restart.json',before)
            print('Retrieval checkpoint saved',flush=True)
            disabled=True
            await restart(True)
            async with httpx.AsyncClient(base_url=BASE,headers=headers,timeout=110) as other:
                otherkey=await owner(other,r)
                response=await other.post(API+'/run',json={'message':'找透明收纳盒','requestId':str(uuid.uuid4()),'mode':'step'})
                response.raise_for_status()
                probe=json.loads(await r.get(otherkey+':run'))
                assert probe.get('workflow')!='catalog_workspace_v1'
                write('rollback-probe.json',{'workflow':probe.get('workflow'),'status':probe['status'],
                    'catalogPlan':probe.get('catalogPlan'),'public_branch_bypassed':True})
                public=(await other.get(API)).json()['run']
                response=await other.post(API+'/control/end',json={'runId':public['id'],'revision':public['revision']})
                response.raise_for_status()
            await restart(False);restored=True
            value=(await c.get(API)).json();run=value['run']
            response=await c.post(API+'/control/continue',json={'runId':run['id'],'revision':run['revision']})
            response.raise_for_status()
            began=time.monotonic()
            while time.monotonic()-began<60:
                value=(await c.get(API)).json();run=value['run']
                if run['status'] in {'completed','interrupted'}:break
                if run['status']=='waiting':
                    # Continue preserves the user's single-step mode. Advance
                    # the remaining publication checkpoint explicitly.
                    step=await c.post(API+'/control/step',json={'runId':run['id'],'revision':run['revision']})
                    if step.status_code!=409:step.raise_for_status()
                await asyncio.sleep(1)
            after=json.loads(await r.get(key+':run'))
            write('after-restart.json',after)
            assert run['status']=='completed'
            assert after['catalogEvidenceSha256']==before['catalogEvidenceSha256']
            assert after['catalogNext']['scope']==before['catalogNext']['scope']
            assert len([n for n in after['nodes'] if n.get('detail',{}).get('phase')=='retrieve'])==1
            assert value['cards']==[]
            write('RESULTS.json',{'status':'PASS','rollback':True,'recoveredSameScope':True,
                'retrievalPhaseCount':1,'resumeSeconds':time.monotonic()-began,'cards_count':0,
                'answer':value['messages'][-1]['content']})
            print('PASS: rollback and same-scope restart recovery',flush=True)
    finally:
        # A failed probe must not leave the authorized feature disabled.
        if disabled and not restored:
            await restart(False)
        await r.aclose()


if __name__=='__main__':asyncio.run(main())
