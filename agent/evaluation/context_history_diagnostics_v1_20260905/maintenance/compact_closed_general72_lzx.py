"""Exactly three closed general72 state logs; reversible storage-only change."""
import json
from pathlib import Path
import shutil
import subprocess

from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new
from .probe_lzx import allocated_bytes


def run():
    cohort = HERE / 'core72_v7_general_cohort001'
    output = HERE / 'closed_v7_general72_state_logs_lzx001.json'
    journal = output.with_name(output.stem + '_before.json')
    if output.exists() or journal.exists():
        raise ValueError('new_receipts_required')
    def read(path):
        return json.loads(path.read_text(encoding='utf-8'))
    parent = cohort.with_name(cohort.name + '_supervisor')
    directories = [cohort, parent] + [cohort / (a + suffix) for a in 'ABC' for suffix in ('001', '001_supervisor')]
    for directory in directories:
        if directory.is_symlink() or directory.is_junction() or not directory.is_dir():
            raise ValueError('closed_nonlinked_directories_required')
        if not directory.resolve().is_relative_to(HERE.resolve()):
            raise ValueError('outside_study')
    terminal = cohort / 'result.json'
    result = read(terminal)
    if not result['completeMatchedCollection'] or len(result['episodes']) != 3:
        raise ValueError('complete_closed_cohort_required')
    for row in result['episodes']:
        if row['childExitCode'] != 0 or row['observedTurns'] != 72 or not row['dataCollectionComplete']:
            raise ValueError('incomplete_arm')
    supervisors = [parent] + [cohort / (a + '001_supervisor') for a in 'ABC']
    pids = set()
    for directory in supervisors:
        if read(directory / 'result.json')['childExitCode'] != 0:
            raise ValueError('supervisor_not_closed_zero')
        pids.add(read(directory / 'started.json')['supervisorPid'])
    inventory = subprocess.run(['powershell', '-NoProfile', '-Command',
        'Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress'],
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    if inventory.returncode:
        raise ValueError('inventory_failed')
    processes = json.loads(inventory.stdout)
    for process in processes:
        command = (process.get('CommandLine') or '').replace('\\', '/').casefold()
        if process['ProcessId'] in pids or (cohort.as_posix().casefold() in command):
            raise ValueError('old_process_active_or_pid_reused')
    probe = HERE / 'storage_lzx_probe001/result.json'
    if read(probe)['status'] != 'ROUNDTRIP_PASS':
        raise ValueError('codec_roundtrip_required')
    logs = [cohort / (a + '001') / 'state_transitions.jsonl' for a in 'ABC']
    bound = [terminal] + [d / 'result.json' for d in supervisors]
    bound += [cohort / (a + '001') / 'result.json' for a in 'ABC'] + logs
    for path in bound:
        if path.is_symlink() or not path.is_file() or path.resolve().parent != path.parent.resolve():
            raise ValueError('linked_artifact')
    hashes = {str(path): digest(path) for path in bound}
    before = sum(allocated_bytes(path) for path in logs)
    free = shutil.disk_usage(HERE).free
    write_new(journal, {'at': now(), 'sourceSha256': digest(Path(__file__)),
        'files': hashes, 'targets': [str(path) for path in logs], 'allocatedBytesBefore': before,
        'freeBytesBefore': free, 'operation': 'EXACT_CLOSED_GENERAL72_LOGS_LZX',
        'codecProbeSha256': digest(probe), 'closedSupervisorPidsAbsent': sorted(pids)})
    commands = []
    for path in logs:
        command = ['compact.exe', '/c', '/f', '/exe:lzx', '/q', str(path)]
        done = subprocess.run(command, capture_output=True, text=True, encoding='oem', errors='replace', timeout=300)
        commands.append({'command': command, 'exitCode': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr})
        if done.returncode:
            break
    changed = [p for p, expected in hashes.items() if digest(Path(p)) != expected]
    success = len(commands) == 3 and not changed and all(c['exitCode'] == 0 for c in commands)
    receipt = {'at': now(), 'status': 'VERIFIED' if success else 'HOLD',
        'commands': commands, 'filesChecked': len(hashes), 'changedFiles': changed, 'deletedFiles': 0,
        'beforeJournalSha256': digest(journal), 'allocatedBytesBefore': before,
        'allocatedBytesAfter': sum(allocated_bytes(p) for p in logs),
        'freeBytesBefore': free, 'freeBytesAfter': shutil.disk_usage(HERE).free,
        'scope': 'Post-collection storage only; 3 logs and 8 terminal records checked, not all cohort artifacts.',
        'recovery': 'compact.exe /u /exe on these exact three logs, then /c for original NTFS compression; requires free space.'}
    write_new(output, receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k != 'commands'}), flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(run())
