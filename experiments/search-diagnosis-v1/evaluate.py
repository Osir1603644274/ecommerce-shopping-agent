import hashlib,json,math,sqlite3,sys
from pathlib import Path
from run_plans import ROOT,REPO,verify,write
sys.path.insert(0,str(REPO))
from agent.app.catalog_fast_retrieval_v3 import FastRuntime
from agent.app.catalog_fast_retrieval import old

BASE=Path('D:/agent-datasets/catalog-latency-20260913-v1/dev-eval004')
MODEL='D:/agent-datasets/search-closure-v1/training-preparation/runs/pairwise-lora-v1/checkpoints/epoch-2'
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6/frozen')
def ndcg(ids,labels):
    if len(ids)<10 or any(type(labels.get(d)) is not int for d in ids[:10]):return None
    ideal=sorted([v for v in labels.values() if type(v) is int],reverse=True)[:10]
    den=sum((2**g-1)/math.log2(i+2) for i,g in enumerate(ideal))
    return sum((2**labels[d]-1)/math.log2(i+2) for i,d in enumerate(ids[:10]))/den if den else None
def main():
    verify();out=ROOT/'retrieval';out.mkdir(exist_ok=True)
    qs=json.loads((ROOT/'single-inputs.json').read_text('utf-8'))
    labels={q['query_id']:{} for q in qs}
    for l in (DEV/'qrels.jsonl').read_text('utf-8').splitlines():
        r=json.loads(l);labels[r['query_id']][r['document_id']]=r['grade']
    plans={q['query_id']:json.loads((ROOT/'single-plans'/(q['query_id']+'.json')).read_text('utf-8')) for q in qs}
    if not (out/'BASELINE-REFERENCES.json').exists():
        write(out/'BASELINE-REFERENCES.json',{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [BASE/'runtime.json']+[BASE/(q['query_id']+'.json') for q in qs]})
    rt=FastRuntime()
    try:
        binding=rt.strategy_manifest(profile='w211',model_path=MODEL)
        assert binding==json.loads((BASE/'runtime.json').read_text('utf-8')),'baseline runtime mismatch'
        if not (out/'runtime.json').exists():write(out/'runtime.json',binding)
        for q in qs:
            qid=q['query_id'];p=out/(qid+'.json')
            if p.exists():continue
            plan=plans[qid].get('plan') or {};baseline=json.loads((BASE/(qid+'.json')).read_text('utf-8'))
            assert baseline['query']['query']==q['query']
            ranks=baseline['ranking'];orig=[r['document_id'] for r in ranks];lab=labels[qid]
            row={'input':q,'plan':plan,'baselineMode':'hash_bound_saved_v3','baselineNdcg':ndcg(orig,lab)}
            def display(ids):
                text=rt.document_texts(q['source'],ids[:10])
                return [{'rank':i,'document_id':d,'title':text[d],'grade':lab.get(d,'ABSENT')} for i,d in enumerate(ids[:10],1)]
            row['baselineTop10']=display(orig)
            if plan.get('route')!='catalog' or plan.get('action') not in {'search','new','refine'}:
                row.update(status='NOT_SEARCHED_BY_PLANNER',rewrittenNdcg=None,delta=None)
            else:
                changed=plan['query']!=q['query']
                if changed:
                    recalled=rt.retrieve_batch(q['source'],[{'query_id':qid,'query':plan['query']}])
                    pool=old.weighted_rrf(recalled['channels'][qid],old.WEIGHTS['w211'])
                    texts=rt.document_texts(q['source'],[x['document_id'] for x in pool])
                    scores,timing=rt.score_pairs(MODEL,[[plan['query'],texts[x['document_id']]] for x in pool])
                    ranked=old.rerank_same_candidates(pool,{x['document_id']:s for x,s in zip(pool,scores)})
                    row.update(recall=recalled,ce=timing,ranking=ranked)
                else:ranked=ranks
                ids=[x['document_id'] for x in ranked];new=ndcg(ids,lab)
                row.update(status='EVALUATED',rewrittenMode='fresh_same_runtime' if changed else 'identical_query_reused',
                    rewrittenTop10=display(ids),rewrittenNdcg=new,
                    delta=new-row['baselineNdcg'] if new is not None and row['baselineNdcg'] is not None else None,
                    overlap10=len(set(ids[:10])&set(orig[:10]))/10,
                    judgedTop10=sum(type(lab.get(d)) is int for d in ids[:10]))
            write(p,row)
            print(json.dumps({'query':q['query'],'rewrite':plan.get('query'),'status':row['status'],'delta':row['delta'],'coverage':row.get('judgedTop10')},ensure_ascii=False),flush=True)
        # Stage tracing, not a new quality metric or policy experiment.
        for cid in ['M02-T2','M04-T2','M04-T4']:
            live=json.loads((ROOT/'multi-live'/(cid+'.json')).read_text('utf-8'))
            query=live['summary']['query']
            for source in ['kuaisearch','multicpr']:
                path=out/(cid+'-'+source+'-trace.json')
                if path.exists():continue
                qid=cid+'-'+source
                recalled=rt.retrieve_batch(source,[{'query_id':qid,'query':query}])
                pool=old.weighted_rrf(recalled['channels'][qid],old.WEIGHTS['w211'])
                texts=rt.document_texts(source,[x['document_id'] for x in pool])
                scores,timing=rt.score_pairs(MODEL,[[query,texts[x['document_id']]] for x in pool])
                ranked=old.rerank_same_candidates(pool,{x['document_id']:s for x,s in zip(pool,scores)})
                liveids=next(s for s in live['after']['catalogSearch']['scope']['sources'] if s['source']==source)['hits']
                record={'case':cid,'source':source,'query':query,'recall':recalled,'pool':pool,'texts':texts,'ranking':ranked,'ce':timing,
                    'liveTop10ExactlyReproduced':[r['document_id'] for r in ranked[:10]]==[h['docid'] for h in liveids]}
                write(path,record)
                print(json.dumps({'trace':qid,'liveTop10ExactlyReproduced':record['liveTop10ExactlyReproduced']}),flush=True)
    finally:rt.close();verify()
if __name__=='__main__':main()
