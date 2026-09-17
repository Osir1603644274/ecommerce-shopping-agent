"""One-shot gated follow-through; never starts judges beside a live cohort."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import psutil

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, ROOT, append, file_sha, now, verify_sources, write_new
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise
from .supervised_review import closed_attempts


def same_process_alive(pid, created):
    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - created) < .001 and process.is_running()
    except psutil.NoSuchProcess:
        return False


def readiness(alive, terminal, collection):
    if terminal is not None and terminal.get('childExitCode') != 0:
        raise ValueError('cohort_supervisor_failed_no_review')
    if collection is not None and collection.get('completeMatchedCollection') is not True:
        raise ValueError('cohort_incomplete_no_review')
    if alive:
        return False  # Even an early terminal file cannot authorize overlap.
    if terminal is None or collection is None:
        raise ValueError('cohort_gone_without_complete_terminal_no_review')
    return True


def read_optional_terminal(path, alive):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except json.JSONDecodeError:
        if alive:
            return None  # Observe again; a write in progress is not a crash.
        raise ValueError('closed_terminal_corrupt_no_review')


def outputs():
    return {'native': HERE / 'core72_v7_general_A_native_audit001.json',
        'cost': HERE / 'core72_v7_general_cost_report001.json',
        'packet': HERE / 'core72_v7_general_review_packet001',
        'review': HERE / 'core72_v7_general_review001',
        'closeout': HERE / 'core72_v7_general_review_closeout001.json'}


def require_new(paths):
    for path in paths:
        if path.exists():
            raise ValueError('existing_output_preserved:' + str(path))


def command_matches_cohort(command, cohort):
    expected = str(cohort).replace('\\', '/').lower()
    return ('agent.evaluation.context_history_diagnostics_v1_20260905.serial_long_cohort' in command and
            any(str(part).replace('\\', '/').lower() == expected for part in command))


def run(cohort, output, cohort_pid, cohort_created):
    if cohort != HERE / 'core72_v7_general_cohort001' or output.parent != HERE:
        raise ValueError('approved_general72_scope_required')
    targets = outputs()
    require_new([output, *targets.values(), targets['review'].with_name(targets['review'].name + '_supervisor')])
    started = json.loads((cohort / 'started.json').read_text(encoding='utf8'))
    cohort_started_hash = file_sha(cohort / 'started.json')
    verify_sources(started['sources'])
    hashes = dict(started['sources'])
    dependencies = ['long_followthrough.py', 'supervised_review.py', 'native_cost_audit.py',
                    'cohort_report.py', 'review_closeout.py', 'close_interrupted_cohort.py']
    for path in [*(Path(__file__).parent / name for name in dependencies),
                 *(ROOT / 'agent/evaluation/context_history_review_v2_20260905').glob('*.py')]:
        hashes[path.relative_to(ROOT).as_posix()] = file_sha(path)
    output.mkdir()
    for name in hashes:
        target = output / 'source_snapshot' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
        if file_sha(target) != hashes[name]:
            raise ValueError('followthrough_snapshot_drift')
    write_new(output / 'started.json', {'at': now(), 'kind': 'ONE_SHOT_GENERAL72_GATED_FOLLOWTHROUGH',
        'pid': os.getpid(), 'cohortPid': cohort_pid, 'cohortProcessCreated': cohort_created,
        'cohort': str(cohort), 'cohortStartedSha256': cohort_started_hash,
        'sources': hashes, 'outputs': {k: str(v) for k, v in targets.items()},
        'judgePacketBudget': 192000, 'judgeConcurrency': 1, 'maxWaitSeconds': 7200,
        'automaticRetries': 0, 'goalCompletion': False})

    def event(kind, **fields):
        value = {'at': now(), 'event': kind, **fields}
        append(output / 'events.jsonl', value)
        print(json.dumps(value), flush=True)

    try:
        event('WAITING_FOR_EXACT_COHORT_EXIT')
        waiting = time.monotonic()
        outer = cohort.with_name(cohort.name + '_supervisor') / 'result.json'
        while True:
            alive = same_process_alive(cohort_pid, cohort_created)
            terminal = read_optional_terminal(outer, alive)
            result_path = cohort / 'result.json'
            result = read_optional_terminal(result_path, alive)
            if readiness(alive, terminal, result):
                break
            if time.monotonic() - waiting >= 7200:
                raise TimeoutError('cohort_wait_limit_no_restart')
            append(output / 'wait_observations.jsonl', {'at': now(), 'cohortPid': cohort_pid,
                'sameCreationIdentityAlive': alive, 'terminalPresent': terminal is not None})
            time.sleep(30)
        attempts, turns = closed_attempts(cohort)
        if turns != 72:
            raise ValueError('unexpected_general72_length')
        if file_sha(cohort / 'started.json') != cohort_started_hash:
            raise ValueError('cohort_manifest_changed')
        verify_sources(hashes)
        event('COHORT_CLOSED_AND_SOURCES_VERIFIED')

        def command(step, module, arguments, timeout):
            verify_sources(hashes)
            if file_sha(cohort / 'started.json') != cohort_started_hash:
                raise ValueError('cohort_manifest_changed')
            if same_process_alive(cohort_pid, cohort_created):
                raise ValueError('cohort_still_alive_before_postprocessing')
            args = [sys.executable, '-X', 'utf8', '-B', '-m', module, *map(str, arguments)]
            event('STEP_STARTED', step=step, command=args, timeoutSeconds=timeout)
            with (output / (step + '_stdout.log')).open('x', encoding='utf8') as stdout, \
                 (output / (step + '_stderr.log')).open('x', encoding='utf8') as stderr:
                process = subprocess.run(args, cwd=ROOT, stdout=stdout, stderr=stderr,
                    timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            event('STEP_EXITED', step=step, exitCode=process.returncode)
            if process.returncode:
                raise RuntimeError(step + '_failed_preserve_outputs')
            verify_sources(hashes)

        prefix = 'agent.evaluation.context_history_diagnostics_v1_20260905.'
        command('native_audit', prefix + 'native_cost_audit', [cohort / 'A001', targets['native']], 900)
        native = json.loads(targets['native'].read_text(encoding='utf8'))
        if native['unknownUsageCalls'] or native['failedOrUnclosedCalls']:
            raise ValueError('native_accounting_not_closed_before_review')
        command('cost_report', prefix + 'cohort_report', [cohort, targets['cost']], 1800)
        command('packet_preflight', 'agent.evaluation.context_history_review_v2_20260905.run',
            [targets['packet'], '--attempts', *attempts, '--turns', 72, '--chunk-size', 6,
             '--packet-budget', 192000, '--judge-concurrency', 1, '--prepare-only'], 1800)
        packet = json.loads((targets['packet'] / 'result.json').read_text(encoding='utf8'))
        if (packet['status'] != 'PACKET_ROUND_TRIP_PASS_NO_MODEL_CALLS' or
                packet['packets'] != 36 or packet['maxApplicationRequestTokens'] > 192000):
            raise ValueError('whole_cohort_packet_preflight_not_passed')
        event('ALL_36_PACKETS_PASS_JUDGES_MAY_START')
        command('independent_review', prefix + 'supervised_review',
            [cohort, targets['review'], '--packet-budget', 192000], 34500)
        command('review_closeout', prefix + 'review_closeout', [targets['review'], targets['closeout']], 1800)
        write_new(output / 'result.json', {'at': now(), 'status': 'REVIEW_COLLECTED_AND_BOUND_SEMANTIC_AUDIT_PENDING',
            'outputs': {key: str(path) for key, path in targets.items()}, 'goalComplete': False,
            'note': 'Main agent must inspect actual large-call admission, semantic claims, quality and remaining P3-P7. No automatic source repair or experiment retry.'})
        event('FOLLOWTHROUGH_FINISHED_NOT_GOAL_COMPLETE')
    except BaseException as exc:
        write_new(output / 'failure.json', {'at': now(), 'type': type(exc).__name__, 'message': str(exc),
            'outputsPreserved': True, 'automaticRetries': 0, 'goalComplete': False})
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('cohort', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--cohort-pid', type=int, required=True)
    parser.add_argument('--cohort-created', type=float)
    parser.add_argument('--worker-token')
    args = parser.parse_args()
    if args.worker_token:
        if args.cohort_created is None or sys.stdin.readline().strip() != args.worker_token:
            raise ValueError('owned_start_gate_not_released')
        run(args.cohort.resolve(), args.output.resolve(), args.cohort_pid, args.cohort_created)
        return 0
    require_new([args.output, args.output.with_name(args.output.name + '_supervisor')])
    process = psutil.Process(args.cohort_pid)
    if not command_matches_cohort(process.cmdline(), args.cohort.resolve()):
        raise ValueError('cohort_pid_command_does_not_match')
    token = uuid.uuid4().hex
    command = [sys.executable, '-X', 'utf8', '-B', '-m',
        'agent.evaluation.context_history_diagnostics_v1_20260905.long_followthrough',
        str(args.cohort.resolve()), str(args.output.resolve()), '--cohort-pid', str(args.cohort_pid),
        '--cohort-created', str(process.create_time()), '--worker-token', token]
    return supervise(command, args.output.with_name(args.output.name + '_supervisor').resolve(),
                     timeout=48000, start_token=token)


if __name__ == '__main__':
    raise SystemExit(main())
