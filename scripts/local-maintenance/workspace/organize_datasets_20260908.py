"""Prepare a byte-preserving dataset relocation plan. Does not move/delete data."""
from pathlib import Path
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[3]
RECORD = ROOT / 'docs/data/dataset-organization-2026-09-08'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    RECORD.mkdir(parents=True, exist_ok=True)
    target = RECORD / 'migration-plan.json'
    if target.exists():
        raise SystemExit('Existing plan preserved; do not regenerate over execution evidence.')
    special = {
        'kuaisearch_multicategory_retrieval_g0_20260824_r7': ('datasets/current/kuaisearch', 'CURRENT_CORPUS'),
        'used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3': ('datasets/current/used-phone', 'CURRENT_CORPUS'),
        'used_phone_human_qrel_v2': ('datasets/benchmarks/phone-retrieval/scenarios', 'CURRENT_QUERY_COLLECTION'),
        'kuaisearch_multicategory_breadth_v1_20260824_human_r1': ('datasets/support/multicategory-breadth', 'SOURCE_LABELS_AND_QUERY_METADATA'),
        'kuaisearch_multicategory_retrieval_g2_phase_a_adjudicated_20260824_r2': ('datasets/support/multicategory-adjudication', 'PARTIAL_REVIEW_EVIDENCE'),
        'kuaisearch_multicategory_retrieval_g1_20260824_r1': ('datasets/support/multicategory-retrieval-runs', 'RETRIEVAL_EVIDENCE'),
        'kuaisearch_multicategory_retrieval_g1_score_20260824_r1': ('datasets/support/multicategory-retrieval-scores', 'RETRIEVAL_EVIDENCE'),
        'used_phone_real_query_qrel_v1': ('datasets/support/phone-original-query-labels', 'SOURCE_LABELS_AND_LEGACY_DEPENDENCY'),
        'used_phone_synthetic_reference_price_v1': ('datasets/support/phone-252-legacy-prices', 'LEGACY_RUNTIME_DEPENDENCY'),
        'kuaisearch_lite_phone_behavior_feasibility_09807c_20260829_v1': ('datasets/support/phone-behavior-feasibility', 'MEMORY_SOURCE_FEASIBILITY'),
    }
    sources = []
    for rel in ['data/benchmarks/ecommerce', 'data/derived/ecommerce', 'data/annotations/ecommerce']:
        sources.extend(sorted((ROOT / rel).iterdir()))
    sources.append(ROOT / 'agent/evaluation/reviews/used_phone_439_attempt012')
    special['used_phone_439_attempt012'] = ('datasets/benchmarks/phone-retrieval/review-and-score', 'CODEX_ASSISTED_QREL_NOT_FORMAL_HUMAN_GOLD')
    moves = []
    for source in sources:
        if not source.is_dir():
            continue
        old = source.relative_to(ROOT).as_posix()
        new, role = special.get(source.name, ('data/_archive/2026-09-08/' + old.removeprefix('data/'), 'HISTORICAL_EVIDENCE'))
        if (ROOT / new).exists():
            raise RuntimeError('Target already exists: ' + new)
        files = []
        for file in sorted(source.rglob('*')):
            if file.is_symlink() or (hasattr(file, 'is_junction') and file.is_junction()):
                raise RuntimeError('Nested link: ' + str(file))
            if file.is_file():
                files.append({'path': file.relative_to(source).as_posix(), 'bytes': file.stat().st_size, 'sha256': digest(file)})
        moves.append({'old': old, 'new': new, 'role': role, 'compatibility': 'windows_directory_junction', 'files': files})
    status = subprocess.check_output(['git', 'status', '--porcelain=v1', '-z'], cwd=ROOT)
    (RECORD / 'git-status-before.bin').write_bytes(status)
    changed = subprocess.check_output(['git', 'diff', '--name-only', '-z', 'HEAD'], cwd=ROOT).decode('utf-8').split('\0')
    before = {p: digest(ROOT / p) if (ROOT / p).is_file() else None for p in changed if p}
    (RECORD / 'tracked-working-files-before.json').write_text(json.dumps(before, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    target.write_text(json.dumps({'root': str(ROOT), 'date': '2026-09-08', 'mode': 'RELOCATE_WITH_COMPATIBILITY_NO_DATA_DELETION', 'moves': moves}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'directories': len(moves), 'files': sum(len(m['files']) for m in moves), 'bytes': sum(f['bytes'] for m in moves for f in m['files']), 'historical': sum(m['role']=='HISTORICAL_EVIDENCE' for m in moves)}))


if __name__ == '__main__':
    main()
