"""Reversible metadata compression of exactly three closed v5 logs."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new
from .probe_lzx import allocated_bytes


def run(output):
    began = now()
    output = output.resolve()
    journal = output.with_name(output.stem + '_before.json')
    if output.parent != HERE.resolve() or output.exists() or journal.exists():
        raise ValueError('new_direct_study_receipts_required')
    allowed = {HERE / f'core48_v5_vivo_{arm}001' for arm in 'ABC'}
    targets = {d / 'state_transitions.jsonl' for d in allowed}
    prior_path = HERE / 'closed_v5_ntfs_compression001.json'
    before_path = HERE / 'closed_v5_ntfs_compression001_before.json'
    probe_path = HERE / 'storage_lzx_probe001/result.json'
    prior = json.loads(prior_path.read_text(encoding='utf-8'))
    if prior['status'] != 'VERIFIED' or digest(before_path) != prior['beforeJournalSha256']:
        raise ValueError('prior_compression_receipt_invalid')
    if json.loads(probe_path.read_text(encoding='utf-8'))['status'] != 'ROUNDTRIP_PASS':
        raise ValueError('codec_roundtrip_required')
    before = json.loads(before_path.read_text(encoding='utf-8'))
    if {Path(p) for p in before['attemptTerminalHashes']} != {d / 'result.json' for d in allowed}:
        raise ValueError('exact_three_closed_attempts_required')
    for directory in allowed:
        if (directory.is_symlink() or directory.is_junction() or
                directory.resolve().parent != HERE.resolve()):
            raise ValueError('linked_or_outside_directory')
    inventory = subprocess.run(['powershell', '-NoProfile', '-Command',
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','codex.exe','redis-server.exe') } | "
        'Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress'],
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    if inventory.returncode:
        raise ValueError('process_inventory_failed')
    processes = json.loads(inventory.stdout or '[]')
    if isinstance(processes, dict):
        processes = [processes]
    for directory in allowed:
        name = str(directory).replace('\\', '/').casefold()
        if any(name in (p.get('CommandLine') or '').replace('\\', '/').casefold() for p in processes):
            raise ValueError('old_attempt_process_still_active')
    rows = [r for r in before['files'] if Path(r['path']) in targets]
    if len(rows) != 3 or {Path(r['path']) for r in rows} != targets:
        raise ValueError('exact_three_source_log_bindings_required')
    checks = [(Path(p), h) for p, h in before['attemptTerminalHashes'].items()]
    checks.extend((Path(r['path']), r['sha256']) for r in rows)
    for path, expected in checks:
        if (path.parent not in allowed or path.is_symlink() or not path.is_file() or
                path.resolve().parent != path.parent.resolve() or digest(path) != expected):
            raise ValueError('closed_artifact_changed_or_linked')
    files = [{**r, 'allocatedBytesBefore': allocated_bytes(Path(r['path']))} for r in rows]
    free_before = shutil.disk_usage(HERE).free
    write_new(journal, {'beganAt': began, 'at': now(), 'files': files,
        'checks': {str(p): h for p, h in checks}, 'sourceSha256': digest(Path(__file__)),
        'priorReceiptSha256': digest(prior_path), 'priorJournalSha256': digest(before_path),
        'codecProbeSha256': digest(probe_path), 'matchingOldProcesses': [],
        'operation': 'EXACT_THREE_CLOSED_V5_LOGS_LZX_NO_DELETE_NO_RELOCATION'})
    commands = []
    for row in files:
        command = ['compact.exe', '/c', '/f', '/exe:lzx', '/q', row['path']]
        done = subprocess.run(command, capture_output=True, text=True, encoding='oem',
                              errors='replace', timeout=300)
        commands.append({'command': command, 'exitCode': done.returncode,
            'stdout': done.stdout, 'stderr': done.stderr})
        if done.returncode:
            break
    changed = [str(p) for p, h in checks if digest(p) != h]
    success = len(commands) == 3 and not changed and all(c['exitCode'] == 0 for c in commands)
    result = {'beganAt': began, 'at': now(), 'status': 'VERIFIED' if success else 'HOLD',
        'filesChecked': len(checks), 'filesCompressed': len(commands), 'changedFiles': changed,
        'deletedFiles': 0, 'commands': commands, 'freeBytesBefore': free_before,
        'freeBytesAfter': shutil.disk_usage(HERE).free,
        'allocatedBytesBefore': sum(r['allocatedBytesBefore'] for r in files),
        'allocatedBytesAfter': sum(allocated_bytes(Path(r['path'])) for r in files),
        'beforeJournal': str(journal), 'beforeJournalSha256': digest(journal),
        'recovery': 'compact.exe /u /exe on the exact three logs, then /c restores ordinary NTFS compression; requires free space.',
        'timingCaveat': 'After all general72 SUT processes closed, concurrent only with independent review; judge timing is not SUT latency.',
        'scope': 'Six bound files checked, only three log storage attributes changed; no new quality or scientific acceptance.'}
    write_new(output, result)
    print(json.dumps({k: v for k, v in result.items() if k != 'commands'}), flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(run(Path(sys.argv[1])))
