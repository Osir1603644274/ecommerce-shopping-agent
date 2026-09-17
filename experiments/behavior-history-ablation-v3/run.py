"""Fixed MLP, old/new temporal history. Original artifacts stay read-only."""
import argparse, collections, hashlib, json, math, sys, time
from pathlib import Path
import numpy as np

HERE=Path(__file__).resolve().parent
OLD=Path('D:/agent-datasets/behavior-search-v1')
SNAP=Path('D:/agent-datasets/behavior-history-v2')
OUT=Path('D:/agent-datasets/behavior-history-ablation-v3')
DOC=Path('F:/agent/docs/experiments/behavior-history-ablation-v3-20260916')
sys.path.insert(0,str(HERE.parent/'behavior-search-v1'))
from ctr import metrics
SEEDS=[17,29,43]
def read(p): return json.loads(p.read_text(encoding='utf8'))
def save(name,obj):
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8')
def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    return h.hexdigest()
def build():
    assert not OUT.exists(), 'Preserve existing attempt'
    OUT.mkdir(); cfg=read(OLD/'config.json'); ix=read(SNAP/'INDEX.json'); built=read(SNAP/'BUILT.json')
    assert read(SNAP/'VALIDATION.json')['status']=='PASS'
    sources={}; checks={}; meta=np.load(OLD/'item_meta.npy',mmap_mode='r')
    for sp in ['train','dev','test']:
        for name,expected in ix['old_sources'][sp].items():
            assert digest(OLD/name)==expected,name
        p=SNAP/f'{sp}_snapshots.jsonl'; assert digest(p)==built['snapshot_hashes'][sp]
        src=np.load(OLD/f'{sp}_x.npy',mmap_mode='r'); yy=np.load(OLD/f'{sp}_y.npy',mmap_mode='r'); groups=read(OLD/f'{sp}_groups.json')
        dst=np.lib.format.open_memmap(OUT/f'{sp}_new_x.npy',mode='w+',dtype=np.float32,shape=src.shape)
        masks=[]; maxerr=0.; changed=0
        with p.open(encoding='utf8') as sf,(OLD/f'{sp}.jsonl').open(encoding='utf8') as rf:
            for ordinal,(sl,rl) in enumerate(zip(sf,rf,strict=True)):
                s=json.loads(sl); r=json.loads(rl); g=groups[ordinal]; a,b=g['start'],g['end']
                assert s['ordinal']==ordinal and s['sid']==r['session_id']==g['sid']
                assert s['uid']==r['user_id']==g['uid']
                ii=r['impressed_item_ids']; assert b-a==len(ii)
                assert np.array_equal(yy[a:b],np.array([int(i in set(r['clicked_item_ids'])) for i in ii]))
                states=[]
                for arm in ['old','new']:
                    bc=collections.Counter(); cc=collections.Counter(); exposures=0; clicks=0
                    for tg in s['history_time_groups']:
                        if arm=='old' and tg['time_index']>cfg['history_end']:continue
                        assert tg['time_index']<s['request_time_index']
                        if sp!='train':assert tg['time_index']<=cfg['train_end']
                        for hr in tg['requests']:
                            assert hr['sid']!=s['sid']; exposures+=hr['exposures']
                            for item in hr['clicked_item_ids']:
                                brand,cat=map(int,meta[item]);bc[brand]+=1;cc[cat]+=1;clicks+=1
                    assert clicks==s[arm]['clicks'] and exposures==s[arm]['exposures']
                    v=np.empty((len(ii),5),dtype=np.float32)
                    v[:,0]=math.log1p(clicks);v[:,1]=(clicks+20*cfg['calibration_click_fraction'])/(exposures+20);v[:,4]=int(clicks>0)
                    for j,item in enumerate(ii):
                        brand,cat=map(int,meta[item]);v[j,2]=bc[brand]/max(1,clicks);v[j,3]=cc[cat]/max(1,clicks)
                    states.append(v)
                err=float(np.max(np.abs(states[0]-src[a:b,11:])))
                maxerr=max(maxerr,err); assert np.allclose(states[0],src[a:b,11:],rtol=0,atol=1e-7),(sp,g['sid'],err)
                dst[a:b,:11]=src[a:b,:11];dst[a:b,11:]=states[1]
                changed+=int(np.any(states[0]!=states[1]));masks.append('old_history' if s['old']['clicks'] else ('gained_history' if s['new']['clicks'] else 'no_click_history'))
        assert ordinal+1==len(groups) and b==len(src)
        dst.flush();assert np.isfinite(dst).all() and np.array_equal(dst[:,:11],src[:,:11])
        checks[sp]={'requests':len(groups),'rows':len(src),'old_five_feature_max_error':maxerr,'changed_requests':changed,'cohorts':dict(collections.Counter(masks))}
        save(f'{sp}_cohorts.json',masks)
        sources[str(OLD/f'{sp}_x.npy')]=digest(OLD/f'{sp}_x.npy')
        for name in ix['old_sources'][sp]:sources[str(OLD/name)]=digest(OLD/name)
        sources[str(p)]=digest(p); print('BUILD',sp,checks[sp],flush=True)
        del dst,src
    for p in [OLD/'item_meta.npy',OLD/'config.json',HERE/'PLAN.md',HERE/'run.py']:sources[str(p)]=digest(p)
    save('INPUTS.json',sources);save('VALIDATION.json',{'status':'PASS','checks':checks})
    save('CONTRACT.json',{'arms':['old','new'],'seeds':SEEDS,'epochs':8,'batch':4096,'learning_rate':.002,'checkpoint_metric':'dev_logloss','arm_selection':'mean_seed_dev_logloss','test':'exposed_historical_regression','normalization':'per_arm_train_only','base_features_unchanged':True})

