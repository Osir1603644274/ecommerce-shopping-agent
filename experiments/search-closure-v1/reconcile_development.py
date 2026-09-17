"""Prepare blinded third reviews and merge completed, bound independent votes."""
from __future__ import annotations
import argparse
import json
import hashlib
from collections import Counter
from pathlib import Path
from bootstrap import DEV,REPO,sha,write_once
from prepare_development import jsonl_once
from reviews import load,rows,validate_judgments,verify_collected,validate_packet

def verify_original_inputs():
    policy=load(DEV/'policy/FROZEN.json')
    old=Path('D:/agent-datasets/search-stage1-dev-revision-v5/frozen')
    for path,key in [(old/'qrels.jsonl','source_qrels_sha256'),(old/'queries.jsonl','source_queries_sha256'),
                     (DEV/'policy/query-contracts.jsonl','query_contracts_sha256'),(DEV/'policy/RUBRIC.md','rule_sha256')]:
        if sha(path)!=policy[key]:raise ValueError('Frozen policy/source input drift')
    contracts={r['query_id']:r for r in rows(DEV/'policy/query-contracts.jsonl')}
    expected={};pairs={};public={}
    for row in rows(old/'qrels.jsonl'):
        qid,did=row['query_id'],row['document_id']
        pid=hashlib.sha256(('v6-pair:'+qid+'\0'+did).encode()).hexdigest()[:24]
        contract=dict(contracts[qid]);contract['query_id']=contract.pop('anonymous_query_id')
        public[contract['query_id']]=contract
        expected[pid]={'pair_id':pid,'query_id':qid,'document_id':did,'query_cohort':row['query_cohort']}
        pairs[pid]={'pair_id':pid,'query_id':contract['query_id'],'query':row['query'],'document':row['document']}
    mapping=rows(DEV/'private/pair-mapping.jsonl')
    if len(mapping)!=3674 or len(expected)!=3674 or {r['pair_id']:r for r in mapping}!=expected:
        raise ValueError('Private mapping differs from original frozen identities')
    seen=set()
    for packet in load(DEV/'packets/MANIFEST.json')['packets']:
        _,_,values,cs=validate_packet(DEV,packet['packet_id'])
        if any(c!=public.get(c['query_id']) for c in cs):raise ValueError('Public query contract changed')
        for row in values:
            pid=row['pair_id']
            if pid in seen or row!=pairs.get(pid):raise ValueError('Original blind pair changed')
            seen.add(pid)
    if seen!=set(expected):raise ValueError('Original packet coverage differs')
    return policy

def vote_grade(a,b,t=None):
    if a==b: return a,'two_contexts_agree'
    if t is None: raise ValueError('Disagreement requires a completed independent third judgment')
    if t==a or t==b: return t,'two_of_three_majority'
    return 'UNKNOWN','review_no_majority'

def read_review(review_id, packet_id):
    return verify_collected(review_id,packet_id,DEV)

