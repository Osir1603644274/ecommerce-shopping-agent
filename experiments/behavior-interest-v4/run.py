"""Candidate-conditioned categorical history aggregation, not a full DIN reproduction."""
import argparse,collections,hashlib,json,math,sys,time
from pathlib import Path
import numpy as np
HERE=Path(__file__).resolve().parent
OLD=Path('D:/agent-datasets/behavior-search-v1');V3=Path('D:/agent-datasets/behavior-history-ablation-v3');SNAP=Path('D:/agent-datasets/behavior-history-v2');OUT=Path('D:/agent-datasets/behavior-interest-v4')
sys.path.insert(0,str(HERE.parent/'behavior-search-v1'))
from ctr import metrics
SEEDS=[17,29,43];ARMS=['stats','mean','attention']
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(n,o):OUT.mkdir(parents=True,exist_ok=True);(OUT/n).write_text(json.dumps(o,ensure_ascii=False,indent=2),encoding='utf8')
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def recent(s,meta):
 start=s['recent20_including_boundary_ties']['start_time_inclusive'];c=collections.Counter()
 for t in s['history_time_groups']:
  if start is None or t['time_index']<start:continue
  assert t['time_index']<s['request_time_index']
  if s['partition']!='train':assert t['time_index']<=747693
  for r in t['requests']:
   for item in r['clicked_item_ids']:c[tuple(map(int,meta[item]))]+=1
 assert sum(c.values())==s['recent20_including_boundary_ties']['clicks']
 return c
def build():
 assert not OUT.exists(),'Preserve existing attempt'
 OUT.mkdir();assert read(V3/'FINAL_VALIDATION.json')['status']=='PASS'
 meta=np.load(OLD/'item_meta.npy',mmap_mode='r');vocab=[set(),set()]
 with (OLD/'train.jsonl').open(encoding='utf8') as rf,(SNAP/'train_snapshots.jsonl').open(encoding='utf8') as sf:
  for rl,sl in zip(rf,sf,strict=True):
   r=json.loads(rl);s=json.loads(sl);assert r['session_id']==s['sid']
   pairs=set(tuple(map(int,meta[i])) for i in r['impressed_item_ids'])|set(recent(s,meta))
   for p in pairs:
    for j in [0,1]:
     if p[j]>0:vocab[j].add(p[j])
 maps=[{v:i+2 for i,v in enumerate(sorted(vs))} for vs in vocab]
 save('VOCAB.json',{'brand':maps[0],'category':maps[1],'PAD':0,'UNK':1,'fit_partition':'train'})
 def enc(p):return [maps[j].get(int(p[j]),1) for j in [0,1]]
 info={};inputs={}
 for sp in ['train','dev','test']:
  gs=read(OLD/f'{sp}_groups.json');hists=[];rows=gs[-1]['end'];hidx=np.lib.format.open_memmap(OUT/f'{sp}_request_index.npy',mode='w+',dtype=np.int32,shape=(rows,));cur=np.lib.format.open_memmap(OUT/f'{sp}_candidate.npy',mode='w+',dtype=np.int32,shape=(rows,2));lengths=[];tokens=[]
  with (OLD/f'{sp}.jsonl').open(encoding='utf8') as rf,(SNAP/f'{sp}_snapshots.jsonl').open(encoding='utf8') as sf:
   for n,(rl,sl) in enumerate(zip(rf,sf,strict=True)):
    r=json.loads(rl);s=json.loads(sl);g=gs[n];a,b=g['start'],g['end'];assert r['session_id']==s['sid']==g['sid'] and r['user_id']==s['uid']==g['uid']
    cur[a:b]=[enc(meta[i]) for i in r['impressed_item_ids']];hidx[a:b]=n;counts=recent(s,meta)
    # Merge also after UNK mapping, preserving total weight exactly.
    mapped=collections.Counter()
    for p,w in counts.items():mapped[tuple(enc(p))]+=w
    hists.append(sorted(mapped.items()));lengths.append(len(mapped));tokens.append(sum(mapped.values()))
  width=max(1,max(lengths));hist=np.lib.format.open_memmap(OUT/f'{sp}_history.npy',mode='w+',dtype=np.int32,shape=(len(gs),width,2));weight=np.lib.format.open_memmap(OUT/f'{sp}_weight.npy',mode='w+',dtype=np.float32,shape=(len(gs),width));hist[:]=0;weight[:]=0
  for n,hh in enumerate(hists):
   for j,(p,w) in enumerate(hh):hist[n,j]=p;weight[n,j]=w
  np.save(OUT/f'{sp}_length.npy',np.array(lengths,dtype=np.int32));np.save(OUT/f'{sp}_tokens.npy',np.array(tokens,dtype=np.int32))
  assert np.array_equal(weight.sum(axis=1),tokens)
  for a in [cur,hidx,hist,weight]:a.flush()
  info[sp]={'requests':len(gs),'pairs':rows,'history_unique_max':width,'recent_clicks_percentiles':np.percentile(tokens,[0,50,90,99,100]).tolist(),'candidate_unknown_brand_fraction':float((cur[:,0]==1).mean()),'candidate_unknown_category_fraction':float((cur[:,1]==1).mean())}
  for p in [OLD/f'{sp}.jsonl',OLD/f'{sp}_groups.json',OLD/f'{sp}_y.npy',V3/f'{sp}_new_x.npy',SNAP/f'{sp}_snapshots.jsonl']:inputs[str(p)]=sha(p)
  print('BUILD',sp,info[sp],flush=True)
 for p in [HERE/'PLAN.md',HERE/'run.py',OLD/'item_meta.npy',V3/'new_scale.npz']:inputs[str(p)]=sha(p)
 save('INPUTS.json',inputs);save('VALIDATION.json',{'status':'PASS','partitions':info,'vocab_sizes':[len(v)+2 for v in vocab],'train_only_vocabulary':True,'weighted_histories_preserve_click_multiplicity':True})

