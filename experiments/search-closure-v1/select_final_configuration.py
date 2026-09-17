"""Choose once from final dev results, then seal baseline/winner before test access."""
import argparse
import math
import json
from pathlib import Path
from retrieval_runtime import sha,read_json,write_once,fingerprint,model_binding
from train_pairwise import GRID_CE_METHODS
from prepare_dev_topup import verify_training_cycle

ROOT=Path('D:/agent-datasets/search-closure-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6')

def choose(summaries,methods,latencies=None):
    if not methods or any(m not in summaries for m in methods):raise ValueError('Incomplete selection methods')
    usable=[m for m in methods if summaries[m]['equal_source_mean_lower'] is not None]
    if len(usable)!=len(methods):return {'status':'NO_VALID_SELECTION','method':None}
    highest=max(summaries[m]['equal_source_mean_lower'] for m in methods)
    tied=[m for m in methods if summaries[m]['equal_source_mean_lower']==highest]
    if len(tied)>1:
        if not latencies or any(m not in latencies for m in tied):
            return {'status':'NEEDS_ACTUAL_LATENCY_TIE_BREAK','methods':tied,'lower':highest}
        if any(not math.isfinite(latencies[m]) or latencies[m]<0 for m in tied):raise ValueError('Invalid real latency')
        minimum=min(latencies[m] for m in tied);tied=[m for m in tied if latencies[m]==minimum]
    def order(method):
        model=method.split('/')[-1];epoch=0 if model in ('none','base') else int(model[-1]) if model[-1:].isdigit() else 0
        return epoch,method
    return {'status':'SELECTED','method':min(tied,key=order),'lower':highest,'tied_at_lower_bound':len(tied)}

def arm(method,models):
    if '/' in method:
        profile,model=method.split('/');record=models.get(model)
        if model!='none' and record is None:raise ValueError('Unregistered model')
    else:profile,record=method,None
    if record is not None and model_binding(record['path'])!=record:raise ValueError('Actual chosen model binding differs')
    return {'method':method,'profile':profile,'model_path':record['path'] if record else None,
            'model_binding_sha256':fingerprint(record)}

def select(args):
    references=[]
    def bound(path,expected=None):
        actual=sha(path)
        if expected and actual!=expected:raise ValueError('Bound final input changed: '+str(path))
        references.append({'path':str(Path(path).resolve()),'sha256':actual});return read_json(path)
    completed=bound(args.evaluation_complete,args.evaluation_complete_sha256)
    report_path=Path(args.evaluation_complete).parent/'report.json'
    report=bound(report_path,completed['report_sha256'])
    final=bound(DEV/'frozen/COMPLETE.json')
    bound(DEV/'frozen/MANIFEST.json',final['manifest_sha256'])
    if (final.get('status')!='FINAL_DEVELOPMENT_QRELS_FROZEN_MODEL_SILVER'
        or report.get('status')!='DEVELOPMENT_DESCRIPTIVE_EVALUATION_COMPLETE'
        or report['qrels']['sha256']!=final['qrels_sha256']
        or sha(DEV/'frozen/qrels.jsonl')!=final['qrels_sha256']):raise ValueError('Final selection requires fully supplemented dev labels')
    for ref in report['input_evidence']:
        if sha(ref['path'])!=ref['sha256']:raise ValueError('Evaluation actual input changed')
        references.append({'path':ref['path'],'sha256':ref['sha256']})
    for filename,ref in report['outputs'].items():
        p=report_path.parent/filename
        if sha(p)!=ref['sha256']:raise ValueError('Evaluation output changed')
        references.append({'path':str(p),'sha256':ref['sha256']})
    grid=Path(args.grid);grid_complete=bound(grid/'COMPLETE.json',args.grid_complete_sha256)
    binding=bound(grid/'binding.json',grid_complete['binding_sha256'])
    if report['rankings']['sha256']!=grid_complete['rankings_sha256'] or report['methods']!=grid_complete['methods']:
        raise ValueError('Evaluation does not match complete registered grid')
    cycle=ROOT/'training-preparation/TRAINING_CYCLE_DECIDED.json'
    verify_training_cycle(cycle,sha(cycle),binding['models']);bound(cycle)
    methods={'bm25','character','dense'}|{f'{p}/{m}' for p in ('w111','w211','w112','no_dense') for m in ['none',*binding['models']]}
    if set(report['methods'])!=methods:raise ValueError('Final bounded method set differs')
    common=report['common_conditional']['main'];summaries=common['methods'];latency=None
    if args.latency:
        measured=bound(args.latency,args.latency_sha256)
        from run_dev_latency import verify_latency
        verify_latency(args.latency,args.latency_sha256,args.grid_complete_sha256)
        if measured.get('status')!='REAL_DEV_LATENCY_TIE_BREAK_COMPLETE' or measured.get('grid_complete_sha256')!=args.grid_complete_sha256:
            raise ValueError('Tie break lacks bound actual single-query measurements')
        latency=measured['p95_seconds_by_method']
    old=choose(summaries,list(GRID_CE_METHODS),latency);winner=choose(summaries,report['methods'],latency)
    if old['status']!='SELECTED' or winner['status']!='SELECTED':
        return {'status':'FINAL_SELECTION_PENDING','old':old,'winner':winner}
    if report['baseline']!=old['method']:raise ValueError('Final comparison must use final strongest old baseline')
    result={'status':'FINAL_SEARCH_SELECTION_FROZEN','seed':20260909,'qrels_sha256':final['qrels_sha256'],
        'strongest_old':arm(old['method'],binding['models']),'selected':arm(winner['method'],binding['models']),
        'old_selection':old,'winner_selection':winner,'selection_scope':'equal-source pooled nDCG lower bound on shared main dev queries',
        'eligibility':report['shared_eligibility']['main'],'comparison':common['comparisons'].get(winner['method']),
        'inputs':references,'new_test_accessed':False,'default_activation':False,'code_sha256':sha(Path(__file__))}
    out=ROOT/'final-selection';write_once(out/'SELECTION.json',result)
    return {'status':result['status'],'baseline':old['method'],'winner':winner['method'],'path':str(out/'SELECTION.json'),'sha256':sha(out/'SELECTION.json')}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evaluation-complete',type=Path,required=True);p.add_argument('--evaluation-complete-sha256',required=True)
    p.add_argument('--grid',type=Path,required=True);p.add_argument('--grid-complete-sha256',required=True)
    p.add_argument('--latency',type=Path);p.add_argument('--latency-sha256')
    print(json.dumps(select(p.parse_args()),ensure_ascii=False))

if __name__=='__main__':main()
