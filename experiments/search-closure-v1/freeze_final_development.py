"""Freeze base plus exactly one independently reviewed all-method Top10 union."""
import argparse
from collections import Counter
import json
from pathlib import Path
import baseline_eval as evaluation
import review_workspace as review
from reconcile_development import freeze_base
from prepare_dev_topup import verify_pool_provenance
from retrieval_runtime import read_json,sha,write_once
from reviews import rows

ROOT=Path('D:/agent-datasets/search-closure-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6')

def merge_rows(base,supplement,candidates):
    def indexed(values):
        by={(r['query_id'],r['document_id']):r for r in values}
        if len(by)!=len(values):raise ValueError('Duplicate qrel identity')
        return by
    initial,extra,pool=map(indexed,(base,supplement,candidates))
    if set(initial)&set(extra):raise ValueError('Supplement overwrites a base judgment')
    if set(extra)!=set(pool):raise ValueError('Supplement omits/adds all-method Top10 members')
    for key,row in extra.items():
        if any(row.get(k)!=pool[key].get(k) for k in ('query','document','source','query_cohort')):
            raise ValueError('Supplement changed original candidate evidence')
    combined={**initial,**extra}
    return [combined[key] for key in sorted(combined)]

def freeze(args):
    base=freeze_base()
    complete_path=ROOT/'development-topup/POOL_COMPLETE.json'
    verify_pool_provenance(complete_path,args.pool_sha256)
    pool=read_json(complete_path)
    if pool['base_qrels_sha256']!=base['qrels_sha256']:raise ValueError('Supplement based on different labels')
    workspace=Path(args.review_workspace).resolve()
    inputs=read_json(workspace/'packets/MANIFEST.json')
    if inputs['pool_provenance_sha256']!=args.pool_sha256:raise ValueError('Reviews belong to another supplement')
    labeled=review.freeze(workspace,label_version='search-closure-development-topup-v1',status='FROZEN_DEVELOPMENT_TOPUP_QRELS')
    if labeled['pairs']!=pool['pairs']:raise ValueError('Incomplete supplemental judgments')
    values=merge_rows(rows(DEV/'frozen/base/qrels.jsonl'),rows(workspace/'frozen/qrels.jsonl'),rows(pool['candidate_rows_path']))
    queries=evaluation.load_queries(rows(evaluation.QUERIES))
    labels,_=evaluation.load_qrels(values,queries)
    grid=Path(pool['grid_complete_path']).parent;grid_complete=read_json(pool['grid_complete_path'])
    rankings=evaluation.load_rankings(rows(grid/'rankings.jsonl'),queries,methods=grid_complete['methods'])
    for method,qr in rankings.items():
        for qid,ranking in qr.items():
            if not set(ranking[:10])<=set(labels[qid]):raise ValueError('A registered method Top10 remains unjudged')
    target=DEV/'frozen'
    write_once(target/'qrels.jsonl',values,jsonl=True)
    write_once(target/'queries.jsonl',rows(evaluation.QUERIES),jsonl=True)
    manifest={'status':'FINAL_DEVELOPMENT_QRELS_FROZEN_MODEL_SILVER','pairs':len(values),'queries':40,
        'base_pairs':3674,'supplement_pairs':len(values)-3674,'human_gold':False,'label_kind':'model_silver',
        'qrels_sha256':sha(target/'qrels.jsonl'),'queries_sha256':sha(target/'queries.jsonl'),
        'base_complete':{'path':str(DEV/'frozen/base/COMPLETE.json'),'sha256':sha(DEV/'frozen/base/COMPLETE.json')},
        'supplement_pool':{'path':str(complete_path),'sha256':args.pool_sha256},
        'supplement_labels':{'path':str(workspace/'frozen/MANIFEST.json'),'sha256':sha(workspace/'frozen/MANIFEST.json')},
        'grade_counts':dict(Counter(str(r['grade']) for r in values)),
        'registered_methods':grid_complete['methods'],'all_registered_top10_have_judgment':True,
        'unknowns_are_not_negative':True,'new_test_read':False,'code_sha256':sha(Path(__file__))}
    write_once(target/'MANIFEST.json',manifest)
    write_once(target/'COMPLETE.json',{'status':manifest['status'],'manifest_sha256':sha(target/'MANIFEST.json'),
        'qrels_sha256':manifest['qrels_sha256'],'pairs':len(values)})
    return manifest

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pool-sha256',required=True)
    p.add_argument('--review-workspace',type=Path,default=ROOT/'development-topup/review')
    print(json.dumps(freeze(p.parse_args()),ensure_ascii=False))

if __name__=='__main__':main()
