"""Publish a separately retained, full-path verified storage configuration."""
import hashlib
import json
from pathlib import Path

ROOT=Path('D:/agent-datasets/catalog-latency-20260913-v1')
path=ROOT/'dev-eval004/RESULTS.json'
report=json.loads(path.read_text('utf-8'))
runtime=json.loads((path.parent/'runtime.json').read_text('utf-8'))
contract=json.loads((path.parent/'CONTRACT.json').read_text('utf-8'))
assert report['status']=='DEV_GATE_PASS' and report['verifiedConcurrentQueries']==40
assert report['fullChannelsExactlyEqualV2'] and report['top10OrderExactlyEqualV2']
value={'status':'VERIFIED_DEV_CONFIGURATION','runtimeVersion':3,
    'evaluation':'dev-eval004/RESULTS.json','evaluationSha256':hashlib.sha256(path.read_bytes()).hexdigest(),
    'parameters':report['parameters'],'fastCodeSha256':runtime['fastCodeSha256'],
    'parentFastCodeSha256':runtime['parentFastCodeSha256'],'indexRuntimeCodeSha256':runtime['indexRuntimeCodeSha256'],
    'ann':runtime['ann'],'modelPath':contract['model'],'storagePolicy':runtime['storagePolicy']}
with (ROOT/'selected-v3.json').open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
print('Verified version 3 configuration saved')
