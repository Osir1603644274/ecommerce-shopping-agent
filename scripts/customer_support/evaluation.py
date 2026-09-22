"""Independent report aggregation. Input verdicts must cite external oracle evidence.

Never interpret the assistant's own status/success as an evaluation verdict.
Missing cases, failed requests, absent usage and unreviewed labels stay visible.
"""
from collections import Counter
from decimal import Decimal
import math


def percentile(values, quantile=.95):
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*quantile)-1)] if values else None


def wilson(passed,total):
    if not total:return None
    z=1.959963984540054;p=passed/total;den=1+z*z/total
    center=(p+z*z/(2*total))/den
    margin=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return [max(0,center-margin),min(1,center+margin)]


def usage_summary(calls, complete):
    calls=list(calls);tokens=Counter();unknown_usage=0;unknown_cost=0;cost=Decimal(0)
    for call in calls:
        usage=call.get('usage')
        if not isinstance(usage,dict) or any(type(usage.get(k)) is not int or usage[k]<0 for k in ('prompt_tokens','completion_tokens')):
            unknown_usage+=1
        else:
            tokens.update({k:usage[k] for k in ('prompt_tokens','completion_tokens')})
        if call.get('costStatus')!='LIST_PRICE_ESTIMATE' or call.get('cost') is None:
            unknown_cost+=1
        else:
            value=Decimal(call['cost'])
            if not value.is_finite() or value<0:raise ValueError('invalid estimated cost')
            cost+=value
    return {'modelCalls':len(calls),'knownTokens':dict(tokens),'unknownUsageCalls':unknown_usage,
            'knownEstimatedCostUSD':str(cost),'estimatedTotalCostUSD':None if unknown_cost or not complete else str(cost),
            'unknownCostCalls':unknown_cost,'meteringComplete':complete}


