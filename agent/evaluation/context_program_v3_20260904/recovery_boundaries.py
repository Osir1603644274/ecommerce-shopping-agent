"""Bounded current-source crash/tamper/fence matrix; no model or shared Redis."""
import json
import os
import subprocess
from unittest.mock import patch
from .common import HERE, file_sha, freeze, check_freeze, json_new, now
from .multiturn import owned_redis

def main():
    out=HERE/'p6/process002';out.mkdir(parents=True,exist_ok=False)
    os.environ['DEEPSEEK_API_KEY']='offline-placeholder-not-a-credential'
    os.environ['OPENAI_API_KEY']='offline-placeholder-not-a-credential'
    created=[];real_popen=subprocess.Popen
    def hidden(command,*args,**kwargs):
        if '--real-model' in command:raise RuntimeError('real_model_forbidden')
        kwargs['creationflags']=kwargs.get('creationflags',0)|subprocess.CREATE_NO_WINDOW
        process=real_popen(command,*args,**kwargs);created.append(process);return process
    with owned_redis(out):
        import redis
        from agent.evaluation.react_v1_durable_checkpoint_v2_20260901_v1 import runner
        port=json.loads((out/'redis_identity.json').read_text())['port']
        sync=redis.Redis(host='127.0.0.1',port=port,decode_responses=True)
        specs=(('baseline','none',None,3),
            ('react_action_commit','after_action_atomic_commit',81,1),
            ('inflight_before_effect','after_in_flight_before_tool_effect',82,1),
            ('effect_before_inbox_complete','after_tool_effect_before_inbox_complete',83,1),
            ('inbox_before_projection','after_inbox_success_before_taskstate_projection',84,1),
            ('projection_before_checkpoint','after_executor_receipt',86,1),
            ('validator_before_checkpoint','after_validator_receipt',85,1))
        json_new(out/'protocol.json',{'at':now(),'graphFamilies':[r[0] for r in specs],
            'repeatsPerFamily':1,'tamperCases':12,'fenceRaces':1,'terminalReplayCases':1,
            'runnerSha256':file_sha(runner.__file__),'actualProviderCalls':0,
            'toolEffects':'simulated read-only results with real durable graph/Redis and distinct processes',
            'notFormalOriginalMatrix':True,'noDefaultSwitch':True,'sharedRedisTouched':False})
        freeze(out/'source_freeze.json')
        try:
            with patch.object(subprocess,'Popen',hidden):
                for family,fault,exitcode,restarts in specs:
                    row=runner._run_graph_bundle(sync,port,family=family,index=1,fault=fault,
                        expected_fault_exit=exitcode,post_restarts=restarts)
                    json_new(out/(family+'.json'),row);print('P6 boundary',family,flush=True)
                for row in runner._run_tamper_matrix(sync,port):
                    json_new(out/(row['id']+'.json'),row);print('P6 tamper',row['id'],flush=True)
                json_new(out/'fence.json',runner._run_fence_race(port,1))
                json_new(out/'terminal_replay.json',runner._run_terminal_replay(port,1))
                model_rows=[]
                for key in sync.scan_iter(match='eval:react-v1-durable:model:*'):
                    model_rows.extend(sync.lrange(key,0,-1))
                json_new(out/'collection.json',{'at':now(),'status':'COLLECTED_PENDING_ASSERTIONS',
                    'modelEventCount':len(model_rows),'modelEvents':model_rows,
                    'workerPids':[p.pid for p in created],'actualProviderCalls':0})
        finally:
            for process in created:
                if process.poll() is None:
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW)
            sync.close()
    check_freeze(out/'source_freeze.json')
    json_new(out/'cleanup.json',{'at':now(),'ownedServicesStopped':True})

if __name__=='__main__':main()
