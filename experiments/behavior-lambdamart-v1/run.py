"""Read-only legacy inputs, frozen same-candidate behavior ranking experiment."""
import os
os.environ.setdefault('OMP_NUM_THREADS', '2')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
import argparse, gc, hashlib, json, sys, time
from pathlib import Path
import numpy as np

BASE=Path('D:/agent-datasets/behavior-search-v1')
PREV=Path('D:/agent-datasets/behavior-history-ablation-v3')
OUT=Path(os.environ.get('BEHAVIOR_MART_ROOT','D:/agent-datasets/behavior-lambdamart-v1'))
DOC=Path('F:/agent/docs/experiments/behavior-lambdamart-20260917')
HERE=Path(__file__).resolve().parent
sys.path.insert(0,'D:/agent-datasets/recommendation-completion-v1/deps')
import lightgbm as lgb
SEEDS=[17,29,43]
ARMS={'binary16':('binary',16),'rank16':('lambdarank',16),'rank11':('lambdarank',11)}

def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(name,x): (OUT/name).write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8')
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def groups(sp):return read(BASE/f'{sp}_groups.json')
def xmat(sp,n):return np.ascontiguousarray(np.load(PREV/f'{sp}_new_x.npy',mmap_mode='r')[:,:n])

class Layout:
    def __init__(self,y,gs):
        self.y=np.asarray(y);self.gs=gs
        self.starts=np.array([g['start'] for g in gs]);self.sizes=np.array([g['end']-g['start'] for g in gs])
        assert np.array_equal(self.starts,np.r_[0,np.cumsum(self.sizes)[:-1]]) and sum(self.sizes)==len(y)
        self.ids=np.repeat(np.arange(len(gs)),self.sizes)
        self.pos=np.add.reduceat(self.y.astype(float),self.starts)
        self.discount=1/np.log2(np.arange(2,12))
        self.ideal=np.r_[0,np.cumsum(self.discount)][np.minimum(10,self.pos.astype(int))]
    def per(self,p):
        assert len(p)==len(self.y) and np.isfinite(p).all()
        # Lexsort is stable: equal scores retain original candidate order.
        order=np.lexsort((-np.asarray(p),self.ids));gi=self.ids[order]
        ranks=np.arange(len(order))-self.starts[gi];keep=ranks<10
        dcg=np.bincount(gi[keep],weights=self.y[order[keep]]*self.discount[ranks[keep]],minlength=len(self.gs))
        return np.divide(dcg,self.ideal,out=np.zeros_like(dcg),where=self.ideal>0)
    def feval(self,p,data):return 'ndcg10_all',float(self.per(p).mean()),True

def check_metric():
    gs=[{'start':0,'end':3},{'start':3,'end':5}];y=np.array([1,0,1,0,0]);lo=Layout(y,gs)
    assert np.allclose(lo.per(np.array([4.,3.,2.,9.,8.])),[(1+0.5)/(1+1/np.log2(3)),0])
    assert np.allclose(lo.per(np.array([-10.,-20.,-30.,0.,1.])),lo.per(np.array([4.,3.,2.,9.,8.])))
    assert np.allclose(lo.per(np.zeros(5)),lo.per(np.array([4.,3.,2.,9.,8.])) )

