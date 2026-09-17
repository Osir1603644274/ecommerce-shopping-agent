"""Predeclared clustered uncertainty and subgroup analysis; no training."""
import json,collections,sys
from pathlib import Path
import numpy as np
import ctr
D=ctr.DATA;r=json.loads((D/'RESULT.json').read_text(encoding='utf8'));groups=json.loads((D/'test_groups.json').read_text(encoding='utf8'));y=np.load(D/'test_y.npy');xx=np.load(D/'test_x.npy',mmap_mode='r')
users=sorted({g['uid'] for g in groups});ui={u:i for i,u in enumerate(users)};gu=np.array([ui[g['uid']] for g in groups]);starts=np.array([g['start'] for g in groups]);ends=np.array([g['end'] for g in groups]);den=np.bincount(gu,weights=ends-starts,minlength=len(users));request_den=np.bincount(gu,minlength=len(users));losses=collections.defaultdict(list);ndcgs=collections.defaultdict(list);arms=collections.defaultdict(list);subgroups=collections.defaultdict(list)
for run in r['runs']:
 name=run['name'];arm=name.rsplit('_seed',1)[0];p=np.load(D/(name+'_test.npy'));p=np.clip(p,1e-7,1-1e-7);loss=-(y*np.log(p)+(1-y)*np.log1p(-p));byrequest=np.add.reduceat(loss.astype(np.float64),starts);losses[arm].append(np.bincount(gu,weights=byrequest,minlength=len(users)));arms[arm].append(run['test'])
 nd=[]
 for g in groups:
  a,b=g['start'],g['end'];yy=y[a:b];k=min(10,b-a);v=yy[np.argsort(-p[a:b],kind='stable')[:k]];ideal=(1/np.log2(np.arange(2,min(k,int(yy.sum()))+2))).sum();nd.append(float((v/np.log2(np.arange(2,k+2))).sum()/ideal) if ideal else 0.)
 ndcgs[arm].append(np.bincount(gu,weights=nd,minlength=len(users)))
 for has in [False,True]:
  mask=xx[:,-1]==int(has);subgroups[arm+'_'+('with_prefix_clicks' if has else 'no_prefix_clicks')].append({'pairs':int(mask.sum()),'clicks':int(y[mask].sum()),'auc':ctr.auc(y[mask],p[mask]),'logloss':float(loss[mask].mean())})
def ci(delta,denominator):
 rng=np.random.default_rng(20260915);values=[]
 for _ in range(2000):
  ix=rng.integers(0,len(users),len(users));values.append(float(delta[ix].sum()/denominator[ix].sum()))
 return {'delta':float(delta.sum()/denominator.sum()),'ci95':np.quantile(values,[.025,.975]).tolist(),'iterations':2000,'clusters':len(users),'resampling':'paired user clusters; mean loss or nDCG over fixed three seeds; excludes training-seed population uncertainty'}
comparisons={}
for left,right in [('lr_history0','lr_history1'),('mlp_history0','mlp_history1'),('lr_history0','mlp_history1')]:
 comparisons[right+' minus '+left]={'logloss':ci(np.mean(losses[right],axis=0)-np.mean(losses[left],axis=0),den),'ndcg10_all_requests':ci(np.mean(ndcgs[right],axis=0)-np.mean(ndcgs[left],axis=0),request_den)}
summary={'seed_mean_test_metrics':{a:{k:float(np.mean([v[k] for v in vs])) for k in vs[0]} for a,vs in arms.items()},'subgroups_seed_mean':{a:{k:float(np.mean([v[k] for v in vs])) for k in vs[0]} for a,vs in subgroups.items()},'comparisons':comparisons,'constant':r['constant'],'selected_by_development':r['selected_arm'],'history_requests':sum(g['has_history'] for g in groups),'history_protocol':'earliest 20% native train prefix only, not whole train history','ranking_ties':'stable input ordering; constant-ranking metrics not evidence of learned ranking'}
ctr.save('ANALYSIS.json',summary)
print(json.dumps(summary,ensure_ascii=False,indent=2))
