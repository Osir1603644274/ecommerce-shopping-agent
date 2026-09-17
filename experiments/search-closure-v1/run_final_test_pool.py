"""Once-frozen baseline/winner held-out retrieval; no test labels or selection.

Committed recall and CE outputs are reused after interruption. Model selection
and query-only interpretations must both predate the first candidate retrieval.
"""
import argparse
from collections import Counter,defaultdict
import json
import math
from pathlib import Path
import sys
from authorize_final_test import authorize
from retrieval_runtime import RetrievalRuntime,CHANNELS,SOURCES,WEIGHTS,weighted_rrf,rerank_same_candidates,model_binding,fingerprint,sha,read_json,write_once
from run_dev_grid import source_recalls,save_cached,verify_cached,ranking_row
from run_training_pool import actual_documents,validate_channels
from reviews import rows

ROOT=Path('D:/agent-datasets/search-closure-v1')
RUBRIC_SHA='404b2d8111ddf4373a9f1bc107690a60141ff103ed02ef4a6df475567eb2ad67'

def require(value,message):
    if not value:raise ValueError(message)

def choose_test_candidates(channels,rankings):
    required={r['document_id'] for values in [*channels.values(),*rankings.values()] for r in values[:10]}
    require(len(required)<=80,'Unexpected registered Top10 union exceeds fixed pool')
    universe=weighted_rrf(channels,WEIGHTS['w111'],depth=900)
    selected=set(required)
    for row in universe:
        if len(selected)==80:break
        selected.add(row['document_id'])
    require(len(selected)==80,'Cannot fill 80 distinct heldout candidates')
    result=[r for r in universe if r['document_id'] in selected]
    require(len(result)==80,'Selected model returned a document outside recall channels')
    return result

def verify_contracts(path,freeze_path,freeze_sha,queries,selection_sha):
    require(sha(freeze_path)==freeze_sha,'Test query interpretation freeze changed')
    freeze=read_json(freeze_path)
    require(freeze.get('status')=='TEST_QUERY_CONTRACTS_FROZEN_BEFORE_CANDIDATES'
            and freeze.get('query_count')==80 and freeze.get('model_selection_sha256')==selection_sha
            and freeze.get('query_contracts_sha256')==sha(path) and freeze.get('rubric_sha256')==RUBRIC_SHA,
            'Query-only test contracts were not frozen against selected configuration')
    from prepare_test_contracts import validate_proposal
    require(Path(freeze_path).resolve()==(ROOT/'test-preparation/QUERY_CONTRACTS_FROZEN.json').resolve()
            and Path(path).resolve()==(ROOT/'test-preparation/query-contracts-frozen.jsonl').resolve(),
            'Use original canonical test interpretation freeze')
    require(sha(freeze['proposal_path'])==freeze['proposal_sha256']==sha(path)
            and sha(freeze['author_review']['path'])==freeze['author_review']['sha256']
            and freeze['freezer_sha256']==sha(Path(__file__).with_name('prepare_test_contracts.py')),
            'Original query interpretation author chain changed')
    authored=read_json(freeze['author_review']['path'])
    require(authored.get('status')=='ROOT_READ_ALL80_ORIGINAL_TEST_QUERIES_BEFORE_CANDIDATES'
            and authored.get('proposal_sha256')==sha(path) and authored.get('model_selection_sha256')==selection_sha
            and authored.get('query_identity_sha256')==fingerprint(queries) and authored.get('human_gold') is False,
            'Missing actual bound test query-only review')
    contracts=rows(path);validate_proposal(contracts,queries);by={r['query_id']:r for r in contracts}
    require(len(contracts)==80 and set(by)=={q['query_id'] for q in queries},'Test interpretation coverage differs')
    for q in queries:
        c=by[q['query_id']];keys=c.get('required_attribute_keys',[])
        require(c.get('query')==q['query'] and c.get('source')==q['source'] and c.get('no_added_requirements') is True
                and keys and keys[0]=='本体' and len(keys)==len(set(keys))
                and c.get('intent_policy') in ('score_product_relevance','query_intent_ambiguous'),
                'Test query text/interpretation contract differs')
    return {'path':str(Path(path).resolve()),'sha256':sha(path),'freeze_path':str(Path(freeze_path).resolve()),'freeze_sha256':freeze_sha}

