"""Independent bounded assertions, never treating an arbitrary crash as rejection."""
import json
from .common import HERE, file_sha, json_new, now
out=HERE/'p6/process002'
def read(name):return json.loads((out/(name+'.json')).read_text(encoding='utf-8'))
def result(process):return process.get('result') or {}
checks={};details=[]
families=read('protocol')['graphFamilies']
for family in families:
    row=read(family);first=row['first'];restarts=row['restarts'];final=row['finalState']
    expected_effects=0 if family=='inflight_before_effect' else 1
    local={'seedExit':first['returnCode']==(0 if family=='baseline' else row['expectedFaultExit']),
        'expectedRestarts':len(restarts)==(3 if family=='baseline' else 1),
        'restartsExitCleanly':all(p['returnCode']==0 for p in restarts),
        'sameRunAndThread':all(result(p).get('runId')==row['runId'] and result(p).get('threadId')==f"v2-task:{row['identity']['taskId']}:{row['runId']}" for p in restarts),
        'expectedEffectCount':len(row['ledger'])==expected_effects,
        'noModelCalls':not row['modelCalls'] and all(not result(p).get('modelEvents') for p in restarts),
        'distinctLaunches':len({p['pid'] for p in [first,*restarts]})==1+len(restarts)}
    if family in ('inflight_before_effect','effect_before_inbox_complete'):
        local['explicitUnknownReceipt']=(final.get('reactV1OutcomeReceipt',{}).get('outcome',{}).get('errorCode')=='tool_inbox_unknown')
        local['safeUnknownBoundary']=result(restarts[0]).get('boundary') in ('stop_turn','task_completed')
    else:
        local['completed']=all(result(p).get('boundary')=='task_completed' for p in restarts)
    if family=='baseline':
        local['baselineCompleted']=result(first).get('boundary')=='task_completed'
        local['replayRevisionUnchanged']=len({result(p).get('revision') for p in [first,*restarts]})==1
    details.append({'case':family,'checks':local,'passed':all(local.values())})
for index in range(1,13):
    row=read(f'T{index:02d}');local={'notAccepted':row['accepted'] is False}
    if index==11:
        local['knownGoodBeforeCorruption']=row['beforeValid'] is True
        local['invalidAfterCorruption']=row['afterValid'] is False
    else:
        restart=row['restart'];boundary=result(restart).get('boundary')
        recognized=restart['returnCode']==0 and boundary in ('resume_rejected','state_diverged','stop_turn')
        if restart['returnCode']==2 and index in (1,2):
            error=json.loads(restart['stdout'].splitlines()[-1])
            recognized=error.get('workerError')=='GraphV2CheckpointIntegrityError'
        if restart['returnCode']==2 and index in (3,4,5):
            error=json.loads(restart['stdout'].splitlines()[-1])
            recognized=error.get('workerError')=='DurableResumeRejected' and error.get('message')=='checkpoint_identity_mismatch'
        local['explicitRejectionNotArbitraryCrash']=recognized
        local['noAdditionalEffect']=len(row['ledger'])==(1 if index in (9,10,12) else 0)
        seed=row.get('first',row.get('parked',row.get('paused')))
        local['seedReachedExpectedBoundary']=seed['returnCode']==(0 if index in (2,12) else 86 if index in (9,10) else 81)
        if index==2:local['pendingWriteActuallyTampered']=row['tamperedWrites']>0
        if index==12:
            paused=result(row['paused']);receipt=paused.get('pauseReceipt') or {}
            local['effectBelongsToPrePauseWorker']=len(row['ledger'])==1 and row['ledger'][0]['pid']==paused['pid'] and row['ledger'][0]['pid']!=result(restart)['pid']
            local['pausedAfterExecutorBeforeValidator']=receipt.get('pausedBeforeNode')=='validator'
            local['rejectionDoesNotAdvanceRevision']=result(restart)['stateRevision']==paused['stateRevision']
    details.append({'case':row['id'],'target':row['target'],'checks':local,'passed':all(local.values())})
fence=read('fence')
fc={'newFenceGreater':fence['newFence']>fence['oldFence'],
    'newCompleted':fence['newComplete']=='SUCCEEDED',
    'oldFencedOut':fence['oldLateComplete']=='FENCED_OUT',
    'distinctProcesses':fence['oldPid']!=fence['newPid'],
    'competitorClean':fence['competitor']['returnCode']==0}
details.append({'case':'lease_fence','checks':fc,'passed':all(fc.values())})
replay=read('terminal_replay')
rc={'tripleHit':replay['replayHits']==[True]*3,'onePublication':replay['publicationCount']==1,
    'revisionUnchanged':replay['publishedRevision']==replay['finalRevision']}
details.append({'case':'terminal_outbox_replay','checks':rc,'passed':all(rc.values())})
checks={'all21Cases':len(details)==21 and all(r['passed'] for r in details),
    'noUnexpectedModelEvents':read('collection')['modelEventCount']==0,
    'ownedServicesStopped':read('cleanup')['ownedServicesStopped']}
verified={'at':now(),'status':'PASS_BOUNDED_RECOVERY_MATRIX' if all(checks.values()) else 'FAIL',
    'checks':checks,'caseCount':len(details),'passedCases':sum(r['passed'] for r in details),'cases':details,
    'sourceHashes':{p.name:file_sha(p) for p in out.glob('*.json')},
    'auditorSha256':file_sha(__file__),
    'scorerCorrection':'T12 pauses after executor. Its single effect must belong to the pre-pause worker, never the rejected restart. Original score retained.',
    'boundary':'one sample per graph family; simulated effects; terminal outbox checks are in-process storage contracts; not original full repeat matrix or external transaction exactly-once'}
json_new(out/'verification_v2.json',verified);print({k:v for k,v in verified.items() if k not in ('sourceHashes','cases')})
print([r for r in details if not r['passed']])
