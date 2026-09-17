"""Bounded offline regressions or explicit first-edition source freeze."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, ROOT, capture_sources, file_sha, now, verify_sources, write_new


def bound_sources():
    paths = [*(ROOT / 'agent/app').rglob('*.py'), *HERE.glob('*.py'),
        *Path(__file__).parent.glob('*.py'),
        *(ROOT / 'agent/evaluation/context_history_review_v2_20260905').glob('*.py')]
    return {p.relative_to(ROOT).as_posix(): file_sha(p) for p in paths}


def test(output):
    if output.exists():
        raise FileExistsError(output)
    before = bound_sources()
    modules = ['agent.evaluation.context_history_strategies_v1_20260905.' + name for name in
        ('test_history', 'test_context_client', 'test_answer_gate', 'test_history_lookup', 'test_subscription')]
    commands = [(ROOT, [sys.executable, '-X', 'utf8', '-B', '-m', 'unittest', *modules, '-q']),
        (ROOT / 'agent', [sys.executable, '-X', 'utf8', '-B', '-m', 'unittest', 'tests.test_run_agent', '-q'])]
    results = []
    for cwd, command in commands:
        started = time.perf_counter()
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding='utf8', timeout=300)
        results.append({'command': command, 'cwd': str(cwd), 'exitCode': result.returncode,
            'seconds': time.perf_counter() - started, 'stdout': result.stdout, 'stderr': result.stderr})
    verify_sources(before)
    write_new(output, {'at': now(), 'kind': 'FIRST_EDITION_OFFLINE_REGRESSIONS', 'sourceHashes': before,
        'results': results, 'passed': all(r['exitCode'] == 0 for r in results), 'sourceDrift': [], 'modelCalls': 0})
    print(json.dumps({'passed': all(r['exitCode'] == 0 for r in results),
        'results': [{'exitCode': r['exitCode'], 'seconds': r['seconds'], 'tail': r['stderr'][-1800:]} for r in results]}))


def freeze(output):
    if output.name not in ('first_edition_release001.json', 'first_edition_release002.json') or output.parent != HERE or output.exists():
        raise ValueError('exact_new_first_release_required')
    revision = '002' if output.name.endswith('002.json') else '001'
    checks = [HERE / ('first_edition_green003.json' if revision == '002' else 'first_edition_green002.json'),
        HERE / ('first_edition_regression' + revision + '.json')]
    values = [json.loads(p.read_text(encoding='utf8')) for p in checks]
    if values[0]['exitCode'] or values[0]['sourceDrift'] or not values[1]['passed'] or values[1]['sourceDrift']:
        raise ValueError('passing_regressions_required')
    # Check current files against the actual successful regression version.
    verify_sources(values[1]['sourceHashes'])
    snapshot = HERE / ('first_edition_source_snapshot' + revision)
    hashes = capture_sources(snapshot)
    hashes.update(bound_sources())
    # Preserve all reviewed infrastructure as well as SUT source bytes.
    import shutil
    for relative, digest in hashes.items():
        destination = snapshot / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        if file_sha(destination) != digest:
            raise ValueError('snapshot_source_mismatch')
    verify_sources(hashes)
    write_new(output, {'at': now(), 'kind': 'FIRST_EDITION_SOURCE_FREEZE', 'sources': hashes,
        'testReceipts': {str(p.relative_to(ROOT)): file_sha(p) for p in checks},
        'planSha256': file_sha(HERE / 'FIRST_EDITION_EXECUTION.md'),
        'profile': 'firstedition48', 'newFullCohortsLimit': 1, 'formalHoldout': False})
    print(json.dumps({'frozenSources': len(hashes), 'output': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['test', 'freeze'])
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    (test if args.mode == 'test' else freeze)(args.output.resolve())