def execute(runtime,out,queries,binding,progress=None,*,cache_only=False):
    progress=progress or (lambda _:None)
    def publish(path,value,jsonl=False):
        if cache_only:
            require((rows(path) if jsonl else read_json(path))==value,'Heldout export differs from CPU replay')
        else:write_once(path,value,jsonl=jsonl)
    def cache(path,expected,value):
        if cache_only:require(verify_cached(path,expected)==value,'Heldout cache differs from CPU replay')
        else:save_cached(path,expected,value)
    require(len(queries)==80 and Counter(q['source'] for q in queries)=={s:40 for s in SOURCES},'Expected fixed80 test query split')
    require(set(binding['arms'])=={'baseline','winner'},'Only frozen baseline/winner may be tested')
    current=fingerprint(binding);publish(out/'binding.json',binding);publish(out/'asset-binding.json',runtime.asset)
    recalls={};pools={};texts={};model_inputs=defaultdict(dict);models={}
    for source in SOURCES:
        source_queries=[q for q in queries if q['source']==source]
        if not cache_only:recalls.update(source_recalls(runtime,out,source,source_queries,current,allow_inference=True))
        else:
            official={q['query_id']:q for q in source_queries}
            for q in source_queries:
                value=verify_cached(out/'recall'/source/(q['query_id']+'.json'),{'grid_sha256':current,'query':q})
                require(sha(value['batch_receipt'])==value['batch_sha256'],'Heldout recall batch changed')
                batch=read_json(value['batch_receipt']);members=batch['binding']['queries']
                require(members and len({v['query_id'] for v in members})==len(members)
                        and all(official.get(v['query_id'])==v for v in members),'Foreign heldout recall query')
                actual=verify_cached(value['batch_receipt'],{'grid_sha256':current,'source':source,'queries':members})
                require(actual['channels'][q['query_id']]==value['channels'],'Heldout channel cache changed')
                recalls[q['query_id']]=value
    files={out/'binding.json',out/'asset-binding.json'}
    for query in queries:
        qid=query['query_id'];channels=recalls[qid]['channels'];validate_channels(channels,query['source'])
        pools[qid]={}
        for arm,spec in binding['arms'].items():
            profile=spec['profile']
            pools[qid][arm]=channels[profile] if profile in CHANNELS else weighted_rrf(channels,WEIGHTS[profile])
            if spec['model'] is not None:
                key=fingerprint(spec['model']);models[key]=spec['model']
                model_inputs[key].setdefault(qid,set()).update(r['document_id'] for r in pools[qid][arm])
        union=sorted({r['document_id'] for pool in pools[qid].values() for r in pool})
        texts[qid]=runtime.document_texts(query['source'],union)
        path=out/'pools'/(qid+'.json')
        cache(path,{'test_binding_sha256':current,'query':query},{'arms':pools[qid],
            'document_text_sha256':{d:fingerprint(texts[qid][d]) for d in union},'recall_batch_sha256':recalls[qid]['batch_sha256']})
        files.update([path,Path(recalls[qid]['batch_receipt']),out/'recall'/query['source']/(qid+'.json')])
    # Model outer loop prevents repeatedly swapping adapters between queries.
    scored={}
    for key,model in sorted(models.items()):
        scored[key]={}
        for query in queries:
            qid=query['query_id']
            if qid not in model_inputs[key]:continue
            ids=sorted(model_inputs[key][qid]);pairs=[[query['query'],texts[qid][d]] for d in ids]
            expected={'test_binding_sha256':current,'query':query,'model':model,'document_ids':ids,'input_sha256':fingerprint(pairs)}
            path=out/'scores'/key/(qid+'.json')
            if not path.exists():
                require(not cache_only,'CPU verifier cannot infer missing heldout scores')
                values,timing=runtime.score_pairs(model['path'],pairs)
                require(len(values)==len(ids) and all(math.isfinite(v) for v in values),'Invalid actual CE scores')
                require(timing['model_binding']==model and timing['input_sha256']==fingerprint(pairs),'Actual CE receipt differs')
                cache(path,expected,{'scores':[{'document_id':d,'score':v} for d,v in zip(ids,values)],'timings':timing})
                progress({'stage':'frozen_test_ce_scored','model_binding':key,'query_id':qid,'pairs':len(ids)})
            value=verify_cached(path,expected)
            require(len(value['scores'])==len(ids) and {r['document_id'] for r in value['scores']}==set(ids),'Incomplete saved scores')
            scored[key][qid]={r['document_id']:r['score'] for r in value['scores']};files.add(path)
    candidates=[];rankings=[];scores_export=[]
    for query in queries:
        qid=query['query_id'];channels=recalls[qid]['channels'];arm_rankings={}
        for arm,spec in binding['arms'].items():
            model=spec['model'];pool=pools[qid][arm]
            arm_rankings[arm]=rerank_same_candidates(pool,scored[fingerprint(model)][qid]) if model is not None else pool
            rankings.append(ranking_row(arm,query,arm_rankings[arm]))
        for channel in CHANNELS:rankings.append(ranking_row(channel,query,channels[channel]))
        selected=choose_test_candidates(channels,arm_rankings)
        actual=actual_documents(runtime.catalog,query['source'],[r['document_id'] for r in selected])
        for row in selected:
            did=row['document_id'];document=actual[did]
            candidates.append({**query,'document_id':did,'catalog_text':document['catalog_text'],'document':document['document'],
                'provenance':{'rrf_rank':row['rank'],'rrf_score':row['score'],**document['catalog_provenance'],
                    'test_binding_sha256':current,'top10_methods':[name for name,values in {**channels,**arm_rankings}.items() if did in {r['document_id'] for r in values[:10]}]}})
    for key,byquery in sorted(scored.items()):
        for qid,values in sorted(byquery.items()):
            for did,score in sorted(values.items()):scores_export.append({'model_binding':key,'query_id':qid,'document_id':did,'score':score})
    require(len(candidates)==6400 and len({(r['query_id'],r['document_id']) for r in candidates})==6400,'Incomplete held-out candidate pool')
    publish(out/'rankings.jsonl',rankings,jsonl=True);publish(out/'scores.jsonl',scores_export,jsonl=True)
    publish(out/'candidate-rows.jsonl',candidates,jsonl=True)
    report={'status':'FINAL_TEST_CANDIDATE_POOL_READY_UNJUDGED','query_count':80,'candidate_count':6400,
        'candidate_rows_path':str(out/'candidate-rows.jsonl'),'candidate_rows_sha256':sha(out/'candidate-rows.jsonl'),
        'query_contracts_path':binding['contracts']['path'],'query_contracts_sha256':binding['contracts']['sha256'],
        'binding_sha256':sha(out/'binding.json'),'rankings_sha256':sha(out/'rankings.jsonl'),'scores_sha256':sha(out/'scores.jsonl'),
        'files':[{'path':str(p),'sha256':sha(p)} for p in sorted(files)],
        'quality_comparison_arms':['baseline','winner'],'raw_rankings_scope':'pool provenance only, never heldout selection',
        'labels_generated':False,'models_changed_after_test_access':False,'latency_scope':'batch wall, not online P95'}
    publish(out/'POOL_COMPLETE.json',report);return report

