"""Read held-out queries only after actual final dev labels and model selection bind."""
from collections import Counter
import json
from pathlib import Path
from retrieval_runtime import read_json,sha,model_binding,fingerprint,query_key

ROOT=Path('D:/agent-datasets/search-closure-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6')
SEALED_SHA='ede7f7427360f0d34bd6b3066e7482550e3f411822a6a2823db13831f4bd9d37'

def verify_ref(ref):
    p=Path(ref['path']).resolve()
    if sha(p)!=ref['sha256']:raise ValueError('Final test authorization input changed: '+str(p))
    return p

def validate_selection(choice):
    if (choice.get('status')!='FINAL_SEARCH_SELECTION_FROZEN' or choice.get('seed')!=20260909
        or choice.get('new_test_accessed') is not False or not choice.get('inputs')):
        raise ValueError('Test requires actual final model selection')
    for name in ('selected','strongest_old'):
        spec=choice.get(name,{})
        if set(spec)!={'method','profile','model_path','model_binding_sha256'}:raise ValueError('Final arm schema differs')
        method=spec['method'];profile=spec['profile'];path=spec['model_path']
        if method in ('bm25','character','dense'):
            if profile!=method or path is not None:raise ValueError('Raw retrieval arm changed')
        else:
            parts=method.split('/')
            if len(parts)!=2 or parts[0]!=profile or profile not in ('w111','w211','w112','no_dense'):
                raise ValueError('Final arm profile differs')
            if (parts[1]=='none')!=(path is None):raise ValueError('Final arm model presence differs')
        current=model_binding(path) if path is not None else None
        if fingerprint(current)!=spec['model_binding_sha256']:raise ValueError('Final selected model changed')

def authorize(selection_path,selection_sha256):
    selection_path=Path(selection_path).resolve()
    if selection_path!=(ROOT/'final-selection/SELECTION.json').resolve():raise ValueError('Use actual final selection receipt')
    verify_ref({'path':str(selection_path),'sha256':selection_sha256})
    choice=read_json(selection_path);validate_selection(choice)
    # All development/model evidence is validated before any heldout file access.
    for ref in choice['inputs']:verify_ref(ref)
    final=read_json(DEV/'frozen/COMPLETE.json')
    if final.get('status')!='FINAL_DEVELOPMENT_QRELS_FROZEN_MODEL_SILVER' or final['qrels_sha256']!=choice['qrels_sha256']:
        raise ValueError('Final dev labels missing or different')
    verify_ref({'path':str(DEV/'frozen/MANIFEST.json'),'sha256':final['manifest_sha256']})
    verify_ref({'path':str(DEV/'frozen/qrels.jsonl'),'sha256':final['qrels_sha256']})
    seal_path=ROOT/'selection/SEALED.json';verify_ref({'path':str(seal_path),'sha256':SEALED_SHA})
    seal=read_json(seal_path)
    if seal.get('status')!='SEALED_QUERY_SPLITS_ONLY' or seal.get('queries')!={'train':200,'test':80}:
        raise ValueError('Held-out selection not sealed')
    for name,key in [('FROZEN.json','frozen_sha256'),('MANIFEST.json','manifest_sha256'),('VALIDATION.json','validation_sha256')]:
        verify_ref({'path':str(seal_path.parent/name),'sha256':seal[key]})
    frozen=read_json(seal_path.parent/'FROZEN.json');validation=read_json(seal_path.parent/'VALIDATION.json')
    if (validation.get('status')!='PASS_FROZEN_QUERY_ONLY_AUDIT'
        or validation.get('frozen_manifest_sha256')!=seal['frozen_sha256']
        or validation.get('cross_split_exact_or_threshold_violations')!=0
        or validation.get('root_review_binding_verified') is not True):raise ValueError('Held-out isolation audit failed')
    path=ROOT/'selection/frozen/test.queries.jsonl'
    if Path(seal['test_queries_path']).resolve()!=path.resolve():raise ValueError('Heldout path differs')
    verify_ref({'path':str(path),'sha256':frozen['files']['test.queries.jsonl']})
    values=[json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if len(values)!=80 or len({r['query_id'] for r in values})!=80 or Counter(r['source'] for r in values)!={'kuaisearch':40,'multicpr':40}:
        raise ValueError('Held-out query membership differs')
    queries=[]
    for row in values:
        if (row['split']!='test' or row['native_split']!='train' or row['native_origin']['native_split']!='train'
            or not row['query_id'].startswith('closure-'+row['source'][:2]+'-test-')
            or row['query_key']!=query_key(row['text'])):raise ValueError('Held-out original query identity changed')
        queries.append({'query_id':row['query_id'],'source':row['source'],'query':row['text'],'query_cohort':'main'})
    proof={'status':'FINAL_TEST_QUERY_ACCESS_AUTHORIZED','model_selection':{'path':str(selection_path),'sha256':selection_sha256},
        'sealed_selection_sha256':SEALED_SHA,'heldout_queries_sha256':sha(path),'final_dev_qrels_sha256':choice['qrels_sha256'],
        'queries':80,'source_counts':{'kuaisearch':40,'multicpr':40},'query_rewrite':False,'test_retrieval_or_labels_executed_by_authorizer':False}
    return sorted(queries,key=lambda r:(r['source'],r['query_id'])),choice,proof
