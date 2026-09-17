"""SASRec-inspired daily-basket ID model and an unordered-basket aggregation control."""
import os
os.environ.setdefault('OMP_NUM_THREADS','2')
import argparse, copy, itertools, random, time
import torch
from torch import nn
from common import *

OUT=ROOT/'sequence-dev-001'

class BasketModel(nn.Module):
    def __init__(self,items,architecture):
        super().__init__();self.architecture=architecture
        self.item=nn.Embedding(items+1,64,padding_idx=0)
        nn.init.normal_(self.item.weight,std=.02)
        with torch.no_grad():self.item.weight[0].zero_()
        if architecture=='causal':
            self.position=nn.Embedding(50,64)
            self.encoder=nn.TransformerEncoder(nn.TransformerEncoderLayer(64,2,128,.1,batch_first=True,norm_first=True),2,enable_nested_tensor=False)
        self.norm=nn.LayerNorm(64)

    def forward(self,ids):
        # [batch, chronological day, unordered items]; ID zero denotes padding.
        present=ids!=0;step=present.any(-1)
        x=(self.item(ids)*present.unsqueeze(-1)).sum(2)/present.sum(-1,keepdim=True).clamp(min=1)
        if self.architecture=='mean':
            output=(x*step.unsqueeze(-1)).sum(1)/step.sum(-1,keepdim=True).clamp(min=1)
        else:
            x=x+self.position(torch.arange(ids.shape[1],device=ids.device))[None,:,:]
            mask=torch.ones(ids.shape[1],ids.shape[1],device=ids.device,dtype=torch.bool).triu(1)
            x=self.encoder(x,mask=mask,src_key_padding_mask=~step)
            output=x[torch.arange(len(ids),device=ids.device),step.sum(1)-1]
        return self.norm(output)

def baskets(history,index):
    days=collections.defaultdict(set)
    for e in history:
        if e['item_id'] in index:days[e['timestamp']].add(index[e['item_id']])
    # Sorting inside a basket only serializes an unordered mean; cannot affect output.
    return [sorted(days[t]) for t in sorted(days)][-50:]

def batch_tensor(histories,device):
    length=max(map(len,histories));width=max(len(b) for h in histories for b in h)
    value=np.zeros((len(histories),length,width),dtype=np.int64)
    for n,h in enumerate(histories):
        for t,b in enumerate(h):value[n,t,:len(b)]=b
    return torch.as_tensor(value,device=device)

def examples(events,index):
    users=collections.defaultdict(list)
    for e in events:users[e['user_id']].append(e)
    result=[]
    for user,ev in sorted(users.items()):
        history=[];seen=set()
        for timestamp,day in itertools.groupby(sorted(ev,key=lambda e:e['timestamp']),key=lambda e:e['timestamp']):
            day=list(day);positive=sorted({e['item_id'] for e in day if e['rating']>=4 and e['item_id'] in index})
            current=baskets(history,index)
            targets=[index[i] for i in positive if i not in seen]
            if current and targets:
                excluded={index[i] for i in seen if i in index}|{index[i] for i in positive}
                # Each new observed positive gets a ranking pair; other same-day positives cannot be negatives.
                for target in targets:result.append((user,timestamp,current,target,excluded.copy()))
            history.extend(e for e in day if e['rating']>=4);seen.update(e['item_id'] for e in day)
    return result

def predict_model(model,public,index,catalog_ids,device):
    model.eval();items=sorted(index);item_numbers=torch.tensor([index[i] for i in items],device=device)
    histories=[baskets(r['history'],index) for r in public];pred=[]
    with torch.inference_mode():
        for offset in range(0,len(public),64):
            hs=histories[offset:offset+64];assert all(hs)
            context=model(batch_tensor(hs,device))
            scores=(context@model.item(item_numbers).T).cpu().numpy()
            for r,values in zip(public[offset:offset+64],scores):
                pred.append({'request_id':r['request_id'],'source':SOURCE,'arm':model.architecture,
                             'item_ids':rank(values,items,set(r['seen_all_fit_item_ids'])| (set(items)-catalog_ids))})
    return pred

