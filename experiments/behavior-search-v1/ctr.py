"""Bounded, temporal, request-sampled CTR experiment. All source files read-only."""
import os,json,hashlib,sqlite3,collections,time,argparse,math
from pathlib import Path
import numpy as np
HERE=Path(__file__).resolve().parent
DATA=Path('D:/agent-datasets/behavior-search-v1'); DATA.mkdir(exist_ok=True)
SRC=Path('D:/agent-datasets/kuaisearch-lite-09807c773ce67360ed8df30842e372182fcf7ad9')
BASE=['query_len','title_len','char_coverage','bigram_coverage','exact_substring','item_log_exposures','item_ctr','brand_log_exposures','brand_ctr','category_log_exposures','category_ctr']
HIST=['user_log_clicks','user_ctr','brand_click_share','category_click_share','has_click_history']
def save(name,x): (DATA/name).write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8')
def stream():
 with (SRC/'recall_lite.train.jsonl').open('rb') as f:
  for l in f:yield json.loads(l)
def sampled(sid,mod):return int.from_bytes(hashlib.sha256(('ctr-v1:'+str(sid)).encode()).digest()[:8],'big')%mod==0
def norm(t):return ''.join(c.lower() for c in t if c.isalnum())
def prepare():
 started=time.time();times=[];maxid=0
 for r in stream():
  if r['split']=='train':times.append(r['time_index'])
  maxid=max(maxid,max(r['impressed_item_ids'],default=0))
 times.sort();low=times[int(len(times)*.2)];high=times[int(len(times)*.8)];del times
 cfg={'history_end':low,'train_end':high,'maxid':maxid,'base_features':BASE,'history_features':HIST,'sample_mod':{'train':16,'dev':8,'test':1},'source_hashes':json.loads((Path('docs/data/behavior-audit-20260915/AUDIT.json')).read_text(encoding='utf8'))['requests']['request_sha256']}
 save('config.json',cfg); print('boundaries',low,high,maxid,flush=True)
 needed=np.zeros(maxid+1,dtype=np.bool_);counts=collections.Counter();users=set();files={x:(DATA/(x+'.jsonl')).open('w',encoding='utf8') for x in ['train','dev','test']}
 for r in stream():
  sp='test' if r['split']=='test' else ('dev' if r['time_index']>high else 'train')
  if r['split']=='train' and r['time_index']<=low:continue
  if not sampled(r['session_id'],cfg['sample_mod'][sp]):continue
  files[sp].write(json.dumps(r,ensure_ascii=False)+'\n');needed[r['impressed_item_ids']]=True;users.add(r['user_id']);counts[sp+'_requests']+=1;counts[sp+'_pairs']+=len(r['impressed_item_ids'])
 for f in files.values():f.close()
 save('sample_counts.json',dict(counts));print('samples',dict(counts),flush=True)
 meta=np.lib.format.open_memmap(DATA/'item_meta.npy',mode='w+',dtype=np.int32,shape=(maxid+1,2));meta[:]=0
 db=sqlite3.connect(DATA/'titles.sqlite3');db.execute('pragma cache_size=-4096');db.execute('create table titles(id integer primary key,title text)');batch=[]
 with (SRC/'items_lite.train.jsonl').open('rb') as f:
  for n,l in enumerate(f,1):
   r=json.loads(l);i=r['item_id']
   if i<=maxid:
    meta[i]=[r['brand_id'],r['category_level3_id']]
    if needed[i]:batch.append((i,norm(r['item_title'])))
   if len(batch)>=5000:db.executemany('insert into titles values(?,?)',batch);db.commit();batch=[]
   if n%2000000==0:print('item metadata',n,flush=True)
 if batch:db.executemany('insert into titles values(?,?)',batch);db.commit()
 meta.flush();del needed
 pop=np.lib.format.open_memmap(DATA/'item_pop.npy',mode='w+',dtype=np.int32,shape=(maxid+1,2));pop[:]=0
 brand=collections.defaultdict(lambda:[0,0]);cat=collections.defaultdict(lambda:[0,0]);uh=collections.defaultdict(lambda:[0,0,collections.Counter(),collections.Counter()]);total=[0,0]
 for r in stream():
  if r['split']!='train' or r['time_index']>low:continue
  ii=r['impressed_item_ids'];cc=set(r['clicked_item_ids']);u=r['user_id'];total[0]+=len(ii);total[1]+=len(cc)
  for i in ii:
   b,c=map(int,meta[i]);y=int(i in cc);pop[i,0]+=1;pop[i,1]+=y;brand[b][0]+=1;brand[b][1]+=y;cat[c][0]+=1;cat[c][1]+=y
   if u in users:
    h=uh[u];h[0]+=1;h[1]+=y
    if y:h[2][b]+=1;h[3][c]+=1
 pop.flush();prior=total[1]/total[0];print('history complete',len(uh),prior,flush=True)
 cfg['calibration_click_fraction']=prior;save('config.json',cfg)
 for sp in files:
  num=counts[sp+'_pairs'];X=np.lib.format.open_memmap(DATA/(sp+'_x.npy'),mode='w+',dtype=np.float32,shape=(num,len(BASE)+len(HIST)));Y=np.lib.format.open_memmap(DATA/(sp+'_y.npy'),mode='w+',dtype=np.uint8,shape=(num,));groups=[];offset=0
  with (DATA/(sp+'.jsonl')).open(encoding='utf8') as f:
   for rn,l in enumerate(f,1):
    r=json.loads(l);q=norm(r['query']);chars=set(q);bigrams={q[j:j+2] for j in range(len(q)-1)};ii=r['impressed_item_ids'];cc=set(r['clicked_item_ids']);h=uh.get(r['user_id'],[0,0,{},{}]);titles={}
    for st in range(0,len(ii),400):
     part=ii[st:st+400];titles.update(db.execute('select id,title from titles where id in ('+','.join('?'*len(part))+')',part).fetchall())
    assert len(titles)==len(ii)
    for i in ii:
     title=titles[i];b,c=map(int,meta[i]);ie,ic=map(int,pop[i]);be,bc=brand[b];ce,cl=cat[c];tchars=set(title)
     X[offset]=[len(q),len(title),len(chars&tchars)/max(1,len(chars)),sum(x in title for x in bigrams)/max(1,len(bigrams)),int(bool(q) and q in title),math.log1p(ie),(ic+20*prior)/(ie+20),math.log1p(be),(bc+20*prior)/(be+20),math.log1p(ce),(cl+20*prior)/(ce+20),math.log1p(h[1]),(h[1]+20*prior)/(h[0]+20),h[2].get(b,0)/max(1,h[1]),h[3].get(c,0)/max(1,h[1]),int(h[1]>0)]
     Y[offset]=int(i in cc);offset+=1
    groups.append({'uid':r['user_id'],'sid':r['session_id'],'start':offset-len(ii),'end':offset,'has_history':bool(h[1])})
    if rn%5000==0:print('features',sp,rn,flush=True)
  assert offset==num;X.flush();Y.flush();save(sp+'_groups.json',groups);del X,Y
 save('PREPARED.json',{'seconds':time.time()-started,'counts':dict(counts),'feature_labels_separated':True,'history_frozen_to_prefix':True});print('PREPARED',flush=True)
