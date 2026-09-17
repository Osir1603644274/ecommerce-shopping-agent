"""Isolated, bounded dense retrieval experiment. Never writes source data or serving assets."""
import argparse, collections, difflib, hashlib, json, math, os, random, re, sqlite3, time, unicodedata
from pathlib import Path
import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

ROOT=Path('D:/agent-datasets/dense-recall-20260915-v1')
OLD=Path('D:/agent-datasets/search-closure-v1')
STAGE=Path('D:/agent-datasets/search-stage1-v1')
PREFIX='为这个句子生成表示以用于检索相关文章：'
SEEDS=[20260915,20260916,20260917]
def read(p): return json.loads(Path(p).read_text(encoding='utf8'))
def rows(p): return [json.loads(s) for s in Path(p).read_text(encoding='utf-8-sig').splitlines() if s.strip()]
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
def jsonl(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(''.join(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n' for r in x),encoding='utf8')
def key(s):return re.sub(r'[^\w\u3400-\u9fff]','',unicodedata.normalize('NFKC',s).casefold())
def base():return read(STAGE/'models/manifest.json')['dense']['path']
def documents(ids):
    db=sqlite3.connect((STAGE/'catalog.sqlite').as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
    out={};ids=sorted(ids)
    for i in range(0,len(ids),400):
        chunk=ids[i:i+400]
        for did,src,text in db.execute('SELECT docid,source,text FROM documents WHERE docid IN ('+','.join('?'*len(chunk))+')',chunk):out[did]=dict(document_id=did,source=src,text=text)
    db.close();assert len(out)==len(ids);return out
def load(path):
    torch.set_num_threads(2)
    return AutoTokenizer.from_pretrained(path,local_files_only=True),AutoModel.from_pretrained(path,local_files_only=True).cuda()
def encode(tok,model,texts,batch=64):
    model.eval();result=[]
    with torch.inference_mode(),torch.autocast('cuda',dtype=torch.bfloat16):
        for start in range(0,len(texts),batch):
            t=tok(texts[start:start+batch],padding=True,truncation=True,max_length=128,return_tensors='pt').to('cuda')
            v=torch.nn.functional.normalize(model(**t).last_hidden_state[:,0].float(),dim=-1)
            result.append(v.cpu().numpy())
    return np.concatenate(result)
def prepare():
    assert not (ROOT/'PREPARED.json').exists(),'Preparation already frozen'
    tr=rows(OLD/'training-preparation/repair-v2/qrels-expanded.jsonl');g=collections.defaultdict(list)
    for r in tr:g[r['query_id']].append(r)
    groups=[]
    for qid,rr in sorted(g.items()):
        pos=[r for r in rr if r['grade']==3];neg=[r for r in rr if r['grade']==0]
        if pos and len(neg)>=2:groups.append(dict(query_id=qid,query=rr[0]['query'],source=rr[0]['source'],pos=pos,neg=neg))
    assert len(groups)==68
    er=rows(STAGE/'queries.selected.jsonl')+rows(OLD/'selection/frozen/test.queries.jsonl')
    near=[]
    for q in groups:
        a=key(q['query'])
        for e in er:
            et=e.get('query',e.get('text',''));b=key(et)
            assert a!=b,'Exact evaluation overlap'
            ratio=difflib.SequenceMatcher(None,a,b).ratio()
            if ratio>=.65 or (min(len(a),len(b))>=3 and (a in b or b in a)):
                near.append(dict(train=q['query'],evaluation=et,ratio=ratio))
    docs=documents({r['document_id'] for q in groups for r in q['pos']+q['neg']})
    packet=[]
    for q in groups:
        for field in ['pos','neg']:
            dedup={}
            for r in sorted(q[field],key=lambda r:r['document_id']):
                d=docs[r['document_id']];assert d['source']==q['source']
                assert hashlib.sha256(d['text'].encode()).hexdigest()==r['catalog_text_sha256']
                dedup.setdefault(key(d['text']),dict(**d,grade=r['grade'],reason=r.get('reason','')))
            q[field]=list(dedup.values())
        assert q['pos'] and len(q['neg'])>=2,'Dedup changes eligibility; must revise contract before training'
        assert not ({key(d['text']) for d in q['pos']}&{key(d['text']) for d in q['neg']})
        packet.append(dict(query=q['query'],source=q['source'],positive=q['pos'][0],negatives=q['neg'][:2]))
    jsonl(ROOT/'training-groups.jsonl',groups);jsonl(ROOT/'review-sample.jsonl',packet)
    save(ROOT/'near-query-review.json',near)
    devinputs=rows(OLD/'cross-encoder-final-v1/evaluation/dev/inputs.jsonl')
    qr=rows(OLD/'cross-encoder-final-v1/evaluation/dev/qrels-final.jsonl')
    qrels=collections.defaultdict(dict)
    for r in qr:qrels[r['query_id']][r['document_id']]=r['grade']
    dev=[];allids=set()
    for x in devinputs:
        p=read(OLD/f'development-grid-r3/pools/{x["query_id"]}.json')['result']
        ids=sorted(set(p['union'])|set(qrels[x['query_id']]))
        allids.update(ids);dev.append(dict(query_id=x['query_id'],query=x['query'],source=x['source'],ids=ids,qrels=qrels[x['query_id']]))
    dd=documents(allids)
    jsonl(ROOT/'dev.jsonl',dev);jsonl(ROOT/'dev-documents.jsonl',[dd[k] for k in sorted(dd)])
    save(ROOT/'PREPARED.json',dict(groups=len(groups),sources=dict(collections.Counter(q['source'] for q in groups)),
        near_query_pairs=len(near),dev_queries=len(dev),dev_unique_documents=len(dd),qrel_sha256=sha(OLD/'cross-encoder-final-v1/evaluation/dev/qrels-final.jsonl'),
        hashes={f:sha(ROOT/f) for f in ['training-groups.jsonl','dev.jsonl','dev-documents.jsonl']},scope='Silver labels; metadata-only historical overlap review'))
    print(json.dumps(read(ROOT/'PREPARED.json')),flush=True)
def mine():
    assert read(ROOT/'REVIEW.json')['approved_for_pilot']
    assert not (ROOT/'MINED.json').exists()
    review=read(ROOT/'REVIEW.json')
    groups=[q for q in rows(ROOT/'training-groups.jsonl') if q['query_id'] not in review.get('quarantined_query_ids',[])]
    jsonl(ROOT/'pilot-groups.jsonl',groups)
    tok,m=load(base());counts=collections.Counter(q['source'] for q in groups)
    tables={'random':[],'hard':[]};ranks=[];rng=random.Random(20260915)
    for q in groups:
        v=encode(tok,m,[PREFIX+q['query']]+[d['text'] for d in q['neg']])
        scores=v[1:]@v[0]; order=sorted(range(len(scores)),key=lambda i:(-float(scores[i]),q['neg'][i]['document_id']))
        hard=q['neg'][order[0]]
        ranks.append(dict(query=q['query'],negative_id=hard['document_id'],text=hard['text'],reason=hard['reason'],score=float(scores[order[0]])))
        for i in range(8):
            common=dict(query_id=q['query_id'],query=q['query'],source=q['source'],pos=q['pos'][i%len(q['pos'])],weight=len(groups)/(2*counts[q['source']]))
            tables['random'].append(dict(**common,neg=q['neg'][rng.randrange(len(q['neg']))]))
            tables['hard'].append(dict(**common,neg=hard))
    for arm,rr in tables.items():jsonl(ROOT/f'{arm}.jsonl',rr)
    jsonl(ROOT/'hard-negative-review.jsonl',ranks)
    changed=sum(a['neg']['document_id']!=b['neg']['document_id'] for a,b in zip(tables['random'],tables['hard']))
    assert changed>0
    save(ROOT/'MINED.json',dict(changed=changed,total=len(tables['hard']),group_sha256=sha(ROOT/'training-groups.jsonl'),sampling_seed=20260915,
        model=base(),model_sha256=sha(Path(base())/'model.safetensors'),code_sha256=sha(__file__),arm_hashes={a:sha(ROOT/f'{a}.jsonl') for a in tables}))
    print('MINED',changed,len(tables['hard']),flush=True)
def loss_values(tok,m,b):
    texts=[PREFIX+x['query'] for x in b]+[x['pos']['text'] for x in b]+[x['neg']['text'] for x in b]
    inputs=tok(texts,padding=True,truncation=True,max_length=128,return_tensors='pt').to('cuda')
    with torch.autocast('cuda',dtype=torch.bfloat16):
        v=torch.nn.functional.normalize(m(**inputs).last_hidden_state[:,0].float(),dim=-1)
        q,p,n=v.chunk(3)
        logits=torch.stack(((q*p).sum(-1),(q*n).sum(-1)),dim=1)/.02
        return torch.nn.functional.cross_entropy(logits,torch.zeros(len(b),device='cuda',dtype=torch.long),reduction='none')
def train(arm,seed,smoke=False):
    assert read(ROOT/'HARD_REVIEW.json')['approved_for_pilot']
    contract=read(ROOT/'RUN_CONTRACT.json')
    assert contract['code_sha256']==sha(__file__),'Code differs from frozen contract'
    for f,digest in contract['hashes'].items():assert sha(ROOT/f)==digest,f
    out=ROOT/'runs'/('smoke' if smoke else f'{arm}-{seed}')
    assert not out.exists(),'Never overwrite a started run'
    out.mkdir(parents=True)
    random.seed(seed);np.random.seed(seed);torch.manual_seed(seed);torch.cuda.manual_seed_all(seed)
    torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
    rr=rows(ROOT/f'{arm}.jsonl');tok,m=load(base());m.train()
    opt=torch.optim.AdamW(m.parameters(),lr=1e-5,weight_decay=.01)
    steps=32 if smoke else math.ceil(len(rr)/32)*3;start=time.monotonic();log=[];step=0
    before=next(m.parameters()).detach().float().cpu().clone()
    fixed=rr[:8];m.eval()
    with torch.no_grad():initial=float(loss_values(tok,m,fixed).mean())
    rng=random.Random(seed);torch.cuda.reset_peak_memory_stats();m.train()
    for epoch in range(100 if smoke else 3):
        indices=list(range(len(rr)));rng.shuffle(indices)
        for st in range(0,len(rr),32):
            elapsed=time.monotonic()-start
            if elapsed>(600 if smoke else 1800):raise TimeoutError('Frozen hard timeout')
            ids=indices[st:st+32];opt.zero_grad(set_to_none=True);batch_loss=0.
            for j in range(0,len(ids),4):
                b=[rr[k] for k in ids[j:j+4]]
                losses=loss_values(tok,m,b)
                value=(losses*torch.tensor([x['weight'] for x in b],device='cuda')).sum()/len(ids)
                assert torch.isfinite(value);value.backward();batch_loss+=float(value.detach())
            grad=float(torch.nn.utils.clip_grad_norm_(m.parameters(),1.0));assert math.isfinite(grad) and grad>0
            warm=max(1,math.ceil(steps*.1));scale=min(1.,(step+1)/warm)*max(0.,(steps-step)/(steps-warm)) if step>=warm else (step+1)/warm
            for pg in opt.param_groups:pg['lr']=1e-5*scale
            opt.step();step+=1
            log.append(dict(step=step,epoch=epoch+1,loss=batch_loss,grad_norm=grad,seconds=time.monotonic()-start))
            jsonl(out/'progress.jsonl',log)
            if step%10==0:print(arm,seed,step,round(batch_loss,4),round(time.monotonic()-start,1),flush=True)
            if step>=steps:break
        if step>=steps:break
    m.eval()
    with torch.no_grad():final=float(loss_values(tok,m,fixed).mean())
    drift=float((next(m.parameters()).detach().float().cpu()-before).abs().max());assert drift>0
    if not smoke:m.save_pretrained(out/'model');tok.save_pretrained(out/'model')
    save(out/'COMPLETE.json',dict(arm=arm,seed=seed,smoke=smoke,steps=step,seconds=time.monotonic()-start,peak_allocated_bytes=torch.cuda.max_memory_allocated(),
        fixed_batch_initial_loss=initial,fixed_batch_final_loss=final,embedding_weight_max_change=drift,training_sha256=sha(ROOT/f'{arm}.jsonl'),code_sha256=sha(__file__),
        config=dict(lr=1e-5,temperature=.02,microbatch=4,accumulation=8,epochs=3,source_weight='global per-source equal mean via fixed inverse-frequency weights',precision='BF16 autocast; FP32 parameters')))
    print('COMPLETE',str(out),flush=True)
def evaluate(name):
    out=ROOT/'evaluation'/name
    assert not out.exists(),'Frozen evaluation exists'
    out.mkdir(parents=True)
    path=base() if name=='base' else ROOT/'runs'/name/'model'
    tok,m=load(path);dd=rows(ROOT/'dev-documents.jsonl');dev=rows(ROOT/'dev.jsonl');start=time.monotonic()
    vv=encode(tok,m,[d['text'] for d in dd]);mapping={d['document_id']:i for i,d in enumerate(dd)}
    qv=encode(tok,m,[PREFIX+q['query'] for q in dev]);metrics=[];ranks=[]
    for i,q in enumerate(dev):
        scores=vv[[mapping[d] for d in q['ids']]]@qv[i]
        order=sorted(zip(q['ids'],scores),key=lambda x:(-float(x[1]),x[0]));labels=q['qrels']
        relevant={d for d,g in labels.items() if isinstance(g,int) and g>=2}
        grade3={d for d,g in labels.items() if g==3}
        known=[(d,s) for d,s in order if isinstance(labels.get(d),int)]
        gains=lambda did:(2**labels[did]-1)
        ideal=sorted([2**g-1 for g in labels.values() if isinstance(g,int)],reverse=True)[:10]
        idcg=sum(g/math.log2(j+2) for j,g in enumerate(ideal))
        ndcg=sum(gains(d)/math.log2(j+2) for j,(d,s) in enumerate(known[:10]))/idcg if idcg else None
        vals={f'recall{k}':len({d for d,s in order[:k]}&relevant)/len(relevant) if relevant else None for k in [100,300]}
        metrics.append(dict(query_id=q['query_id'],query=q['query'],source=q['source'],**vals,judged_only_ndcg10=ndcg,
            grade3_recall300=len({d for d,s in order[:300]}&grade3)/len(grade3) if grade3 else None,
            unjudged_top10=sum(not isinstance(labels.get(d),int) for d,s in order[:10]),pool_size=len(order)))
        ranks.append(dict(query_id=q['query_id'],ranking=[dict(document_id=d,score=float(s)) for d,s in order[:300]]))
    summary={}
    for metric in ['recall100','recall300','judged_only_ndcg10']:
        by={s:float(np.mean([r[metric] for r in metrics if r['source']==s and r[metric] is not None])) for s in ['kuaisearch','multicpr']}
        summary[metric]=dict(by_source=by,equal_source=float(np.mean(list(by.values()))))
    jsonl(out/'per-query.jsonl',metrics);jsonl(out/'rankings.jsonl',ranks)
    save(out/'REPORT.json',dict(name=name,seconds=time.monotonic()-start,summary=summary,
        unjudged_top10=sum(r['unjudged_top10'] for r in metrics),scope='Frozen development pool diagnostic; judged-only nDCG excludes unjudged before ranking and is NOT full-candidate nDCG or full-corpus recall',
        code_sha256=sha(__file__),dev_sha256=sha(ROOT/'dev.jsonl')))
    print(name,summary,flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','mine','smoke','train','evaluate']);p.add_argument('--arm',default='random',choices=['random','hard']);p.add_argument('--seed',type=int,default=20260915);p.add_argument('--name',default='base');a=p.parse_args()
    if a.mode=='prepare':prepare()
    elif a.mode=='mine':mine()
    elif a.mode in ['smoke','train']:train(a.arm,a.seed,a.mode=='smoke')
    else:evaluate(a.name)
