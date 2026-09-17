"""One-shot postprocessing of the approved vivo48 cohort, after exact exit.

Never retries an experiment or judge, never overlaps a live SUT, and leaves
semantic adjudication and the full P4-P7 goal with the main agent.
"""
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
from .long_followthrough import same_process_alive, readiness, read_optional_terminal, require_new, command_matches_cohort
from .supervised_review import closed_attempts


def targets():
    return {**{a + '_native': HERE / ('core48_v7_vivo_' + a + '_native_audit001.json') for a in 'ABC'},
        'cost': HERE / 'core48_v7_vivo_cost_report001.json',
        'packet': HERE / 'core48_v7_vivo_review_packet001',
        'review': HERE / 'core48_v7_vivo_review001',
        'closeout': HERE / 'core48_v7_vivo_review_closeout001.json'}


def validate_profile(cohort, output, manifest):
    if cohort != HERE / 'core48_v7_vivo_cohort001' or output.parent != HERE:
        raise ValueError('approved_vivo48_scope_required')
    if manifest.get('profile') != 'vivo48' or manifest.get('plannedTurnsEach') != 48 or manifest.get('order') != 'CAB':
        raise ValueError('approved_vivo48_manifest_required')
    if manifest.get('scriptSha256') != '60e945655d27f36a9ffe10d5e1e39e99d458b0370a948e561440db1f91573485':
        raise ValueError('approved_script_required')


def validate_packet(value):
    if (value.get('status') != 'PACKET_ROUND_TRIP_PASS_NO_MODEL_CALLS' or value.get('packets') != 24 or
            type(value.get('maxApplicationRequestTokens')) is not int or
            not 0 < value['maxApplicationRequestTokens'] <= 192000):
        raise ValueError('all24_packets_must_pass_before_judges')


