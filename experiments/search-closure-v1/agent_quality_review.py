"""Bound independent review of actual paired Agent answers; never assign grades."""
import argparse
from collections import Counter
import json
from pathlib import Path
import numpy as np
import reviews
import run_agent_pairs as runner
from retrieval_runtime import read_json,sha,write_once,fingerprint
from review_workspace import exact_copy

ROOT=Path('D:/agent-datasets/search-closure-v1/agent-quality-review')
CHECKS=('citation_integrity','grounding','explicit_requirements','uncertainty_disclosure','commerce_boundary')
VERDICTS={'PASS','FAIL','UNKNOWN','NOT_ASSESSABLE'}

def validate(values,packet):
    expected={(r['review_query_id'],a['label']):(r,a) for r in packet for a in r['answers']}
    seen={}
    for row in values:
        if set(row)!={'review_query_id','label','checks','useful_grade','reason','findings'}:raise ValueError('Quality row schema differs')
        key=(row['review_query_id'],row['label'])
        if key not in expected or key in seen:raise ValueError('Quality answer identity absent or duplicate')
        _,answer=expected[key];checks=row['checks'];grade=row['useful_grade']
        if not isinstance(checks,dict) or set(checks)!=set(CHECKS) or any(v not in VERDICTS for v in checks.values()):raise ValueError('Quality check schema differs')
        if not isinstance(row['reason'],str) or not row['reason'].strip() or not isinstance(row['findings'],list):raise ValueError('Quality explanation missing')
        if answer['run_outcome']!='SUCCEEDED':
            if grade!='NOT_ASSESSABLE' or set(checks.values())!={'NOT_ASSESSABLE'} or row['findings']:
                raise ValueError('Failed/ambiguous run was promoted to an assessed answer')
        else:
            if (type(grade) is not int or grade not in range(4)) and grade!='UNKNOWN':raise ValueError('Invalid useful grade for successful run')
            if 'NOT_ASSESSABLE' in checks.values():raise ValueError('Successful answer skipped')
        hits={h['docid']:h['text'] for h in answer['evidence']};covered=set()
        for finding in row['findings']:
            if set(finding)!={'criterion','answer_quote','docid','evidence_quote','explanation'}:raise ValueError('Finding schema differs')
            if finding['criterion'] not in CHECKS or any(not isinstance(v,str) for v in finding.values()) or not finding['explanation'].strip():raise ValueError('Finding fields invalid')
            quote=finding['answer_quote']
            if not quote or quote not in (answer['answer'] or ''):raise ValueError('Finding answer quote not in actual answer')
            did=finding['docid'];evidence=finding['evidence_quote']
            if did:
                if did not in hits or not evidence or evidence not in hits[did]:raise ValueError('Finding evidence not in this answer original documents')
            elif evidence:raise ValueError('Evidence quote without document identity')
            covered.add(finding['criterion'])
        if any(v in ('FAIL','UNKNOWN') and k not in covered for k,v in checks.items()):raise ValueError('Failure/uncertainty lacks actual finding')
        seen[key]=row
    if set(seen)!=set(expected):raise ValueError('Quality review does not cover every supplied answer')
    return seen

