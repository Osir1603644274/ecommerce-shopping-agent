"""Measure real single-query latency only when final dev quality scores tie."""
import argparse
import json
import math
import random
import time
from pathlib import Path
import baseline_eval as evaluation
from select_final_configuration import choose,arm
from train_pairwise import GRID_CE_METHODS,RunLock
from agent_retrieval_adapter import AgentRetrievalRuntime
from retrieval_runtime import read_json,sha,write_once,fingerprint
from run_agent_pairs import percentile
from reviews import rows

ROOT=Path('D:/agent-datasets/search-closure-v1')

def tied_methods(report):
    summary=report['common_conditional']['main']['methods']
    decisions=[choose(summary,list(GRID_CE_METHODS)),choose(summary,report['methods'])]
    if any(d['status']=='NO_VALID_SELECTION' for d in decisions):raise ValueError('Latency cannot repair missing quality denominators')
    return sorted({m for d in decisions if d['status']=='NEEDS_ACTUAL_LATENCY_TIE_BREAK' for m in d['methods']})

def schedule(queries,methods):
    ordered=sorted(queries,key=lambda q:q['query_id']);random.Random(20260909).shuffle(ordered)
    result=[]
    for i,q in enumerate(ordered):
        order=methods[i%len(methods):]+methods[:i%len(methods)]
        for method in order:result.append({'slot':len(result)+1,'query':q,'method':method})
    return result

def run_slots(runtime,out,binding,slots,*,verify_only=False):
    records=[]
    for slot in slots:
        directory=out/'slots'/f"{slot['slot']:04d}";rp=directory/'RECEIPT.json'
        identity={'binding_sha256':fingerprint(binding),'slot':slot}
        if rp.exists():
            record=read_json(rp)
            if record['identity']!=identity or record['status']!='COMPLETE':raise ValueError('Existing latency slot is not valid')
        else:
            if verify_only:raise ValueError('Missing original latency receipt during read-only verification')
            if (directory/'STARTED.json').exists():raise ValueError('Interrupted latency measurement retained; do not silently rerun it')
            write_once(directory/'STARTED.json',identity)
            spec=binding['arms'][slot['method']];q=slot['query'];start=time.perf_counter()
            result=runtime.search_one(q['query'],q['source'],profile=spec['profile'],model_path=spec['model_path'],limit=10)
            elapsed=time.perf_counter()-start
            record={'status':'COMPLETE','identity':identity,'single_query_seconds':elapsed,'actual_result':result}
            write_once(rp,record)
        if type(record['single_query_seconds']) not in (int,float) or not math.isfinite(record['single_query_seconds']) or record['single_query_seconds']<0:
            raise ValueError('Invalid observed latency')
        records.append(record)
    return records

