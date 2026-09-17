"""Chunked semantic memory retrieval, selection only on development labels."""
import argparse,collections,json,time
import history as h

def run(partition):
    import numpy as np
    import torch
    from transformers import AutoTokenizer,AutoModel
    torch.set_num_threads(2)
    tokenizer=AutoTokenizer.from_pretrained(h.OUT/'model',local_files_only=True)
    device='cuda' if torch.cuda.is_available() else 'cpu'
    model=AutoModel.from_pretrained(h.OUT/'model',local_files_only=True,use_safetensors=True).to(device).eval()
    if device=='cuda':model=model.half()
    def encode(tokenlists):
        vec=[]
        with torch.inference_mode():
            for start in range(0,len(tokenlists),16):
                inputs=tokenizer.pad([{'input_ids':tokenizer.build_inputs_with_special_tokens(ids)} for ids in tokenlists[start:start+16]],padding=True,return_tensors='pt').to(device)
                out=model(**inputs).last_hidden_state.float();mask=inputs['attention_mask'].unsqueeze(-1).float()
                pooled=(out*mask).sum(1)/mask.sum(1).clamp(min=1)
                vec.append(torch.nn.functional.normalize(pooled,p=2,dim=1).cpu().numpy())
        return np.concatenate(vec)
    for meta in h.read(h.OUT/'DATASET.json')['public_tasks']:
        if meta['partition']!=partition:continue
        dest=h.OUT/'semantic'/f"{meta['id']}.json"
        if dest.exists():continue
        task=h.read(h.OUT/'public'/f"{meta['id']}.json");start=time.perf_counter();chunkids=[];tokens=[]
        for session in task['sessions']:
            ids=tokenizer.encode(session['text'],add_special_tokens=False,truncation=False)
            for pos in range(0,len(ids),180):
                tokens.append(ids[pos:pos+220]);chunkids.append(session['id'])
                if pos+220>=len(ids):break
        embeddings=encode(tokens);index_seconds=time.perf_counter()-start
        index=h.OUT/'semantic/index';index.mkdir(parents=True,exist_ok=True)
        np.save(index/f"{meta['id']}.npy",embeddings)
        h.write(index/f"{meta['id']}.json",{'chunk_session_ids':chunkids,'public_sha256':h.sha(json.dumps(task,sort_keys=True))})
        begin=time.perf_counter();q=encode([tokenizer.encode(task['query'],add_special_tokens=False)[:220]])[0]
        scores={}
        for sid,score in zip(chunkids,embeddings@q):scores[sid]=max(scores.get(sid,-1),float(score))
        ranked=sorted(scores,key=lambda x:(-scores[x],x));dense=ranked[:20]
        bm=[x['id'] for x in h.read(h.OUT/'retrieval'/f"{meta['id']}.json")['ranked']]
        fused=collections.Counter()
        for order in [bm,dense]:
            for i,sid in enumerate(order):fused[sid]+=1/(60+i+1)
        hybrid=sorted(fused,key=lambda x:(-fused[x],x))[:20]
        h.write(dest,{'id':meta['id'],'dense':[{'id':sid,'cosine':scores[sid]} for sid in dense],'hybrid':[{'id':sid,'rrf':fused[sid]} for sid in hybrid],'chunks':len(tokens),'index_seconds':index_seconds,'query_seconds':time.perf_counter()-begin,'device':device,'precision':'fp16' if device=='cuda' else 'fp32'})
        print(partition,meta['query'],'chunks',len(tokens),'seconds',round(time.perf_counter()-start,1),flush=True)

def select():
    assert not (h.OUT/'SEMANTIC_SELECTION.json').exists()
    gold={x['id']:x['injected_session_id'] for x in h.read(h.OUT/'private/injection_map.json')}
    scores=collections.Counter({'bm25':0,'dense':0,'hybrid':0});details=[]
    for meta in h.read(h.OUT/'DATASET.json')['public_tasks']:
        if meta['partition']!='development':continue
        r=h.read(h.OUT/'semantic'/f"{meta['id']}.json")
        orders={'bm25':h.read(h.OUT/'retrieval'/f"{meta['id']}.json")['ranked'],'dense':r['dense'],'hybrid':r['hybrid']}
        ranks={}
        for method,order in orders.items():
            ids=[x['id'] for x in order];rank=ids.index(gold[meta['id']])+1 if gold[meta['id']] in ids else None
            ranks[method]=rank;scores[method]+=rank is not None and rank<=5
        details.append({'id':meta['id'],'query':meta['query'],'ranks':ranks})
    selected=max(['bm25','dense','hybrid'],key=lambda x:scores[x])
    h.write(h.OUT/'SEMANTIC_SELECTION.json',{'criterion':'development injected Hit@5; tie priority bm25,dense,hybrid','counts':dict(scores),'selected':selected,'details':details})
    print('selection',selected,dict(scores),flush=True)

def score():
    selection=h.read(h.OUT/'SEMANTIC_SELECTION.json');gold={x['id']:x['injected_session_id'] for x in h.read(h.OUT/'private/injection_map.json')};counts=collections.defaultdict(collections.Counter);details=[]
    for meta in h.read(h.OUT/'DATASET.json')['public_tasks']:
        r=h.read(h.OUT/'semantic'/f"{meta['id']}.json")
        order=r[selection['selected']] if selection['selected']!='bm25' else h.read(h.OUT/'retrieval'/f"{meta['id']}.json")['ranked']
        ids=[x['id'] for x in order];rank=ids.index(gold[meta['id']])+1 if gold[meta['id']] in ids else None
        c=counts[meta['partition']];c['tasks']+=1
        for k in [1,3,5,10,20]:c[f'hit@{k}']+=rank is not None and rank<=k
        details.append({'id':meta['id'],'query':meta['query'],'rank':rank})
    h.write(h.OUT/'SELECTED_MEMORY_SCORES.json',{'selected':selection['selected'],'counts':{k:dict(v) for k,v in counts.items()},'details':details})
    print(json.dumps({k:dict(v) for k,v in counts.items()}),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['development','fixed_regression','select','score']);a=p.parse_args()
    if a.phase in ['development','fixed_regression']:
        if a.phase=='fixed_regression':assert (h.OUT/'SEMANTIC_SELECTION.json').exists()
        run(a.phase)
    else:globals()[a.phase]()