def prepare():
    assert not OUT.exists(),'Use a new output directory; never overwrite a frozen attempt'
    OUT.mkdir(parents=True);check_metric()
    old=read(PREV/'INPUTS.json')
    for s,h in old.items():assert sha(Path(s))==h,s
    checks={};sources={};seen=set()
    for sp in ['train','dev','test']:
        gs=groups(sp);xx=np.load(PREV/f'{sp}_new_x.npy',mmap_mode='r');yy=np.load(BASE/f'{sp}_y.npy',mmap_mode='r')
        lo=Layout(yy,gs);assert xx.shape==(len(yy),16) and np.isfinite(xx).all()
        sids={g['sid'] for g in gs};assert len(sids)==len(gs) and not seen&sids;seen.update(sids)
        with (BASE/f'{sp}.jsonl').open(encoding='utf8') as f:
            for line,g in zip(f,gs,strict=True):
                r=json.loads(line);a,b=g['start'],g['end'];assert r['session_id']==g['sid'] and r['user_id']==g['uid']
                assert len(r['impressed_item_ids'])==b-a
                assert np.array_equal(yy[a:b],[int(i in set(r['clicked_item_ids'])) for i in r['impressed_item_ids']])
        checks[sp]={'requests':len(gs),'rows':len(yy),'positive_requests':int((lo.pos>0).sum()),'max_group_size':int(lo.sizes.max())}
        for p in [PREV/f'{sp}_new_x.npy',BASE/f'{sp}_y.npy',BASE/f'{sp}_groups.json',BASE/f'{sp}.jsonl']:
            sources[str(p)]=sha(p)
        for seed in SEEDS:
            if sp!='train':sources[str(PREV/f'new_seed{seed}_{sp}.npy')]=sha(PREV/f'new_seed{seed}_{sp}.npy')
    for p in [HERE/'run.py',HERE/'PLAN.md']:sources[str(p)]=sha(p)
    save('INPUTS.json',sources);save('VALIDATION.json',{'status':'PASS','checks':checks,'legacy_inputs_verified':len(old),'metric_selftest':'PASS'})
    save('CONTRACT.json',{'arms':ARMS,'seeds':SEEDS,'max_rounds':300,'early_stopping':30,'selection':'mean seed dev ndcg10_all','zero_click_requests':0,'test':'exposed historical regression','lightgbm':lgb.__version__,'numpy':np.__version__})
    print('PREPARED',checks,flush=True)

def train():
    assert not (OUT/'TRAINING.json').exists(),'Preserve existing fit'
    assert read(OUT/'VALIDATION.json')['status']=='PASS'
    y=np.load(BASE/'train_y.npy');dy=np.load(BASE/'dev_y.npy');tg=groups('train');dg=groups('dev');layout=Layout(dy,dg)
    runs=[]
    for arm,(objective,n) in ARMS.items():
        xx=xmat('train',n);dd=xmat('dev',n)
        tr=lgb.Dataset(xx,label=y,group=[g['end']-g['start'] for g in tg],free_raw_data=True)
        dv=lgb.Dataset(dd,label=dy,group=layout.sizes,reference=tr,free_raw_data=True)
        for seed in SEEDS:
            name=f'{arm}_seed{seed}';t=time.time();history={}
            params={'objective':objective,'metric':'None','learning_rate':.05,'num_leaves':15,'min_data_in_leaf':100,
                    'lambda_l2':1.,'max_bin':63,'bagging_fraction':.8,'bagging_freq':1,'feature_fraction':1.,
                    'seed':seed,'bagging_seed':seed,'data_random_seed':17,'num_threads':2,'verbosity':-1,
                    'deterministic':True,'force_col_wise':True,'feature_pre_filter':False}
            if objective=='lambdarank':params.update(label_gain=[0,1],lambdarank_truncation_level=13)
            model=lgb.train(params,tr,300,valid_sets=[dv],valid_names=['dev'],feval=layout.feval,
                            callbacks=[lgb.early_stopping(30,verbose=False),lgb.record_evaluation(history),lgb.log_evaluation(50)])
            model.save_model(str(OUT/f'{name}.txt'));p=model.predict(dd);np.save(OUT/f'{name}_dev.npy',p)
            run={'name':name,'arm':arm,'seed':seed,'iteration':model.best_iteration,'dev_ndcg10':float(layout.per(p).mean()),'seconds':time.time()-t}
            runs.append(run);save('TRAINING.json',runs);save(f'{name}_curve.json',history)
            save(f'{name}_importance.json',{'gain':model.feature_importance('gain').tolist(),'split':model.feature_importance('split').tolist()})
            print('FIT',run,flush=True)
        del tr,dv,xx,dd,model;gc.collect()
    means={arm:float(np.mean([r['dev_ndcg10'] for r in runs if r['arm']==arm])) for arm in ARMS}
    mlp=float(np.mean([layout.per(np.load(PREV/f'new_seed{s}_dev.npy')).mean() for s in SEEDS]))
    save('FROZEN_SELECTION.json',{'selected_tree':max(means,key=means.get),'tree_dev_ndcg10':means,'historical_mlp_dev_ndcg10':mlp,'test_used_for_selection':False})
    print('FROZEN',read(OUT/'FROZEN_SELECTION.json'),flush=True)

def auc(y,p):
    order=np.argsort(p,kind='stable');s=p[order];yy=y[order];starts=np.r_[0,np.flatnonzero(s[1:]!=s[:-1])+1];ends=np.r_[starts[1:],len(y)]
    pos=np.add.reduceat(yy.astype(float),starts);neg=ends-starts-pos
    return float((pos*(np.cumsum(neg)-neg+.5*neg)).sum()/(y.sum()*(len(y)-y.sum())))

