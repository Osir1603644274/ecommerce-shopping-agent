"""Two real processes race on one expected TaskState revision in owned Redis."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from .common import HERE, ROOT, check_freeze, freeze, json_new, now
from .multiturn import owned_redis

async def worker(args):
    import redis.asyncio as redis
    from agent.app import task_state
    from agent.app.task_state import TaskStatePatchRequest
    client=redis.Redis(host='127.0.0.1',port=args.port,decode_responses=True)
    task_state._client=client
    try:
        await client.rpush('ctxv3:race:ready',str(os.getpid()))
        for _ in range(400):
            if await client.get('ctxv3:race:go'):break
            await asyncio.sleep(.05)
        else:raise TimeoutError('barrier_timeout')
        try:
            state=await task_state.update_task_state(args.task,
                TaskStatePatchRequest(expectedRevision=args.revision,actor='agent',goal=args.goal))
            return {'pid':os.getpid(),'status':'WON','revision':state.revision,'goal':state.goal,'at':now()}
        except task_state.TaskStateRevisionConflictError as exc:
            return {'pid':os.getpid(),'status':'REVISION_CONFLICT','errorType':type(exc).__name__,'at':now()}
    finally:await client.aclose();task_state._client=None

def main():
    import redis
    out=HERE/'p6/revisionrace001';out.mkdir(parents=True,exist_ok=False)
    freeze(out/'source_freeze.json')
    with owned_redis(out):
        port=json.loads((out/'redis_identity.json').read_text())['port']
        async def seed_or_read(task_id=None):
            import redis.asyncio as aredis
            from agent.app import task_state
            client=aredis.Redis(host='127.0.0.1',port=port,decode_responses=True);task_state._client=client
            try:
                state=await task_state.get_task_state(task_id) if task_id else await task_state.create_task_state(
                    task_state.TaskStateCreateRequest(goal='revision race fixture',taskType='general',sessionId='ctxv3-revision-race'))
                return state.model_dump(by_alias=True,mode='json')
            finally:await client.aclose();task_state._client=None
        before=asyncio.run(seed_or_read());sync=redis.Redis(host='127.0.0.1',port=port,decode_responses=True)
        processes=[];rows=[]
        try:
            for goal in ('worker-left','worker-right'):
                cmd=[sys.executable,'-m','agent.evaluation.context_program_v3_20260904.revision_race',
                    '--worker','--port',str(port),'--task',before['taskId'],'--revision',str(before['revision']),'--goal',goal]
                processes.append(subprocess.Popen(cmd,cwd=ROOT,text=True,encoding='utf-8',
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW))
            for _ in range(400):
                if sync.llen('ctxv3:race:ready')==2:break
                time.sleep(.05)
            else:raise TimeoutError('workers_not_ready')
            ready_pids=sync.lrange('ctxv3:race:ready',0,-1);sync.set('ctxv3:race:go','1')
            for process in processes:
                stdout,stderr=process.communicate(timeout=30)
                rows.append({'pid':process.pid,'exitCode':process.returncode,'stdout':stdout,'stderr':stderr,
                    'result':json.loads(stdout.splitlines()[-1]) if process.returncode==0 else None})
            after=asyncio.run(seed_or_read(before['taskId']))
            wins=[r['result'] for r in rows if (r['result'] or {}).get('status')=='WON']
            losses=[r['result'] for r in rows if (r['result'] or {}).get('status')=='REVISION_CONFLICT']
            checks={'twoReadyProcesses':len(set(ready_pids))==2,
                'oneWinnerOneExplicitRevisionConflict':len(wins)==len(losses)==1,
                'workersExitCleanly':all(r['exitCode']==0 for r in rows),
                'oneRevisionIncrement':after['revision']==before['revision']+1,
                'onlyWinnerGoalStored':len(wins)==1 and after['goal']==wins[0]['goal'],
                'taskSessionPreserved':all(after[k]==before[k] for k in ('taskId','sessionId'))}
            json_new(out/'observations.json',{'at':now(),'before':before,'after':after,'workers':rows,
                'checks':checks,'passed':all(checks.values()),'modelCalls':0,'businessToolCalls':0,
                'scope':'actual concurrent processes and atomic TaskState optimistic concurrency; test-owned Redis only'})
            print(checks)
        finally:
            for process in processes:
                if process.poll() is None:
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            sync.close()
    check_freeze(out/'source_freeze.json');json_new(out/'cleanup.json',{'at':now(),'ownedServicesStopped':True})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--worker',action='store_true');p.add_argument('--port',type=int)
    p.add_argument('--task');p.add_argument('--revision',type=int);p.add_argument('--goal');args=p.parse_args()
    if args.worker:print(json.dumps(asyncio.run(worker(args))))
    else:main()
