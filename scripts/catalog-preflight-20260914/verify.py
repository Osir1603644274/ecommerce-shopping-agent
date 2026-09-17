"""Independently reconcile preflight evidence, capture read-only capacity, seal artifacts."""
import argparse
import collections
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from rules import EXPECTED, VERSION, simulated_price, BASE, WIDTH, decode, normalize

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main(out):
    out=out.resolve()
    if (out/'VALIDATION.json').exists(): raise ValueError('validation already exists; preserve it')
    result=json.loads((out/'RESULT.json').read_text(encoding='utf-8'))
    assert result['status']=='FULL_SOURCE_SCAN_COMPLETE_NO_IMPORT'
    assert result['ruleVersion']==VERSION
    counts=collections.Counter();reasons=collections.defaultdict(collections.Counter)
    duplicates=set();examples=[]
    for raw in (out/'anomalies.jsonl').open(encoding='utf-8'):
        row=json.loads(raw);key=(row['source'],row['line'])
        assert key not in duplicates;duplicates.add(key)
        counts[row['source']]+=1;reasons[row['source']].update(row['reasons'])
        if len(examples)<20:examples.append(row)
    for source,total in EXPECTED.items():
        s=result['sources'][source]
        assert s['rows']==total==s['technicalCandidateRows']+s['quarantineRows']
        assert counts[source]==s['quarantineRows']
        assert dict(reasons[source])==s['reasons']
    assert result['technicalCandidates']==sum(s['technicalCandidateRows'] for s in result['sources'].values())
    existing=result['existingIdentityMatches']
    assert len(existing)==len({x['docid'] for x in existing})
    assert len(existing)==sum(s['preservedExisting'] for s in result['sources'].values())
    for x in result['sampleCandidates']:
        if x['action']=='STAGING_ONLY':
            source=x['source'];native=x['sourceItemId']
            assert int(x['id'])==BASE+({'kuaisearch':0,'multicpr':1}[source])*WIDTH+int(native)
            assert x['localOffer']['priceMinor']==simulated_price(source,native)
    code=json.loads((out/'PROVENANCE.json').read_text())['files']
    for path,expected in code.items(): assert sha(Path(path))==expected, 'code drift: '+path
    before=json.loads((out/'target-before.json').read_text(encoding='utf-8'))
    after=json.loads((out/'target-after.json').read_text(encoding='utf-8'))
    unchanged={}
    for table in ['product','product_local_offer','inventory_stock']:
        # Compare by primary key, not SELECT result order.
        pk='product_id' if table=='product_local_offer' else 'id'
        unchanged[table]=sorted(before[table],key=lambda r:r[pk])==sorted(after[table],key=lambda r:r[pk])
    metadata_root=Path('D:/agent-datasets/integration-repair-20260913-v1/metadata')
    source_manifest=json.loads((metadata_root/'MANIFEST.json').read_text(encoding='utf-8'))
    assert sha(metadata_root/'MANIFEST.json')==result['manifestSha256']
    db=sqlite3.connect((metadata_root/'metadata.sqlite').as_uri()+'?mode=ro',uri=True)
    old_by_id={str(r['id']):r for r in before['product']}
    differences=[]; old_states=collections.Counter()
    for match in existing:
        old=old_by_id[match['productId']];old_states[old['lifecycle_status']]+=1
        source,offset,length,expected=db.execute('SELECT source,byte_offset,byte_length,record_sha256 FROM records WHERE docid=?',(match['docid'],)).fetchone()
        with Path(source_manifest['sources'][source]['path']).open('rb') as f:
            f.seek(offset);raw=f.read(length)
        assert hashlib.sha256(raw).hexdigest()==expected
        _,fields=decode(source,raw)
        keys={'title':'title','brand':'brand','seller':'seller','categoryL1':'category_l1','categoryL2':'category_l2','categoryL3':'category_l3'}
        changed=[k for k,v in keys.items() if normalize(old[v])!=fields[k]]
        if changed: differences.append({**match,'differentFields':changed,'action':'PRESERVE_NO_OVERWRITE_NO_REACTIVATION'})
    db.close()
    (out/'EXISTING-IDENTITY-REVIEW.json').write_text(json.dumps({'matched':len(existing),
        'lifecycleCounts':old_states,'differentContentRows':len(differences),'differences':differences},
        ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    suite=unittest.defaultTestLoader.discover(str(Path(__file__).parent),pattern='test_rules.py')
    run=unittest.TextTestRunner(verbosity=2).run(suite)
    assert run.wasSuccessful()
    health=[json.loads(l) for l in (out/'health.jsonl').read_text().splitlines()]
    assert health
    disk_path=Path('C:/Users/ming/AppData/Local/Docker/wsl').resolve()
    volumes=[]
    for p in disk_path.rglob('*.vhdx'):
        volumes.append({'path':str(p),'logicalFileBytes':p.stat().st_size})
    capacity={'dockerWslResolvedPath':str(disk_path),'vhdx':volumes,
              'hostFreeBytes':{d:shutil.disk_usage(d+':/').free for d in 'CDEF'},
              'mysqlContainerDf':subprocess.check_output(['docker','exec','local-life-mysql','df','-B1','/var/lib/mysql'],text=True),
              'note':'VHDX file length is not allocated bytes. Container df is virtual; host headroom limits growth.'}
    (out/'CAPACITY.json').write_text(json.dumps(capacity,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    validation={'status':'BOUNDED_PREFLIGHT_VERIFIED_NOT_IMPORT_READY','testsRun':run.testsRun,'testFailures':len(run.failures),
                'testErrors':len(run.errors),'rows':sum(EXPECTED.values()),'anomalyRows':sum(counts.values()),
                'targetTablesUnchanged':unchanged,'frontendHealthSamples':len(health),
                'existingIdentityContentDifferences':len(differences),'preservedLifecycleCounts':dict(old_states),
                'frontendFailures':sum(r.get('status')!=200 for r in health),'frontendMaxMs':max(r['ms'] for r in health),
                'examples':examples,'remainingGates':['isolated 10000-row write/restart/idempotency drill',
                  'measured storage amplification and restoration','search/cache and purchase eligibility integration'],
                'sourceFullShaValidatedByScanner':True,'independentSourceHashRerun':False,
                'noTradeWrites':True,'noImportOrModelOrBenchmarkExecution':True}
    validation['verificationCodeSha256']={str(p):sha(p) for p in [Path(__file__),Path(__file__).with_name('test_rules.py')]}
    (out/'VALIDATION.json').write_text(json.dumps(validation,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    manifest={p.name:{'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(out.iterdir()) if p.is_file()}
    (out/'EVIDENCE-MANIFEST.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in validation.items() if k!='examples'},ensure_ascii=False))

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);args=p.parse_args();main(args.output)
