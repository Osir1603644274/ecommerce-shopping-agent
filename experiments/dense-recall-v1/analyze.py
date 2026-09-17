"""Paired, source-stratified diagnostics with pre-frozen selection rules."""
import collections, json
import numpy as np
import run as r

names=['base']+[f'{a}-{s}' for a in ['random','hard'] for s in r.SEEDS]
records={n:r.rows(r.ROOT/'evaluation'/n/'per-query.jsonl') for n in names}
base=records['base'];ids=[x['query_id'] for x in base]
assert all([x['query_id'] for x in records[n]]==ids for n in names)
groups={s:np.array([i for i,x in enumerate(base) if x['source']==s]) for s in ['kuaisearch','multicpr']}
assert all(len(i)>0 for i in groups.values())
rng=np.random.default_rng(20260915)
draws={s:rng.choice(i,(10000,len(i)),replace=True) for s,i in groups.items()}
def avg(x):return float(np.mean([np.mean(x[i]) for i in groups.values()]))
def paired(delta):
    boot=np.mean([delta[d].mean(axis=1) for d in draws.values()],axis=0)
    return {'equal_source_mean_delta':avg(delta),'bootstrap95':np.quantile(boot,[.025,.975]).tolist(),
            'source_delta':{s:float(delta[i].mean()) for s,i in groups.items()},
            'wins':int((delta>1e-12).sum()),'ties':int((abs(delta)<=1e-12).sum()),'losses':int((delta< -1e-12).sum())}
report={'scope':'33-query development pool diagnostic, no full-corpus claim; selection-set intervals descriptive',
        'source_query_counts':{s:len(i) for s,i in groups.items()},'arms':{},'comparisons':{}}
perquery=[]
for metric in ['recall300','recall100','grade3_recall300','judged_only_ndcg10']:
    values={n:np.array([x[metric] for x in records[n]],dtype=float) for n in names}
    valid=np.isfinite(values['base'])
    assert all(np.array_equal(np.isfinite(v),valid) for v in values.values()),'Metric denominators differ'
    if metric=='recall300':assert valid.all(),'Primary requires explicit zero-positive-query handling'
    groups={s:np.array([i for i,x in enumerate(base) if x['source']==s and valid[i]]) for s in ['kuaisearch','multicpr']}
    assert all(len(i)>0 for i in groups.values())
    rng=np.random.default_rng(20260915)
    draws={s:rng.choice(i,(10000,len(i)),replace=True) for s,i in groups.items()}
    report.setdefault('metric_defined_query_counts',{})[metric]={s:len(i) for s,i in groups.items()}
    armmean={a:np.mean([values[f'{a}-{s}'] for s in r.SEEDS],axis=0) for a in ['random','hard']}
    report['comparisons'][metric]={a+'_vs_base':paired(v-values['base']) for a,v in armmean.items()}
    report['comparisons'][metric]['hard_vs_random']=paired(armmean['hard']-armmean['random'])
    for a in ['random','hard']:
        entry=report['arms'].setdefault(a,{})
        entry[metric]={'seed_values':[avg(values[f'{a}-{s}']) for s in r.SEEDS],
                      'seed_deltas':[avg(values[f'{a}-{s}']-values['base']) for s in r.SEEDS],
                      'mean':avg(armmean[a]),'base':avg(values['base'])}
    if metric=='recall300':
        for i,x in enumerate(base):perquery.append({'query_id':x['query_id'],'query':x['query'],'source':x['source'],
            'base':float(values['base'][i]),**{a:float(v[i]) for a,v in armmean.items()},
            'random_delta':float(armmean['random'][i]-values['base'][i]),'hard_delta':float(armmean['hard'][i]-values['base'][i])})
winner=max(['random','hard'],key=lambda a:report['arms'][a]['recall300']['mean'])
stats=report['comparisons']['recall300'][winner+'_vs_base']
reasons=[]
if stats['equal_source_mean_delta']<=0:reasons.append('No mean primary improvement')
if min(stats['source_delta'].values())< -.02:reasons.append('Source drop exceeds .02')
if min(report['arms'][winner]['recall300']['seed_deltas'])<=0:reasons.append('Not all three seeds improve')
report['gate']={'best_arm':winner,'advance_full_corpus':not reasons,'reasons':reasons,'contract_sha256':r.sha(r.ROOT/'RUN_CONTRACT.json')}
r.save(r.ROOT/'DIAGNOSTIC.json',report);r.jsonl(r.ROOT/'diagnostic-per-query.jsonl',perquery)
print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
