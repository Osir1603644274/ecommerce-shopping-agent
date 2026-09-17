"""Close the interrupted first edition; review only independently closed A/B.

Does not amend frozen SUT, cohort terminals, or the complete-ABC review gate.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new
from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest as file_sha
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise
from agent.evaluation.context_history_diagnostics_v1_20260905.native_cost_audit import audit as native_audit
from agent.evaluation.context_history_diagnostics_v1_20260905.cohort_report import compare, load_environment_notes

COHORT = HERE / 'first_edition_vivo48_cohort001'
COST = HERE / 'first_edition_rescue_cost001.json'
REVIEW = HERE / 'first_edition_ab_review001'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verify_hash(path, expected):
    if path.is_symlink() or file_sha(path) != expected:
        raise ValueError('changed_or_linked_artifact:' + str(path))


def close_cost():
    if COST.exists():
        raise ValueError('new_receipt_required')
    start = read(COHORT / 'started.json')
    outer = read(COHORT.with_name(COHORT.name + '_supervisor') / 'result.json')
    if start['kind'] != 'SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL' or outer['childExitCode'] != 1:
        raise ValueError('expected_closed_failed_cohort')
    release = read(HERE / 'first_edition_release002.json')
    if start['sources'] != release['sources']:
        raise ValueError('release_sources_mismatch')
    verify_hash(HERE / 'first_edition_release002.json',
                'fc3818f35170cb675e3829add1ef8243896b002883d9f2413fbbfbf4af2d6d6c')
    script = Path(start['script'])
    verify_hash(script, start['scriptSha256'])
    queries = [row['userText'] for row in read(script)['turns']]
    bindings = {}
    def bind(path):
        bindings[str(path.resolve())] = file_sha(path)
    for name, expected in start['sources'].items():
        archived = HERE / 'first_edition_source_snapshot002' / name
        verify_hash(archived, expected)
        bind(archived)
    records, snapshots = {}, []
    for label in 'ABC':
        directory = COHORT / (label + '001')
        census_path = COHORT / (label + '001_census.json')
        census = read(census_path)
        job = next(job for job in start['jobs'] if job['label'] == label)
        if Path(job['output']).resolve() != directory.resolve():
            raise ValueError('wrong_job_directory')
        child = read(directory / 'started.json')
        if child['arm'] != job['arm'] or child['scriptSha256'] != start['scriptSha256']:
            raise ValueError('arm_or_script_mismatch')
        sources = read(directory / 'source_snapshot/manifest.json')['sources']
        snapshots.append(sources)
        for name, expected in sources.items():
            if start['sources'].get(name) != expected:
                raise ValueError('child_freeze_mismatch')
            verify_hash(directory / 'source_snapshot' / name, expected)
        for name, expected in census['sourceSha256'].items():
            path = directory / name
            verify_hash(path, expected)
            bind(path)
        native = native_audit(directory)
        for key in ('completeNativeTokenTotal', 'knownNativeTokenSubtotal', 'unknownUsageCalls'):
            if native[key] != census[key]:
                raise ValueError('native_recomputation_mismatch:' + key)
        for call in native['calls']:
            for name, expected in call['sourceHashes'].items():
                bindings[str((directory / name).resolve())] = expected
        events = [json.loads(line) for line in (directory / 'runner_events.jsonl').read_text(encoding='utf-8').splitlines()]
        commits = {event['turn']: event for event in events if event['event'] == 'TURN_COMMITTED'}
        elapsed = 0
        for turn in range(1, census['observedTurns'] + 1):
            path = directory / f'turn-{turn:02d}.json'
            row = read(path)
            if row['turn'] != turn or row['query'] != queries[turn - 1] or row['runId'] != row['expectedRunId']:
                raise ValueError('turn_script_or_identity_mismatch')
            verify_hash(path, commits[turn]['turnSha256'])
            bind(path)
            elapsed += row['durationMs']
        if elapsed != census['observedTurnMsSubtotal']:
            raise ValueError('time_recomputation_mismatch')
        supervisor = directory.with_name(directory.name + '_supervisor') / 'result.json'
        terminal = read(supervisor)
        if label in 'AB':
            closure = read(COHORT / (label + '001_closed.json'))
            execution = read(COHORT / (label + '001_audit.json'))
            if (not census['dataCollectionComplete'] or census['observedTurns'] != 48 or
                census['completeConversationMs'] != elapsed or terminal['childExitCode'] != 0 or
                closure['executionAuditStatus'] != 'EXECUTION_AUDIT_PASS' or
                execution['status'] != 'EXECUTION_AUDIT_PASS' or events[-1]['event'] != 'RUNNER_FINISHED'):
                raise ValueError('incomplete_AB_not_salvageable')
            bind(COHORT / (label + '001_closed.json'))
            bind(COHORT / (label + '001_audit.json'))
        elif (census['dataCollectionComplete'] or census['observedTurns'] != 43 or
              terminal['childExitCode'] != 1 or events[-1].get('error') !=
              'source_changed_during_attempt:agent/app/main.py,agent/app/settings.py'):
            raise ValueError('unexpected_C_failure_boundary')
        for path in (census_path, supervisor, directory / 'started.json', directory / 'source_snapshot/manifest.json', directory / 'runner_events.jsonl'):
            bind(path)
        records[label] = {'census': census, 'nativeCostAudit': native, 'exitCode': terminal['childExitCode']}
    if snapshots[0] != snapshots[1] or snapshots[0] != snapshots[2]:
        raise ValueError('different_initial_sut_snapshots')
    root = HERE.parents[2]
    drift = [{ 'path': name, 'frozenSha256': expected,
               'currentSha256': file_sha(root / name) if (root / name).exists() else None}
             for name, expected in start['sources'].items()
             if not (root / name).exists() or file_sha(root / name) != expected]
    env, env_bindings = load_environment_notes(COHORT)
    bindings.update(env_bindings)
    for path in (COHORT / 'started.json', COHORT.with_name(COHORT.name + '_supervisor') / 'result.json', HERE / 'first_edition_release002.json', script, Path(__file__)):
        bind(path)
    value = {'kind': 'CLOSED_AB_ONLY_RESCUE_NOT_COMPLETE_ABC', 'at': now(), 'cohort': str(COHORT),
             'arms': records, 'ABFrozenSourcesAndScriptsEqual': True, 'comparisons': {'B_vs_A': compare(records['A']['census'], records['B']['census'])},
             'CComparison': None, 'CFrozenSourceValidity': 'BROKEN_AT_FINAL_CHECK_DO_NOT_USE_AS_CONTROLLED_COMPLETE_ARM',
             'sourceDriftObserved': drift, 'environment': env, 'artifactHashes': bindings,
             'qualityAcceptance': False, 'formalAcceptance': False,
             'note': 'AB completed before detected drift; all archived bytes and original native receipts rechecked. C43 is charged failed development only. No current SUT edits reverted; no complete-ABC gate changed.'}
    write_new(COST, value)
    print(json.dumps({'kind': value['kind'], 'AB': value['comparisons'], 'sourceDrift': drift}, ensure_ascii=False), flush=True)


def review(worker_token):
    if worker_token and sys.stdin.readline().strip() != worker_token:
        raise ValueError('start_gate_not_released')
    if REVIEW.exists():
        raise ValueError('new_review_required')
    proof = read(COST)
    if proof['kind'] != 'CLOSED_AB_ONLY_RESCUE_NOT_COMPLETE_ABC' or not proof['ABFrozenSourcesAndScriptsEqual']:
        raise ValueError('closed_AB_proof_required')
    for name, expected in proof['artifactHashes'].items():
        verify_hash(Path(name), expected)
    # Current unrelated SUT changes are not used to judge archived answers.
    # Judge implementation itself must still equal the approved frozen bytes.
    frozen = read(COHORT / 'started.json')['sources']
    for name, expected in frozen.items():
        if name.startswith('agent/evaluation/'):
            verify_hash(HERE.parents[2] / name, expected)
    if worker_token:
        from agent.evaluation.context_history_review_v2_20260905.run import run
        attempts = [f'{COHORT.name}/{label}001' for label in 'AB']
        try:
            asyncio.run(run(REVIEW, attempts, 48, 6, 192000, False, 2))
        except BaseException as exc:
            if REVIEW.exists() and not (REVIEW / 'failure.json').exists():
                write_new(REVIEW / 'failure.json', {'at': now(), 'type': type(exc).__name__, 'error': str(exc)})
            raise
        return 0
    token = uuid.uuid4().hex
    command = [sys.executable, '-X', 'utf8', '-B', '-m',
               'agent.evaluation.context_history_diagnostics_v1_20260905.maintenance.first_edition_rescue',
               'review', '--worker-token', token]
    return supervise(command, REVIEW.with_name(REVIEW.name + '_supervisor'), timeout=15600, start_token=token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('audit', 'review'))
    parser.add_argument('--worker-token')
    args = parser.parse_args()
    if args.action == 'audit':
        close_cost()
    else:
        raise SystemExit(review(args.worker_token))
