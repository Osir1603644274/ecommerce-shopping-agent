"""Freeze selection, predict later-period users without labels, then score once."""
import os
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
os.environ.setdefault('OMP_NUM_THREADS','2')
import argparse, time
from common import *

OUT=ROOT/'final-001'

def freeze():
    if OUT.exists():raise RuntimeError('Final attempt already exists')
    for seed in [17,29,43]:assert (ROOT/f'sequence-dev-001/seed-{seed}/RESULT.json').exists()
    OUT.mkdir(parents=True)
    files={str(p):sha(p) for p in [BASE/'MANIFEST.json',ROOT/'ranking-dev-001/model_0.txt',ROOT/'embeddings/ENCODED.json']}
    for seed in [17,29,43]:
        for arch in ['mean','causal']:files[str(ROOT/f'sequence-dev-001/seed-{seed}/{arch}.pt')]=sha(ROOT/f'sequence-dev-001/seed-{seed}/{arch}.pt')
    write(OUT/'SELECTION.json',{'status':'FROZEN_BEFORE_FINAL_PREDICTIONS','primary_candidate':'lambdamart_0',
        'primary_control':'fixed_union_rrf','secondary_control':'equal_rrf',
        'choice_reason':'best of three fixed-candidate dev tree configs; includes content cold-item candidates; sequence seed comparisons retained, no best-seed picking',
        'fit_policy':'no retraining on dev, original fit model/history held fixed',
        'cohort':'each user with fit positive history: earliest eligible final day, all same-day positive ASINs absent from all fit reviews',
        'limits':'new since fit, not necessarily unseen during intermediate dev; static metadata and prefiltered raw sample, not causal production replay',
        'primary_metric':'macro user nDCG10','secondary':'macro Recall100','bootstrap':{'replicates':10000,'seed':20260916,'unit':'user','paired':True,'CI':'percentile 95%'},
        'final_access_history':'raw rows previously structurally cleaned and counted; no prior predictions or scores',
        'files':files,'code':{p.name:sha(p) for p in Path(__file__).parent.glob('*.py')},'post_final_tuning':False})

def prepare():
    assert (OUT/'SELECTION.json').exists()
    events,_,_=old.clean_reviews(Path('D:/agent-datasets/recommendation-unified-v1/source-probe/Luxury_Beauty_5.json.gz'))
    threshold=read(BASE/'PROTOCOL.json')['thresholds']
    eligible=[e for e in events if e['timestamp']<=threshold['fit_last_inclusive'] or e['timestamp']>threshold['dev_last_inclusive']]
    public,labels,_,audit=old.dev_requests(eligible,{'fit_last_inclusive':threshold['fit_last_inclusive'],'dev_last_inclusive':max(e['timestamp'] for e in events)})
    for r in public+labels:r['request_id']=r['request_id'].replace('amazon-dev-','amazon-final-')
    assert all(r['cutoff_timestamp']>threshold['dev_last_inclusive'] for r in public)
    write_rows(OUT/'public_histories.jsonl',public);write_rows(OUT/'labels.private.jsonl',labels)
    write(OUT/'PREPARED.json',{'users':len(public),'targets':sum(len(l['target_item_ids']) for l in labels),
         'public_sha256':sha(OUT/'public_histories.jsonl'),'labels_sha256':sha(OUT/'labels.private.jsonl'),'cohort_audit':audit})

