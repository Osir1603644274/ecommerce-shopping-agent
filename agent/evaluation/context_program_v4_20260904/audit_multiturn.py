"""Recompute state/oracle/identity/hash edges from runner-owned artifacts."""
import argparse
from collections import Counter
import hashlib
import json
from agent.evaluation.context_program_v2_20260904.datasets import check_requirements
from .common import HERE, V3, file_sha, json_new, now, rows, sha

def audit(attempt):
    out=HERE/'p4'/attempt
    outputs=rows(out/'outputs.jsonl');byordinal={r['executionOrdinal']:r for r in outputs}
    conversations=json.loads((out/'conversations.json').read_text(encoding='utf-8'))
    protocol=json.loads((out/'protocol.json').read_text(encoding='utf-8'))
    registered={file_sha(V3/'p1/breadth001/confirm.jsonl'):'canonical_json_string'}
    convention=registered[protocol['datasetSha256']]
    text_hash=lambda text:hashlib.sha256(text.encode('utf-8')).hexdigest() if convention=='raw_utf8' else sha(text)
    turns={(c['conversationId'],t['turnId']):t for c in conversations for t in c['turns']}
    trace_rows=rows(out/'trace_capture.jsonl');traces={r['executionOrdinal']:r for r in trace_rows}
    result=json.loads((out/'result.json').read_text(encoding='utf-8'))
    checks={name:True for name in ('recordHashes','stateIdentities','revisions','stateContinuity','authorityGuideEquality',
        'oracleRecomputed','inputHashes','dialogueHashes','traceBindings','traceHashes','noContextBoundaryMismatches')}
    previous={};statecount=0;violations=[]
    with (out/'private_states.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            statecount+=1;row=json.loads(line);ordinal=row['executionOrdinal'];record=byordinal[ordinal]
            before,after=row['before'],row['after'];turn=turns[record['conversationId'],record['turnId']]
            guide=after['domainState'].get('shoppingGuide');v2=after['domainState'].get('shoppingTaskStateV2',{})
            key=(record['conversationId'],record['arm']);trace=traces.get(ordinal)
            observed={
                'recordHashes':sha(record)==row['recordSha256'],
                'stateIdentities':all(s['taskId']==record['taskId'] and s['sessionId']==record['sessionId'] for s in (before,after)),
                'revisions':before['revision']==record['preStateRevision'] and after['revision']==record['postStateRevision'] and after['revision']>=before['revision'],
                'stateContinuity':key not in previous or previous[key]==sha(before),
                'authorityGuideEquality':v2.get('shoppingGuide')==guide,
                'oracleRecomputed':'expectedHard' not in turn or check_requirements(guide,turn)==record.get('oracleErrors',[]),
                'inputHashes':record['inputMessageSha256']==turn['messageSha256']==text_hash(turn['rawUserText']),
                'dialogueHashes':record['dialogueSha256']==sha(record['dialogue']),
                'traceBindings':trace is not None and all(trace['trace'].get(k)==record[k] for k in ('runId','taskId','sessionId')),
                'traceHashes':trace is not None and trace['traceSha256']==sha(trace['trace']),
                'noContextBoundaryMismatches':trace is not None and not trace['trace'].get('contextBoundaryMismatches'),
            }
            for name,passed in observed.items():
                checks[name] &= passed
                if not passed:violations.append({'ordinal':ordinal,'check':name})
            previous[key]=sha(after)
    actual={arm:dict(Counter(r['status'] for r in outputs if r['arm']==arm)) for arm in ('RAW_FULL_CONTROL','CONTEXT_TREATMENT')}
    checks.update(uniqueOrdinals=len(byordinal)==len(outputs),completeStates=statecount==len(outputs),
        completeTraceRows=len(traces)==len(trace_rows)==len(outputs),reportedCounts=actual==result['summary'],
        completedMatches=len(outputs)==result['completedArmTurns'],
        executionClaimMatches=(result['status']=='PASS_EXECUTION')==(len(outputs)==result['plannedArmTurns'] and all(r['status']=='SUCCEEDED' for r in outputs)),
        identityMismatchCount=sum(r.get('runIdentityMatched') is False for r in outputs)==result['runIdentityMismatches'],
        safeStopCount=sum(bool(r.get('safeStopLikeAnswer')) for r in outputs)==result['safeStops'])
    audit={'at':now(),'status':'PASS_RUNNER_EVIDENCE_EDGES' if all(checks.values()) else 'FAIL',
        'checks':checks,'violations':violations,'armTurns':len(outputs),
        'inputHashConvention':convention,
        'hashConventionBinding':'Selected by exact registered dataset file SHA256; v1 uses raw UTF8, v3 uses canonical JSON string. Neither dataset was changed.',
        'auditorSha256':file_sha(__file__),
        'sourceHashes':{n:file_sha(out/n) for n in ('outputs.jsonl','private_states.jsonl','trace_capture.jsonl','result.json','conversations.json')},
        'boundary':'verifies captured endpoints and identity; not an exhaustive audit of every intermediate write or natural-language truth'}
    json_new(out/'integrity_v2.json',audit);print({k:v for k,v in audit.items() if k!='sourceHashes'})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('attempt');audit(p.parse_args().attempt)
