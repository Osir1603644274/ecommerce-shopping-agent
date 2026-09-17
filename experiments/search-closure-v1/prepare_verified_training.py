"""Revalidate actual independent author chains before pairwise preparation."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import review_workspace
import train_pairwise
from retrieval_runtime import sha,read_json,write_once

ROOT=Path('D:/agent-datasets/search-closure-v1')

def prepare(args):
    root=Path(args.workspace).resolve()
    # freeze() replays the original pool provenance, real author outputs,
    # dispatch/completion events and majority, rejecting changed source bytes.
    selection=ROOT/'selection/FROZEN.json'
    if sha(selection)!=args.selection_sha256:raise ValueError('Original selected-query seal changed')
    train_sha=read_json(selection)['files']['train.queries.jsonl']
    labels=review_workspace.freeze(root,label_version='search-closure-training-v1',status='FROZEN_TRAINING_QRELS',selected_train_sha256=train_sha)
    manifest=root/'frozen/MANIFEST.json'
    inner=SimpleNamespace(gate=args.gate,gate_sha256=args.gate_sha256,selection_sha256=args.selection_sha256,
        annotation_manifest=manifest,annotation_sha256=sha(manifest),training_qrels=root/'frozen/qrels.jsonl',output_name=args.output_name)
    result=train_pairwise.prepare(inner)
    target=ROOT/'training-preparation/prepared'/args.output_name
    proof={'status':'ORIGINAL_AUTHOR_CHAIN_REVALIDATED_BEFORE_PAIR_PREPARATION','review_workspace':str(root),
        'review_manifest_sha256':sha(manifest),'qrels_sha256':labels['qrels_sha256'],
        'prepared_manifest_sha256':sha(target/'MANIFEST.json'),'training_input_status':result['status'],
        'files':{str(Path(__file__).with_name(n)):sha(Path(__file__).with_name(n)) for n in ['prepare_verified_training.py','review_workspace.py','reviews.py','session_completion.py','run_training_pool.py']},
        'semantic_judgments_generated_by_wrapper':0,'test_access':'Original train/heldout query-only isolation validation in preparation; no heldout retrieval, candidates or labels.'}
    write_once(target/'AUTHOR_CHAIN_VERIFIED.json',proof)
    return {k:result[k] for k in ['status','valid_query_count','pair_count']}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,default=ROOT/'training-review')
    p.add_argument('--gate',type=Path,required=True);p.add_argument('--gate-sha256',required=True)
    p.add_argument('--selection-sha256',required=True);p.add_argument('--output-name',default='pairwise-v1')
    print(json.dumps(prepare(p.parse_args())))

if __name__=='__main__':main()
