"""Seal immutable local artifacts, then recompute hashes and current-source edges."""
import hashlib
import json
from pathlib import Path
from .common import HERE, ROOT, json_new, manifest_check, now
from .audit_ledger import audit

EXCLUDED={'SHA256SUMS.txt','verification.json','SEAL.json','provider-writer.lock'}
def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def main():
    decision=json.loads((HERE/'FINAL_DECISION.json').read_text(encoding='utf-8'))
    assert (HERE/'FINAL_REPORT.md').is_file(),'human_readable_report_missing'
    ledger=audit();assert ledger==decision['budget'],'budget_changed_since_final_decision'
    assert ledger['status']=='PASS_LEDGER_EDGES'
    assert not (HERE/'SHA256SUMS.txt').exists(),'no_resealing_over_old_manifest'
    files=[]
    for path in sorted(HERE.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo'):continue
        rel=path.relative_to(HERE).as_posix()
        if rel in EXCLUDED:continue
        if path.is_symlink() or not path.resolve().is_relative_to(HERE.resolve()):raise RuntimeError('manifest_path_escape')
        files.append((rel,digest(path)))
    manifest=HERE/'SHA256SUMS.txt'
    with manifest.open('x',encoding='utf-8',newline='\n') as stream:
        for rel,hashed in files:stream.write(f'{hashed}  {rel}\n')
    mismatches=[rel for rel,hashed in files if digest(HERE/rel)!=hashed]
    application=json.loads((HERE/'p4/confirm001/source_freeze.json').read_text(encoding='utf-8'))['sources']
    app_drift=[r['path'] for r in application if r['path'].startswith('agent/app/') and digest(ROOT/r['path'])!=r['sha256']]
    config_drift=[r['path'] for r in application if r['path'] in ('.env','agent/evaluation/context_program_v3_20260904/p0/contract.json') and digest(ROOT/r['path'])!=r['sha256']]
    test_sources=json.loads((HERE/'p8/full005_sources.json').read_text(encoding='utf-8'))['sources']
    full_drift=[r['path'] for r in test_sources if digest(ROOT/r['path'])!=r['sha256']]
    baseline=json.loads((HERE/'p0/baseline.json').read_text(encoding='utf-8'))
    old=[]
    for relative,record in baseline['historicalManifests'].items():
        path=HERE.parent/relative;check=manifest_check(path)
        if check['mismatches'] or digest(path)!=record['sha256']:old.append(relative)
    checks={'allArtifactHashes':not mismatches,'sameApplicationAsConfirmation':not app_drift,
        'sameConfigAndBudgetAsConfirmation':not config_drift,
        'settingsFileUnchangedSinceRepairStart':digest(ROOT/'agent/app/settings.py')==digest(HERE/'p0/repair001_sources/agent/app/settings.py'),
        'sameSourcesAsFinalSuite':not full_drift,'oldManifestsUnchanged':not old,
        'ledgerEdgesAndBudgets':ledger['status']=='PASS_LEDGER_EDGES',
        'noFalseAllPassedClaim':decision['allP0P8Passed'] is False,
        'noProductionDefaultSwitch':decision['productionDefaultsChanged'] is False}
    receipt={'at':now(),'status':'PASS_ARTIFACT_INTEGRITY_NOT_PRODUCTION_ACCEPTANCE' if all(checks.values()) else 'FAIL',
        'checks':checks,'artifacts':len(files),'manifestSha256':digest(manifest),
        'mismatches':mismatches,'applicationDrift':app_drift,'configDrift':config_drift,'fullSuiteSourceDrift':full_drift,'historicalDrift':old,
        'excluded':sorted(EXCLUDED)+['**/__pycache__/**','*.pyc','*.pyo'],
        'manifestRoot':str(HERE),'ledgerSha256':digest(HERE/'provider_ledger.jsonl')}
    json_new(HERE/'verification.json',receipt)
    if not all(checks.values()):raise RuntimeError('final_integrity_failed')
    json_new(HERE/'SEAL.json',{'at':now(),'manifestSha256':digest(manifest),
        'verificationSha256':digest(HERE/'verification.json'),'decisionSha256':digest(HERE/'FINAL_DECISION.json'),
        'reportSha256':digest(HERE/'FINAL_REPORT.md'),'status':receipt['status'],'localPrivateEvidenceOnly':True})
    print({'status':receipt['status'],'artifacts':len(files),'manifestSha256':receipt['manifestSha256']})

if __name__=='__main__':main()