def matrix(sp,arm):return np.load((OLD/f'{sp}_x.npy') if arm=='old' else (OUT/f'{sp}_new_x.npy'),mmap_mode='r')
def train():
    import torch
    assert not (OUT/'TRAINING.json').exists(),'Preserve trained attempt'
    assert read(OUT/'VALIDATION.json')['status']=='PASS'
    torch.set_num_threads(3);torch.use_deterministic_algorithms(True)
    device='cuda' if torch.cuda.is_available() else 'cpu'
    save('RUNTIME.json',{'torch':torch.__version__,'numpy':np.__version__,'device':device,'cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(0) if device=='cuda' else None})
    y=np.load(OLD/'train_y.npy',mmap_mode='r');dy=np.load(OLD/'dev_y.npy',mmap_mode='r');runs=[];epochs=[]
    def net():return torch.nn.Sequential(torch.nn.Linear(16,32),torch.nn.ReLU(),torch.nn.Linear(32,16),torch.nn.ReLU(),torch.nn.Linear(16,1)).to(device)
    def predict(model,x,mean,std):
        arr=np.empty(len(x),dtype=np.float32)
        with torch.no_grad():
            for a in range(0,len(x),8192):arr[a:a+8192]=torch.sigmoid(model(torch.tensor((x[a:a+8192]-mean)/std,device=device))).cpu().numpy().ravel()
        return arr
    print('TRAIN device',device,flush=True)
    for arm in ['old','new']:
        x=matrix('train',arm);d=matrix('dev',arm);mean=x.mean(axis=0);std=np.maximum(x.std(axis=0),1e-5)
        np.savez(OUT/f'{arm}_scale.npz',mean=mean,std=std)
        for seed in SEEDS:
            torch.manual_seed(seed);rng=np.random.default_rng(seed);model=net();name=f'{arm}_seed{seed}';t=time.time()
            with torch.no_grad():model[-1].bias.fill_(math.log(float(y.mean())/(1-float(y.mean()))))
            opt=torch.optim.Adam(model.parameters(),lr=.002);best=float('inf')
            for ep in range(8):
                model.train();order=rng.permutation(len(y))
                for a in range(0,len(y),4096):
                    ids=order[a:a+4096];xx=torch.tensor((x[ids]-mean)/std,device=device);target=torch.tensor(np.asarray(y[ids],dtype=np.float32),device=device).reshape(-1,1)
                    opt.zero_grad();loss=torch.nn.functional.binary_cross_entropy_with_logits(model(xx),target);loss.backward();opt.step()
                model.eval();p=predict(model,d,mean,std);ll=float(np.mean(-(dy*np.log(np.clip(p,1e-7,1))+(1-dy)*np.log(np.clip(1-p,1e-7,1)))))
                epochs.append({'arm':arm,'seed':seed,'epoch':ep+1,'dev_logloss':ll})
                if ll<best:
                    best=ll;bestepoch=ep+1;torch.save(model.state_dict(),OUT/f'{name}.pt');np.save(OUT/f'{name}_dev.npy',p)
                print(name,'epoch',ep+1,'dev_logloss',round(ll,8),flush=True)
            runs.append({'arm':arm,'seed':seed,'name':name,'epoch':bestepoch,'dev_logloss':best,'train_seconds':time.time()-t})
            save('TRAINING.json',runs);save('EPOCHS.json',epochs)
    means={arm:float(np.mean([r['dev_logloss'] for r in runs if r['arm']==arm])) for arm in ['old','new']}
    selected=min(means,key=means.get);save('FROZEN_SELECTION.json',{'selected':selected,'dev_mean_logloss':means,'test_used_for_selection':False})
    results=[]
    for r in runs:
        scale=np.load(OUT/f"{r['arm']}_scale.npz");model=net();model.load_state_dict(torch.load(OUT/f"{r['name']}.pt",weights_only=True,map_location=device));model.eval();entry=dict(r)
        for sp in ['dev','test']:
            p=np.load(OUT/f"{r['name']}_dev.npy") if sp=='dev' else predict(model,matrix(sp,r['arm']),scale['mean'],scale['std'])
            if sp=='test':np.save(OUT/f"{r['name']}_test.npy",p)
            entry[sp]=metrics(np.load(OLD/f'{sp}_y.npy',mmap_mode='r'),p,read(OLD/f'{sp}_groups.json'))
        results.append(entry);print('RESULT',r['name'],entry['test'],flush=True)
    save('RESULT.json',{'selected':selected,'runs':results})

def per_request(y,p,groups):
    p=np.clip(p.astype(float),1e-7,1-1e-7);loss=-(y*np.log(p)+(1-y)*np.log1p(-p));ls=[];nds=[]
    for g in groups:
        a,b=g['start'],g['end'];ys=y[a:b];k=min(10,b-a);ideal=np.sum(1/np.log2(np.arange(2,min(k,int(ys.sum()))+2)))
        nd=float(np.sum(ys[np.argsort(-p[a:b],kind='stable')[:k]]/np.log2(np.arange(2,k+2)))/ideal) if ideal else 0.
        ls.append(float(loss[a:b].sum()));nds.append(nd)
    return np.array(ls),np.array(nds)
def analyze():
    res=read(OUT/'RESULT.json');summary={};detail={}
    for sp in ['dev','test']:
        y=np.load(OLD/f'{sp}_y.npy',mmap_mode='r');groups=read(OLD/f'{sp}_groups.json');cohorts=np.array(read(OUT/f'{sp}_cohorts.json'))
        arrays={}
        for arm in ['old','new']:
            pairs=[per_request(y,np.load(OUT/f'{arm}_seed{s}_{sp}.npy'),groups) for s in SEEDS]
            arrays[arm]=np.mean([p[0] for p in pairs],axis=0),np.mean([p[1] for p in pairs],axis=0)
        count=np.array([g['end']-g['start'] for g in groups]);uid=np.array([g['uid'] for g in groups]);parts={}
        for co in ['all','old_history','gained_history','no_click_history']:
            mask=np.ones(len(groups),dtype=bool) if co=='all' else cohorts==co
            users,idx=np.unique(uid[mask],return_inverse=True);n=len(users);unit=np.bincount(idx,minlength=n);exp=np.bincount(idx,weights=count[mask],minlength=n)
            dl=np.bincount(idx,weights=(arrays['new'][0]-arrays['old'][0])[mask],minlength=n);dn=np.bincount(idx,weights=(arrays['new'][1]-arrays['old'][1])[mask],minlength=n)
            rng=np.random.default_rng(20260916);boot=[]
            for _ in range(2000):
                draw=rng.integers(0,n,n);boot.append([dl[draw].sum()/exp[draw].sum(),dn[draw].sum()/unit[draw].sum()])
            cis=np.percentile(boot,[2.5,97.5],axis=0)
            parts[co]={'requests':int(mask.sum()),'users':n,'pairs':int(count[mask].sum()),'old_logloss':float(arrays['old'][0][mask].sum()/count[mask].sum()),'new_logloss':float(arrays['new'][0][mask].sum()/count[mask].sum()),'old_ndcg10':float(arrays['old'][1][mask].mean()),'new_ndcg10':float(arrays['new'][1][mask].mean()),'delta_logloss':float(dl.sum()/exp.sum()),'delta_logloss_ci95':cis[:,0].tolist(),'delta_ndcg10':float(dn.sum()/unit.sum()),'delta_ndcg10_ci95':cis[:,1].tolist()}
        detail[sp]=parts
        summary[sp]={arm:{key:float(np.mean([r[sp][key] for r in res['runs'] if r['arm']==arm])) for key in ['auc','logloss','ndcg10_all_requests','ndcg10_positive_requests','brier']} for arm in ['old','new']}
    # Reproduction is diagnostic only; original old arm must agree up to floating point.
    oldreport=read(OLD/'RESULT.json');repro=[]
    for r in res['runs']:
        if r['arm']!='old':continue
        prev=next(p for p in oldreport['runs'] if p['name']==f"mlp_history1_seed{r['seed']}")
        repro.append({'seed':r['seed'],'old_epoch':prev['epoch'],'rerun_epoch':r['epoch'],'dev_logloss_delta':r['dev_logloss']-prev['dev_logloss'],'test_logloss_delta':r['test']['logloss']-prev['test']['logloss']})
    save('ANALYSIS.json',{'summary':summary,'cohorts':detail,'old_reproduction':repro,'bootstrap':{'unit':'user_cluster','resamples':2000,'seed':20260916,'delta':'new-minus-old','seed_aggregation':'mean per-request metrics before bootstrap; not ensemble predictions','scope':'conditional on three fixed training seeds; exploratory subgroup intervals; no position debiasing'}})
    print(json.dumps({'summary':summary,'test_all':detail['test']['all'],'old_reproduction':repro},ensure_ascii=False,indent=2),flush=True)
def verify():
    for p,h in read(OUT/'INPUTS.json').items():assert digest(Path(p))==h,p
    assert len(read(OUT/'TRAINING.json'))==6 and len(read(OUT/'EPOCHS.json'))==48
    for r in read(OUT/'RESULT.json')['runs']:
        eps=[e for e in read(OUT/'EPOCHS.json') if e['arm']==r['arm'] and e['seed']==r['seed']]
        assert r['epoch']==min(eps,key=lambda e:e['dev_logloss'])['epoch']
        for sp in ['dev','test']:
            p=np.load(OUT/f"{r['name']}_{sp}.npy");y=np.load(OLD/f'{sp}_y.npy',mmap_mode='r')
            assert len(p)==len(y) and np.isfinite(p).all() and (p>=0).all() and (p<=1).all()
    save('FINAL_VALIDATION.json',{'status':'PASS','input_hashes_unchanged':True,'fits':6,'epochs':48,'selection':'verified_dev_only','predictions':'shape_finite_range_checked'})
    print('FINAL VALIDATION PASS',flush=True)
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('phase',choices=['build','train','analyze','verify']);globals()[ap.parse_args().phase]()
