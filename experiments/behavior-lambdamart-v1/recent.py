"""Seven recent behavior relation features on the dev-selected BCE tree."""
import collections, math
import run as base
from run import *
BASE_OUT=OUT
OUT=Path('D:/agent-datasets/behavior-lambdamart-recent-v1')
SNAP=Path('D:/agent-datasets/behavior-history-v2')
FEATURES=['recent_item_share','recent_brand_share','recent_category_share',
          'last_clicked_time_item_share','last_clicked_time_brand_share','last_clicked_time_category_share','recent_log_clicks']
def save(name,x):(OUT/name).write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8')
def matrix(sp):return np.load(OUT/f'{sp}_x.npy',mmap_mode='r')

def build():
    assert not OUT.exists();assert (BASE_OUT/'FROZEN_SELECTION.json').exists()
    assert read(BASE_OUT/'FROZEN_SELECTION.json')['selected_tree']=='binary16'
    # No v1 test predictions should exist when deciding this feature experiment.
    assert not list(BASE_OUT.glob('*_seed*_test.npy'))
    OUT.mkdir();meta=np.load(BASE/'item_meta.npy',mmap_mode='r');cfg=read(BASE/'config.json');sources={};checks={}
    sources[str(BASE/'item_meta.npy')]=sha(BASE/'item_meta.npy')
    for sp in ['train','dev','test']:
        old=np.load(PREV/f'{sp}_new_x.npy',mmap_mode='r');gs=groups(sp)
        dst=np.lib.format.open_memmap(OUT/f'{sp}_x.npy',mode='w+',dtype=np.float32,shape=(len(old),23));dst[:,:16]=old
        with (BASE/f'{sp}.jsonl').open(encoding='utf8') as f,(SNAP/f'{sp}_snapshots.jsonl').open(encoding='utf8') as sf:
            for g,rl,sl in zip(gs,f,sf,strict=True):
                r=json.loads(rl);s=json.loads(sl);assert s['sid']==r['session_id']==g['sid'] and s['uid']==g['uid']
                boundary=s['recent20_including_boundary_ties']['start_time_inclusive'];times=[]
                for tg in s['history_time_groups']:
                    assert tg['time_index']<s['request_time_index']
                    if sp!='train':assert tg['time_index']<=cfg['train_end']
                    if boundary is not None and tg['time_index']>=boundary:times.append(tg)
                assert sum(len(t['requests']) for t in times)==s['recent20_including_boundary_ties']['requests']
                clicked=[[i for hr in t['requests'] for i in hr['clicked_item_ids']] for t in times]
                recent=[i for ids in clicked for i in ids];last=next((ids for ids in reversed(clicked) if ids),[])
                assert len(recent)==s['recent20_including_boundary_ties']['clicks']
                a,b=g['start'],g['end'];extra=np.zeros((b-a,7),dtype=np.float32)
                for k,items in enumerate([recent,last]):
                    ic=collections.Counter(items);bc=collections.Counter(int(meta[i,0]) for i in items if meta[i,0]!=0);cc=collections.Counter(int(meta[i,1]) for i in items if meta[i,1]!=0);den=max(1,len(items))
                    for j,item in enumerate(r['impressed_item_ids']):
                        brand,cat=map(int,meta[item]);extra[j,k*3:k*3+3]=[ic[item]/den,bc[brand]/den if brand else 0,cc[cat]/den if cat else 0]
                extra[:,6]=math.log1p(len(recent));dst[a:b,16:]=extra
        dst.flush();assert np.isfinite(dst).all() and np.array_equal(dst[:,:16],old)
        checks[sp]={'rows':len(dst),'requests':len(gs),'nonzero_recent_rows':int((dst[:,22]>0).sum())}
        for p in [PREV/f'{sp}_new_x.npy',SNAP/f'{sp}_snapshots.jsonl',BASE/f'{sp}.jsonl',BASE/f'{sp}_groups.json',BASE/f'{sp}_y.npy']:sources[str(p)]=sha(p)
        del dst
    for p in [HERE/'RECENT_PLAN.md',HERE/'recent.py',HERE/'run.py',BASE_OUT/'FROZEN_SELECTION.json']:sources[str(p)]=sha(p)
    save('INPUTS.json',sources);save('BUILD.json',{'status':'PASS','checks':checks,'added_features':FEATURES});print('RECENT BUILT',checks,flush=True)

