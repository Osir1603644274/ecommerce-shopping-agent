"""Exact closed-log compression while the cohort controller is suspended.

No active SUT may exist. No content deletion, no broad directory compression.
The controller is resumed separately only after the receipt is verified.
"""
import json
from pathlib import Path
import shutil
import subprocess

import psutil

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new
from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest
from .probe_lzx import allocated_bytes


def run():
    current = HERE / 'first_edition_vivo48_cohort001'
    old = HERE / 'core48_v7_vivo_cohort001'
    output = HERE / 'first_edition_idle_storage001.json'
    journal = HERE / 'first_edition_idle_storage001_before.json'
    if output.exists() or journal.exists():
        raise ValueError('new_receipts_only')
    controller = psutil.Process(18112)
    if abs(controller.create_time() - 1788662844.534845) > .01 or controller.status() != psutil.STATUS_STOPPED:
        raise ValueError('exact_suspended_controller_required')
    if not all(x in controller.cmdline() for x in ('firstedition48', '--worker-token')):
        raise ValueError('controller_command_mismatch')
    if (current / 'B001').exists() or (current / 'C001').exists():
        raise ValueError('next_sut_already_started')
    result = json.loads((current / 'A001/result.json').read_text(encoding='utf8'))
    events = [json.loads(line) for line in (current / 'A001/runner_events.jsonl').read_text(encoding='utf8').splitlines()]
    if not result['dataCollectionComplete'] or result['turns'] != 48 or events[-1]['event'] != 'RUNNER_FINISHED':
        raise ValueError('A_collection_not_closed')
    stopped = json.loads((HERE / 'USER_SAFE_STOP_20260906_0932.json').read_text(encoding='utf8'))
    if stopped['arms']['A']['committedTurns'] != 41 or stopped['arms']['C']['committedTurns'] != 48:
        raise ValueError('old_stop_boundary_mismatch')
    directories = [old / 'A001', old / 'C001', current / 'A001']
    for directory in directories:
        for path in (directory, *directory.parents):
            if path.is_symlink() or path.is_junction():
                raise ValueError('linked_path_rejected')
        if not directory.resolve().is_relative_to(HERE.resolve()):
            raise ValueError('outside_study')
    for process in psutil.process_iter(['pid', 'cmdline', 'name']):
        if (process.info['name'] or '').lower() not in ('python.exe', 'codex.exe', 'redis-server.exe'):
            continue
        command = ' '.join(process.info['cmdline'] or []).replace('\\', '/').casefold()
        if any(d.as_posix().casefold() in command for d in directories):
            raise ValueError('target_has_live_process:' + str(process.pid))
    logs = [d / 'state_transitions.jsonl' for d in directories]
    if any(p.is_symlink() or not p.is_file() for p in logs):
        raise ValueError('exact_regular_logs_required')
    bound = [*logs, current / 'A001/result.json', current / 'A001/runner_events.jsonl',
        old / 'C001/result.json', HERE / 'USER_SAFE_STOP_20260906_0932.json']
    hashes = {str(p): digest(p) for p in bound}
    before = sum(allocated_bytes(p) for p in logs)
    free = shutil.disk_usage(HERE).free
    write_new(journal, {'at': now(), 'files': hashes, 'targets': [str(p) for p in logs],
        'allocatedBefore': before, 'freeBefore': free, 'controllerPid': 18112,
        'controllerCreated': controller.create_time(), 'controllerStatus': controller.status(),
        'AClosedAt': events[-1]['at'], 'newSutRunning': False, 'BStarted': False, 'CStarted': False})
    commands = []
    for path in logs:
        command = ['compact.exe', '/c', '/f', '/exe:lzx', '/q', str(path)]
        result = subprocess.run(command, capture_output=True, text=True, encoding='oem', errors='replace', timeout=300)
        commands.append({'command': command, 'exitCode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        if result.returncode:
            break
    changed = [p for p, h in hashes.items() if digest(Path(p)) != h]
    receipt = {'at': now(), 'status': 'VERIFIED' if len(commands) == 3 and not changed and
        all(c['exitCode'] == 0 for c in commands) else 'HOLD',
        'commands': commands, 'changedFiles': changed, 'deletedFiles': 0,
        'allocatedBefore': before, 'allocatedAfter': sum(allocated_bytes(p) for p in logs),
        'freeBefore': free, 'freeAfter': shutil.disk_usage(HERE).free,
        'beforeJournalSha256': digest(journal), 'noActiveSut': True,
        'scope': 'Between arms only; A fully ended, controller suspended, B/C not started. Old partialA stays partial.',
        'recovery': 'compact.exe /u /exe on exactly these files; then /c for original NTFS representation, needs free space.'}
    write_new(output, receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k != 'commands'}), flush=True)


if __name__ == '__main__':
    run()
