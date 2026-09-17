"""Bind actual final development selection to the configured Agent before runs."""
import argparse
import json
from pathlib import Path
from retrieval_runtime import sha,read_json
from authorize_final_test import validate_selection,verify_ref
import run_agent_pairs as runner

ROOT=Path('D:/agent-datasets/search-closure-v1')

def freeze(args):
    path=Path(args.selection).resolve()
    if path!=(ROOT/'final-selection/SELECTION.json').resolve():raise ValueError('Use actual final selection')
    verify_ref({'path':str(path),'sha256':args.selection_sha256})
    choice=read_json(path);validate_selection(choice)
    for ref in choice['inputs']:verify_ref(ref)
    final_path=runner.evaluation.V6/'frozen/COMPLETE.json';final=read_json(final_path)
    if final.get('status')!='FINAL_DEVELOPMENT_QRELS_FROZEN_MODEL_SILVER' or final['qrels_sha256']!=choice['qrels_sha256']:
        raise ValueError('Final development labels differ from selected model')
    qrels={'path':str((runner.evaluation.V6/'frozen/qrels.jsonl').resolve()),'sha256':choice['qrels_sha256']}
    verify_ref(qrels)
    verify_ref({'path':str(runner.evaluation.V6/'frozen/MANIFEST.json'),'sha256':final['manifest_sha256']})
    queries={'path':str(runner.evaluation.QUERIES.resolve()),'sha256':runner.evaluation.FIXED_QUERY_METADATA_SHA256}
    verify_ref(queries)
    settings=runner.app_modules().settings
    lock={'status':'FINAL_SEARCH_CONFIGURATION_FROZEN','seed':runner.SEED,
        'selection_receipt':{'path':str(path),'sha256':args.selection_sha256},'final_dev_qrels':qrels,'dev_queries':queries,
        'arms':{'baseline':choice['strongest_old'],'winner':choice['selected']},
        'llm':{'provider':'deepseek','base_url':settings.deepseek_base_url,'model':settings.deepseek_model,
            'temperature':0,'max_tokens':512,'sdk_timeout_seconds':30,'max_retries':2,'agent_deadline_seconds':120,'tool_timeout_seconds':60}}
    runner.validate_selection_lock(lock,choice)
    output=runner.ROOT/'SELECTION_LOCK.json';runner.write_once(output,lock)
    return {'status':lock['status'],'path':str(output),'sha256':sha(output),'test_queries_read':False,'model_calls':0}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--selection',type=Path,required=True);p.add_argument('--selection-sha256',required=True)
    print(json.dumps(freeze(p.parse_args())))

if __name__=='__main__':main()
