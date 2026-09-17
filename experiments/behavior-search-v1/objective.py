"""Same-input same-architecture objective ablation, selection only on dev."""
import time,json,math,collections
from pathlib import Path
import numpy as np,torch
import ctr
D=ctr.DATA;O=D/'objective-v2';O.mkdir(exist_ok=True)
def save(n,x):(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8')
torch.set_num_threads(3);device='cuda' if torch.cuda.is_available() else 'cpu'
X=np.load(D/'train_x.npy',mmap_mode='r');y=np.load(D/'train_y.npy',mmap_mode='r');V=np.load(D/'dev_x.npy',mmap_mode='r');vy=np.load(D/'dev_y.npy',mmap_mode='r');scale=np.load(D/'scale.npz');mean,std=scale['mean'],scale['std'];gg=json.loads((D/'train_groups.json').read_text());vg=json.loads((D/'dev_groups.json').read_text());pairs=[]
for g in gg:
 a,b=g['start'],g['end'];pos=np.flatnonzero(y[a:b])+a;neg=np.flatnonzero(y[a:b]==0)+a
 if len(pos) and len(neg):pairs.append((pos,neg))
rng=np.random.default_rng(20260915);pp=[];nn=[]
for pos,neg in pairs:
 for _ in range(4):pp.append(rng.choice(pos));nn.append(rng.choice(neg))
pp=np.array(pp);nn=np.array(nn);np.savez(O/'fixed_pairs.npz',positive=pp,negative=nn);del pairs
def make():return torch.nn.Sequential(torch.nn.Linear(16,32),torch.nn.ReLU(),torch.nn.Linear(32,16),torch.nn.ReLU(),torch.nn.Linear(16,1)).to(device)
def tensor(x):return torch.tensor((x-mean)/std,device=device)
def pred(net,xx):
 out=[]
 with torch.no_grad():
  for a in range(0,len(xx),8192):out.append(torch.sigmoid(net(tensor(xx[a:a+8192]))).cpu().numpy().ravel())
 return np.concatenate(out)
results=[];epochs=[]
for weight in [0.,.25]:
 for seed in [17,29,43]:
  torch.manual_seed(seed);rng=np.random.default_rng(seed);prng=np.random.default_rng(seed+1234);net=make();name=f'pair{weight}_seed{seed}'
  with torch.no_grad():net[-1].bias.fill_(math.log(float(y.mean())/(1-float(y.mean()))))
  opt=torch.optim.Adam(net.parameters(),lr=.002);best=-1.;t=time.time()
  for epoch in range(8):
   net.train();order=rng.permutation(len(y))
   for a in range(0,len(y),4096):
    ix=order[a:a+4096];opt.zero_grad();loss=torch.nn.functional.binary_cross_entropy_with_logits(net(tensor(X[ix])),torch.tensor(np.asarray(y[ix],dtype=np.float32),device=device).reshape(-1,1))
    if weight:
     pi=prng.integers(0,len(pp),1024);loss=loss+weight*torch.nn.functional.softplus(net(tensor(X[nn[pi]]))-net(tensor(X[pp[pi]]))).mean()
    loss.backward();opt.step()
   net.eval();p=pred(net,V);m=ctr.metrics(vy,p,vg);epochs.append({'name':name,'epoch':epoch+1,'dev':m})
   if m['ndcg10_all_requests']>best:best=m['ndcg10_all_requests'];bestm=m;be=epoch+1;torch.save(net.cpu().state_dict(),O/(name+'.pt'));net.to(device)
  results.append({'name':name,'weight':weight,'seed':seed,'epoch':be,'dev':bestm,'train_seconds':time.time()-t});save('TRAINING.json',results);save('EPOCHS.json',epochs);print(name,'dev ndcg',best,flush=True)
means={str(w):float(np.mean([x['dev']['ndcg10_all_requests'] for x in results if x['weight']==w])) for w in [0.,.25]};save('FROZEN_SELECTION.json',{'selected_weight':max(means,key=means.get),'dev_mean_ndcg10':means,'test_role':'previously exposed historical time regression only'})
T=np.load(D/'test_x.npy',mmap_mode='r');ty=np.load(D/'test_y.npy',mmap_mode='r');tg=json.loads((D/'test_groups.json').read_text())
for run in results:
 net=make();net.load_state_dict(torch.load(O/(run['name']+'.pt'),map_location=device,weights_only=True));net.eval();p=pred(net,T);np.save(O/(run['name']+'_test.npy'),p);run['regression']=ctr.metrics(ty,p,tg);print(run['name'],run['regression'],flush=True)
save('RESULT.json',{'runs':results,'pair_count':len(pp),'selection':json.loads((O/'FROZEN_SELECTION.json').read_text())})
