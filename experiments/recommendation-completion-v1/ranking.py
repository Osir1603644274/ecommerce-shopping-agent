"""Temporal feature snapshots and fixed-candidate LambdaMART, no target injection."""
import os
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
os.environ.setdefault('OMP_NUM_THREADS','2')
import argparse, itertools, time
from common import *

OUT=ROOT/'ranking-dev-001'
FEATURES=['log_users','cf_score','mean_content','max_content','recent_basket_content',
          'brand_fraction','category_fraction','history_log_size','cold_item',
          'cf_reciprocal_rank','content_reciprocal_rank','tfidf_reciprocal_rank']

def make_fit(events, cutoff):
    positive=collections.defaultdict(set); reviewed=collections.defaultdict(set)
    for e in events:
        if e['timestamp']<=cutoff:
            reviewed[e['user_id']].add(e['item_id'])
            if e['rating']>=4: positive[e['user_id']].add(e['item_id'])
    counts=collections.Counter(i for items in positive.values() for i in items)
    return {'source':SOURCE,'fit_last_inclusive':cutoff,'user_positive_items':{u:sorted(v) for u,v in positive.items()},
            'user_reviewed_items':{u:sorted(v) for u,v in reviewed.items()},'positive_item_user_counts':dict(counts)}

class Retriever:
    def __init__(self,fit):
        self.catalog=sorted(rows(BASE/'catalog.jsonl'),key=lambda p:p['item_id'])
        self.ids=[p['item_id'] for p in self.catalog];self.index={i:n for n,i in enumerate(self.ids)}
        self.vectors=np.load(ROOT/'embeddings/vectors.npy',mmap_mode='r')
        assert read(ROOT/'embeddings/item_ids.json')==self.ids
        self.fit=fit;self.cf=old.cf_index(fit,set(self.ids))
        self.tv,self.postings,_=old.content_index(self.catalog)

    def features(self,r):
        seen=old.check_public(r,self.fit)
        history=sorted({h['item_id'] for h in r['history']})
        hi=[self.index[i] for i in history if i in self.index]
        cfscore=collections.Counter()
        tq=collections.Counter()
        for item in history: cfscore.update(self.cf.get(item,{}));tq.update(self.tv.get(item,{}))
        norm=math.sqrt(sum(v*v for v in tq.values()));tscore=collections.Counter()
        if norm:
            for term,w in tq.items():
                for item,v in self.postings[term]:tscore[item]+=w/norm*v
        neural=np.zeros(len(self.ids),dtype=np.float32)
        if hi:
            mean=self.vectors[hi].mean(0);mean/=max(np.linalg.norm(mean),1e-12);neural=self.vectors@mean
        cf=old.top_items(cfscore,seen,set(self.ids));tfidf=old.top_items(tscore,seen,set(self.ids))
        content=rank(neural,self.ids,seen,positive=True) if hi else []
        candidate=sorted(set(cf)|set(tfidf)|set(content))
        ci=[self.index[i] for i in candidate]
        recent=max((h['timestamp'] for h in r['history']),default=0)
        ri=sorted({self.index[h['item_id']] for h in r['history'] if h['timestamp']==recent and h['item_id'] in self.index})
        brands=collections.Counter(self.catalog[i]['brand'].lower() for i in hi if self.catalog[i]['brand'])
        categories=collections.Counter(self.catalog[i]['category'].lower() for i in hi if self.catalog[i]['category'])
        maxsim=(self.vectors[ci]@self.vectors[hi].T).max(1) if hi and ci else np.zeros(len(ci))
        recent_score=(self.vectors[ci]@self.vectors[ri].mean(0)) if ri and ci else np.zeros(len(ci))
        ranks=[{item:1/(60+n) for n,item in enumerate(lst,1)} for lst in [cf,content,tfidf]]
        matrix=[]
        for n,(item,i) in enumerate(zip(candidate,ci)):
            p=self.catalog[i];count=self.fit['positive_item_user_counts'].get(item,0)
            matrix.append([math.log1p(count),cfscore[item],float(neural[i]),float(maxsim[n]),float(recent_score[n]),
                           brands[p['brand'].lower()]/max(1,len(hi)) if p['brand'] else 0,
                           categories[p['category'].lower()]/max(1,len(hi)) if p['category'] else 0,
                           math.log1p(len(hi)),int(count==0),*[rk.get(item,0) for rk in ranks]])
        return candidate,np.asarray(matrix,dtype=np.float32).reshape(-1,len(FEATURES)),rrf([cf,content,tfidf])

