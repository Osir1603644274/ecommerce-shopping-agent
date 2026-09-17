"""Frozen dev ANN sweep followed by unchanged CE and judged-pool metrics."""
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from agent.app.catalog_fast_retrieval import FastRuntime,old,lexical_query

ROOT=Path('D:/agent-datasets/catalog-latency-20260913-v1')
GRID=Path('D:/agent-datasets/search-closure-v1/development-grid-r3')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6/frozen')
MODEL='D:/agent-datasets/search-closure-v1/training-preparation/runs/pairwise-lora-v1/checkpoints/epoch-2'
ARMS={
 'fast':{'kuaisearch':{'nprobe':32,'candidates':4096},'multicpr':{'nprobe':16,'candidates':2048}},
 'medium':{'kuaisearch':{'nprobe':64,'candidates':8192},'multicpr':{'nprobe':32,'candidates':4096}},
 'conservative':{'kuaisearch':{'nprobe':128,'candidates':16384},'multicpr':{'nprobe':64,'candidates':8192}},
 'high':{'kuaisearch':{'nprobe':256,'candidates':32768},'multicpr':{'nprobe':128,'candidates':16384}},
}


def write(path,value):
    with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def prepare(out):
    out.mkdir(parents=True,exist_ok=False)
    queries=[json.loads(l) for l in (DEV/'queries.jsonl').read_text('utf-8').splitlines()]
    baseline={};refs={}
    for q in queries:
        source='kuaisearch' if q['query_id'].startswith('s1-ku-') else 'multicpr';q['source']=source
        path=GRID/'recall'/source/(q['query_id']+'.json');value=old.read_json(path)
        assert value['binding']['query']['query']==q['query']
        assert old.fingerprint(value['result'])==value['result_sha256']
        baseline[q['query_id']]=value['result']['channels'];refs[str(path)]=old.sha(path)
    rankings={r['query_id']:r['ranking'] for r in map(json.loads,(GRID/'rankings.jsonl').read_text('utf-8').splitlines()) if r['method']=='w211/new_epoch2'}
    assert len(queries)==40 and len(rankings)==40
    labels={q['query_id']:{} for q in queries}
    for line in (DEV/'qrels.jsonl').read_text('utf-8').splitlines():
        r=json.loads(line);labels[r['query_id']][r['document_id']]=r['grade']
    refs.update({str(p):old.sha(p) for p in [DEV/'queries.jsonl',DEV/'qrels.jsonl',GRID/'rankings.jsonl',Path(MODEL)/'adapter_model.safetensors'] if p.exists()})
    write(out/'CONTRACT.json',{'queries':queries,'refs':refs,'arms':ARMS,'lexical':'same FTS5 scores and complete tie boundary',
        'qualityGates':{'meanDenseRecall300':.98,'minimumDenseRecall300':.90,'mainNdcgMeanDeltaAtLeast':-.003},
        'model':MODEL,'profile':'w211','depth':300,'scope':'exposed development silver, no test or labels modified'})
    return queries,baseline,rankings,labels


