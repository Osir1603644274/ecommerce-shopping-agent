"""Check historical bytes and actual regression receipts; not a goal-completion verdict."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
from retrieval_runtime import read_json,sha,write_once

ROOT=Path('D:/agent-datasets/search-closure-v1')

def main():
    p=argparse.ArgumentParser();p.add_argument('--output-name',required=True);args=p.parse_args()
    if not args.output_name.replace('-','').replace('_','').isalnum():raise ValueError('Simple new snapshot name required')
    target=ROOT/'delivery-audit'/args.output_name
    if target.exists():raise ValueError('Audit snapshot already exists; inspect rather than overwrite')
    started=read_json(ROOT/'STARTED.json');hp=ROOT/'provenance/historical-files-before.json'
    if sha(hp)!=started['historical_manifest_sha256']:raise ValueError('Original preservation manifest changed')
    original=read_json(hp);history=[]
    for path,expected in original.items():
        file=Path(path);actual=sha(file) if file.is_file() else None
        history.append({'path':path,'expected_sha256':expected['sha256'],'actual_sha256':actual,
                        'matches':actual==expected['sha256'] and file.stat().st_size==expected['bytes']})
    integration=ROOT/'agent-integration';receipt=read_json(integration/'IMPLEMENTATION_RECEIPT.json');sources=[]
    for f in receipt['files']:
        actual=sha(f['path']);sources.append({'path':f['path'],'expected_sha256':f['after_sha256'],'actual_sha256':actual,'matches':actual==f['after_sha256']})
    tests=[];identities=[]
    for run in receipt['test_runs']:
        if sha(run['path'])!=run['sha256']:raise ValueError('Recorded regression XML changed')
        cases=list(ET.parse(run['path']).iter('testcase'))
        counts={'tests':len(cases),'failures':sum(c.find('failure') is not None for c in cases),
                'errors':sum(c.find('error') is not None for c in cases),'skipped':sum(c.find('skipped') is not None for c in cases)}
        if any(counts[k]!=run[k] for k in counts):raise ValueError('Actual regression counts differ')
        keys=[(c.get('classname'),c.get('name')) for c in cases];identities.extend(keys)
        tests.append({'path':run['path'],'sha256':run['sha256'],**counts})
    head=subprocess.run(['git','rev-parse','HEAD'],cwd='F:/agent',capture_output=True,text=True,check=True).stdout.strip()
    distinct=len(set(identities));head_matches=head==started['git_head']
    result={'status':'PRESERVATION_AND_RECORDED_REGRESSION_AUDIT','captured_at':datetime.now(timezone.utc).isoformat(),
        'historical_files':len(history),'historical_mismatches':[r for r in history if not r['matches']],
        'protected_head_matches':head_matches,'expected_head':started['git_head'],'actual_head':head,
        'integration_source_mismatches':[r for r in sources if not r['matches']],
        'regression_tests':tests,'distinct_testcases':distinct,'duplicate_testcases':len(identities)-distinct,
        'integration_patch_sha256':sha(integration/'implementation.patch'),
        'recorded_tests_match_current_implementation':all(r['matches'] for r in sources),
        'source_and_recorded_tests_pass':all(r['matches'] for r in history+sources) and head_matches and distinct==256
             and len(identities)==256 and sha(integration/'implementation.patch')==receipt['patch_sha256'],
        'scope':'Byte preservation and existing software-contract regressions only. No new model call, full-suite, relevance, real-commerce-positive-path, or production claim.',
        'inputs':[{'path':str(f),'sha256':sha(f)} for f in [ROOT/'STARTED.json',hp,integration/'IMPLEMENTATION_RECEIPT.json',Path(__file__)]]}
    write_once(target/'historical-files.json',history);write_once(target/'integration-sources.json',sources)
    result['outputs']={name:sha(target/name) for name in ['historical-files.json','integration-sources.json']}
    write_once(target/'REPORT.json',result)
    print({k:result[k] for k in ['status','historical_files','historical_mismatches','protected_head_matches','integration_source_mismatches','distinct_testcases','source_and_recorded_tests_pass']})

if __name__=='__main__':main()
