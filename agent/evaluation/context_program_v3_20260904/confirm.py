"""Confirm only after same-application-source regression and development pass."""
import json
import subprocess
import sys
import os
from .common import HERE, ROOT, file_sha, json_new, now
from .audit_ledger import audit

if __name__=='__main__':
    sources={};gates={}
    for attempt in ('regression004','dev003'):
        out=HERE/'p4'/attempt
        result=json.loads((out/'result.json').read_text(encoding='utf-8'))
        integrity=json.loads((out/'integrity_v2.json').read_text(encoding='utf-8'))
        assert result['status']=='PASS_EXECUTION' and integrity['status']=='PASS_RUNNER_EVIDENCE_EDGES'
        for row in json.loads((out/'source_freeze.json').read_text(encoding='utf-8'))['sources']:
            if row['path'].startswith('agent/app/'):
                assert file_sha(ROOT/row['path'])==row['sha256'],'application_source_changed'
                sources[row['path']]=row['sha256']
        gates[attempt]={n:file_sha(out/n) for n in ('result.json','integrity_v2.json','source_freeze.json')}
    dataset=HERE/'p1/breadth001/confirm.jsonl';plan=HERE/'p4/analysis_plan_v1.json'
    assert json.loads(plan.read_text(encoding='utf-8'))['datasetSha256']==file_sha(dataset)
    budget=audit();assert budget['status']=='PASS_LEDGER_EDGES'
    json_new(HERE/'p4/confirm001_readiness.json',{'at':now(),'gates':gates,'appSourceCount':len(sources),
        'datasetSha256':file_sha(dataset),'analysisPlanSha256':file_sha(plan),'budget':budget,
        'noExpansionAfterOutcomes':True,'executionDoesNotWaiveCostOrNIGates':True})
    command=[sys.executable,'-m','agent.evaluation.context_program_v3_20260904.launch_multiturn',
        '--attempt','confirm001','--dataset',str(dataset)]
    raise SystemExit(subprocess.call(command,cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
