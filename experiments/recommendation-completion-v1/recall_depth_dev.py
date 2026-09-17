"""Predeclared recall-depth ablation; frozen development data/model, no holdout access."""
import os
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
os.environ.setdefault('OMP_NUM_THREADS','2')
import inspect
import time
from common import *
from ranking import Retriever, FEATURES

OUT = ROOT/'recall-depth-dev-20260917-001'
DEPTHS = [100,200,300,600]

def main():
    assert not OUT.exists(), 'Preserve prior attempts'
    verify_base()
    parent=ROOT/'ranking-dev-001'
    for name,digest in read(parent/'MANIFEST.json')['files'].items(): assert sha(parent/name)==digest,name
    source=inspect.getsource(Retriever)
    source=source.replace('class Retriever:', 'class DepthRetriever:')
    source=source.replace('def features(self,r):','def features(self,r,depth):')
    source=source.replace('old.top_items(cfscore,seen,set(self.ids))','old.top_items(cfscore,seen,set(self.ids),k=depth)')
    source=source.replace('old.top_items(tscore,seen,set(self.ids))','old.top_items(tscore,seen,set(self.ids),k=depth)')
    source=source.replace('rank(neural,self.ids,seen,positive=True)','rank(neural,self.ids,seen,k=depth,positive=True)')
    source=source.replace('candidate=sorted(set(cf)|set(tfidf)|set(content))',
        'union=sorted(set(cf)|set(tfidf)|set(content))\n        candidate=sorted(rrf([cf,content,tfidf],k=600))')
    source=source.replace('rrf([cf,content,tfidf])','rrf([cf,content,tfidf]),union')
    scope=dict(globals());exec(compile(source,'frozen_depth_retriever.py','exec'),scope)
    OUT.mkdir(parents=True)
    (OUT/'frozen_depth_retriever.py').write_text(source,encoding='utf-8')
    tracked=[BASE/n for n in ['public_histories.jsonl','labels.private.jsonl','catalog.jsonl','fit_artifacts.json']]
    tracked += [parent/'model_0.txt',parent/'RESULT.json',parent/'predictions.jsonl',parent/'dev_candidates.jsonl',ROOT/'embeddings/vectors.npy']
    hashes={str(p):sha(p) for p in tracked}
    write(OUT/'PROTOCOL.json',{'status':'PREDECLARED_BEFORE_PREDICTIONS_AND_LABEL_READ',
        'depths_per_route':DEPTHS,'routes':['ItemCF','TF-IDF','frozen MiniLM'],
        'cap':'equal RRF k=60, take up to 600 union items, lexical ID ties',
        'constant':'same dev users, fit history, feature formulas, tree model_0, embeddings, ranking outputs Top100',
        'changed':'route retrieval depth only; cap600 fixed across arms (inactive when union<=600)',
        'feature_note':'reciprocal-rank feature becomes nonzero when an item newly enters a deeper route',
        'selection':'highest dev macro nDCG@10; exact ties prefer smaller depth; no deployment switch',
        'timing':'single-process local CPU, loaded artifacts, excludes init/LLM/HTTP; rotated depth order; no claim of production latency',
        'metrics':['pre-cap/post-cap target coverage cold/warm','oracle nDCG10','tree nDCG10','Recall100','paired user bootstrap','P50/P95'],
        'holdout':'not opened; next model choice needs fresh evaluation or explicitly historical regression',
        'inputs':hashes,'runner_sha256':sha(Path(__file__)),'retriever_sha256':sha(OUT/'frozen_depth_retriever.py')})
    sys.path.insert(0,str(ROOT/'deps'));import lightgbm as lgb
    model=lgb.Booster(model_file=str(parent/'model_0.txt'))
    public=rows(BASE/'public_histories.jsonl');fit=read(BASE/'fit_artifacts.json');catalog=rows(BASE/'catalog.jsonl')
    retriever=scope['DepthRetriever'](fit)
    old_candidates={r['request_id']:r['item_ids'] for r in rows(parent/'dev_candidates.jsonl')}
    old_predictions={(r['request_id'],r['arm']):r['item_ids'] for r in rows(parent/'predictions.jsonl')}
    # Warm each route-depth once; exclude startup from timing.
    for depth in DEPTHS:
        _,x,_,_=retriever.features(public[0],depth);model.predict(x,num_threads=2)
    predictions=[];pools=[];latencies=collections.defaultdict(list)
    for i,r in enumerate(public):
        order=DEPTHS[i%4:]+DEPTHS[:i%4]
        for depth in order:
            start=time.perf_counter();cand,x,rrf_ids,union=retriever.features(r,depth)
            output=rank(model.predict(x,num_threads=2),cand,set())
            latencies[depth].append((time.perf_counter()-start)*1000)
            if depth==100:
                assert cand==old_candidates[r['request_id']],r['request_id']
                assert output==old_predictions[r['request_id'],'lambdamart_0'],r['request_id']
                assert rrf_ids==old_predictions[r['request_id'],'fixed_union_rrf']
            assert set(output)<=set(cand)<=set(union)
            assert not set(union)&set(r['seen_all_fit_item_ids'])
            for name,items in [('tree',output),('rrf',rrf_ids)]:
                predictions.append({'request_id':r['request_id'],'source':SOURCE,'arm':f'{name}_{depth}','item_ids':items})
            pools.append({'request_id':r['request_id'],'depth':depth,'item_ids':cand,'uncapped_item_ids':union})
        if i%100==0:print(f'predicted {i}/{len(public)}',flush=True)
    write_rows(OUT/'predictions.jsonl',predictions);write_rows(OUT/'pools.jsonl',pools)
    write(OUT/'PREDICTED.json',{'prediction_sha256':sha(OUT/'predictions.jsonl'),'pool_sha256':sha(OUT/'pools.jsonl'),
        'baseline_all_992_top100_exact':True,'labels_read_for_prediction':False})
    labels=rows(BASE/'labels.private.jsonl')
    details,result=score(predictions,public,labels,fit,catalog)
    truth={r['request_id']:set(r['target_item_ids']) for r in labels};warm=set(fit['positive_item_user_counts'])
    coverage=collections.defaultdict(list)
    for p in pools:
        t=truth[p['request_id']]
        for bucket,targets in [('all',t),('warm',t&warm),('cold',t-warm)]:
            if not targets: continue
            hit=len(set(p['item_ids'])&targets);pre=len(set(p['uncapped_item_ids'])&targets)
            discount=lambda n:sum(1/math.log2(k+2) for k in range(min(n,10)))
            coverage[p['depth'],bucket].append({'targets':len(targets),'hit':hit,'pre_hit':pre,
                'recall':hit/len(targets),'pre_recall':pre/len(targets),'oracle_ndcg10':discount(hit)/discount(len(targets))})
    result['coverage']={}
    for depth in DEPTHS:
        result['coverage'][str(depth)]={}
        for bucket in ['all','warm','cold']:
            values=coverage[depth,bucket]
            result['coverage'][str(depth)][bucket]={'users':len(values),
                **{k:sum(v[k] for v in values) for k in ['targets','hit','pre_hit']},
                **{k:float(np.mean([v[k] for v in values])) for k in ['recall','pre_recall','oracle_ndcg10']}}
    result['pool_sizes']={str(d):{k:{'min':min(v),'median':float(np.median(v)),'max':max(v)} for k,v in
        [('capped',[len(p['item_ids']) for p in pools if p['depth']==d]),('uncapped',[len(p['uncapped_item_ids']) for p in pools if p['depth']==d])]} for d in DEPTHS}
    result['latency_ms']={str(d):{'p50':float(np.median(v)),'p95':float(np.percentile(v,95))} for d,v in latencies.items()}
    from final_evaluation import paired_bootstrap
    result['comparisons']=[paired_bootstrap(details,f'tree_{d}','tree_100') for d in DEPTHS[1:]]
    result['selected_depth']=max(DEPTHS,key=lambda d:(result['metrics'][f'tree_{d}']['all']['ndcg_at_10'],-d))
    result['status']='DEVELOPMENT_DEPTH_ABLATION_COMPLETE_NO_DEPLOYMENT_CHANGE'
    for p,h in hashes.items():assert sha(Path(p))==h
    expected=read(parent/'RESULT.json')['metrics']['lambdamart_0']['all']['ndcg_at_10']
    assert abs(result['metrics']['tree_100']['all']['ndcg_at_10']-expected)<1e-12
    write_rows(OUT/'per_user.jsonl',details);write(OUT/'RESULT.json',result)
    write(OUT/'VALIDATION.json',{'baseline_992_lists_exact':True,'baseline_metric_exact':True,'inputs_unchanged':True,'no_holdout_access':True})
    seal(OUT)
    print(json.dumps({'selected':result['selected_depth'],'metrics':{k:v['all'] for k,v in result['metrics'].items()},'coverage':result['coverage'],'latency':result['latency_ms']},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
