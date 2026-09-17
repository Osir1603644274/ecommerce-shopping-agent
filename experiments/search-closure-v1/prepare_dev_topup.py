"""One fixed all-method Top10 supplement, only after the training cycle is decided.

No model calls or semantic judgments. Original v6 base labels remain immutable.
"""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
import json
from pathlib import Path
from retrieval_runtime import sha,read_json,write_once,readonly
from reviews import rows
import baseline_eval as evaluation

ROOT=Path('D:/agent-datasets/search-closure-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6')
CATALOG=Path('D:/agent-datasets/search-stage1-v1/catalog.sqlite')

def verified_json(path,expected):
    if sha(path)!=expected:raise ValueError('Bound file changed: '+str(path))
    return read_json(path)

def verify_training_cycle(path,expected,models):
    decision=verified_json(path,expected)
    if decision.get('status')!='TRAINING_CYCLE_DECIDED':raise ValueError('Training cycle is not decided')
    gate=verified_json(decision['gate']['path'],decision['gate']['sha256'])
    base={'base','epoch1','epoch2','epoch3'}
    branch=decision.get('branch')
    if branch=='not_triggered':
        if gate.get('triggered') is not False or gate.get('status')!='TRAINING_NOT_TRIGGERED':raise ValueError('False training skip')
        required=base
    elif branch=='insufficient_new_training_queries':
        prepared=verified_json(decision['prepared']['path'],decision['prepared']['sha256'])
        if (gate.get('triggered') is not True or prepared.get('status')!='INSUFFICIENT_TRAINING_QUERIES'
            or prepared['valid_query_count']>=50 or prepared['gate_sha256']!=decision['gate']['sha256']):
            raise ValueError('Training insufficiency not supported')
        required=base
    elif branch=='three_epochs_completed':
        completion_path=Path(decision['training_complete']['path'])
        complete=verified_json(completion_path,decision['training_complete']['sha256'])
        if gate.get('triggered') is not True or complete.get('status')!='FIXED_THREE_EPOCH_TRAINING_COMPLETE':
            raise ValueError('Training not completed')
        from train_pairwise import verify_checkpoint
        for epoch in (1,2,3):
            ckpt=completion_path.parent/'checkpoints'/f'epoch-{epoch}'
            verify_checkpoint(ckpt,complete['config_sha256'],expected_epoch=epoch)
            if sha(ckpt/'complete.json')!=complete['checkpoint_receipts'][str(epoch)]:raise ValueError('Epoch receipt changed')
            if Path(models[f'new_epoch{epoch}']['path']).resolve()!=ckpt.resolve():raise ValueError('Grid uses wrong trained epoch')
        required=base|{'new_epoch1','new_epoch2','new_epoch3'}
    else:raise ValueError('Unknown training branch')
    if set(models)!=required:raise ValueError('Not the complete pre-registered model grid')
    return decision

def topup_members(queries,rankings,base_rows):
    covered=defaultdict(set)
    for row in base_rows:
        if row['query_id'] not in queries:raise ValueError('Base contains foreign query')
        if row['document_id'] in covered[row['query_id']]:raise ValueError('Duplicate base pair')
        covered[row['query_id']].add(row['document_id'])
    members=defaultdict(lambda:defaultdict(list))
    for method,query_ranks in rankings.items():
        if set(query_ranks)!=set(queries):raise ValueError('Methods differ in query membership')
        for qid,ranking in query_ranks.items():
            for rank,did in enumerate(ranking[:10],1):
                if did not in covered[qid]:members[qid][did].append({'method':method,'rank':rank})
    return members