def train(seed=17):
    verify_base();dest=OUT/f'seed-{seed}';dest.mkdir(parents=True,exist_ok=True)
    if (dest/'RESULT.json').exists():raise RuntimeError('Preserve completed sequence attempt')
    fit=read(BASE/'fit_artifacts.json');catalog=rows(BASE/'catalog.jsonl');catalog_ids={p['item_id'] for p in catalog}
    items=sorted(set(fit['positive_item_user_counts'])&catalog_ids);index={i:n+1 for n,i in enumerate(items)}
    events=rows(BASE/'fit_events.jsonl');data=examples(events,index)
    public=rows(BASE/'public_histories.jsonl');labels=rows(BASE/'labels.private.jsonl')
    # Public cohort must remain identical. Users with only absent-metadata history are explicit empty predictions.
    supported=[r for r in public if baskets(r['history'],index)];supported_ids={r['request_id'] for r in supported}
    write(dest/'PROTOCOL.json',{'name':'SASRec-inspired daily basket encoder vs mean-basket ID control',
        'not_vanilla_sasrec':'same-day mean pooling, next new positive review supervision, BPR sampled negatives',
        'seed':seed,'hidden':64,'heads':2,'layers':2,'max_days':50,'negative_samples':32,'batch_size':64,
        'epochs_max':10,'patience':2,'lr':.001,'loss':'mean softplus(negative_score-positive_score)',
        'negative_scope':'fit-positive known IDs; excludes all earlier reviewed IDs and all current day positives',
        'selection':'dev nDCG10 separately for each architecture, tie earlier epoch',
        'cold_item_policy':'no trained ID representation; excluded from ID candidate space, retained in label denominator',
        'training_examples':len(data),'training_users':len({d[0] for d in data}),'known_ids':len(items),
        'unsupported_dev_users':len(public)-len(supported),'source_sha256':sha(BASE/'MANIFEST.json')})
    write(dest/'item_ids.json',items)
    audit=[];predictions=[];torch.set_num_threads(2);device='cuda'
    for architecture in ['mean','causal']:
        random.seed(seed);np.random.seed(seed);torch.manual_seed(seed);torch.cuda.manual_seed_all(seed)
        rng=np.random.default_rng(seed)
        model=BasketModel(len(items),architecture).to(device);opt=torch.optim.AdamW(model.parameters(),lr=.001,weight_decay=.01)
        best=-1;best_epoch=0;history=[];best_state=None;start=time.perf_counter()
        for epoch in range(1,11):
            model.train();order=rng.permutation(len(data));losses=[]
            for offset in range(0,len(order),64):
                selected=[data[n] for n in order[offset:offset+64]]
                h=batch_tensor([d[2] for d in selected],device)
                positives=torch.tensor([d[3] for d in selected],device=device)
                negative=[]
                for d in selected:
                    allowed=np.array([i for i in range(1,len(items)+1) if i not in d[4]],dtype=np.int64)
                    assert len(allowed)>0
                    negative.append(rng.choice(allowed,32,replace=len(allowed)<32))
                negative=torch.as_tensor(np.array(negative),device=device)
                context=model(h);pos=(context*model.item(positives)).sum(-1)
                neg=(context[:,None,:]*model.item(negative)).sum(-1)
                loss=nn.functional.softplus(neg-pos[:,None]).mean()
                assert torch.isfinite(loss)
                opt.zero_grad();loss.backward();nn.utils.clip_grad_norm_(model.parameters(),1);opt.step();losses.append(loss.item())
            preds=predict_model(model,supported,index,catalog_ids,device)
            preds.extend({'request_id':r['request_id'],'source':SOURCE,'arm':architecture,'item_ids':[]} for r in public if r['request_id'] not in supported_ids)
            _,res=score(preds,public,labels,fit,catalog);metric=res['metrics'][architecture]['all']['ndcg_at_10']
            history.append({'epoch':epoch,'loss':float(np.mean(losses)),'dev_ndcg10':metric})
            print(json.dumps({'seed':seed,'architecture':architecture,**history[-1]}),flush=True)
            if metric>best:
                best=metric;best_epoch=epoch;best_state={k:v.detach().cpu().clone() for k,v in model.state_dict().items()}
            if epoch-best_epoch>=2:break
        model.load_state_dict(best_state);torch.save(best_state,dest/f'{architecture}.pt')
        preds=predict_model(model,supported,index,catalog_ids,device)
        preds.extend({'request_id':r['request_id'],'source':SOURCE,'arm':architecture,'item_ids':[]} for r in public if r['request_id'] not in supported_ids)
        predictions.extend(preds);audit.append({'architecture':architecture,'best_epoch':best_epoch,'history':history,'seconds':time.perf_counter()-start})
        del model,opt;torch.cuda.empty_cache()
    write_rows(dest/'predictions.jsonl',predictions)
    details,result=score(predictions,public,labels,fit,catalog);result.update(status='DEVELOPMENT_ONLY',training=audit)
    write_rows(dest/'per_user.jsonl',details);write(dest/'RESULT.json',result);seal(dest)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--seed',type=int,default=17);args=p.parse_args();train(args.seed)