def verify_run(path,expected):
    path=Path(path).resolve()
    if not path.is_relative_to(runner.ROOT.resolve()) or path.name!='COMPLETE.json' or sha(path)!=expected:raise ValueError('Actual Agent completion differs')
    complete=read_json(path);out=path.parent
    if complete.get('status')!='ALL40_SLOTS_TERMINAL_NOT_QUALITY_APPROVED':raise ValueError('Actual Agent run not complete')
    report=read_json(out/'report.json');prepared=read_json(out/'PREPARED.json')
    if sha(out/'report.json')!=complete['report_sha256'] or sha(out/'PREPARED.json')!=complete['prepared_sha256']:raise ValueError('Agent report/preparation changed')
    for item in prepared['inputs']+prepared['code']:
        if sha(item['path'])!=item['sha256']:raise ValueError('Agent frozen source changed')
    for name,wanted in prepared['files'].items():
        if sha(out/name)!=wanted:raise ValueError('Agent prepared input changed')
    schedule=reviews.rows(out/'schedule.jsonl');receipts=[]
    if len(schedule)!=40 or len(report['receipt_hashes'])!=40:raise ValueError('Incomplete Agent run')
    hash_by={r['slot']:r['sha256'] for r in report['receipt_hashes']}
    if len(hash_by)!=40:raise ValueError('Duplicate Agent slot receipts')
    for slot in schedule:
        rp=out/'attempts'/f"slot-{slot['slot']:03d}"/'RECEIPT.json'
        if sha(rp)!=hash_by[slot['slot']]:raise ValueError('Actual Agent answer receipt changed')
        receipt=read_json(rp)
        if receipt['identity']!={'slot':slot,'prepared_sha256':complete['prepared_sha256']}:raise ValueError('Actual answer identity differs')
        for event in receipt['events']:
            ep=(rp.parent/event['name']).resolve()
            if not ep.is_relative_to((rp.parent/'events').resolve()) or sha(ep)!=event['sha256']:raise ValueError('Actual Agent observation changed')
        receipts.append(receipt)
    packet,mapping=runner.blind_packet(receipts)
    if packet!=reviews.rows(out/'blind-review/packet.jsonl') or mapping!=reviews.rows(out/'private/blind-mapping.jsonl'):
        raise ValueError('Blind answers differ from actual paired receipts')
    if sha(out/'blind-review/packet.jsonl')!=complete['blind_packet_sha256']:raise ValueError('Original blind packet changed')
    return packet,mapping,report

def prepare(args):
    packet,_,_=verify_run(args.agent_complete,args.agent_complete_sha256)
    source=Path(args.agent_complete).parent/'blind-review';target=ROOT/'packets/agent-01'
    for name in ['packet.jsonl','INSTRUCTIONS.json','AGENT_QUALITY_RUBRIC.md','INPUT_MANIFEST.json']:exact_copy(source/name,target/name)
    original=read_json(target/'INPUT_MANIFEST.json')
    for name,wanted in original['files'].items():
        if sha(target/name)!=wanted:raise ValueError('Original Agent blind input changed')
    write_once(ROOT/'INPUTS.json',{'status':'ACTUAL_AGENT_ANSWERS_READY_FOR_INDEPENDENT_REVIEW',
        'agent_complete':{'path':str(Path(args.agent_complete).resolve()),'sha256':args.agent_complete_sha256},
        'packet_input_sha256':sha(target/'INPUT_MANIFEST.json'),'answer_count':sum(len(p['answers']) for p in packet)})
    return {'status':'AGENT_QUALITY_PACKETS_READY_UNJUDGED','answers':40,'path':str(target)}