def verify_pool_provenance(path,expected_sha):
    from retrieval_runtime import readonly,verify_asset_audit
    path=Path(path).resolve();out=path.parent
    require(path==(ROOT/'test-pool/POOL_COMPLETE.json').resolve() and sha(path)==expected_sha,'Heldout pool receipt differs')
    receipt=read_json(path)
    require(sha(out/'binding.json')==receipt['binding_sha256'],'Heldout binding changed')
    binding=read_json(out/'binding.json');ref=binding['authority']['model_selection']
    queries,choice,proof=authorize(ref['path'],ref['sha256'])
    require(proof==binding['authority'] and fingerprint(queries)==binding['queries_sha256'],'Heldout original queries/selection changed')
    contracts=binding['contracts'];verify_contracts(contracts['path'],contracts['freeze_path'],contracts['freeze_sha256'],queries,ref['sha256'])
    for name,field in [('baseline','strongest_old'),('winner','selected')]:
        actual={**choice[field],'model':model_binding(choice[field]['model_path']) if choice[field]['model_path'] else None}
        require(actual==binding['arms'][name],'Heldout arm changed')
    for p,wanted in binding['code'].items():require(sha(p)==wanted,'Heldout producer code changed')
    for item in receipt['files']:
        require(Path(item['path']).resolve().is_relative_to(out) and sha(item['path'])==item['sha256'],'Heldout evidence file changed')
    asset=verify_asset_audit();require(asset==binding['asset'],'Heldout source assets changed')
    class Runtime:
        def __init__(self):
            self.asset=asset;self.catalog=readonly('D:/agent-datasets/search-stage1-v1/catalog.sqlite')
        def document_texts(self,source,ids):return {d:r['catalog_text'] for d,r in actual_documents(self.catalog,source,ids).items()}
        def score_pairs(self,*args):raise ValueError('Read-only heldout verifier cannot call models')
    runtime=Runtime()
    try:actual=execute(runtime,out,queries,binding,cache_only=True)
    finally:runtime.catalog.close()
    require(actual==receipt,'Heldout producer replay differs')
    return {'status':'CPU_FINAL_TEST_POOL_PROVENANCE_PASS','queries':80,'pairs':6400,'model_calls':0,'files_written':0}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--selection',type=Path,default=ROOT/'final-selection/SELECTION.json');p.add_argument('--selection-sha256',required=True)
    p.add_argument('--query-contracts',type=Path,required=True);p.add_argument('--contracts-freeze',type=Path,required=True);p.add_argument('--contracts-freeze-sha256',required=True)
    a=p.parse_args();queries,choice,authority=authorize(a.selection,a.selection_sha256)
    contracts=verify_contracts(a.query_contracts,a.contracts_freeze,a.contracts_freeze_sha256,queries,a.selection_sha256)
    runtime=RetrievalRuntime(progress=lambda r:print(json.dumps(r),flush=True))
    try:
        arms={name:{**choice[field],'model':model_binding(choice[field]['model_path']) if choice[field]['model_path'] else None} for name,field in [('baseline','strongest_old'),('winner','selected')]}
        binding={'version':'frozen-final-test-v1','authority':authority,'contracts':contracts,'queries_sha256':fingerprint(queries),
            'arms':arms,'asset':runtime.asset,'code':{str(p.resolve()):sha(p) for p in [Path(__file__),Path(__file__).with_name('authorize_final_test.py'),Path(__file__).with_name('retrieval_runtime.py'),Path(__file__).with_name('run_dev_grid.py'),Path(__file__).with_name('run_training_pool.py')]},
            'query_rewrite':False,'depth':300,'top10_union_fill':80,'labels_read':False}
        result=execute(runtime,ROOT/'test-pool',queries,binding,progress=lambda r:print(json.dumps(r),flush=True))
        print(json.dumps({k:result[k] for k in ['status','query_count','candidate_count','candidate_rows_sha256']}))
    finally:runtime.close()

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');main()