def prepare(packet_id):
    number=packet_id.split('-')[-1]
    a,ar=read_review('A'+number,packet_id); b,br=read_review('B'+number,packet_id)
    if ar['model_metadata']['source_thread_id']==br['model_metadata']['source_thread_id']:
        raise ValueError('A/B are not independent task contexts')
    if set(a)!=set(b): raise ValueError('A/B pair coverage differs')
    disagreements={pid for pid in a if a[pid]['grade']!=b[pid]['grade']}
    record={'packet_id':packet_id,'pairs':len(a),'agreement_pairs':len(a)-len(disagreements),
            'disagreement_pairs':len(disagreements),'disagreement_pair_ids':sorted(disagreements),
            'A_collected_sha256':sha(DEV/'reviews'/('A'+number)/'COLLECTED.json'),
            'B_collected_sha256':sha(DEV/'reviews'/('B'+number)/'COLLECTED.json'),
            'third_review_required':bool(disagreements), 'agreement_is_not_accuracy':True}
    if disagreements:
        source=DEV/'packets'/packet_id; destination=DEV/'packets'/('third-'+packet_id)
        selected=[r for r in rows(source/'pairs.jsonl') if r['pair_id'] in disagreements]
        qids={r['query_id'] for r in selected}
        contracts=[q for q in rows(source/'query-contracts.jsonl') if q['query_id'] in qids]
        jsonl_once(destination/'pairs.jsonl',selected);jsonl_once(destination/'query-contracts.jsonl',contracts)
        rubric=destination/'RUBRIC.md'
        if rubric.exists():
            if sha(rubric)!=sha(source/'RUBRIC.md'):raise ValueError('Third rubric differs')
        else:
            with rubric.open('xb') as f:f.write((source/'RUBRIC.md').read_bytes())
        manifest={'packet_id':'third-'+packet_id,'pairs':len(selected),'queries':len(contracts),
                  'files':{f:sha(destination/f) for f in ['pairs.jsonl','query-contracts.jsonl','RUBRIC.md']},
                  'no_old_labels_or_rankings':True,'no_prior_reviewer_answers':True,'human_gold':False}
        write_once(destination/'INPUT_MANIFEST.json',manifest)
        record['third_packet_id']='third-'+packet_id
        record['third_input_manifest_sha256']=sha(destination/'INPUT_MANIFEST.json')
    write_once(DEV/'adjudication/requests'/f'{packet_id}.json',record)
    return record