def collect(rid):
    if rid not in ('A01','B01','T01'):raise ValueError('Invalid quality reviewer')
    inputs=read_json(ROOT/'INPUTS.json');verify_run(inputs['agent_complete']['path'],inputs['agent_complete']['sha256'])
    dispatch=read_json(ROOT/'dispatch'/f'{rid}.json')
    if (dispatch.get('review_id')!=rid or dispatch.get('role')!=rid[0] or dispatch.get('fresh_context') is not True
        or dispatch.get('packet_id')!=('third-agent-01' if rid=='T01' else 'agent-01')):raise ValueError('Quality reviewer identity/context differs')
    completion=reviews.completion_binding(ROOT,rid,dispatch)
    if completion is None:raise ValueError('Quality reviewer is not actually complete')
    packet_path=ROOT/'packets'/dispatch['packet_id'];manifest=read_json(packet_path/'INPUT_MANIFEST.json')
    wanted=inputs['packet_input_sha256'] if rid!='T01' else read_json(ROOT/'THIRD_REQUEST.json')['input_manifest_sha256']
    if sha(packet_path/'INPUT_MANIFEST.json')!=wanted:raise ValueError('Quality input anchor changed')
    output=reviews.find_output(dispatch['projectlessOutputDirectory'],'judgments.jsonl')
    receipt=reviews.find_output(dispatch['projectlessOutputDirectory'],'receipt.json')
    local=output.parent/'input'
    if not local.is_dir():local=Path(dispatch['projectlessOutputDirectory']).parent/'input'
    for name,digest in {**manifest['files'],'INPUT_MANIFEST.json':wanted}.items():
        if sha(packet_path/name)!=digest or sha(local/name)!=digest:raise ValueError('Actual author quality inputs changed')
    authored=read_json(receipt)
    if (authored.get('human_gold') is not False or authored.get('self_review_completed') is not True
        or authored.get('label_source')!='independent_context_agent_quality_silver_v1'):raise ValueError('Quality author completion/silver disclosure missing')
    if not {*manifest['files'].values(),wanted,sha(output)}<=reviews.receipt_hashes(authored):raise ValueError('Quality author receipt lacks exact file bindings')
    values=reviews.rows(output);validate(values,reviews.rows(packet_path/'packet.jsonl'))
    model=reviews.first_model_metadata(dispatch['threadId'])
    record={'review_id':rid,'source_thread_id':dispatch['threadId'],'model_metadata':model,
        'judgments_sha256':sha(output),'receipt_sha256':sha(receipt),'input_manifest_sha256':wanted,
        'completion_event_sha256':sha(completion[0]),'answers':len(values),'human_gold':False,**completion[1]}
    target=ROOT/'reviews'/rid
    exact_copy(output,target/'judgments.jsonl');exact_copy(receipt,target/'receipt.json')
    write_once(target/'COLLECTED.json',record)
    return { (v['review_query_id'],v['label']):v for v in values},record

def decision(row):return {'checks':row['checks'],'useful_grade':row['useful_grade']}

def third():
    a,ar=collect('A01');b,br=collect('B01')
    if ar['source_thread_id']==br['source_thread_id']:raise ValueError('Quality contexts reused')
    different={k for k in a if decision(a[k])!=decision(b[k])}
    source=ROOT/'packets/agent-01';packet=[]
    for row in reviews.rows(source/'packet.jsonl'):
        answers=[a for a in row['answers'] if (row['review_query_id'],a['label']) in different]
        if answers:packet.append({**row,'answers':answers})
    target=ROOT/'packets/third-agent-01'
    write_once(target/'packet.jsonl',packet,jsonl=True)
    for name in ['INSTRUCTIONS.json','AGENT_QUALITY_RUBRIC.md']:exact_copy(source/name,target/name)
    write_once(target/'INPUT_MANIFEST.json',{'status':'UNJUDGED','files':{n:sha(target/n) for n in ['packet.jsonl','INSTRUCTIONS.json','AGENT_QUALITY_RUBRIC.md']},'answer_slots':len(different)})
    request={'disagreement_answers':len(different),'keys':sorted([list(k) for k in different]),
        'A_collected_sha256':sha(ROOT/'reviews/A01/COLLECTED.json'),'B_collected_sha256':sha(ROOT/'reviews/B01/COLLECTED.json'),
        'input_manifest_sha256':sha(target/'INPUT_MANIFEST.json')}
    write_once(ROOT/'THIRD_REQUEST.json',request);return request

def majority(values):
    counts=Counter(values)
    found=[value for value,n in counts.items() if n>=2]
    return found[0] if found else 'UNKNOWN'

def paired_usefulness(records):
    groups={s:{} for s in ['kuaisearch','multicpr']}
    for r in records:groups[r['source']].setdefault(r['query_id'],{})[r['arm']]=r['useful_grade']
    if any(len(g)!=10 for g in groups.values()):raise ValueError('Quality source pairing differs')
    arrays=[]
    for group in groups.values():
        values=[]
        for qid,arms in sorted(group.items()):
            if set(arms)!={'baseline','winner'}:raise ValueError('Missing quality arm')
            if any(type(v) is not int for v in arms.values()):return {'status':'INCOMPLETE_QUALITY_EVIDENCE','eligible_for_activation':False}
            values.append(arms['winner']-arms['baseline'])
        arrays.append(np.array(values,dtype=float))
    rng=np.random.default_rng(20260909);draws=sum(a[rng.integers(0,len(a),(10000,len(a)))].mean(axis=1) for a in arrays)/2
    interval=np.quantile(draws,[.025,.975]).tolist()
    return {'status':'COMPLETE_PAIRED_MODEL_SILVER_QUALITY','mean_difference':float(sum(a.mean() for a in arrays)/2),
        'bootstrap95':interval,'eligible_for_activation':interval[0]>0,'repetitions':10000,'seed':20260909}