def build(args, *, verify_only=False):
    grid=Path(args.grid).resolve();out=ROOT/'development-topup'
    def publish(path,value,jsonl=False):
        if verify_only:
            actual=rows(path) if jsonl else read_json(path)
            if actual!=value:raise ValueError('Top-up differs from original-grid/catalog CPU replay')
        else:write_once(path,value,jsonl=jsonl)
    complete=verified_json(grid/'COMPLETE.json',args.grid_complete_sha256)
    binding=verified_json(grid/'binding.json',complete['binding_sha256'])
    if complete.get('status')!='COMPLETE' or complete.get('query_count')!=40 or binding.get('test_access') is not False:
        raise ValueError('Requires actual complete development grid')
    decision=verify_training_cycle(args.training_decision,args.training_decision_sha256,binding['models'])
    expected_methods={'bm25','character','dense'}|{f'{profile}/{model}' for profile in ('w111','w211','w112','no_dense') for model in ['none',*binding['models']]}
    if set(complete['methods'])!=expected_methods:raise ValueError('Missing or unregistered grid methods')
    for item in complete['files']:
        path=Path(item['path']).resolve()
        if not path.is_relative_to(grid) or sha(path)!=item['sha256']:raise ValueError('Grid artifact changed')
    if sha(grid/'scores.jsonl')!=complete['scores_sha256']:raise ValueError('Grid scores changed')
    queries=evaluation.load_queries(rows(evaluation.QUERIES))
    if sha(evaluation.QUERIES)!=evaluation.FIXED_QUERY_METADATA_SHA256:raise ValueError('Dev metadata changed')
    if sha(grid/'rankings.jsonl')!=complete['rankings_sha256']:raise ValueError('Rankings changed')
    rankings=evaluation.load_rankings(rows(grid/'rankings.jsonl'),queries,methods=complete['methods'])
    base_complete=read_json(DEV/'frozen/base/COMPLETE.json')
    if base_complete.get('status')!='V6_BASE_FROZEN_MODEL_SILVER':raise ValueError('Complete v6 base required')
    base_path=DEV/'frozen/base/qrels.jsonl'
    if sha(base_path)!=base_complete['qrels_sha256']:raise ValueError('Base qrels changed')
    base_rows=rows(base_path)
    if len(base_rows)!=3674:raise ValueError('Incomplete base')
    members=topup_members(queries,rankings,base_rows)
    contracts_path=DEV/'policy/query-contracts.jsonl';policy=read_json(DEV/'policy/FROZEN.json')
    if sha(contracts_path)!=policy['query_contracts_sha256']:raise ValueError('Query interpretations changed after labels')
    audit=read_json(ROOT/'assets/files.json')
    catalog_entries=[r for r in audit['files'] if Path(r['path']).resolve()==CATALOG.resolve()]
    if len(catalog_entries)!=1 or sha(CATALOG)!=catalog_entries[0]['sha256']:raise ValueError('Full catalog no longer matches the audited pool source')
    candidates=[];provenance=[]
    with readonly(CATALOG) as catalog:
        for qid,docs in sorted(members.items()):
            q=queries[qid]
            for did,origins in sorted(docs.items()):
                row=catalog.execute('SELECT source,text,payload,source_line FROM documents WHERE docid=?',(did,)).fetchone()
                if row is None or row[0]!=q['source']:raise ValueError('Top10 document absent or from wrong corpus')
                payload=json.loads(row[2])
                document={k:payload.get(k,[] if k=='categories' else '') for k in ('title','brand','categories','seller_name')}
                candidates.append({'query_id':qid,'query':q['query'],'source':q['source'],'query_cohort':q['query_cohort'],
                                   'document_id':did,'catalog_text':row[1],'document':document})
                provenance.append({'query_id':qid,'document_id':did,'source_line':row[3],'top10_origins':origins})
    publish(out/'candidate-rows.jsonl',candidates,jsonl=True)
    publish(out/'private-provenance.jsonl',provenance,jsonl=True)
    result={'status':'ONE_ALL_GRID_TOP10_SUPPLEMENT_READY_NOT_JUDGED','candidate_rows_sha256':sha(out/'candidate-rows.jsonl'),
            'query_contracts_sha256':sha(contracts_path),'query_contracts_path':str(contracts_path),
            'candidate_rows_path':str(out/'candidate-rows.jsonl'),'private_provenance_sha256':sha(out/'private-provenance.jsonl'),
            'base_qrels_sha256':sha(base_path),'grid_complete_path':str(grid/'COMPLETE.json'),
            'grid_complete_sha256':args.grid_complete_sha256,'training_decision_path':str(Path(args.training_decision).resolve()),
            'training_decision_sha256':args.training_decision_sha256,'methods':complete['methods'],
            'pairs':len(candidates),'queries_requiring_supplement':len(members),
            'source_counts':dict(Counter(r['source'] for r in candidates)),
            'catalog_sha256':catalog_entries[0]['sha256'],
            'one_time_policy':'Complete bounded method set Top10 union minus existing base pairs, no candidate-dependent filtering.',
            'new_test_accessed':False,'labels_generated':False,'code_sha256':sha(Path(__file__))}
    publish(out/'POOL_COMPLETE.json',result)
    return result

def verify_pool_provenance(path,expected):
    from types import SimpleNamespace
    path=Path(path).resolve()
    if path!=(ROOT/'development-topup/POOL_COMPLETE.json').resolve():raise ValueError('Unexpected top-up output')
    receipt=verified_json(path,expected)
    args=SimpleNamespace(grid=Path(receipt['grid_complete_path']).parent,
        grid_complete_sha256=receipt['grid_complete_sha256'],training_decision=receipt['training_decision_path'],
        training_decision_sha256=receipt['training_decision_sha256'])
    actual=build(args,verify_only=True)
    if actual!=receipt:raise ValueError('Top-up producer chain differs')
    return {'status':'CPU_TOPUP_PROVENANCE_PASS','pairs':actual['pairs'],'model_calls':0,'files_written':0}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--grid',required=True,type=Path);p.add_argument('--grid-complete-sha256',required=True)
    p.add_argument('--training-decision',required=True,type=Path);p.add_argument('--training-decision-sha256',required=True)
    args=p.parse_args();print(json.dumps(build(args),ensure_ascii=False))

if __name__=='__main__':main()