def ndcg(ranking,labels):
    if any(type(labels.get(d)) is not int for d in ranking[:10]):return None
    ideal=sorted([v for v in labels.values() if type(v) is int],reverse=True)[:10]
    denominator=sum((2**g-1)/math.log2(i+2) for i,g in enumerate(ideal))
    if not denominator:return None
    return sum((2**labels[d]-1)/math.log2(i+2) for i,d in enumerate(ranking[:10]))/denominator


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default='dev-eval001');args=p.parse_args()
    out=ROOT/args.out;queries,baseline,rankings,labels=prepare(out)
    runtime=FastRuntime(parameters=ARMS['conservative'])
    write(out/'runtime.json',runtime.strategy_manifest(profile='w211',model_path=MODEL))
    dense_records=[];lexical={}
    try:
        for position,q in enumerate(queries):
            qid=q['query_id'];source=q['source'];vec,_=runtime._query_vectors([q]);truth={x['document_id'] for x in baseline[qid]['dense']}
            channels,timing=lexical_query(runtime.indexes[source],runtime.catalog,source,q['query'])
            same=all(channels[k]==baseline[qid][k] for k in ['bm25','character'])
            if not same:raise ValueError('lexical_order_or_score_changed:'+qid)
            lexical[qid]={'channels':channels,'timings':timing,'exact_match':same}
            names=list(ARMS);names=names[position%len(names):]+names[:position%len(names)]
            for name in names:
                began=time.perf_counter();hits,details=runtime.dense_query(source,vec[0],ARMS[name])
                record={'query_id':qid,'source':source,'arm':name,'seconds':time.perf_counter()-began,
                        'recall300':len(truth&{x['document_id'] for x in hits})/300,'hits':hits,'timings':details}
                dense_records.append(record)
                write(out/(qid+'-'+name+'.json'),record)
            print(json.dumps({'query':position+1,'denseRecall':{r['arm']:round(r['recall300'],4) for r in dense_records[-4:]},'lexicalSame':same}),flush=True)
        summary={name:{'meanRecall300':statistics.mean(r['recall300'] for r in dense_records if r['arm']==name),
            'minRecall300':min(r['recall300'] for r in dense_records if r['arm']==name),
            'meanSeconds':statistics.mean(r['seconds'] for r in dense_records if r['arm']==name)} for name in ARMS}
        write(out/'DENSE-SUMMARY.json',summary);write(out/'LEXICAL.json',lexical)
        eligible=[name for name in ARMS if summary[name]['meanRecall300']>=.98 and summary[name]['minRecall300']>=.90]
        if not eligible:
            write(out/'RESULTS.json',{'status':'DENSE_QUALITY_GATE_FAILED','summary':summary});return
        chosen=min(eligible,key=lambda name:summary[name]['meanSeconds']);write(out/'DENSE-SELECTED.json',{'arm':chosen,'parameters':ARMS[chosen]})
        scored=[]
        for q in queries:
            qid=q['query_id'];source=q['source'];channels=dict(lexical[qid]['channels'])
            channels['dense']=next(r['hits'] for r in dense_records if r['query_id']==qid and r['arm']==chosen)
            pool=old.weighted_rrf(channels,old.WEIGHTS['w211']);texts=runtime.document_texts(source,[x['document_id'] for x in pool])
            scores,timing=runtime.score_pairs(MODEL,[[q['query'],texts[x['document_id']]] for x in pool])
            ranked=old.rerank_same_candidates(pool,{x['document_id']:s for x,s in zip(pool,scores)});ids=[x['document_id'] for x in ranked]
            prior=rankings[qid];newmetric=ndcg(ids,labels[qid]);oldmetric=ndcg(prior,labels[qid])
            result={'query':q,'ranking':ranked,'baselineTop10':prior[:10],'top10Overlap':len(set(ids[:10])&set(prior[:10]))/10,
                'oldNdcg':oldmetric,'newNdcg':newmetric,'newUnjudgedTop10':[d for d in ids[:10] if type(labels[qid].get(d)) is not int],
                'delta':newmetric-oldmetric if newmetric is not None and oldmetric is not None else None,'ceTiming':timing}
            scored.append(result);write(out/(qid+'-ce.json'),result)
            print(json.dumps({'ceQuery':len(scored),'overlap':result['top10Overlap'],'delta':result['delta'],'unjudged':len(result['newUnjudgedTop10'])}),flush=True)
        main=[r for r in scored if r['query']['cohort']=='main'];judged=[r for r in main if r['delta'] is not None]
        delta=statistics.mean(r['delta'] for r in judged) if judged else None
        unresolved=[r['query']['query_id'] for r in main if r['newUnjudgedTop10'] and r['top10Overlap']<1]
        passed=delta is not None and delta>=-.003 and not unresolved
        write(out/'RESULTS.json',{'status':'DEV_GATE_PASS' if passed else 'DEV_REVIEW_REQUIRED','chosen':chosen,'parameters':ARMS[chosen],
            'dense':summary,'mainQueries':len(main),'fullyJudgedPairedQueries':len(judged),'pairedMeanNdcgDelta':delta,
            'meanMainTop10Overlap':statistics.mean(r['top10Overlap'] for r in main),'unjudgedChangedQueries':unresolved,
            'interpretation':'NDCG only for both top10 fully judged on fixed silver qrels; absent labels never treated as zero'})
    finally:runtime.close()


if __name__=='__main__':main()