def execute(args):
    evidence=evaluation.Evidence()
    completed=evidence.json(evaluation.safe_input(args.evaluation_complete),args.evaluation_complete_sha256)
    report=evidence.json(Path(args.evaluation_complete).parent/'report.json',completed['report_sha256'])
    final=evidence.json(evaluation.V6/'frozen/COMPLETE.json')
    if (final.get('status')!='FINAL_DEVELOPMENT_QRELS_FROZEN_MODEL_SILVER'
        or report.get('status')!='DEVELOPMENT_DESCRIPTIVE_EVALUATION_COMPLETE'
        or report['qrels']['sha256']!=final['qrels_sha256']):raise ValueError('Actual final development evaluation required')
    for ref in report['input_evidence']:evidence.file(ref['path'],ref['sha256'])
    grid=Path(args.grid);complete=evidence.json(grid/'COMPLETE.json',args.grid_complete_sha256)
    gb=evidence.json(grid/'binding.json',complete['binding_sha256'])
    if complete['rankings_sha256']!=report['rankings']['sha256'] or complete['methods']!=report['methods']:
        raise ValueError('Tied evaluation differs from registered actual grid')
    methods=tied_methods(report)
    if not methods:return {'status':'NO_LATENCY_TIE_BREAK_NEEDED','model_calls':0}
    queries=evaluation.load_queries(evidence.rows(evaluation.QUERIES,evaluation.FIXED_QUERY_METADATA_SHA256))
    arms={method:arm(method,gb['models']) for method in methods}
    for name in ['run_dev_latency.py','run_agent_pairs.py','agent_retrieval_adapter.py','retrieval_runtime.py','select_final_configuration.py','baseline_eval.py','metrics_v2.py']:
        evidence.file(Path(__file__).with_name(name))
    out=ROOT/'latency-tie-break';binding={'status':'FINAL_DEV_TIED_METHODS_ONLY','arms':arms,
        'grid_complete_sha256':args.grid_complete_sha256,'inputs':list(evidence.files.values()),
        'queries_sha256':fingerprint(list(queries.values())),'seed':20260909,
        'scope':'Actual single-query full search on all40 fixed dev queries, including diagnostic queries; observed process/OS/model cache state, startup asset hashing excluded.'}
    evidence.recheck();write_once(out/'BINDING.json',binding)
    slots=schedule(list(queries.values()),methods);write_once(out/'schedule.jsonl',slots,jsonl=True)
    with RunLock(out/'.run.lock'):
        runtime=AgentRetrievalRuntime()
        try:records=run_slots(runtime,out,binding,slots)
        finally:runtime.close()
    evidence.recheck()
    p95={method:percentile([r['single_query_seconds'] for r in records if r['identity']['slot']['method']==method],.95) for method in methods}
    result={'status':'REAL_DEV_LATENCY_TIE_BREAK_COMPLETE','grid_complete_sha256':args.grid_complete_sha256,
        'p95_seconds_by_method':p95,'queries_per_method':40,'binding_sha256':sha(out/'BINDING.json'),
        'schedule_sha256':sha(out/'schedule.jsonl'),'scope':binding['scope'],
        'receipts':[{'path':str(out/'slots'/f"{r['identity']['slot']['slot']:04d}"/'RECEIPT.json'),
            'sha256':sha(out/'slots'/f"{r['identity']['slot']['slot']:04d}"/'RECEIPT.json')} for r in records],
        'labels_modified':False,'test_accessed':False}
    write_once(out/'COMPLETE.json',result)
    return {'status':result['status'],'path':str(out/'COMPLETE.json'),'p95_seconds_by_method':p95}

def verify_latency(path,expected,grid_sha):
    path=Path(path).resolve();out=ROOT/'latency-tie-break'
    if path!=(out/'COMPLETE.json').resolve() or sha(path)!=expected:raise ValueError('Actual latency receipt differs')
    complete=read_json(path);binding=read_json(out/'BINDING.json')
    if (complete.get('status')!='REAL_DEV_LATENCY_TIE_BREAK_COMPLETE' or complete['grid_complete_sha256']!=grid_sha
        or sha(out/'BINDING.json')!=complete['binding_sha256'] or sha(out/'schedule.jsonl')!=complete['schedule_sha256']):
        raise ValueError('Latency source binding changed')
    for ref in binding['inputs']:
        if sha(ref['path'])!=ref['sha256']:raise ValueError('Actual latency frozen input changed')
    queries=evaluation.load_queries(rows(evaluation.QUERIES));methods=sorted(binding['arms'])
    slots=rows(out/'schedule.jsonl')
    if fingerprint(list(queries.values()))!=binding['queries_sha256'] or slots!=schedule(list(queries.values()),methods):
        raise ValueError('Latency used a different query/method schedule')
    expected_paths={(out/'slots'/f"{s['slot']:04d}"/'RECEIPT.json').resolve() for s in slots}
    refs=complete['receipts']
    if len(refs)!=len(expected_paths) or {Path(r['path']).resolve() for r in refs}!=expected_paths:raise ValueError('Incomplete latency receipts')
    for ref in refs:
        if sha(ref['path'])!=ref['sha256']:raise ValueError('Actual latency observation changed')
    # Every receipt must exist; no inference can be invoked during validation.
    records=run_slots(None,out,binding,slots,verify_only=True)
    actual={m:percentile([r['single_query_seconds'] for r in records if r['identity']['slot']['method']==m],.95) for m in methods}
    if actual!=complete['p95_seconds_by_method'] or complete['queries_per_method']!=40:raise ValueError('Latency summary differs from observed query timings')
    return complete

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evaluation-complete',type=Path,required=True);p.add_argument('--evaluation-complete-sha256',required=True)
    p.add_argument('--grid',type=Path,required=True);p.add_argument('--grid-complete-sha256',required=True)
    print(json.dumps(execute(p.parse_args())))

if __name__=='__main__':main()
