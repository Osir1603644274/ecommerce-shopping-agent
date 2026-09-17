"""Owned, gated execution of the existing exact-input review continuation."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid

import psutil

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, write_new
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise
from .resume_review import run


def closed_parent(parent):
    if parent.is_symlink() or parent.is_junction() or parent.resolve().parent != HERE.resolve():
        raise ValueError('direct_nonlinked_review_parent_required')
    parent = parent.resolve()
    outer = parent.with_name(parent.name + '_supervisor')
    started = json.loads((outer / 'started.json').read_text(encoding='utf-8'))
    terminal = json.loads((outer / 'result.json').read_text(encoding='utf-8'))
    if psutil.pid_exists(started['supervisorPid']):
        # Conservative refusal on PID reuse; never terminate a foreign PID.
        raise ValueError('parent_supervisor_pid_still_exists')
    if terminal.get('childExitCode') == 0 or not (parent / 'failure.json').is_file():
        raise ValueError('failed_closed_review_parent_required')
    if (parent / 'result.json').exists():
        raise ValueError('do_not_resume_completed_review')
    return parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('parent', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--worker-token')
    args = parser.parse_args()
    if (not args.output.is_absolute() or args.output.exists() or
            args.output.parent.resolve() != HERE.resolve()):
        raise ValueError('new_direct_absolute_review_output_required')
    if args.worker_token and sys.stdin.readline().strip() != args.worker_token:
        raise RuntimeError('review_resume_gate_not_released')
    parent = closed_parent(args.parent)
    if args.worker_token:
        try:
            asyncio.run(run(parent, args.output))
        except BaseException as exc:
            if args.output.exists() and not (args.output / 'failure.json').exists():
                write_new(args.output / 'failure.json', {'type': type(exc).__name__,
                    'error': str(exc), 'formalAcceptance': False})
            raise
        return 0
    token = uuid.uuid4().hex
    command = [sys.executable, '-X', 'utf8', '-B', '-m',
        'agent.evaluation.context_history_diagnostics_v1_20260905.supervised_review_resume',
        str(parent), str(args.output), '--worker-token', token]
    return supervise(command, args.output.with_name(args.output.name + '_supervisor'),
                     timeout=33600, start_token=token)


if __name__ == '__main__':
    raise SystemExit(main())