def run(cohort, output, pid, created):
    target = targets()
    manifest = json.loads((cohort / 'started.json').read_text(encoding='utf8'))
    validate_profile(cohort, output, manifest)
    require_new([output, *target.values(), target['review'].with_name(target['review'].name + '_supervisor')])
    manifest_sha = file_sha(cohort / 'started.json')
    hashes = dict(manifest['sources'])
    dependencies = ['vivo48_followthrough.py', 'long_followthrough.py', 'supervised_review.py',
        'native_cost_audit.py', 'cohort_report.py', 'review_closeout.py', 'close_interrupted_cohort.py']
    for path in [*(Path(__file__).parent / n for n in dependencies),
                 *(ROOT / 'agent/evaluation/context_history_review_v2_20260905').glob('*.py')]:
        hashes[path.relative_to(ROOT).as_posix()] = file_sha(path)
    verify_sources(hashes)
    output.mkdir()
    for relative, expected in hashes.items():
        destination = output / 'source_snapshot' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
        if file_sha(destination) != expected:
            raise ValueError('snapshot_drift')
    write_new(output / 'started.json', {'at': now(), 'kind': 'ONE_SHOT_VIVO48_GATED_FOLLOWTHROUGH',
        'pid': os.getpid(), 'cohortPid': pid, 'cohortProcessCreated': created,
        'cohortStartedSha256': manifest_sha, 'sources': hashes,
        'outputs': {k: str(v) for k, v in target.items()}, 'maxWaitSeconds': 22200,
        'automaticRetries': 0, 'judgePacketBudget': 192000, 'judgeConcurrency': 1,
        'goalComplete': False})
    def event(kind, **fields):
        value = {'at': now(), 'event': kind, **fields}
        append(output / 'events.jsonl', value)
        print(json.dumps(value), flush=True)
    def bound():
        verify_sources(hashes)
        if file_sha(cohort / 'started.json') != manifest_sha:
            raise ValueError('cohort_manifest_drift')
    try:
        event('WAITING_FOR_EXACT_COHORT_EXIT')
        began = time.monotonic()
        while True:
            alive = same_process_alive(pid, created)
            terminal = read_optional_terminal(cohort.with_name(cohort.name + '_supervisor') / 'result.json', alive)
            collection = read_optional_terminal(cohort / 'result.json', alive)
            if readiness(alive, terminal, collection):
                break
            if time.monotonic() - began >= 22200:
                raise TimeoutError('bounded_wait_expired_no_restart')
            append(output / 'wait_observations.jsonl', {'at': now(), 'sameIdentityAlive': alive, 'cohortPid': pid})
            time.sleep(30)
        bound()
        attempts, turns = closed_attempts(cohort)
        if turns != 48:
            raise ValueError('unexpected_length')
        event('COHORT_CLOSED_AND_SOURCES_VERIFIED')
        def command(step, module, args, timeout):
            bound()
            if same_process_alive(pid, created):
                raise ValueError('live_sut_reject_postprocessing')
            args = [sys.executable, '-X', 'utf8', '-B', '-m', module, *map(str, args)]
            event('STEP_STARTED', step=step, command=args)
            with (output / (step + '_stdout.log')).open('x', encoding='utf8') as out, (output / (step + '_stderr.log')).open('x', encoding='utf8') as err:
                result = subprocess.run(args, cwd=ROOT, stdout=out, stderr=err, timeout=timeout,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            event('STEP_EXITED', step=step, exitCode=result.returncode)
            if result.returncode:
                raise RuntimeError(step + '_failed_preserve_outputs')
            bound()
        prefix = 'agent.evaluation.context_history_diagnostics_v1_20260905.'
        for arm in 'ABC':
            command(arm + '_native', prefix + 'native_cost_audit', [cohort / (arm + '001'), target[arm + '_native']], 900)
            audit = json.loads(target[arm + '_native'].read_text(encoding='utf8'))
            if audit['unknownUsageCalls'] or audit['failedOrUnclosedCalls']:
                raise ValueError('native_accounting_incomplete_before_review')
        command('cost', prefix + 'cohort_report', [cohort, target['cost']], 1800)
        command('packet', 'agent.evaluation.context_history_review_v2_20260905.run',
            [target['packet'], '--attempts', *attempts, '--turns', 48, '--chunk-size', 6,
             '--packet-budget', 192000, '--judge-concurrency', 1, '--prepare-only'], 1800)
        validate_packet(json.loads((target['packet'] / 'result.json').read_text(encoding='utf8')))
        event('ALL24_PACKETS_PASS_JUDGES_MAY_START')
        command('review', prefix + 'supervised_review', [cohort, target['review'], '--packet-budget', 192000], 23700)
        command('closeout', prefix + 'review_closeout', [target['review'], target['closeout']], 1800)
        write_new(output / 'result.json', {'at': now(), 'status': 'MECHANICAL_CLOSEOUT_DONE_SEMANTIC_AUDIT_PENDING',
            'outputs': {k: str(v) for k, v in target.items()}, 'goalComplete': False, 'automaticRetries': 0})
        event('FINISHED_NOT_GOAL_COMPLETE')
    except BaseException as exc:
        write_new(output / 'failure.json', {'at': now(), 'type': type(exc).__name__, 'message': str(exc),
            'outputsPreserved': True, 'automaticRetries': 0, 'goalComplete': False})
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--cohort-pid', type=int, required=True)
    parser.add_argument('--cohort-created', type=float)
    parser.add_argument('--worker-token')
    args = parser.parse_args()
    cohort = HERE / 'core48_v7_vivo_cohort001'
    if not args.output.is_absolute() or args.output.resolve().parent != HERE:
        raise ValueError('new_absolute_direct_output_required')
    if args.worker_token:
        if args.cohort_created is None or sys.stdin.readline().strip() != args.worker_token:
            raise ValueError('start_gate_not_released')
        run(cohort, args.output.resolve(), args.cohort_pid, args.cohort_created)
        return 0
    require_new([args.output, args.output.with_name(args.output.name + '_supervisor')])
    process = psutil.Process(args.cohort_pid)
    if not command_matches_cohort(process.cmdline(), cohort):
        raise ValueError('exact_cohort_command_required')
    token = uuid.uuid4().hex
    command = [sys.executable, '-X', 'utf8', '-B', '-m',
        'agent.evaluation.context_history_diagnostics_v1_20260905.vivo48_followthrough', str(args.output.resolve()),
        '--cohort-pid', str(args.cohort_pid), '--cohort-created', str(process.create_time()), '--worker-token', token]
    return supervise(command, args.output.with_name(args.output.name + '_supervisor').resolve(),
        timeout=56000, start_token=token)


if __name__ == '__main__':
    raise SystemExit(main())
