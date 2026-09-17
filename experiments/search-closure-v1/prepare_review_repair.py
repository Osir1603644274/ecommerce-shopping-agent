"""Archive an invalid author submission and enumerate only its own schema errors."""
import argparse,json,shutil
from pathlib import Path
from reviews import DEV,load,rows,find_output,validate_judgments
from retrieval_runtime import sha,write_once

def main():
    p=argparse.ArgumentParser();p.add_argument('review_id');p.add_argument('--workspace',type=Path);a=p.parse_args();rid=a.review_id
    root=a.workspace.resolve() if a.workspace else DEV
    if a.workspace and not root.is_relative_to(Path('D:/agent-datasets/search-closure-v1').resolve()):raise ValueError('Additional workspace must be in new experiment')
    dispatch=load(root/'dispatch'/f'{rid}.json');packet=root/'packets'/dispatch['packet_id']
    pairs={r['pair_id']:r for r in rows(packet/'pairs.jsonl')}
    contracts={r['query_id']:r for r in rows(packet/'query-contracts.jsonl')}
    judgment=find_output(dispatch['projectlessOutputDirectory'],'judgments.jsonl')
    receipt=find_output(dispatch['projectlessOutputDirectory'],'receipt.json')
    values=rows(judgment);errors=[]
    if {r['pair_id'] for r in values}!=set(pairs) or len(values)!=len(pairs):raise ValueError('Incomplete submission needs completion, not pair-level repair')
    for row in values:
        pair=pairs[row['pair_id']]
        try:validate_judgments([row],[pair],[contracts[pair['query_id']]])
        except ValueError as error:errors.append({'pair_id':row['pair_id'],'error':str(error),'submitted':row})
    if not errors:raise ValueError('No schema repair needed')
    archive=root/'repairs'/rid/'original';archive.mkdir(parents=True,exist_ok=True)
    event=root/'completion-events'/f'{rid}.json'
    for source in [judgment,receipt,event]:
        target=archive/source.name
        if target.exists():
            if sha(target)!=sha(source):raise ValueError('Original archive changed')
        else:shutil.copyfile(source,target)
    request={'status':'AUTHOR_REPAIR_REQUIRED_NOT_YET_COMPLETED','review_id':rid,'author_thread_id':dispatch['threadId'],
        'original_judgments_sha256':sha(judgment),'original_receipt_sha256':sha(receipt),
        'original_judgments_path':str(archive/judgment.name),'original_receipt_path':str(archive/receipt.name),
        'initial_completion_sha256':sha(event),'original_archive':str(archive),'errors':errors,
        'next_completion_event':rid+'-r1.json','other_reviewer_labels_disclosed':False}
    write_once(root/'repair-requests'/f'{rid}.json',request)
    print(json.dumps({'review_id':rid,'error_count':len(errors),'pair_ids':[e['pair_id'] for e in errors]}))

if __name__=='__main__':main()
