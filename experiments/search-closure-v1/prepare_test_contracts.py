"""Export heldout query-only text after selection; freeze explicitly authored intents."""
import argparse
import json
from pathlib import Path
from authorize_final_test import authorize
from retrieval_runtime import sha,read_json,write_once,fingerprint
from reviews import rows
from review_workspace import RUBRIC,RUBRIC_SHA,exact_copy

ROOT=Path('D:/agent-datasets/search-closure-v1')

def validate_proposal(proposal,queries):
    by={r['query_id']:r for r in queries}
    if len(proposal)!=80 or len(by)!=80 or {r['query_id'] for r in proposal}!=set(by):raise ValueError('All original80 query contracts required')
    for r in proposal:
        q=by[r['query_id']]
        if r.get('query')!=q['query'] or r.get('source')!=q['source'] or r.get('no_added_requirements') is not True:
            raise ValueError('Query text/source was changed')
        purpose=r.get('core_purpose_requirements');attributes=r.get('substitutable_attributes')
        if (not isinstance(purpose,list) or not isinstance(attributes,list)
            or any(not isinstance(v,str) or not v.strip() for v in [*purpose,*attributes])):raise ValueError('Explicit attribute lists required')
        keys=['本体',*purpose,*attributes]
        if r.get('required_attribute_keys')!=keys or len(keys)!=len(set(keys)):raise ValueError('Explicit contract attribute coverage differs')
        if r.get('intent_policy') not in ('score_product_relevance','query_intent_ambiguous'):raise ValueError('Unknown intent policy')
        if any(not isinstance(r.get(k),str) or not r[k].strip() for k in ['core_product','interpretation_notes']):raise ValueError('Actual core/intent notes required')

def execute(args):
    queries,_,authority=authorize(args.selection,args.selection_sha256)
    target=ROOT/'test-preparation'
    if args.command=='export':
        write_once(target/'queries.query-only.jsonl',queries,jsonl=True)
        write_once(target/'QUERY_ACCESS.json',authority)
        return {'status':'FINAL_TEST_QUERY_ONLY_EXPORTED_FOR_INTENT_REVIEW','queries':80,'path':str(target/'queries.query-only.jsonl')}
    if sha(args.proposal)!=args.proposal_sha256 or sha(args.review_receipt)!=args.review_receipt_sha256:
        raise ValueError('Authored test intent review changed')
    proposal=rows(args.proposal);validate_proposal(proposal,queries)
    review=read_json(args.review_receipt)
    if (review.get('status')!='ROOT_READ_ALL80_ORIGINAL_TEST_QUERIES_BEFORE_CANDIDATES'
        or review.get('proposal_sha256')!=args.proposal_sha256 or review.get('model_selection_sha256')!=args.selection_sha256
        or review.get('query_identity_sha256')!=fingerprint(queries) or review.get('human_gold') is not False):
        raise ValueError('Missing bound actual query-only author review')
    if sha(RUBRIC)!=RUBRIC_SHA:raise ValueError('Relevance rule changed')
    complete=target/'QUERY_CONTRACTS_FROZEN.json'
    if not complete.exists() and any(p.is_file() for p in (ROOT/'test-pool').rglob('*')):
        raise ValueError('Test candidates already exist before intent freeze')
    contracts=target/'query-contracts-frozen.jsonl';exact_copy(args.proposal,contracts)
    result={'status':'TEST_QUERY_CONTRACTS_FROZEN_BEFORE_CANDIDATES','query_count':80,
        'model_selection_sha256':args.selection_sha256,'query_contracts_path':str(contracts),'query_contracts_sha256':sha(contracts),
        'rubric_sha256':RUBRIC_SHA,'authority':authority,'proposal_path':str(Path(args.proposal).resolve()),
        'proposal_sha256':args.proposal_sha256,'author_review':{'path':str(Path(args.review_receipt).resolve()),'sha256':args.review_receipt_sha256},
        'freezer_sha256':sha(Path(__file__)),'semantic_judgments_generated_by_freezer':0}
    write_once(complete,result)
    return {'status':result['status'],'path':str(complete),'sha256':sha(complete)}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['export','freeze'])
    p.add_argument('--selection',type=Path,required=True);p.add_argument('--selection-sha256',required=True)
    p.add_argument('--proposal',type=Path);p.add_argument('--proposal-sha256')
    p.add_argument('--review-receipt',type=Path);p.add_argument('--review-receipt-sha256')
    args=p.parse_args()
    if args.command=='freeze' and not all([args.proposal,args.proposal_sha256,args.review_receipt,args.review_receipt_sha256]):p.error('freeze requires authored proposal and review receipt plus exact hashes')
    print(json.dumps(execute(args)))

if __name__=='__main__':main()