def freeze_base():
    output=DEV/'frozen/base'
    verify_original_inputs()
    if (output/'COMPLETE.json').exists():
        complete=load(output/'COMPLETE.json')
        if sha(output/'MANIFEST.json')!=complete['manifest_sha256']:raise ValueError('Base manifest changed')
        for name,item in load(output/'MANIFEST.json')['files'].items():
            if sha(output/name)!=item['sha256']:raise ValueError('Frozen base file changed')
        for item in load(output/'MANIFEST.json')['review_bindings']:
            rid=item['review_id'];record=load(item['collected_path'])
            if sha(item['collected_path'])!=item['collected_sha256']:raise ValueError('Frozen review binding changed')
            read_review(rid,record['packet_id'])
        return complete
    packets=load(DEV/'packets/MANIFEST.json')['packets']
    mapping={r['pair_id']:r for r in rows(DEV/'private/pair-mapping.jsonl')}
    old_path=Path('D:/agent-datasets/search-stage1-dev-revision-v5/frozen/qrels.jsonl')
    old_rows=rows(old_path); old={(r['query_id'],r['document_id']):r for r in old_rows}
    policy=load(DEV/'policy/FROZEN.json')
    if sha(old_path)!=policy['source_qrels_sha256']:raise ValueError('Historical label source drift')
    result=[];changes=[];review_bindings=[];unknown=[]
    for packet in packets:
        pid=packet['packet_id'];number=pid.split('-')[-1]
        request=prepare(pid)
        a,ar=read_review('A'+number,pid);b,br=read_review('B'+number,pid)
        t,tr=({},None)
        if request['third_review_required']:
            t,tr=read_review('T'+number,'third-'+pid)
            if set(t)!=set(request['disagreement_pair_ids']):raise ValueError('Third review wrong pair coverage')
            if tr['model_metadata']['source_thread_id'] in {ar['model_metadata']['source_thread_id'],br['model_metadata']['source_thread_id']}:
                raise ValueError('Third reviewer context reused')
        for rid,rr in [('A'+number,ar),('B'+number,br),('T'+number,tr)]:
            if rr:
                review_bindings.append({'review_id':rid,'collected_path':str(DEV/'reviews'/rid/'COLLECTED.json'),
                    'collected_sha256':sha(DEV/'reviews'/rid/'COLLECTED.json'),
                    'source_thread_id':rr['model_metadata']['source_thread_id'],
                    'model':rr['model_metadata']['model'],'context_isolation':'no_prior_conversation'})
        for pair in rows(DEV/'packets'/pid/'pairs.jsonl'):
            key=pair['pair_id'];identity=mapping[key];prior=old[(identity['query_id'],identity['document_id'])]
            third=t.get(key)
            grade,resolution=vote_grade(a[key]['grade'],b[key]['grade'],third['grade'] if third else None)
            supporting=next((r for r in [a[key],b[key],third] if r and r['grade']==grade),None)
            cause=['review_no_majority'] if resolution=='review_no_majority' else ([supporting['unknown_reason']] if grade=='UNKNOWN' else [])
            row={**identity,'query':pair['query'],'document':pair['document'],'grade':grade,
                'rule_sha256':policy['rule_sha256'],'human_gold':False,
                'label_source':'independent_context_model_silver_v6','resolution':resolution,
                'primary':a[key],'review':b[key],'third':third,'unknown_causes':cause,
                'reason':supporting['reason'] if supporting and resolution!='review_no_majority' else '三方等级不同，按冻结规则保留UNKNOWN',
                'reviewer_contexts':{'A':ar['model_metadata']['source_thread_id'],'B':br['model_metadata']['source_thread_id'],
                                     'T':tr['model_metadata']['source_thread_id'] if third else None}}
            if row['query']!=prior['query'] or row['document']!=prior['document']:raise ValueError('Input identity/text altered')
            result.append(row)
            changes.append({**identity,'query':row['query'],'title':row['document']['title'],
                'old_grade':prior['grade'],'new_grade':grade,'grade_changed':prior['grade']!=grade,
                'old_reason':prior.get('primary',{}).get('reason'),'new_reason':row['reason'],
                'new_evidence':supporting.get('evidence',[]) if supporting else [],'resolution':resolution})
            if grade=='UNKNOWN':unknown.append({**identity,'query':row['query'],'unknown_causes':cause})
    if len(result)!=3674 or {r['pair_id'] for r in result}!=set(mapping):raise ValueError('Base is incomplete')
    result.sort(key=lambda r:(r['query_id'],r['document_id']))
    jsonl_once(output/'qrels.jsonl',result);jsonl_once(output/'changes.jsonl',changes)
    jsonl_once(output/'unknown-pairs.jsonl',unknown)
    queries=rows(Path('D:/agent-datasets/search-stage1-dev-revision-v5/frozen/queries.jsonl'))
    jsonl_once(output/'queries.jsonl',queries)
    stats={'status':'BASE_3674_REJUDGED_NOT_FINAL_TOPUP_OR_RETRIEVAL_IMPROVEMENT',
           'queries':40,'pairs':3674,'main_queries':33,'diagnostic_queries':7,
           'grade_counts':dict(Counter(str(r['grade']) for r in result)),
           'unknown_pairs':len(unknown),'grade_changed_pairs':sum(c['grade_changed'] for c in changes),
           'resolution_counts':dict(Counter(r['resolution'] for r in result)),
           'model_silver':True,'human_gold':False,'review_bindings':review_bindings,
           'policy':policy,'prior_qrels_sha256':sha(old_path)}
    write_once(output/'RESULTS.json',stats)
    manifest={'files':{p.name:{'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(output.iterdir()) if p.is_file()},
              'rule_sha256':policy['rule_sha256'],'review_bindings':review_bindings,
              'assembler_sha256':sha(Path(__file__))}
    write_once(output/'MANIFEST.json',manifest)
    complete={'status':'V6_BASE_FROZEN_MODEL_SILVER','pairs':3674,'qrels_sha256':sha(output/'qrels.jsonl'),
              'manifest_sha256':sha(output/'MANIFEST.json'),'final_development_topup_complete':False}
    write_once(output/'COMPLETE.json',complete)
    return complete

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare-third','freeze-base']);p.add_argument('--packet')
    a=p.parse_args()
    if a.command=='prepare-third':
        if not a.packet:raise ValueError('--packet required')
        result=prepare(a.packet)
    else:result=freeze_base()
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
