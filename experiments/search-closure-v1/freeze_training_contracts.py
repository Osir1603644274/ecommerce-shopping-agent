"""Publish the 200 query interpretations after root's actual query-only review."""
import json
from pathlib import Path
from retrieval_runtime import read_json, sha, write_once
from reviews import rows

ROOT=Path('D:/agent-datasets/search-closure-v1')
DIR=ROOT/'training-preparation'
SOURCE=DIR/'query-contracts-proposed.jsonl'
EXPECTED='6d78e3243450a1bc6a6af55be57ae8b69927e1e47989aec12fe7967c3de8de81'

def main():
    if sha(SOURCE)!=EXPECTED:raise ValueError('Actual root-reviewed proposal changed')
    receipt=read_json(DIR/'QUERY_CONTRACT_REVIEW.json')
    if receipt['output']['sha256']!=EXPECTED:raise ValueError('Author proposal receipt differs')
    selected_path=ROOT/'selection/frozen/train.queries.jsonl'
    if sha(selected_path)!=receipt['input_train_queries']['sha256']:raise ValueError('Selected train changed')
    selected={r['query_id']:r['text'] for r in rows(selected_path)}
    contracts=rows(SOURCE)
    if len(contracts)!=200 or {r['query_id']:r['query'] for r in contracts}!=selected:
        raise ValueError('Training query membership/text differs')
    for row in contracts:
        if row['required_attribute_keys']!=['本体',*row['core_purpose_requirements'],*row['substitutable_attributes']]:
            raise ValueError('Contract attributes do not cover explicit requirements')
    target=DIR/'query-contracts-frozen.jsonl'
    if target.exists():
        if sha(target)!=EXPECTED:raise ValueError('Frozen contracts changed')
    else:
        with target.open('xb') as stream:stream.write(SOURCE.read_bytes())
    result={'status':'TRAINING_QUERY_CONTRACTS_FROZEN_BEFORE_CANDIDATES',
            'query_count':200,'selected_train_sha256':sha(selected_path),
            'query_contracts_path':str(target),'query_contracts_sha256':sha(target),
            'proposal_path':str(SOURCE),'proposal_sha256':EXPECTED,
            'author_review_sha256':sha(DIR/'QUERY_CONTRACT_REVIEW.json'),
            'rubric_sha256':receipt['rubric']['sha256'],
            'root_review':{'root_thread_id':'01a07f07-28fa-7523-84fe-516a1cf42fc6',
                'all_200_query_core_purpose_attribute_notes_read':True,
                'truncated_segment_reread':True,'query_text_changes':0,'query_exclusions':0,
                'contract_changes':0,'product_candidates_or_labels_seen':False,
                'interpretation_scope':'Text relevance. Unknown units/models are not corrected from candidate availability; marketing/efficacy associations do not establish real efficacy. Compatibility failure requires explicit unusability evidence under the unchanged rubric.'},
            'training_gate_consumed':False,'candidate_retrieval_authorized_by_this_receipt_alone':False,
            'freezer_sha256':sha(Path(__file__))}
    write_once(DIR/'QUERY_CONTRACTS_FROZEN.json',result)
    print(json.dumps({'status':result['status'],'queries':200,'sha256':sha(target)}))

if __name__=='__main__':main()