def materialize():
    verify_base();OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'MATERIALIZED.json').exists():raise RuntimeError('Preserve materialized attempt')
    events=rows(BASE/'fit_events.jsonl');times=sorted(e['timestamp'] for e in events)
    cutoffs=sorted({times[math.ceil(len(times)*q)-1] for q in [.4,.6,.8]})
    ends=cutoffs[1:]+[max(times)]
    write(OUT/'PROTOCOL.json',{'status':'PREDECLARED_DEV_TRAINING','features':FEATURES,
        'snapshots':[{'feature_cutoff_inclusive':c,'target_end_inclusive':e} for c,e in zip(cutoffs,ends)],
        'train_target':'earliest new positive day in each window, all same-day positive items; no within-day order',
        'candidate':'union CF100, MiniLM100, TFIDF100; never inject targets',
        'label':'observed future positive review=1, otherwise unobserved=0 for pairwise ordering, NOT confirmed dislike',
        'dropped_groups':'training only: groups with no observed positive in candidate cannot form ranking pair',
        'selection':'dev macro nDCG10, exactly three depth/regularization configs, no final access',
        'parent_sha256':sha(BASE/'MANIFEST.json'),'embeddings_sha256':sha(ROOT/'embeddings/ENCODED.json')})
    audit=[];matrices=[];ys=[];groups=[];training_rows=[]
    for sn,(cut,end) in enumerate(zip(cutoffs,ends)):
        subset=[e for e in events if e['timestamp']<=end]
        public,labels,_,_=old.dev_requests(subset,{'fit_last_inclusive':cut,'dev_last_inclusive':end})
        fit=make_fit(subset,cut);retriever=Retriever(fit);truth={l['request_id']:set(l['target_item_ids']) for l in labels}
        kept=0;hits=0;targets=0
        for n,r in enumerate(public):
            cand,x,_=retriever.features(r);t=truth[r['request_id']];targets+=len(t);hits+=len(set(cand)&t)
            y=np.array([int(i in t) for i in cand],dtype=np.int32)
            if len(cand)>1 and y.sum()>0 and y.sum()<len(cand):
                matrices.append(x);ys.append(y);groups.append(len(cand));kept+=1
                training_rows.append({'snapshot':sn,'request_id':r['request_id'],'user_id':r['user_id'],
                     'feature_cutoff':cut,'target_timestamp':r['cutoff_timestamp'],'item_ids':cand,
                     'positive_items':sorted(set(cand)&t),'history':r['history']})
            if n%250==0:print(f'snapshot {sn} {n}/{len(public)} kept {kept}',flush=True)
        audit.append({'snapshot':sn,'eligible_requests':len(public),'training_groups':kept,'targets':targets,'candidate_hits':hits})
        del retriever
    np.savez_compressed(OUT/'train.npz',x=np.concatenate(matrices),y=np.concatenate(ys),groups=np.asarray(groups))
    write_rows(OUT/'train_groups.jsonl',training_rows)
    public=rows(BASE/'public_histories.jsonl');retriever=Retriever(read(BASE/'fit_artifacts.json'))
    matrix=[];index=[];pred=[]
    for n,r in enumerate(public):
        cand,x,rrf_ids=retriever.features(r);matrix.append(x)
        index.append({'request_id':r['request_id'],'item_ids':cand})
        pred.append({'request_id':r['request_id'],'arm':'fixed_union_rrf','source':SOURCE,'item_ids':rrf_ids})
        if n%250==0:print(f'dev features {n}/{len(public)}',flush=True)
    np.save(OUT/'dev_features.npy',np.concatenate(matrix))
    write_rows(OUT/'dev_candidates.jsonl',index);write_rows(OUT/'rrf_predictions.jsonl',pred)
    write(OUT/'MATERIALIZED.json',{'snapshots':audit,'training_groups':len(groups),'training_pairs_rows':sum(groups),
         'features':len(FEATURES),'files':{p.name:sha(p) for p in OUT.iterdir() if p.is_file()},'code_sha256':sha(__file__)})

def train():
    sys.path.insert(0,str(ROOT/'deps'));import lightgbm as lgb
    for name,digest in read(OUT/'MATERIALIZED.json')['files'].items():assert sha(OUT/name)==digest
    data=np.load(OUT/'train.npz');dev=np.load(OUT/'dev_features.npy');indices=rows(OUT/'dev_candidates.jsonl')
    public=rows(BASE/'public_histories.jsonl');labels=rows(BASE/'labels.private.jsonl');fit=read(BASE/'fit_artifacts.json');catalog=rows(BASE/'catalog.jsonl')
    configs=[{'num_leaves':7,'min_data_in_leaf':100,'lambda_l2':10},
             {'num_leaves':15,'min_data_in_leaf':100,'lambda_l2':10},
             {'num_leaves':7,'min_data_in_leaf':200,'lambda_l2':30}]
    predictions=rows(OUT/'rrf_predictions.jsonl');audit=[]
    for cn,config in enumerate(configs):
        params={'objective':'lambdarank','metric':'ndcg','ndcg_eval_at':[10],'learning_rate':.05,'num_threads':2,
                'seed':17,'deterministic':True,'force_col_wise':True,'verbosity':-1,**config}
        ds=lgb.Dataset(data['x'],label=data['y'],group=data['groups'],feature_name=FEATURES)
        start=time.perf_counter();model=lgb.train(params,ds,num_boost_round=100)
        model.save_model(str(OUT/f'model_{cn}.txt'));scores=model.predict(dev,num_threads=2);offset=0
        for row in indices:
            count=len(row['item_ids']);values=scores[offset:offset+count];offset+=count
            listing=rank(values,row['item_ids'],set())
            predictions.append({'request_id':row['request_id'],'arm':f'lambdamart_{cn}','source':SOURCE,'item_ids':listing})
        assert offset==len(scores)
        audit.append({'arm':f'lambdamart_{cn}','parameters':params,'iterations':100,'seconds':time.perf_counter()-start,
                      'feature_gain':dict(zip(FEATURES,model.feature_importance(importance_type='gain').tolist()))})
    write_rows(OUT/'predictions.jsonl',predictions)
    details,result=score(predictions,public,labels,fit,catalog)
    selected=max(result['metrics'],key=lambda a:(result['metrics'][a]['all']['ndcg_at_10'],a=='fixed_union_rrf'))
    result.update(status='DEVELOPMENT_ONLY_FIXED_CANDIDATES',selected_arm=selected,training=audit,
        candidate_targets=sum(len(set(c['item_ids'])&set(l['target_item_ids'])) for c,l in zip(indices,labels)))
    write_rows(OUT/'per_user.jsonl',details);write(OUT/'RESULT.json',result);seal(OUT)
    print(json.dumps({a:v['all'] for a,v in result['metrics'].items()},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['materialize','train']);args=p.parse_args();globals()[args.stage]()
