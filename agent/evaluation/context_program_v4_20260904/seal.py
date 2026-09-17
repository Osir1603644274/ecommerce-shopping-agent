"""Finalize new artifacts without rewriting any older experiment evidence."""
import json
import socket
from xml.etree import ElementTree as ET
from .common import HERE, V3, ROOT, file_sha, json_new, now, check_freeze
from .audit_ledger import audit


def main():
    assert (HERE / 'FINAL_REPORT.md').exists()
    decision = json.loads((HERE / 'FINAL_DECISION.json').read_text(encoding='utf-8'))
    ledger = audit(); assert ledger == decision['budget'] and ledger['status'] == 'PASS_LEDGER_EDGES'
    check_freeze(HERE / 'p4/recovery001/source_freeze.json')
    old_mismatches=[]
    for line in (V3 / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        expected, rel = line.split('  ', 1)
        path = (V3 / rel).resolve(); assert path.is_relative_to(V3.resolve())
        if file_sha(path) != expected:
            old_mismatches.append(rel)
    assert not old_mismatches
    old_seal = json.loads((V3 / 'SEAL.json').read_text(encoding='utf-8'))
    for name,key in [('SHA256SUMS.txt','manifestSha256'),('verification.json','verificationSha256'),
                     ('FINAL_DECISION.json','decisionSha256'),('FINAL_REPORT.md','reportSha256')]:
        assert file_sha(V3 / name) == old_seal[key]
    test_results = {}
    for path, expected in ((HERE / 'p0/runner_tests.xml', 15), (HERE / 'p8/analysis_tests.xml', 8)):
        suites = list(ET.parse(path).getroot().iter('testsuite'))
        assert not any(int(s.get('errors','0')) or int(s.get('failures','0')) or int(s.get('skipped','0')) for s in suites)
        assert sum(int(s.get('tests','0')) for s in suites) == expected
        test_results[path.relative_to(HERE).as_posix()] = expected
    identity = json.loads((HERE / 'p4/recovery001/redis_identity.json').read_text(encoding='utf-8'))
    with socket.socket() as sock:
        sock.settimeout(1)
        owned_closed = sock.connect_ex(('127.0.0.1', identity['port'])) != 0
    assert owned_closed
    json_new(HERE / 'p8/final_checks.json', {'at': now(), 'status': 'PASS', 'old1136ArtifactsUnchanged': True,
        'oldSealUnchanged': True, 'liveRunFreezeUnchanged': True, 'newTests': test_results,
        'newProviderErrors': ledger['httpErrorCounts'], 'ownedRedisPortClosed': owned_closed,
        'sharedServiceTouched': False, 'budget': ledger})
    excluded={'SHA256SUMS.txt','verification.json','SEAL.json','provider-writer.lock'}
    files=[]
    for path in sorted(HERE.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo'):
            continue
        rel=path.relative_to(HERE).as_posix()
        if rel in excluded:
            continue
        assert not path.is_symlink() and path.resolve().is_relative_to(HERE.resolve())
        files.append((rel,file_sha(path)))
    with (HERE / 'SHA256SUMS.txt').open('x',encoding='utf-8',newline='\n') as stream:
        for rel, hashed in files:
            stream.write(f'{hashed}  {rel}\n')
    assert all(file_sha(HERE / rel) == hashed for rel,hashed in files)
    verification={'at':now(),'status':'PASS_ARTIFACT_INTEGRITY_NOT_EFFECT_ACCEPTANCE','artifacts':len(files),
        'manifestSha256':file_sha(HERE/'SHA256SUMS.txt'),'oldSealSha256':file_sha(V3/'SEAL.json'),
        'checksSha256':file_sha(HERE/'p8/final_checks.json'),'ledgerSha256':file_sha(HERE/'provider_ledger.jsonl'),
        'excluded':sorted(excluded)+['**/__pycache__/**','*.pyc','*.pyo']}
    json_new(HERE/'verification.json',verification)
    json_new(HERE/'SEAL.json',{'at':now(),'status':verification['status'],
        'manifestSha256':verification['manifestSha256'],'verificationSha256':file_sha(HERE/'verification.json'),
        'decisionSha256':file_sha(HERE/'FINAL_DECISION.json'),'reportSha256':file_sha(HERE/'FINAL_REPORT.md'),
        'localPrivateEvidenceOnly':True})
    print(json.dumps(verification,indent=2))


if __name__ == '__main__':
    main()