def fit():
    assert not (OUT/'TRAINING.json').exists()
    y=np.load(BASE/'train_y.npy');dy=np.load(BASE/'dev_y.npy');tg=groups('train');layout=Layout(dy,groups('dev'));runs=[]
    tr=lgb.Dataset(matrix('train'),label=y,group=[g['end']-g['start'] for g in tg]);dv=lgb.Dataset(matrix('dev'),label=dy,group=layout.sizes,reference=tr)
    for seed in SEEDS:
        # Exact previous BCE parameters, including its bin construction seed.
        prev=lgb.Booster(model_file=str(BASE_OUT/f'binary16_seed{seed}.txt'))
        params=dict(prev.params);params['metric']='None';params.pop('num_iterations',None);curve={};t=time.time()
        model=lgb.train(params,tr,300,valid_sets=[dv],valid_names=['dev'],feval=layout.feval,callbacks=[lgb.early_stopping(30,verbose=False),lgb.record_evaluation(curve),lgb.log_evaluation(50)])
        name=f'recent23_seed{seed}';model.save_model(str(OUT/f'{name}.txt'));p=model.predict(matrix('dev'),num_threads=2);np.save(OUT/f'{name}_dev.npy',p)
        runs.append({'seed':seed,'iteration':model.best_iteration,'dev_ndcg10':float(layout.per(p).mean()),'seconds':time.time()-t});save(f'{name}_curve.json',curve);save('TRAINING.json',runs);print('RECENT FIT',runs[-1],flush=True)
    means={'binary16':read(BASE_OUT/'FROZEN_SELECTION.json')['tree_dev_ndcg10']['binary16'],'recent23':float(np.mean([r['dev_ndcg10'] for r in runs]))}
    save('FROZEN_SELECTION.json',{'selected':max(means,key=means.get),'dev_ndcg10':means,'test_used_for_selection':False});print('RECENT FROZEN',means,flush=True)

def evaluate():
    assert (OUT/'FROZEN_SELECTION.json').exists() and not (OUT/'RESULT.json').exists();result={}
    for sp in ['dev','test']:
        yy=np.load(BASE/f'{sp}_y.npy');gs=groups(sp);lo=Layout(yy,gs);scores=[];aucs=[];old=[]
        for seed in SEEDS:
            if sp=='dev':p=np.load(OUT/f'recent23_seed{seed}_dev.npy')
            else:
                m=lgb.Booster(model_file=str(OUT/f'recent23_seed{seed}.txt'));p=m.predict(matrix(sp),num_threads=2);np.save(OUT/f'recent23_seed{seed}_test.npy',p)
            scores.append(lo.per(p));aucs.append(auc(yy,p));old.append(lo.per(np.load(BASE_OUT/f'binary16_seed{seed}_{sp}.npy')))
        per=np.mean(scores,axis=0);delta=per-np.mean(old,axis=0);np.save(OUT/f'{sp}_per_request.npy',per)
        result[sp]={'ndcg10_all':float(per.mean()),'ndcg10_positive':float(per[lo.pos>0].mean()),'auc':float(np.mean(aucs)),
                    'ndcg10_seeds':[float(s.mean()) for s in scores],
                    'vs_binary16':interval(delta,gs,np.ones(len(gs),dtype=bool)),
                    'vs_mlp16':interval(per-np.mean([lo.per(np.load(PREV/f'new_seed{s}_{sp}.npy')) for s in SEEDS],axis=0),gs,np.ones(len(gs),dtype=bool))}
    save('RESULT.json',result);print('RECENT RESULT',json.dumps(result),flush=True)

def verify():
    for p,h in read(OUT/'INPUTS.json').items():assert sha(Path(p))==h,p
    for r in read(OUT/'TRAINING.json'):
        c=read(OUT/f"recent23_seed{r['seed']}_curve.json")['dev']['ndcg10_all'];assert r['iteration']==int(np.argmax(c))+1 and abs(r['dev_ndcg10']-max(c))<1e-12
    assert len(read(OUT/'TRAINING.json'))==3
    save('FINAL_VALIDATION.json',{'status':'PASS','sources_unchanged':True,'fits':3,'old16_features_unchanged':True,'temporal_boundary_checked':True})
    save('MANIFEST.json',{str(p):sha(p) for p in OUT.iterdir() if p.is_file() and p.name!='MANIFEST.json'});print('RECENT VERIFIED PASS')

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('phase',choices=['build','fit','evaluate','verify']);globals()[ap.parse_args().phase]()