def train():
 import torch
 assert not (OUT/'TRAINING.json').exists(),'Preserve existing trained attempt'
 torch.set_num_threads(3);torch.use_deterministic_algorithms(True);device='cuda' if torch.cuda.is_available() else 'cpu'
 cfg=read(OUT/'VALIDATION.json');assert cfg['status']=='PASS';sizes=cfg['vocab_sizes'];sc=np.load(V3/'new_scale.npz');mean,std=sc['mean'],sc['std']
 class Model(torch.nn.Module):
  def __init__(self,arm):
   super().__init__();self.arm=arm;self.brand=torch.nn.Embedding(sizes[0],8,padding_idx=0);self.cat=torch.nn.Embedding(sizes[1],8,padding_idx=0)
   self.score=torch.nn.Sequential(torch.nn.Linear(64,16),torch.nn.ReLU(),torch.nn.Linear(16,1));self.head=torch.nn.Sequential(torch.nn.Linear(80,32),torch.nn.ReLU(),torch.nn.Linear(32,16),torch.nn.ReLU(),torch.nn.Linear(16,1))
  def embed(self,i):return torch.cat([self.brand(i[...,0]),self.cat(i[...,1])],dim=-1)
  def pool(self,target,h,w):
   if self.arm=='stats':return torch.zeros_like(target)
   hist=self.embed(h)
   if self.arm=='mean':alpha=w/w.sum(1,keepdim=True).clamp_min(1)
   else:
    c=target[:,None,:].expand_as(hist);feat=torch.cat([c,hist,c-hist,c*hist],dim=-1);logit=self.score(feat).squeeze(-1)+torch.log(w.clamp_min(1));logit=logit.masked_fill(w==0,-1e9)
    alpha=torch.softmax(logit,dim=1)*(w>0);alpha=alpha/alpha.sum(1,keepdim=True).clamp_min(1e-8)
   return (hist*alpha[:,:,None]).sum(1)
  def forward(self,x,cur,h,w):
   target=self.embed(cur);hist=self.pool(target,h,w);return self.head(torch.cat([x,target,hist,target-hist,target*hist],dim=-1))
 def data(sp):return {k:np.load(p,mmap_mode='r') for k,p in {'x':V3/f'{sp}_new_x.npy','y':OLD/f'{sp}_y.npy','cur':OUT/f'{sp}_candidate.npy','idx':OUT/f'{sp}_request_index.npy','h':OUT/f'{sp}_history.npy','w':OUT/f'{sp}_weight.npy','length':OUT/f'{sp}_length.npy'}.items()}
 def batch(d,ids):
  idx=d['idx'][ids];width=max(1,int(d['length'][idx].max()));return [torch.tensor((d['x'][ids]-mean)/std,device=device),torch.tensor(d['cur'][ids],dtype=torch.long,device=device),torch.tensor(d['h'][idx,:width],dtype=torch.long,device=device),torch.tensor(d['w'][idx,:width],device=device)]
 def predict(m,d):
  out=np.empty(len(d['y']),dtype=np.float32)
  with torch.no_grad():
   for a in range(0,len(out),2048):out[a:a+2048]=torch.sigmoid(m(*batch(d,slice(a,a+2048)))).cpu().numpy().ravel()
  return out
 tr=data('train');dv=data('dev');results=[];epochs=[]
 # Direct invariants: no-history pool is zero; duplicate compression equals token expansion; history order is irrelevant.
 torch.manual_seed(91)
 for arm in ARMS:
  m=Model(arm).to(device).eval();target=m.embed(torch.tensor([[1,1]],device=device));h=torch.tensor([[[1,1],[min(2,sizes[0]-1),min(2,sizes[1]-1)]]],device=device);w=torch.tensor([[2.,1.]],device=device)
  assert torch.allclose(m.pool(target,h,torch.zeros_like(w)),torch.zeros_like(target))
  assert torch.allclose(m.pool(target,h,w),m.pool(target,h.flip(1),w.flip(1)),atol=1e-6)
  expanded=torch.cat([h[:,:1],h],dim=1);assert torch.allclose(m.pool(target,h,w),m.pool(target,expanded,torch.ones(1,3,device=device)),atol=1e-6)
 save('MODEL_INVARIANTS.json',{'status':'PASS','zero_history':True,'permutation_invariance':True,'weighted_duplicate_equivalence':True})
 print('TRAIN',device,sizes,flush=True)
 for arm in ARMS:
  for seed in SEEDS:
   torch.manual_seed(seed);rng=np.random.default_rng(seed);m=Model(arm).to(device);name=f'{arm}_seed{seed}';start=time.time()
   with torch.no_grad():m.head[-1].bias.fill_(math.log(float(tr['y'].mean())/(1-float(tr['y'].mean()))))
   opt=torch.optim.Adam(m.parameters(),lr=.002);best=float('inf')
   for ep in range(8):
    m.train();order=rng.permutation(len(tr['y']))
    for a in range(0,len(order),2048):
     ids=order[a:a+2048];args=batch(tr,ids);y=torch.tensor(np.asarray(tr['y'][ids],dtype=np.float32),device=device).reshape(-1,1);opt.zero_grad();loss=torch.nn.functional.binary_cross_entropy_with_logits(m(*args),y);loss.backward();opt.step()
    m.eval();p=predict(m,dv);z=np.clip(p,1e-7,1-1e-7);ll=float(np.mean(-(dv['y']*np.log(z)+(1-dv['y'])*np.log1p(-z))));epochs.append({'arm':arm,'seed':seed,'epoch':ep+1,'dev_logloss':ll})
    if ll<best:best=ll;bestep=ep+1;torch.save(m.state_dict(),OUT/f'{name}.pt');np.save(OUT/f'{name}_dev.npy',p)
    print(name,'epoch',ep+1,'dev_logloss',round(ll,8),flush=True)
   dm=metrics(dv['y'],np.load(OUT/f'{name}_dev.npy'),read(OLD/'dev_groups.json'))
   results.append({'name':name,'arm':arm,'seed':seed,'epoch':bestep,'dev':dm,'train_seconds':time.time()-start,'parameters_total':sum(p.numel() for p in m.parameters())});save('TRAINING.json',results);save('EPOCHS.json',epochs)
 averages={arm:{key:float(np.mean([r['dev'][key] for r in results if r['arm']==arm])) for key in ['logloss','ndcg10_all_requests']} for arm in ARMS}
 winner=min(ARMS,key=lambda a:averages[a]['logloss']);base=averages['stats'];eligible=[a for a in ARMS if a!='stats' and averages[a]['logloss']<base['logloss'] and averages[a]['ndcg10_all_requests']>=base['ndcg10_all_requests']]
 promote=min(eligible,key=lambda a:averages[a]['logloss']) if eligible else 'stats'
 save('FROZEN_SELECTION.json',{'logloss_winner':winner,'candidate_after_ranking_gate':promote,'means':averages,'test_used_for_selection':False})
 test=data('test')
 for r in results:
  m=Model(r['arm']).to(device);m.load_state_dict(torch.load(OUT/f"{r['name']}.pt",weights_only=True,map_location=device));m.eval();start=time.time();p=predict(m,test);seconds=time.time()-start;np.save(OUT/f"{r['name']}_test.npy",p)
  r['test']=metrics(test['y'],p,read(OLD/'test_groups.json'));r['test_inference_seconds']=seconds;print('RESULT',r['name'],r['test']['auc'],r['test']['logloss'],r['test']['ndcg10_all_requests'],flush=True)
 save('RESULT.json',{'runs':results,'runtime':{'device':device,'torch':torch.__version__,'numpy':np.__version__,'batch':2048,'epochs':8,'seeds':SEEDS}})
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('phase',choices=['build','train']);globals()[ap.parse_args().phase]()