def interval(delta,gs,mask):
    uid=np.array([g['uid'] for g in gs]);_,ix=np.unique(uid[mask],return_inverse=True)
    total=np.bincount(ix,weights=delta[mask]);count=np.bincount(ix);rng=np.random.default_rng(20260917);b=[]
    for _ in range(2000):
        draw=rng.integers(0,len(count),len(count));b.append(total[draw].sum()/count[draw].sum())
    return {'delta':float(delta[mask].mean()),'ci95':np.percentile(b,[2.5,97.5]).tolist(),'requests':int(mask.sum())}

def evaluate():
    assert (OUT/'FROZEN_SELECTION.json').exists() and not (OUT/'RESULT.json').exists()
    result={'selection':read(OUT/'FROZEN_SELECTION.json'),'splits':{}}
    for sp in ['dev','test']:
        yy=np.load(BASE/f'{sp}_y.npy');gs=groups(sp);lo=Layout(yy,gs);arrs={};summ={}
        for arm in ['mlp16',*ARMS]:
            scores=[];aucs=[]
            for seed in SEEDS:
                if arm=='mlp16':p=np.load(PREV/f'new_seed{seed}_{sp}.npy')
                elif sp=='dev':p=np.load(OUT/f'{arm}_seed{seed}_dev.npy')
                else:
                    m=lgb.Booster(model_file=str(OUT/f'{arm}_seed{seed}.txt'));p=m.predict(xmat(sp,ARMS[arm][1]),num_threads=2);np.save(OUT/f'{arm}_seed{seed}_test.npy',p)
                scores.append(lo.per(p));aucs.append(auc(yy,p))
            per=np.mean(scores,axis=0);arrs[arm]=per;np.save(OUT/f'{arm}_{sp}_per_request.npy',per)
            summ[arm]={'ndcg10_all':float(per.mean()),'ndcg10_positive':float(per[lo.pos>0].mean()),'auc':float(np.mean(aucs)),
                       'ndcg10_seeds':[float(a.mean()) for a in scores]}
        comps={}
        for a,b in [('binary16','mlp16'),('rank16','binary16'),('rank16','mlp16'),('rank16','rank11')]:
            comps[f'{a}-minus-{b}']=interval(arrs[a]-arrs[b],gs,np.ones(len(gs),dtype=bool))
        result['splits'][sp]={'models':summ,'comparisons':comps,'requests':len(gs),'positive_requests':int((lo.pos>0).sum())}
        print('EVALUATED',sp,summ,flush=True)
    save('RESULT.json',result)

def verify():
    check_metric()
    for p,h in read(OUT/'INPUTS.json').items():assert sha(Path(p))==h,p
    assert len(read(OUT/'TRAINING.json'))==9
    for r in read(OUT/'TRAINING.json'):
        curve=read(OUT/f"{r['name']}_curve.json")['dev']['ndcg10_all']
        assert r['iteration']==int(np.argmax(curve))+1
        assert abs(r['dev_ndcg10']-max(curve))<1e-12
    # Independent slow evaluator, including raw negative scores and zero-positive groups.
    for sp in ['dev','test']:
        y=np.load(BASE/f'{sp}_y.npy');gs=groups(sp);lo=Layout(y,gs)
        for arm in ARMS:
            p=np.load(OUT/f'{arm}_seed17_{sp}.npy');fast=lo.per(p)
            for i,g in enumerate(gs):
                a,b=g['start'],g['end'];ys=y[a:b];k=min(10,b-a);discount=1/np.log2(np.arange(2,k+2))
                ideal=discount[:min(k,int(ys.sum()))].sum();expected=float((ys[np.argsort(-p[a:b],kind='stable')[:k]]*discount).sum()/ideal) if ideal else 0.
                assert abs(expected-fast[i])<1e-12
    save('FINAL_VALIDATION.json',{'status':'PASS','inputs_unchanged':True,'fits':9,'checkpoint_selection_verified':True,'independent_metric_verified':True})
    files={str(p):sha(p) for p in OUT.iterdir() if p.is_file() and p.name!='MANIFEST.json'};save('MANIFEST.json',files)
    print('VERIFIED PASS',flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('phase',choices=['prepare','train','evaluate','verify']);globals()[ap.parse_args().phase]()
