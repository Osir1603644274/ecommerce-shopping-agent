"""Materialize the passing configuration without replacing version 1."""
import hashlib
import json
from pathlib import Path

ROOT=Path('D:/agent-datasets/catalog-latency-20260913-v1')
report_path=ROOT/'dev-eval003/RESULTS.json'
report=json.loads(report_path.read_text('utf-8'))
runtime=json.loads((report_path.parent/'runtime.json').read_text('utf-8'))
contract=json.loads((report_path.parent/'CONTRACT.json').read_text('utf-8'))
assert report['status']=='DEV_GATE_PASS'
value={'status':'VERIFIED_DEV_CONFIGURATION','runtimeVersion':2,
    'evaluation':str(report_path.relative_to(ROOT)).replace('\\','/'),
    'evaluationSha256':hashlib.sha256(report_path.read_bytes()).hexdigest(),
    'parameters':report['parameters'],'fastCodeSha256':runtime['fastCodeSha256'],
    'parentFastCodeSha256':runtime['parentFastCodeSha256'],'ann':runtime['ann'],'modelPath':contract['model']}
with (ROOT/'selected-v2.json').open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
print(json.dumps(value,ensure_ascii=False,indent=2))