def finalize():
    request=third();a,ar=collect('A01');b,br=collect('B01');t={};tr=None
    if request['disagreement_answers']:
        t,tr=collect('T01')
        if set(t)!={tuple(k) for k in request['keys']} or tr['source_thread_id'] in (ar['source_thread_id'],br['source_thread_id']):raise ValueError('Third quality coverage/context differs')
    inputs=read_json(ROOT/'INPUTS.json');_,mapping,run_report=verify_run(inputs['agent_complete']['path'],inputs['agent_complete']['sha256'])
    keys={(r['review_query_id'],r['label']):r for r in mapping};records=[]
    dev=runner.evaluation.load_queries(reviews.rows(runner.evaluation.QUERIES))
    for key in sorted(a):
        rows=[a[key],b[key]]+([t[key]] if key in t else [])
        records.append({**keys[key],'source':dev[keys[key]['query_id']]['source'],
            'checks':{c:majority([r['checks'][c] for r in rows]) for c in CHECKS},
            'useful_grade':majority([r['useful_grade'] for r in rows]),'primary':a[key],'review':b[key],'third':t.get(key),'human_gold':False})
    quality=paired_usefulness(records);counts={}
    for arm in ('baseline','winner'):
        subset=[r for r in records if r['arm']==arm]
        counts[arm]={c:dict(Counter(r['checks'][c] for r in subset)) for c in CHECKS}
    winner=[r for r in records if r['arm']=='winner']
    reliable=all(r['checks'][c]=='PASS' for r in winner for c in CHECKS if c!='uncertainty_disclosure')
    constraints=all(w['checks']['explicit_requirements']!='FAIL' for w in winner)
    timings={arm:run_report['arms'][arm]['completed_wall'] for arm in ('baseline','winner')}
    latency=(all(t['n']==20 and t['p95_seconds'] is not None for t in timings.values())
        and timings['winner']['p95_seconds']<=timings['baseline']['p95_seconds']*1.2)
    report={'status':'INDEPENDENT_AGENT_QUALITY_REVIEW_COMPLETE','answer_count':40,'model_silver':True,
        'check_counts':counts,'usefulness':quality,'latency':timings,'latency_gate':latency,
        'winner_required_checks_all_pass':reliable,'no_winner_hard_constraint_failures':constraints,
        'agent_gate_passed':bool(quality['eligible_for_activation'] and reliable and constraints and latency),
        'production_activated':False,'scope':'Only catalog_evidence Agent route. Search heldout evidence and integration regressions remain separate requirements.',
        'inputs':inputs,'review_bindings':[r for r in (ar,br,tr) if r]}
    write_once(ROOT/'frozen/answers.jsonl',records,jsonl=True)
    report['answers_sha256']=sha(ROOT/'frozen/answers.jsonl');write_once(ROOT/'frozen/report.json',report)
    write_once(ROOT/'frozen/COMPLETE.json',{'status':report['status'],'report_sha256':sha(ROOT/'frozen/report.json')})
    return {'status':report['status'],'agent_gate_passed':report['agent_gate_passed']}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['prepare','collect','third','finalize'])
    p.add_argument('--agent-complete',type=Path);p.add_argument('--agent-complete-sha256');p.add_argument('--review-id')
    args=p.parse_args()
    result=prepare(args) if args.command=='prepare' else collect(args.review_id)[1] if args.command=='collect' else third() if args.command=='third' else finalize()
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
