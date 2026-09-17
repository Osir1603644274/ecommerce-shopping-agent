"""Generic label-blind packets and bound A/B/T aggregation for new pools.

This module never assigns semantic grades or calls a model. The caller must
freeze actual query interpretations and candidate-pool provenance first.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from metrics_v2 import canonical_hash
from retrieval_runtime import write_once, sha, read_json
from reviews import rows, validate_judgments, verify_collected, validate_packet
from reconcile_development import vote_grade

ROOT = Path('D:/agent-datasets/search-closure-v1').resolve()
RUBRIC = Path('D:/agent-datasets/search-stage1-dev-revision-v6/policy/RUBRIC.md')
RUBRIC_SHA = '404b2d8111ddf4373a9f1bc107690a60141ff103ed02ef4a6df475567eb2ad67'

def workspace_path(value):
    path=Path(value).resolve()
    if not path.is_relative_to(ROOT): raise ValueError('Review outputs must stay within the new experiment')
    return path

def exact_copy(source,dest):
    source,dest=Path(source),Path(dest)
    dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists():
        if sha(source)!=sha(dest): raise ValueError('Immutable copied artifact differs')
    else:
        with dest.open('xb') as stream: stream.write(source.read_bytes())

def make_packet(root,name,pairs,contracts):
    target=root/'packets'/name
    write_once(target/'pairs.jsonl',pairs,jsonl=True)
    write_once(target/'query-contracts.jsonl',contracts,jsonl=True)
    exact_copy(RUBRIC,target/'RUBRIC.md')
    manifest={'packet_id':name,'pairs':len(pairs),'queries':len(contracts),
              'files':{n:sha(target/n) for n in ('pairs.jsonl','query-contracts.jsonl','RUBRIC.md')},
              'no_old_labels_or_rankings':True,'no_prior_reviewer_answers':True,'human_gold':False}
    write_once(target/'INPUT_MANIFEST.json',manifest)
    return {'packet_id':name,'pairs':len(pairs),'directory':str(target),
            'input_manifest_sha256':sha(target/'INPUT_MANIFEST.json')}

def validate_candidate_inputs(candidate_rows,contracts):
    byquery={r['query_id']:r for r in contracts}
    if len(byquery)!=len(contracts):raise ValueError('Duplicate query contract')
    seen=set()
    for row in candidate_rows:
        qid,did=row['query_id'],row['document_id']
        if (qid,did) in seen:raise ValueError('Duplicate candidate pair')
        seen.add((qid,did))
        if qid not in byquery or row['query']!=byquery[qid]['query']:raise ValueError('Query differs from frozen contract')
        c=byquery[qid]
        if '本体' not in c['required_attribute_keys'] or len(c['required_attribute_keys'])!=len(set(c['required_attribute_keys'])):
            raise ValueError('Contract attribute keys invalid')
        document=row['document']
        if set(document)-{'title','brand','categories','seller_name'}:raise ValueError('Private/source fields in blind document')
        if not isinstance(document.get('title'),str) or not document['title'].strip():raise ValueError('Document title missing')
        if not isinstance(document.get('categories',[]),list):raise ValueError('Categories must preserve string list')
        if not isinstance(row.get('catalog_text'),str) or not row['catalog_text'].strip():raise ValueError('Actual catalog text required')
    return byquery

def build(root,candidate_path,contracts_path,provenance_path,*,prefix,max_pairs=400):
    root=workspace_path(root)
    if sha(RUBRIC)!=RUBRIC_SHA:raise ValueError('Frozen rubric changed')
    candidate_rows=rows(candidate_path);contracts=rows(contracts_path)
    byquery=validate_candidate_inputs(candidate_rows,contracts)
    provenance=read_json(provenance_path)
    if provenance.get('candidate_rows_sha256')!=sha(candidate_path) or provenance.get('query_contracts_sha256')!=sha(contracts_path):
        raise ValueError('Candidate/contract provenance does not bind the supplied files')
    if provenance.get('status')=='TRAINING_CANDIDATE_POOL_READY_UNJUDGED':
        from run_training_pool import verify_pool_provenance
        verify_pool_provenance(provenance_path,sha(provenance_path))
    elif provenance.get('status')=='ONE_ALL_GRID_TOP10_SUPPLEMENT_READY_NOT_JUDGED':
        from prepare_dev_topup import verify_pool_provenance
        verify_pool_provenance(provenance_path,sha(provenance_path))
    elif provenance.get('status')=='FINAL_TEST_CANDIDATE_POOL_READY_UNJUDGED':
        from run_final_test_pool import verify_pool_provenance
        verify_pool_provenance(provenance_path,sha(provenance_path))
    groups=defaultdict(list);mapping=[];public_contracts={}
    for row in candidate_rows:
        qid,did=row['query_id'],row['document_id']
        pid=hashlib.sha256((str(root)+'\0'+qid+'\0'+did).encode()).hexdigest()[:24]
        anon='q-'+hashlib.sha256((str(root)+'\0'+qid).encode()).hexdigest()[:16]
        public_keys={'query','core_product','core_purpose_requirements','substitutable_attributes',
                     'required_attribute_keys','intent_policy','interpretation_notes','no_added_requirements'}
        contract={k:v for k,v in byquery[qid].items() if k in public_keys}
        contract['query_id']=anon
        public_contracts[qid]=contract
        groups[qid].append({'pair_id':pid,'query_id':anon,'query':row['query'],'document':row['document']})
        mapping.append({'pair_id':pid,'query_id':qid,'document_id':did,'query':row['query'],
                        'source':row['source'],'query_cohort':row.get('query_cohort','main'),
                        'catalog_text_sha256':hashlib.sha256(row['catalog_text'].encode()).hexdigest(),
                        'review_document_sha256':canonical_hash(row['document'])})
    if len({r['pair_id'] for r in mapping})!=len(candidate_rows):raise ValueError('Anonymous pair collision')
    write_once(root/'private/pair-mapping.jsonl',sorted(mapping,key=lambda r:r['pair_id']),jsonl=True)
    ordered=sorted(groups,key=lambda q:hashlib.sha256(('20260909:'+q).encode()).hexdigest())
    packets=[];pending=[];member=[]
    def flush():
        if pending:
            packets.append(make_packet(root,f'{prefix}-{len(packets)+1:02d}',list(pending),[public_contracts[q] for q in member]))
            pending.clear();member.clear()
    for qid in ordered:
        if len(groups[qid])>max_pairs:raise ValueError('One query exceeds packet capacity; explicit splitting is required')
        if len(pending)+len(groups[qid])>max_pairs:flush()
        pending.extend(sorted(groups[qid],key=lambda r:r['pair_id']));member.append(qid)
    flush()
    frozen={'status':'BLIND_PACKETS_READY_NOT_LABELS','pairs':len(candidate_rows),'queries':len(groups),
            'rule_sha256':RUBRIC_SHA,'candidate_rows_sha256':sha(candidate_path),
            'candidate_rows_path':str(Path(candidate_path).resolve()),
            'query_contracts_path':str(Path(contracts_path).resolve()),
            'query_contracts_sha256':sha(contracts_path),'pool_provenance_path':str(Path(provenance_path).resolve()),
            'pool_provenance_sha256':sha(provenance_path),'pair_mapping_sha256':sha(root/'private/pair-mapping.jsonl'),
            'packets':packets,'code_sha256':sha(Path(__file__))}
    write_once(root/'packets/MANIFEST.json',frozen)
    return frozen

def read_review(root,review_id,packet_id):
    return verify_collected(review_id,packet_id,root)

def verify_pool_inputs(root,inputs):
    for name in ('candidate_rows','query_contracts','pool_provenance'):
        if sha(inputs[name+'_path'])!=inputs[name+'_sha256']:
            raise ValueError('Original pool input changed: '+name)
    provenance=read_json(inputs['pool_provenance_path'])
    if provenance.get('status')=='TRAINING_CANDIDATE_POOL_READY_UNJUDGED':
        from run_training_pool import verify_pool_provenance
        verify_pool_provenance(inputs['pool_provenance_path'],inputs['pool_provenance_sha256'])
    elif provenance.get('status')=='ONE_ALL_GRID_TOP10_SUPPLEMENT_READY_NOT_JUDGED':
        from prepare_dev_topup import verify_pool_provenance
        verify_pool_provenance(inputs['pool_provenance_path'],inputs['pool_provenance_sha256'])
    elif provenance.get('status')=='FINAL_TEST_CANDIDATE_POOL_READY_UNJUDGED':
        from run_final_test_pool import verify_pool_provenance
        verify_pool_provenance(inputs['pool_provenance_path'],inputs['pool_provenance_sha256'])
    if any(provenance.get(name+'_sha256')!=inputs[name+'_sha256'] for name in ('candidate_rows','query_contracts')):
        raise ValueError('Pool producer binding differs')
    candidates=rows(inputs['candidate_rows_path']);contracts=rows(inputs['query_contracts_path'])
    byquery=validate_candidate_inputs(candidates,contracts)
    expected={};public={}
    public_keys={'query','core_product','core_purpose_requirements','substitutable_attributes',
                 'required_attribute_keys','intent_policy','interpretation_notes','no_added_requirements'}
    for row in candidates:
        qid,did=row['query_id'],row['document_id']
        pid=hashlib.sha256((str(root)+'\0'+qid+'\0'+did).encode()).hexdigest()[:24]
        anon='q-'+hashlib.sha256((str(root)+'\0'+qid).encode()).hexdigest()[:16]
        expected[pid]={'pair_id':pid,'query_id':qid,'document_id':did,'query':row['query'],
            'source':row['source'],'query_cohort':row.get('query_cohort','main'),
            'catalog_text_sha256':hashlib.sha256(row['catalog_text'].encode()).hexdigest(),
            'review_document_sha256':canonical_hash(row['document'])}
        public[pid]=({'pair_id':pid,'query_id':anon,'query':row['query'],'document':row['document']},
                     {**{k:v for k,v in byquery[qid].items() if k in public_keys},'query_id':anon})
    mapping=rows(root/'private/pair-mapping.jsonl')
    if len(mapping)!=len(expected) or {r['pair_id']:r for r in mapping}!=expected:
        raise ValueError('Private mapping differs from actual original candidates')
    seen=set()
    for packet in inputs['packets']:
        _,_,pairs,query_contracts=validate_packet(root,packet['packet_id'])
        qb={r['query_id']:r for r in query_contracts}
        for pair in pairs:
            pid=pair['pair_id']
            if pid in seen or pid not in public or pair!=public[pid][0] or qb[pair['query_id']]!=public[pid][1]:
                raise ValueError('Blind packet differs from actual original candidate/contract')
            seen.add(pid)
    if seen!=set(expected):raise ValueError('Original pool coverage differs')
    return expected

def prepare_third(root,packet_id):
    root=workspace_path(root);number=packet_id.rsplit('-',1)[1]
    a,ar=read_review(root,'A'+number,packet_id);b,br=read_review(root,'B'+number,packet_id)
    if set(a)!=set(b) or ar['model_metadata']['source_thread_id']==br['model_metadata']['source_thread_id']:
        raise ValueError('A/B coverage or independent context mismatch')
    different={p for p in a if a[p]['grade']!=b[p]['grade']}
    request={'packet_id':packet_id,'disagreement_pair_ids':sorted(different),'disagreement_pairs':len(different),
             'pairs':len(a),'agreement_pairs':len(a)-len(different),'agreement_is_not_accuracy':True,
             'A_collected_sha256':sha(root/'reviews'/('A'+number)/'COLLECTED.json'),
             'B_collected_sha256':sha(root/'reviews'/('B'+number)/'COLLECTED.json')}
    if different:
        packet=root/'packets'/packet_id
        selected=[r for r in rows(packet/'pairs.jsonl') if r['pair_id'] in different]
        qids={r['query_id'] for r in selected}
        request['third_packet']=make_packet(root,'third-'+packet_id,selected,
            [r for r in rows(packet/'query-contracts.jsonl') if r['query_id'] in qids])
    write_once(root/'adjudication/requests'/f'{packet_id}.json',request)
    return request

def freeze(root,*,label_version,status,selected_train_sha256=None):
    root=workspace_path(root);inputs=read_json(root/'packets/MANIFEST.json')
    mapping_path=root/'private/pair-mapping.jsonl'
    if sha(mapping_path)!=inputs['pair_mapping_sha256']:raise ValueError('Original identity mapping changed')
    mapping=verify_pool_inputs(root,inputs);output=[];bindings=[]
    for packet in inputs['packets']:
        name=packet['packet_id'];number=name.rsplit('-',1)[1]
        request=prepare_third(root,name)
        a,ar=read_review(root,'A'+number,name);b,br=read_review(root,'B'+number,name)
        t,tr={},None
        if request['disagreement_pairs']:
            t,tr=read_review(root,'T'+number,'third-'+name)
            if set(t)!=set(request['disagreement_pair_ids']):raise ValueError('Third coverage mismatch')
            if tr['model_metadata']['source_thread_id'] in {ar['model_metadata']['source_thread_id'],br['model_metadata']['source_thread_id']}:
                raise ValueError('Third context reused')
        for rid,record in [('A'+number,ar),('B'+number,br),('T'+number,tr)]:
            if record is None:continue
            ip=root/'packets'/record['packet_id']/'INPUT_MANIFEST.json'
            cp=root/'reviews'/rid/'COLLECTED.json'
            bindings.append({'collected_path':str(cp),'collected_sha256':sha(cp),
                'input_manifest_path':str(ip),'input_manifest_sha256':sha(ip),'context_isolation':'no_prior_conversation'})
        for pair in rows(root/'packets'/name/'pairs.jsonl'):
            pid=pair['pair_id'];third=t.get(pid)
            grade,resolution=vote_grade(a[pid]['grade'],b[pid]['grade'],third['grade'] if third else None)
            supporting=next((v for v in [a[pid],b[pid],third] if v and v['grade']==grade),None)
            output.append({**mapping[pid],'document':pair['document'],'grade':grade,'resolution':resolution,
                'reason':supporting['reason'] if supporting and resolution!='review_no_majority' else '三方等级不同，保留UNKNOWN',
                'unknown_causes':['review_no_majority'] if resolution=='review_no_majority' else [supporting['unknown_reason']] if grade=='UNKNOWN' else [],
                'primary':a[pid],'review':b[pid],'third':third,'human_gold':False,
                'rule_sha256':RUBRIC_SHA,'label_source':'independent_context_model_silver_v6'})
    if len(output)!=len(mapping) or {r['pair_id'] for r in output}!=set(mapping):raise ValueError('Merged pool coverage mismatch')
    output.sort(key=lambda r:(r['query_id'],r['document_id']))
    target=root/'frozen'
    write_once(target/'qrels.jsonl',output,jsonl=True)
    manifest={'status':status,'label_version':label_version,'label_kind':'model_silver',
              'qrels_sha256':sha(target/'qrels.jsonl'),'rubric_sha256':RUBRIC_SHA,
              'pair_mapping_path':str(mapping_path),'pair_mapping_sha256':sha(mapping_path),
              'packets_manifest_sha256':sha(root/'packets/MANIFEST.json'),'review_bindings':bindings,
              'pairs':len(output),'grade_counts':dict(Counter(str(r['grade']) for r in output)),
              'resolution_counts':dict(Counter(r['resolution'] for r in output)),
              'selected_train_sha256':selected_train_sha256}
    write_once(target/'MANIFEST.json',manifest)
    write_once(target/'COMPLETE.json',{'status':status,'manifest_sha256':sha(target/'MANIFEST.json'),
                                     'qrels_sha256':sha(target/'qrels.jsonl'),'pairs':len(output)})
    return manifest

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['build','prepare-third','freeze'])
    parser.add_argument('--workspace',required=True,type=Path)
    parser.add_argument('--candidates',type=Path);parser.add_argument('--contracts',type=Path)
    parser.add_argument('--provenance',type=Path);parser.add_argument('--prefix');parser.add_argument('--packet')
    parser.add_argument('--label-version');parser.add_argument('--status');parser.add_argument('--selected-train-sha256')
    args=parser.parse_args(argv)
    if args.command=='build':
        if not all([args.candidates,args.contracts,args.provenance,args.prefix]):parser.error('build inputs required')
        result=build(args.workspace,args.candidates,args.contracts,args.provenance,prefix=args.prefix)
    elif args.command=='prepare-third':result=prepare_third(args.workspace,args.packet)
    else:
        if not args.label_version or not args.status:parser.error('freeze label version/status required')
        result=freeze(args.workspace,label_version=args.label_version,status=args.status,selected_train_sha256=args.selected_train_sha256)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
