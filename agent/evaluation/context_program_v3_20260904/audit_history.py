"""Recheck every frozen historical manifest and each archived source copy."""
import json
from .common import HERE, file_sha, json_new, manifest_check, now
baseline=json.loads((HERE/'p0/baseline.json').read_text(encoding='utf-8'))
historical={}
for relative,expected in baseline['historicalManifests'].items():
    path=HERE.parent/relative;checked=manifest_check(path)
    historical[relative]={'passed':not checked['mismatches'] and file_sha(path)==expected['sha256'],
        'artifacts':checked['checked'],'manifestSha256':file_sha(path),'mismatches':checked['mismatches']}
archives={}
for path in (HERE/'p0').glob('repair*_sources/sources.json'):
    record=json.loads(path.read_text(encoding='utf-8'))
    mismatches=[r['path'] for r in record['sources'] if file_sha(path.parent/r['path'])!=r['sha256']]
    archives[path.parent.name]={'files':len(record['sources']),'mismatches':mismatches,'passed':not mismatches}
before=[r['path'] for r in baseline['sources'] if file_sha(HERE/'p0/before'/r['path'])!=r['sha256']]
result={'at':now(),'passed':all(x['passed'] for x in historical.values()) and all(x['passed'] for x in archives.values()) and not before,
    'historical':historical,'archives':archives,'baselineCopyMismatches':before,
    'boundary':'historical artifact files and source copies, not a claim that current application matches historical source'}
json_new(HERE/'p0/historical_verification.json',result);print(result)