def auc(y,p):
 order=np.argsort(p,kind='stable');s=p[order];yy=y[order];ends=np.r_[np.flatnonzero(s[1:]!=s[:-1])+1,len(s)];starts=np.r_[0,ends[:-1]];pos=np.add.reduceat(yy.astype(float),starts);neg=ends-starts-pos
 return float(np.sum(pos*(np.cumsum(neg)-neg+neg*.5))/(max(1,y.sum())*max(1,len(y)-y.sum())))
def metrics(y,p,groups):
 p=np.clip(p,1e-7,1-1e-7);loss=-(y*np.log(p)+(1-y)*np.log1p(-p));nd=[];positive=[]
 for g in groups:
  a,b=g['start'],g['end'];yy=y[a:b];k=min(10,b-a);v=yy[np.argsort(-p[a:b],kind='stable')[:k]];ideal=1/np.log2(np.arange(2,min(k,int(yy.sum()))+2));score=float((v/np.log2(np.arange(2,k+2))).sum()/ideal.sum()) if len(ideal) else 0.;nd.append(score)
  if yy.sum():positive.append(score)
 ece=0.
 for lo in np.arange(0,1,.1):
  mask=(p>=lo)&(p<lo+.1)
  if mask.any():ece+=float(mask.mean()*abs(p[mask].mean()-y[mask].mean()))
 return {'auc':auc(y,p),'logloss':float(loss.mean()),'brier':float(np.mean((p-y)**2)),'ece_10_equal_width':ece,'ndcg10_all_requests':float(np.mean(nd)),'ndcg10_positive_requests':float(np.mean(positive)),'requests_with_click':len(positive),'pairs':len(y),'clicks':int(y.sum()),'mean_prediction':float(p.mean())}