def predict():
    from ranking import Retriever
    import torch
    from sequence import BasketModel,predict_model,baskets
    sys.path.insert(0,str(ROOT/'deps'));import lightgbm as lgb
    if (OUT/'PREDICTED.json').exists():raise RuntimeError('Final predictions already frozen')
    selection=read(OUT/'SELECTION.json')
    for path,digest in selection['files'].items():assert sha(path)==digest
    public=rows(OUT/'public_histories.jsonl');fit=read(BASE/'fit_artifacts.json');catalog=rows(BASE/'catalog.jsonl');catalog_ids={p['item_id'] for p in catalog}
    assert sha(OUT/'public_histories.jsonl')==read(OUT/'PREPARED.json')['public_sha256']
    # The predictor below receives public histories and frozen model artifacts only.
    predictions,_=old.predict(catalog,fit,public)
    by=collections.defaultdict(dict)
    for p in predictions:by[p['request_id']][p['arm']]=p['item_ids']
    for r in public:predictions.append({'request_id':r['request_id'],'source':SOURCE,'arm':'equal_rrf','item_ids':rrf([by[r['request_id']]['itemcf'],by[r['request_id']]['content_tfidf']])})
    retriever=Retriever(fit);model=lgb.Booster(model_file=str(ROOT/'ranking-dev-001/model_0.txt'));candidate_rows=[];latencies=[]
    for n,r in enumerate(public):
        start=time.perf_counter();cand,x,baseline=retriever.features(r);values=model.predict(x,num_threads=2)
        predictions.extend([{'request_id':r['request_id'],'source':SOURCE,'arm':'fixed_union_rrf','item_ids':baseline},
                            {'request_id':r['request_id'],'source':SOURCE,'arm':'lambdamart_0','item_ids':rank(values,cand,set())}])
        candidate_rows.append({'request_id':r['request_id'],'item_ids':cand});latencies.append(time.perf_counter()-start)
        if n%250==0:print(f'final fixed-candidate predictions {n}/{len(public)}',flush=True)
    del retriever,model
    torch.set_num_threads(2)
    for seed in [17,29,43]:
        folder=ROOT/f'sequence-dev-001/seed-{seed}';items=read(folder/'item_ids.json');index={i:n+1 for n,i in enumerate(items)}
        supported=[r for r in public if baskets(r['history'],index)];rid={r['request_id'] for r in supported}
        for architecture in ['mean','causal']:
            model=BasketModel(len(items),architecture).cuda()
            model.load_state_dict(torch.load(folder/f'{architecture}.pt',map_location='cpu',weights_only=True))
            pred=predict_model(model,supported,index,catalog_ids,'cuda')
            pred.extend({'request_id':r['request_id'],'source':SOURCE,'arm':architecture,'item_ids':[]} for r in public if r['request_id'] not in rid)
            for p in pred:p['arm']=f'{architecture}_seed{seed}'
            predictions.extend(pred);del model;torch.cuda.empty_cache()
    write_rows(OUT/'predictions.jsonl',predictions);write_rows(OUT/'candidates.jsonl',candidate_rows)
    write(OUT/'PREDICTED.json',{'sha256':sha(OUT/'predictions.jsonl'),'labels_opened_by_predict':False,
         'tree_pipeline_ms_p50':float(np.median(latencies)*1000),'tree_pipeline_ms_p95':float(np.percentile(latencies,95)*1000),
         'timing_scope':'local warmed retrieval, 12 features and CPU LightGBM; excludes BFF and text generation'})

def paired_bootstrap(details,left,right,seed=20260916):
    a={r['user_id']:r for r in details if r['arm']==left};b={r['user_id']:r for r in details if r['arm']==right}
    assert a.keys()==b.keys();users=sorted(a);rng=np.random.default_rng(seed)
    result={}
    for metric in ['ndcg_at_10','recall_at_100']:
        diff=np.array([a[u][metric]-b[u][metric] for u in users]);means=[]
        for _ in range(100):
            idx=rng.integers(0,len(users),size=(100,len(users)));means.extend(diff[idx].mean(1))
        result[metric]={'difference':float(diff.mean()),'ci95':np.percentile(means,[2.5,97.5]).tolist(),
                        'win_users':int((diff>0).sum()),'loss_users':int((diff<0).sum()),'tie_users':int((diff==0).sum())}
    return {'left':left,'right':right,'users':len(users),'paired_user_bootstrap':10000,'metrics':result}

def evaluate():
    assert not (OUT/'RESULT.json').exists()
    assert sha(OUT/'predictions.jsonl')==read(OUT/'PREDICTED.json')['sha256']
    assert sha(OUT/'labels.private.jsonl')==read(OUT/'PREPARED.json')['labels_sha256']
    details,result=score(rows(OUT/'predictions.jsonl'),rows(OUT/'public_histories.jsonl'),rows(OUT/'labels.private.jsonl'),read(BASE/'fit_artifacts.json'),rows(BASE/'catalog.jsonl'))
    result['status']='FROZEN_TIME_HOLDOUT_EVALUATED_ONCE'
    result['comparisons']=[paired_bootstrap(details,a,b) for a,b in [('lambdamart_0','fixed_union_rrf'),('lambdamart_0','equal_rrf'),('mean_seed17','popular'),('causal_seed17','mean_seed17')]]
    write_rows(OUT/'per_user.jsonl',details);write(OUT/'RESULT.json',result);seal(OUT)
    print(json.dumps({'users':result['users'],'targets':result['targets'],'metrics':{a:v['all'] for a,v in result['metrics'].items()},'comparisons':result['comparisons']},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['freeze','prepare','predict','evaluate']);a=p.parse_args();globals()[a.stage]()