def summarize(cases, observations, *, repetitions=3, required_review_method='HUMAN'):
    if required_review_method not in {'HUMAN','AGENT'}:raise ValueError('explicit review method required')
    by_id={case['id']:case for case in cases}
    if len(by_id)!=len(cases) or not by_id:raise ValueError('unique nonempty case set required')
    seen={};calls={};control_calls={};hard=[]
    for row in observations:
        key=(row['caseId'],row['repetition'])
        if key[0] not in by_id or not 1<=key[1]<=repetitions or key in seen:raise ValueError('unexpected or duplicate observation')
        seen[key]=row
        if row.get('verdict') not in {'PASS','FAIL','UNJUDGED'}:raise ValueError('independent verdict required')
        if row['verdict']=='PASS' and (not row.get('oracleEvidence') or row.get('hardFailures')):
            raise ValueError('pass requires external evidence and no hard failures')
        hard.extend({'caseId':key[0],'repetition':key[1],'failure':failure} for failure in row.get('hardFailures',[]))
        for call in row.get('modelCalls',[]):
            ident=call['modelCallId']
            if ident in calls and calls[ident]!=call:raise ValueError('conflicting duplicate model receipt')
            calls[ident]=call
        for call in row.get('evaluationControlMetering',{}).get('modelCalls',[]):
            ident=call['modelCallId']
            if ident in control_calls and control_calls[ident]!=call:raise ValueError('conflicting control model receipt')
            control_calls[ident]=call
    if calls.keys() & control_calls.keys():raise ValueError('control and customer receipts must be disjoint')
    def group(ids, run=None):
        keys=[(ident,r) for ident in ids for r in (range(1,repetitions+1) if run is None else [run])]
        passed=sum(seen.get(key,{}).get('verdict')=='PASS' for key in keys)
        return {'denominator':len(keys),'passed':passed,'rate':passed/len(keys),
                'wilson95':wilson(passed,len(keys)),
                'missing':sum(key not in seen for key in keys),
                'unjudged':sum(seen.get(key,{}).get('verdict')=='UNJUDGED' for key in keys)}
    categories={cat:group([i for i,c in by_id.items() if c['category']==cat]) for cat in sorted({c['category'] for c in cases})}
    runs={str(r):group(list(by_id),r) for r in range(1,repetitions+1)}
    stable=sum(all(seen.get((i,r),{}).get('verdict')=='PASS' for r in range(1,repetitions+1)) for i in by_id)
    latency={}
    for field in ('firstContentMs','elapsedMs','modelMs','toolMs','userWaitMs','simulatorWaitMs'):
        values=[row[field] for row in observations if type(row.get(field)) in (int,float) and math.isfinite(row[field]) and row[field]>=0]
        latency[field]={'observed':len(values),'missing':len(cases)*repetitions-len(values),'p50':percentile(values,.5),'p95':percentile(values),'max':max(values) if values else None}
    quality=group(list(by_id))
    human=[row for row in observations if row.get('humanReview',{}).get('status')=='REVIEWED']
    assertions=sum(row['humanReview']['assertions'] for row in human)
    supported=sum(row['humanReview']['supported'] for row in human)
    if any(type(r['humanReview'].get(k)) is not int or r['humanReview'][k]<0 for r in human for k in ('assertions','supported')) or supported>assertions:
        raise ValueError('invalid human review counts')
    human_complete=len(human)==len(cases)*repetitions and assertions>0
    metering_complete=len(observations)==len(cases)*repetitions and all(r.get('meteringComplete') is True for r in observations)
    control_complete=all(r.get('evaluationControlMetering',{}).get('meteringComplete') is True for r in observations if r.get('evaluationControlMetering'))
    agent=[r['agentReview'] for r in observations if r.get('agentReview',{}).get('status')=='REVIEWED']
    for review in agent:
        if review.get('reviewerType')!='AGENT' or any(type(review.get(k)) is not int or review[k]<0 for k in ('assertions','supported','unknown')) or review['supported']+review['unknown']>review['assertions']:
            raise ValueError('invalid agent review counts')
    agent_assertions=sum(r['assertions'] for r in agent);agent_supported=sum(r['supported'] for r in agent)
    agent_complete=len(agent)==len(cases)*repetitions and agent_assertions>0
    critical_total=sum(r.get('criticalAssertions',{}).get('total',0) for r in observations)
    critical_passed=sum(r.get('criticalAssertions',{}).get('passed',0) for r in observations)
    critical_complete=all(r.get('criticalAssertions',{}).get('complete') is True for r in observations) and not quality['missing']
    # Legacy criticalAssertions aggregates routing, completeness and invariants.
    # It must not stand in for an independent judgment of asserted facts.
    fact_reviews=[r.get('independentFactReview',{}) for r in observations]
    facts_complete=bool(fact_reviews) and not quality['missing'] and all(
        r.get('status')=='REVIEWED' and r.get('reviewerType') in {'AGENT','HUMAN'}
        and r.get('sourceReviewSha256') and r.get('complete') is True for r in fact_reviews)
    fact_total=sum(r.get('criticalTotal',0) for r in fact_reviews)
    fact_supported=sum(r.get('criticalSupported',0) for r in fact_reviews)
    for r in fact_reviews:
        if any(type(r.get(k,0)) is not int or r.get(k,0)<0 for k in ('criticalTotal','criticalSupported')) or r.get('criticalSupported',0)>r.get('criticalTotal',0):
            raise ValueError('invalid independent critical fact counts')
    task_latency={}
    for kind,limit in [('query',30000),('preview',45000)]:
        expected=[key for key in by_id if by_id[key].get('taskKind')==kind]
        measured=[seen[(i,r)]['elapsedMs'] for i in expected for r in range(1,repetitions+1) if type(seen.get((i,r),{}).get('elapsedMs')) in (int,float)]
        task_latency[kind]={'denominator':len(expected)*repetitions,'observed':len(measured),'p95':percentile(measured),'withinBudget':bool(expected) and len(measured)==len(expected)*repetitions and percentile(measured)<=limit}
    family_ids={c.get('familyId',c['id']) for c in cases}
    family_runs={}
    for r in range(1,repetitions+1):
        passed_families=sum(all(seen.get((c['id'],r),{}).get('verdict')=='PASS' for c in cases if c.get('familyId',c['id'])==family) for family in family_ids)
        family_runs[str(r)]={'passed':passed_families,'denominator':len(family_ids),'rate':passed_families/len(family_ids),'wilson95':wilson(passed_families,len(family_ids))}
    formal_size=len(cases)==240 and len(categories)==10 and all(Counter(c['split'] for c in cases if c['category']==cat)=={'dev':12,'heldout':12} for cat in categories)
    report={'scope':'Independent externally evidenced verdicts; generated labels are not human gold',
            'quality':quality,'perRun':runs,'perCategory':categories,'perRunFamily':family_runs,
            'uncertaintyNote':'Wilson intervals describe this synthetic suite; variants and repeated runs are correlated, not independent population samples. Family results require every variant to pass.',
            'threeRunStable':{'passed':stable,'denominator':len(cases),'rate':stable/len(cases)} if repetitions==3 else None,
            'allPlannedRunsStable':{'repetitions':repetitions,'passed':stable,'denominator':len(cases),'rate':stable/len(cases)},
            'hardFailures':hard,'latency':latency,'taskLatency':task_latency,
            'usage':usage_summary(calls.values(),metering_complete),
            'evaluationControlUsage':usage_summary(control_calls.values(),control_complete),
            'allExecutedModelUsage':usage_summary([*calls.values(),*control_calls.values()],metering_complete and control_complete),
            'agentGrounding':{'reviewedRuns':len(agent),'assertions':agent_assertions,'supported':agent_supported,
                'unknown':sum(r['unknown'] for r in agent),'rate':agent_supported/agent_assertions if agent_assertions else None,
                'complete':agent_complete,'reviewerType':'AGENT','notHumanGold':True},
            'humanGrounding':{'reviewedRuns':len(human),'assertions':assertions,'supported':supported,'rate':supported/assertions if assertions else None,'complete':human_complete},
            'oracleChecks':{'total':critical_total,'passed':critical_passed,'complete':critical_complete,
                'scope':'Legacy criticalAssertions: mixed task coverage, routing, facts and invariants; not factual accuracy'},
            'criticalFacts':{'total':fact_total,'supported':fact_supported,'complete':facts_complete,
                'rate':fact_supported/fact_total if fact_total else None,'source':'independentFactReview; UNKNOWN remains unsupported'},
            'gates':{'formalDatasetSize':formal_size,'allObserved':quality['missing']==0,'allJudged':quality['unjudged']==0,
                     'overall90':quality['rate']>=.9,'eachCategory80':all(c['rate']>=.8 for c in categories.values()),
                     'noHardFailure':not hard,'firstContentP95Within5s':latency['firstContentMs']['missing']==0 and latency['firstContentMs']['p95']<=5000,
                     'criticalFacts100':facts_complete and fact_total>0 and fact_supported==fact_total,
                     'queryP95Within30s':task_latency['query']['withinBudget'],'previewP95Within45s':task_latency['preview']['withinBudget'],
                     'humanSupport95':human_complete and supported/assertions>=.95},
            'agentReviewGates':{'agentSupport95':agent_complete and agent_supported/agent_assertions>=.95},
            'failures':[{'caseId':i,'repetition':r,'verdict':seen.get((i,r),{}).get('verdict','MISSING'),'reasons':seen.get((i,r),{}).get('reasons',[])} for i in by_id for r in range(1,repetitions+1) if seen.get((i,r),{}).get('verdict')!='PASS']}
    report['requiredReviewMethod']=required_review_method
    report['acceptanceGates']={k:v for k,v in report['gates'].items() if k!='humanSupport95'}
    report['acceptanceGates'].update(threeRepetitions=repetitions==3,
        independentSupport95=report['agentReviewGates']['agentSupport95'] if required_review_method=='AGENT' else report['gates']['humanSupport95'])
    return report