def train():
 import torch
 torch.set_num_threads(3);device='cuda' if torch.cuda.is_available() else 'cpu';print('device',device,flush=True)
 X=np.load(DATA/'train_x.npy',mmap_mode='r');y=np.load(DATA/'train_y.npy',mmap_mode='r');D=np.load(DATA/'dev_x.npy',mmap_mode='r');dy=np.load(DATA/'dev_y.npy',mmap_mode='r')
 mean=np.asarray(X).mean(axis=0);std=np.maximum(np.asarray(X).std(axis=0),1e-5);np.savez(DATA/'scale.npz',mean=mean,std=std)
 def predict(net,xx,n):
  out=[]
  with torch.no_grad():
   for a in range(0,len(xx),8192):out.append(torch.sigmoid(net(torch.tensor((xx[a:a+8192,:n]-mean[:n])/std[:n],device=device))).cpu().numpy().ravel())
  return np.concatenate(out)
 results=[];history=[]
 for kind in ['lr','mlp']:
  for hist in [False,True]:
   n=len(BASE)+(len(HIST) if hist else 0)
   for seed in [17,29,43]:
    torch.manual_seed(seed);rng=np.random.default_rng(seed);name=f'{kind}_history{int(hist)}_seed{seed}';net=(torch.nn.Sequential(torch.nn.Linear(n,1)) if kind=='lr' else torch.nn.Sequential(torch.nn.Linear(n,32),torch.nn.ReLU(),torch.nn.Linear(32,16),torch.nn.ReLU(),torch.nn.Linear(16,1))).to(device)
    with torch.no_grad():net[-1].bias.fill_(math.log(float(y.mean())/(1-float(y.mean()))))
    optimizer=torch.optim.Adam(net.parameters(),lr=.015 if kind=='lr' else .002);best=float('inf');t=time.time()
    for epoch in range(8):
     net.train();order=rng.permutation(len(y))
     for a in range(0,len(y),4096):
      ix=order[a:a+4096];xx=torch.tensor((X[ix,:n]-mean[:n])/std[:n],device=device);yy=torch.tensor(np.asarray(y[ix],dtype=np.float32),device=device).reshape(-1,1);optimizer.zero_grad();loss=torch.nn.functional.binary_cross_entropy_with_logits(net(xx),yy);loss.backward();optimizer.step()
     net.eval();p=predict(net,D,n);ll=float(np.mean(-(dy*np.log(np.clip(p,1e-7,1))+(1-dy)*np.log(np.clip(1-p,1e-7,1)))))
     history.append({'name':name,'epoch':epoch+1,'dev_logloss':ll})
     if ll<best:best=ll;torch.save(net.cpu().state_dict(),DATA/(name+'.pt'));net.to(device);bestepoch=epoch+1;np.save(DATA/(name+'_dev.npy'),p)
    results.append({'name':name,'kind':kind,'history':hist,'seed':seed,'features':n,'dev_logloss':best,'epoch':bestepoch,'train_seconds':time.time()-t});save('TRAINING.json',results);save('EPOCHS.json',history);print(results[-1],flush=True)
 # Configuration selection is by mean dev LogLoss over seeds, never test.
 arm={}
 for r in results:arm.setdefault(r['kind']+'_history'+str(int(r['history'])),[]).append(r['dev_logloss'])
 selected=min(arm,key=lambda a:np.mean(arm[a]));save('FROZEN_SELECTION.json',{'selected_arm':selected,'dev_mean_logloss':{a:float(np.mean(v)) for a,v in arm.items()},'test_used_for_selection':False,'seeds':[17,29,43]})
 T=np.load(DATA/'test_x.npy',mmap_mode='r');ty=np.load(DATA/'test_y.npy',mmap_mode='r');groups=json.loads((DATA/'test_groups.json').read_text());report={'constant':metrics(ty,np.full(len(ty),float(y.mean())),groups),'runs':[],'selected_arm':selected}
 for r in results:
  n=r['features'];net=(torch.nn.Sequential(torch.nn.Linear(n,1)) if r['kind']=='lr' else torch.nn.Sequential(torch.nn.Linear(n,32),torch.nn.ReLU(),torch.nn.Linear(32,16),torch.nn.ReLU(),torch.nn.Linear(16,1))).to(device);net.load_state_dict(torch.load(DATA/(r['name']+'.pt'),weights_only=True,map_location=device));net.eval();t=time.time();p=predict(net,T,n);np.save(DATA/(r['name']+'_test.npy'),p);report['runs'].append({**r,'test':metrics(ty,p,groups),'test_inference_seconds':time.time()-t});print(r['name'],report['runs'][-1]['test'],flush=True)
 save('RESULT.json',report)
if __name__=='__main__':
 mode=argparse.ArgumentParser();mode.add_argument('phase',choices=['prepare','train']);args=mode.parse_args();globals()[args.phase]()
