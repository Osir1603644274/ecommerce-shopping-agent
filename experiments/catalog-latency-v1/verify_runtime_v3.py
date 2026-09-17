"""Verify full concurrent recall and CE against the approved v2 outputs."""
import json
from pathlib import Path
import statistics
import evaluate_fast as evaluation
from agent.app.catalog_fast_retrieval_v3 import FastRuntime

old=evaluation.old;root=evaluation.ROOT;out=root/'dev-eval004'
selected=old.read_json(root/'selected-v2.json');evaluation.ARMS={'conservative':selected['parameters']}
queries,baseline,rankings,labels=evaluation.prepare(out)
assert old.read_json(root/'sqlite-eval001/RESULTS.json')['status']=='PASS'
assert old.read_json(root/'sqlite-eval003/RESULTS.json')['status']=='PASS'
runtime=FastRuntime(parameters=selected['parameters']);records=[]
evaluation.write(out/'runtime.json',runtime.strategy_manifest(profile='w211',model_path=evaluation.MODEL))
try:
    for q in queries:
        qid=q['query_id'];source=q['source']
        recalled=runtime.retrieve_batch(source,[q]);channels=recalled['channels'][qid]
        assert all(channels[c]==baseline[qid][c] for c in ['bm25','character']),'lexical changed'
        assert channels['dense']==old.read_json(root/'dev-eval003'/(qid+'-small.json'))['hits'],'dense changed'
        pool=old.weighted_rrf(channels,old.WEIGHTS['w211'])
        texts=runtime.document_texts(source,[x['document_id'] for x in pool])
        scores,timing=runtime.score_pairs(evaluation.MODEL,[[q['query'],texts[x['document_id']]] for x in pool])
        ranked=old.rerank_same_candidates(pool,{x['document_id']:s for x,s in zip(pool,scores)})
        prior=old.read_json(root/'dev-eval003'/(qid+'-ce.json'))
        assert [x['document_id'] for x in ranked[:10]]==[x['document_id'] for x in prior['ranking'][:10]],'top10 order changed'
        row={'query':q,'ranking':ranked,'recall':recalled['timings'],'ce':timing,'channelsExactlyEqualV2':True,'top10ExactlyEqualV2':True}
        records.append(row);evaluation.write(out/(qid+'.json'),row)
        print(json.dumps({'query':len(records),'exactChannels':True,'exactTop10':True,'recallSeconds':recalled['timings']['wall_seconds']}),flush=True)
    report=old.read_json(root/'dev-eval003/RESULTS.json')
    report.update(status='DEV_GATE_PASS',chosen='v2_small_parallel_mmap',verifiedConcurrentQueries=len(records),
        fullChannelsExactlyEqualV2=True,top10OrderExactlyEqualV2=True,
        qualityReference={'file':'dev-eval003/RESULTS.json','sha256':old.sha(root/'dev-eval003/RESULTS.json')},
        storageChecks={n:old.sha(root/n/'RESULTS.json') for n in ['sqlite-eval001','sqlite-eval003']})
    evaluation.write(out/'RESULTS.json',report)
finally:runtime.close()
