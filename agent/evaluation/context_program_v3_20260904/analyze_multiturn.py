"""Frozen-plan descriptive paired-family analysis, never human gold labels."""
import argparse
from collections import defaultdict
import json
import random
import statistics
from .common import HERE, rows, json_new, now, file_sha

def percentile(values,q):
    ordered=sorted(values)
    if not ordered:return None
    pos=(len(ordered)-1)*q;lo=int(pos);hi=min(lo+1,len(ordered)-1)
    return ordered[lo]*(hi-pos)+ordered[hi]*(pos-lo) if hi!=lo else ordered[lo]

def analyze(attempt):
    out=HERE/'p4'/attempt
    result=json.loads((out/'result.json').read_text(encoding='utf-8'))
    data=json.loads((out/'conversations.json').read_text(encoding='utf-8'))
    family={c['conversationId']:c.get('primitiveFamily',c['conversationId']) for c in data}
    outputs=rows(out/'outputs.jsonl');ledger=rows(HERE/'provider_ledger.jsonl')
    ends={r['requestId']:r for r in ledger if r['event']=='END'}
    costs=defaultdict(int);calls=defaultdict(int)
    for r in ledger:
        b=r.get('binding',{})
        if r['event']!='START' or b.get('attempt')!=attempt or r.get('phase')!='P4':continue
        key=(family[b['conversationId']],b['arm'])
        costs[key]+=(ends.get(r['requestId'],{}).get('usage') or {}).get('total_tokens',r['reservedTokens'])
        calls[key]+=1
    arms=('RAW_FULL_CONTROL','CONTEXT_TREATMENT');clusters=sorted(set(family.values()))
    scores={}
    for f in clusters:
        for a in arms:
            selected=[r for r in outputs if family[r['conversationId']]==f and r['arm']==a]
            scores[f,a]=sum(r['status']=='SUCCEEDED' for r in selected)/len(selected)
    rng=random.Random(20260904);diffs=[];ratios=[]
    for _ in range(5000):
        chosen=rng.choices(clusters,k=len(clusters))
        diffs.append(statistics.mean(scores[f,arms[1]]-scores[f,arms[0]] for f in chosen))
        den=sum(costs[f,arms[0]] for f in chosen)
        if den:ratios.append(sum(costs[f,arms[1]] for f in chosen)/den)
    byarm={}
    for a in arms:
        selected=[r for r in outputs if r['arm']==a]
        durations=[r['durationMs'] for r in selected]
        byarm[a]={'armTurns':len(selected),'successes':sum(r['status']=='SUCCEEDED' for r in selected),
            'calls':sum(calls[f,a] for f in clusters),'chargedTokens':sum(costs[f,a] for f in clusters),
            'p50Ms':percentile(durations,.5),'p95Ms':percentile(durations,.95),
            'oracleErrorTurns':sum(bool(r.get('oracleErrors')) for r in selected)}
    a,b=(byarm[x] for x in arms)
    tokenratio=b['chargedTokens']/a['chargedTokens'] if a['chargedTokens'] else None
    p95ratio=b['p95Ms']/a['p95Ms'] if a['p95Ms'] else None
    analysis={'at':now(),'sourceResultSha256':file_sha(out/'result.json'),
        'analysisPlanSha256':file_sha(HERE/'p4/analysis_plan_v1.json'),'families':len(clusters),'byArm':byarm,
        'pairedFamilySuccessDifference':statistics.mean(scores[f,arms[1]]-scores[f,arms[0]] for f in clusters),
        'pairedFamilyBootstrap95':[percentile(diffs,.025),percentile(diffs,.975)],
        'totalTokenRatio':tokenratio,'tokenRatioClusterBootstrap95':[percentile(ratios,.025),percentile(ratios,.975)],
        'p95LatencyRatio':p95ratio,'executionPass':result['status']=='PASS_EXECUTION',
        'costThresholdMet':tokenratio is not None and tokenratio<=.9 and percentile(ratios,.975)<1 and p95ratio<=1.2,
        'formalPopulationNI':'HOLD_SYNTHETIC_FEW_FAMILIES',
        'zeroWorseningFamilyUpper95IfIndependent':(1-.05**(1/len(clusters))) if all(scores[f,arms[1]]>=scores[f,arms[0]] for f in clusters) else None,
        'boundInterpretation':'zero-event family upper bound, not a turn-rate interval; cannot infer real-user NI from these templates',
        'humanAnswerQuality':'NOT_MEASURED','productionDefaultSwitch':False,
        'confound':'end-to-end action and model call counts may differ; see P3 for fixed-input cost'}
    json_new(out/'analysis.json',analysis)
    print({k:v for k,v in analysis.items() if k not in ('byArm','confound')})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('attempt');analyze(p.parse_args().attempt)
