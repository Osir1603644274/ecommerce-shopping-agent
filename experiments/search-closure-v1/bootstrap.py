"""Create a new, recoverable search experiment without rewriting historical runs."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('D:/agent-datasets/search-closure-v1')
DEV = Path('D:/agent-datasets/search-stage1-dev-revision-v6')
REPO = Path('F:/agent')

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def write_once(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f'Existing immutable file differs: {path}')
    else:
        with path.open('xb') as f:
            f.write(raw)

def main():
    receipt = ROOT / 'STARTED.json'
    if receipt.exists():
        print(receipt.read_text(encoding='utf-8'))
        return
    for p in [ROOT, DEV]:
        p.mkdir(parents=True, exist_ok=True)
    before = {}
    for name in ['search-stage1-dev-revision-v3', 'search-stage1-dev-evidence-v4', 'search-stage1-dev-revision-v5']:
        directory = ROOT.parent / name
        for path in sorted(directory.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                before[str(path)] = {'sha256': sha(path), 'bytes': path.stat().st_size}
    write_once(ROOT / 'provenance/historical-files-before.json', before)
    source_paths = [
        'agent/app/domains/ecommerce/tools.py', 'agent/app/domains/ecommerce/models.py',
        'agent/app/domains/ecommerce/ranking_contract.py', 'agent/app/settings.py',
        'agent/app/tools.py', 'agent/app/schemas.py',
        'agent/tests/test_ecommerce_two_stage_search.py',
        'agent/tests/test_ecommerce_ranking_contract.py',
    ]
    sources = {}
    for rel in source_paths:
        source = REPO / rel
        target = ROOT / 'provenance/source-before' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f'Unexpected existing backup {target}')
        shutil.copyfile(source, target)
        sources[rel] = {'sha256': sha(source), 'backup': str(target)}
    write_once(ROOT / 'provenance/source-before.json', sources)
    status = subprocess.run(['git', 'status', '--porcelain=v1', '--untracked-files=normal'], cwd=REPO,
                            check=True, capture_output=True).stdout
    (ROOT / 'provenance/git-status-before.txt').write_bytes(status)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPO, check=True, capture_output=True, text=True).stdout.strip()
    write_once(receipt, {
        'status': 'EXECUTION_STARTED', 'started_at': datetime.now(timezone.utc).isoformat(),
        'authorization': 'User: 开启目标模式并执行; immediately preceding complete plan accepted for execution.',
        'root_thread_id': '01a07f07-28fa-7523-84fe-516a1cf42fc6',
        'goal_active': True, 'git_head': head, 'seed': 20260909,
        'development_root': str(DEV), 'historical_files': len(before),
        'historical_manifest_sha256': sha(ROOT / 'provenance/historical-files-before.json'),
        'new_test_query_count': 80, 'new_training_query_count': 200,
        'old_test_status': 'historically_used; do not read old test labels or scores for this run',
        'human_gold': False, 'planned_labels_are_model_silver': True,
    })
    write_once(ROOT / 'STATUS.json', {'stage': 'policy_and_preparation', 'complete': False,
        'development_qrels_ready': False, 'test_sealed': False, 'training_started': False,
        'new_test_scored': False, 'agent_verified': False})
    print(receipt.read_text(encoding='utf-8'))

if __name__ == '__main__':
    main()
