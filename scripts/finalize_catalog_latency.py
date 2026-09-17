"""Retain the delivered code and bounded acceptance evidence without secrets."""
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys

REPO=Path(__file__).resolve().parents[1];sys.path.insert(0,str(REPO))
from agent.app.catalog_fast_selection import validated_selection

ROOT=Path('D:/agent-datasets/catalog-latency-20260913-v1')
OUT=ROOT/'delivery-v1';OUT.mkdir(exist_ok=False)
read=lambda p:json.loads(Path(p).read_text('utf-8-sig'))
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
selected=read(ROOT/'selected-v3.json')
validated_selection(ROOT,3,selected['modelPath'])
quality=read(ROOT/'dev-eval004/RESULTS.json')
online=read(ROOT/'online-latency004/RESULTS.json')
live=read(ROOT/'live-final001/RUN-RESULTS.json')
browser=read(ROOT/'browser-final001/results.json')
assert quality['status']=='DEV_GATE_PASS' and quality['verifiedConcurrentQueries']==40
assert online['status']=='PASS' and online['count']==10
assert live['complete'] and all(r['status']=='completed' and r['route_pass'] and r['action_pass'] for r in live['turns'])
assert all(r['cards_count']==0 for r in live['turns'] if r['route']=='catalog')
assert browser['stats']['expected']==1 and browser['stats']['unexpected']==0
assert read(ROOT/'health-final.json')['status']=='ok'
for source,receipt in selected['ann'].items():
    assert sha(ROOT/'ann-v2'/source/'COMPLETE.json')==receipt['sha256']
files=[
 'agent/app/catalog_worker.py','agent/app/catalog_service.py','agent/app/catalog_conversation.py',
 'agent/app/catalog_model_client.py','agent/app/catalog_fast_selection.py','agent/app/catalog_fast_retrieval.py',
 'agent/app/catalog_fast_retrieval_v2.py','agent/app/catalog_fast_retrieval_v3.py','agent/app/settings.py','agent/app/main.py',
 'scripts/merged-commerce.ps1','scripts/measure_catalog_online_latency.py','scripts/verify_catalog_workspace_live.py',
 'scripts/finalize_catalog_latency.py','frontend/tests/catalog-live.spec.ts',
 'agent/tests/test_catalog_fast_selection.py','agent/tests/test_catalog_fast_retrieval.py',
 'agent/tests/test_catalog_parallel_read.py','agent/tests/test_catalog_model_client.py']
files += [str(p.relative_to(REPO)).replace('\\','/') for p in (REPO/'experiments/catalog-latency-v1').glob('*.py')]
files += [str(p.relative_to(REPO)).replace('\\','/') for p in (REPO/'docs/data/catalog-latency-20260913').glob('*.md')]
code={}
for name in files:
    target=OUT/'code'/name;target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(REPO/name,target);code[name]=sha(target)
evidence=[
 'selected.json','selected-v2.json','selected-v3.json','dev-eval001/RESULTS.json',
 'dev-eval002/RESULTS.json','dev-eval003/RESULTS.json','dev-eval004/RESULTS.json',
 'sqlite-eval001/RESULTS.json','sqlite-eval003/RESULTS.json',
 'online-latency001/RESULTS.json','online-latency002/RESULTS.json','online-latency003/RESULTS.json','online-latency004/RESULTS.json',
 'online-latency004/CONTRACT.json','live-final001/RUN-RESULTS.json','browser-final001/results.json',
 'browser-final001/catalog-desktop.png','browser-final001/catalog-mobile.png','health-final.json',
 'pytest-integration003.xml','pytest-worker-final-v3.xml','pytest-client-final.xml']
manifest={'status':'COMPLETE_BOUNDED_ACCEPTANCE','createdUtc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'activeRuntimeVersion':3,'pooledModelClient':True,'code':code,'evidence':{p:sha(ROOT/p) for p in evidence},
 'results':{'ordinaryQueries':10,'p50Seconds':online['p50Seconds'],'maxSeconds':online['maxSeconds'],
     'complexBudgetCaseSeconds':next(r['seconds'] for r in live['turns'] if r['case']=='S5'),
     'mainTop10SetUnchanged':33,'fullyJudgedPairedQueries':29,'meanNdcgDelta':quality['pairedMeanNdcgDelta'],
     'realMultiTurnOperationsCompleted':8,'browserSubmittedToCompletedSeconds':6.921},
 'limits':['Exposed development silver; no new blind test','10 sequential samples, no universal or concurrent latency SLA',
     'Startup loading/warmup is separate; remote model latency remains variable',
     'Complex budget query took 12.575 seconds; original phone model-matching gap remains'],
 'preservation':'Original data, qrels, vectors, model weights and failed derived experiments retained'}
(OUT/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'status':manifest['status'],'codeFiles':len(code),'evidenceFiles':len(evidence),'output':str(OUT)},ensure_ascii=False))
