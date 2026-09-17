"""Restore an immutable release to a new local root, then run C only."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ORIGINAL = Path('F:/agent')
STUDY_REL = Path('agent/evaluation/context_history_strategies_v1_20260905')
STUDY = ORIGINAL / STUDY_REL
ISLAND = Path('E:/context-c-release002-20260906')
MANIFEST = ISLAND / 'isolation_manifest.json'
OUTPUT_NAME = 'first_edition_c_isolated001'
RELEASE_SHA = 'fc3818f35170cb675e3829add1ef8243896b002883d9f2413fbbfbf4af2d6d6c'
SCRIPT_SHA = '60e945655d27f36a9ffe10d5e1e39e99d458b0370a948e561440db1f91573485'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def prepare():
    if ISLAND.exists():
        raise ValueError('new_isolation_root_required')
    release_file = STUDY / 'first_edition_release002.json'
    if digest(release_file) != RELEASE_SHA:
        raise ValueError('release_manifest_changed')
    release = read(release_file)
    if len(release['sources']) != 274:
        raise ValueError('unexpected_release')
    if shutil.disk_usage(ISLAND.parent).free < 5_000_000_000:
        raise ValueError('insufficient_isolated_storage')
    ISLAND.mkdir()
    hashes = {}
    def copy(source, relative, expected=None):
        relative = Path(relative)
        target = ISLAND / relative
        if relative.is_absolute() or '..' in relative.parts or source.is_symlink():
            raise ValueError('unsafe_copy_target')
        before = digest(source)
        if expected is not None and before != expected:
            raise ValueError('source_hash_mismatch:' + str(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError('existing_copy_target')
        shutil.copy2(source, target)
        if digest(target) != before or digest(source) != before:
            raise ValueError('copy_hash_mismatch')
        hashes[relative.as_posix()] = before
    for relative, expected in release['sources'].items():
        copy(STUDY / 'first_edition_source_snapshot002' / relative, relative, expected)
    copy(release_file, STUDY_REL / release_file.name, RELEASE_SHA)
    script_rel = STUDY_REL / 'core_dataset48_vivo002/script.json'
    copy(ORIGINAL / script_rel, script_rel, SCRIPT_SHA)
    data = Path('data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3')
    for name in ('catalog.jsonl', 'prices.jsonl', 'price_manifest.json'):
        copy(ORIGINAL / data / name, data / name)
    if hashes[(data / 'catalog.jsonl').as_posix()] != '725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75':
        raise ValueError('catalog_identity_changed')
    prices = read(ISLAND / data / 'price_manifest.json')
    if prices['output']['sha256'] != hashes[(data / 'prices.jsonl').as_posix()]:
        raise ValueError('price_bundle_identity_changed')
    write_new(MANIFEST, {
        'kind': 'USER_AUTHORIZED_C_ONLY_FROZEN_REPLAY_ISOLATED_ROOT', 'root': str(ISLAND),
        'sourceRoot': str(STUDY / 'first_edition_source_snapshot002'),
        'releaseSha256': RELEASE_SHA, 'hashes': hashes, 'frozenSourceCount': 274,
        'output': str(ISLAND / STUDY_REL / OUTPUT_NAME),
        'python': sys.executable, 'cwd': str(ISLAND), 'modelsStartedByPreparation': 0,
        'resumeDecision': 'FULL_C48_ONLY_NO_RELIABLE_PERSISTED_HISTORYSTRATEGIES_RESUME',
        'resumeEvidence': 'agent_smoke initializes new session/archive/history strategy; summary/covered_ids/failed_source_hash are in-memory; old archive has only identity and raw messages; no verified recovery of all strategy state.',
        'preserved': ['original_A48', 'original_B48', 'old_failed_C43', 'shared_worktree_edits'],
        'copiedSecrets': False, 'envFilesCopied': False,
        'environmentCaveat': 'Same machine/Python and frozen code/data, later run and E-disk storage rather than F. Timing descriptive, not isolated causal or formal acceptance. Root .env contains only backend/API/proxy values, DEEPSEEK_MODEL is overridden, API fallback prohibited.',
        'newCConfiguration': {'arm': 'C_LLM_THRESHOLD', 'input_budget': 96000, 'working_budget': 32000, 'trigger_fraction': .6, 'target_fraction': .55, 'adaptive_summary_items': True},
        'scope': 'Complete C48 and C independent double review only; retain existing AB; no tuning/B+C/production change.'})
    print(json.dumps({'prepared': str(ISLAND), 'files': len(hashes), 'manifestSha256': digest(MANIFEST)}), flush=True)


def verify():
    manifest = read(MANIFEST)
    for relative, expected in manifest['hashes'].items():
        path = ISLAND / relative
        if path.is_symlink() or digest(path) != expected:
            raise ValueError('isolation_source_drift:' + relative)
    return manifest


def run():
    manifest = verify()
    output = Path(manifest['output'])
    if output.exists() or output.with_name(output.name + '_supervisor').exists():
        raise ValueError('new_C_attempt_required')
    sys.path.insert(0, str(ISLAND))
    from agent.evaluation.context_history_strategies_v1_20260905 import artifacts
    if artifacts.ROOT != ISLAND:
        raise ValueError('imports_not_isolated')
    # Match the existing cohort's NTFS compression inheritance without editing old data.
    compression = subprocess.run(['compact.exe', '/c', '/i', '/q', str(ISLAND / STUDY_REL)],
                                 capture_output=True, text=True, timeout=30)
    write_new(ISLAND / 'storage_policy.json', {'exitCode': compression.returncode, 'stdout': compression.stdout,
                                             'stderr': compression.stderr, 'scope': 'new_isolated_study_only'})
    if compression.returncode:
        raise ValueError('storage_setup_failed')
    from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise
    import uuid
    token = uuid.uuid4().hex
    cmd = [sys.executable, '-X', 'utf8', '-B', '-m',
           'agent.evaluation.context_history_strategies_v1_20260905.agent_smoke', str(output),
           '--supervised-start-token', token, '--arm', 'C_LLM_THRESHOLD',
           '--script', str(ISLAND / STUDY_REL / 'core_dataset48_vivo002/script.json'),
           '--input-budget', '96000', '--working-budget', '32000', '--trigger-fraction', '.6',
           '--target-fraction', '.55', '--adaptive-summary-items', '--attempt-timeout', '7200']
    return supervise(cmd, output.with_name(output.name + '_supervisor'), timeout=7320, start_token=token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'verify', 'run'))
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'verify':
        verify()
        print('ISOLATED_RELEASE_HASHES_PASS')
    else:
        raise SystemExit(run())
