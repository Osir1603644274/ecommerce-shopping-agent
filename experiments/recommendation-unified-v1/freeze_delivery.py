"""Freeze this preparation bundle and verify linked local artifacts."""
import datetime
import hashlib
import json
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / 'docs/experiments/recommendation-unified-v1-20260916'
CODE = Path(__file__).resolve().parent
DATA = Path('D:/agent-datasets/recommendation-unified-v1')


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    target = DOC / 'DELIVERY_MANIFEST.json'
    if target.exists():
        old = json.loads(target.read_text(encoding='utf-8'))
        for name, digest in old['files_sha256'].items():
            assert sha(Path(name)) == digest, name
        print(json.dumps({'status': 'EXISTING_DELIVERY_VERIFIED', 'files': len(old['files_sha256'])}))
        return
    snapshot = DOC / 'code_snapshot'
    snapshot.mkdir(exist_ok=False)
    for path in sorted(CODE.glob('*.py')):
        shutil.copyfile(path, snapshot / path.name)
    checks = {'local_markdown_links': [], 'json_parse': [], 'source_hashes': [], 'sealed_artifacts': []}
    for doc in DOC.glob('*.md'):
        for link in re.findall(r'\]\(([^)]+)\)', doc.read_text(encoding='utf-8')):
            if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', link) or link.startswith(('/', '#')):
                continue
            path = doc.parent / link.split('#', 1)[0]
            assert path.exists(), (doc.name, link)
            checks['local_markdown_links'].append([doc.name, link])
    for path in DOC.glob('*.json'):
        json.loads(path.read_text(encoding='utf-8'))
        checks['json_parse'].append(path.name)
    parent = DATA / 'amazon-luxury-dev-001'
    protocol = json.loads((parent / 'PROTOCOL.json').read_text(encoding='utf-8'))
    for name, digest in protocol['source_files'].items():
        assert sha(Path(name)) == digest, name
        checks['source_hashes'].append(name)
    manifest = json.loads((parent / 'MANIFEST.json').read_text(encoding='utf-8'))
    for name, digest in manifest['artifacts_sha256'].items():
        assert sha(parent / name) == digest, name
        checks['sealed_artifacts'].append(str(parent / name))
    fusion = DATA / 'amazon-luxury-fusion-dev-001'
    manifest = json.loads((fusion / 'MANIFEST.json').read_text(encoding='utf-8'))
    for name, digest in manifest.items():
        assert sha(fusion / name) == digest, name
        checks['sealed_artifacts'].append(str(fusion / name))
    smoke = DATA / 'dev-smoke-001'
    prepared = json.loads((smoke / 'PREPARED.json').read_text(encoding='utf-8'))
    for name, digest in prepared['files'].items():
        assert sha(smoke / name) == digest, name
        checks['sealed_artifacts'].append(str(smoke / name))
    assert sha(CODE / 'prepare_smoke.py') == prepared['script_sha256']
    assert sha(CODE / 'amazon_pilot.py') == protocol['implementation_sha256']
    bundle_files = [p for p in DOC.rglob('*') if p.is_file()]
    bundle_files += list(CODE.glob('*.py'))
    for directory in [smoke, parent, fusion, DATA / 'source-probe']:
        bundle_files += [p for p in directory.iterdir() if p.is_file()]
    output = {
        'status': 'PREPARATION_AND_DEVELOPMENT_EXPERIMENTS_VERIFIED',
        'created_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'scope': 'Plan, data contracts, CPU development baselines and fixed fusion; no production deployment or final evaluation',
        'files_sha256': {str(p): sha(p) for p in sorted(set(bundle_files))},
        'checks': checks,
        'test_counts_by_suite': {'contracts': 17, 'kuaisearch_smoke_boundaries': 4, 'amazon_pilot_boundaries': 8},
        'test_scope': 'Component checks reported separately; not Agent E2E pass counts',
    }
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    for name, digest in output['files_sha256'].items():
        assert sha(Path(name)) == digest, name
    print(json.dumps({'status': output['status'], 'files': len(output['files_sha256']),
                     'source_hashes_verified': len(checks['source_hashes']),
                     'sealed_artifacts_verified': len(checks['sealed_artifacts']),
                     'local_links_verified': len(checks['local_markdown_links'])}))


if __name__ == '__main__':
    main()
