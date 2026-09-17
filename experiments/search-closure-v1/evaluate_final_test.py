"""Freeze independently reviewed heldout qrels and compare only frozen two arms.

This entry point cannot select a model, change a query, generate a grade, or
perform retrieval. Original selection, pool, author chains and metrics are
revalidated before publishing the one canonical descriptive test result.
"""
from collections import Counter
from pathlib import Path
import argparse
import json
import baseline_eval as evaluation
import heldout_evaluation
import review_workspace
from authorize_final_test import authorize
from retrieval_runtime import sha,read_json,write_once
from reviews import rows

ROOT=Path('D:/agent-datasets/search-closure-v1')

def load_test_data(query_rows,ranking_rows,qrel_rows):
    queries=evaluation.load_queries(query_rows,fixed_development=False)
    if (len(queries)!=80 or Counter(q['source'] for q in queries.values())!={'kuaisearch':40,'multicpr':40}
        or any(q['query_cohort']!='main' for q in queries.values())):
        raise ValueError('Expected all original 80 heldout queries')
    # Validate all provenance rankings, then exclude raw pooling channels from
    # quality comparisons. Heldout scores can never choose another method.
    all_rankings=evaluation.load_rankings(ranking_rows,queries,methods=['baseline','winner','bm25','character','dense'])
    rankings={name:all_rankings[name] for name in ('baseline','winner')}
    labels,causes=evaluation.load_qrels(qrel_rows,queries)
    if len(qrel_rows)!=6400 or any(len(v)!=80 for v in labels.values()):
        raise ValueError('Fixed heldout pool requires exactly80 labels per query')
    for method in all_rankings:
        for qid,ranking in all_rankings[method].items():
            if not set(ranking[:10])<=set(labels[qid]):
                raise ValueError('Heldout shared pool misses an original method Top10')
    return queries,rankings,labels,causes

def execute(args):
    # authorize validates the actual final dev/model selection before opening
    # the original heldout query-only split.
    query_rows,choice,authority=authorize(args.selection,args.selection_sha256)
    pool_path=ROOT/'test-pool/POOL_COMPLETE.json'
    if sha(pool_path)!=args.pool_complete_sha256:raise ValueError('Heldout pool changed')
    pool=read_json(pool_path)
    if pool.get('status')!='FINAL_TEST_CANDIDATE_POOL_READY_UNJUDGED':raise ValueError('Heldout retrieval not complete')
    binding=read_json(pool_path.parent/'binding.json')
    if sha(pool_path.parent/'binding.json')!=pool['binding_sha256'] or binding['authority']!=authority:
        raise ValueError('Heldout run differs from frozen model selection')
    workspace=ROOT/'test-review'
    packets=read_json(workspace/'packets/MANIFEST.json')
    if (Path(packets['pool_provenance_path']).resolve()!=pool_path.resolve()
        or packets['pool_provenance_sha256']!=args.pool_complete_sha256):
        raise ValueError('Reviewed pool differs from actual heldout retrieval')
    # This also CPU-replays the original candidate producer and verifies every
    # A/B/T completion, actual output, original input and majority decision.
    manifest=review_workspace.freeze(workspace,label_version='search-closure-final-test-v1',
        status='FINAL_TEST_QRELS_FROZEN_MODEL_SILVER')
    qrel_path=workspace/'frozen/qrels.jsonl';ranking_path=pool_path.parent/'rankings.jsonl'
    if sha(ranking_path)!=pool['rankings_sha256'] or sha(qrel_path)!=manifest['qrels_sha256']:
        raise ValueError('Final heldout label/ranking bytes changed')
    queries,rankings,labels,causes=load_test_data(query_rows,rows(ranking_path),rows(qrel_path))
    evidence=evaluation.Evidence()
    paths=[Path(args.selection),pool_path,pool_path.parent/'binding.json',ranking_path,qrel_path,
        workspace/'frozen/MANIFEST.json',workspace/'frozen/COMPLETE.json',workspace/'packets/MANIFEST.json']
    paths.extend(Path(__file__).with_name(n) for n in ['evaluate_final_test.py','authorize_final_test.py','review_workspace.py',
        'reviews.py','session_completion.py','run_final_test_pool.py','baseline_eval.py','metrics_v2.py','heldout_evaluation.py'])
    for p in paths:evidence.file(p)
    result=heldout_evaluation.evaluate(queries,rankings,labels,causes,repetitions=10000,seed=20260909,baseline='baseline')
    evidence.recheck()
    out=ROOT/'evaluation/final-test-v1'
    write_once(out/'per-query.jsonl',result['per_query'],jsonl=True)
    write_once(out/'common-pools.jsonl',result['common_pools'],jsonl=True)
    report={'status':'FINAL_TEST_DESCRIPTIVE_EVALUATION_COMPLETE','query_count':80,'pair_count':6400,
        'methods':['baseline','winner'],'arms':{name:choice[field] for name,field in [('baseline','strongest_old'),('winner','selected')]},
        'authority':authority,'qrels_sha256':manifest['qrels_sha256'],'rankings_sha256':pool['rankings_sha256'],
        'inputs':list(evidence.files.values()),'label_kind':'independent_context_model_silver_not_human_gold',
        'model_inference_by_evaluator':False,'model_selection_from_test':False,'production_activation':False,
        'bootstrap_repetitions':10000,'seed':20260909,
        'cohort_policy':'All original80 are main; all equals main. No diagnostic cohort exists and none is fabricated.',
        'summary':result['summary'],'comparisons':result['comparisons'],
        'shared_eligibility':result['shared_eligibility'],'common_conditional':result['common_conditional'],
        'outputs':{n:sha(out/n) for n in ['per-query.jsonl','common-pools.jsonl']},
        'limitations':['Native training queries form this project holdout; it is not an official dataset test split.',
            'Pooled graded relevance with UNKNOWN uncertainty, not exhaustive full-catalog relevance.',
            'Independent contexts of one model can share systematic judgment errors.',
            'Descriptive evidence; no heldout tuning, activation or human-gold accuracy claim.']}
    write_once(out/'report.json',report)
    write_once(out/'COMPLETE.json',{'status':report['status'],'report_sha256':sha(out/'report.json'),
        'selection_sha256':args.selection_sha256,'qrels_sha256':manifest['qrels_sha256']})
    return {'status':report['status'],'path':str(out/'report.json')}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--selection',required=True,type=Path);p.add_argument('--selection-sha256',required=True)
    p.add_argument('--pool-complete-sha256',required=True)
    print(json.dumps(execute(p.parse_args())))

if __name__=='__main__':main()
