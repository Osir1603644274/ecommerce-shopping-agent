"""Serial fixed-version suite. New directories only; interrupted runs are not resumed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from batch_plan import build_plan
from evaluation import summarize


def fingerprint(root, dataset):
    files = {dataset.resolve(), root/'backend/target/local-life-backend-0.1.0-SNAPSHOT.jar'}
    for directory in ('agent/app', 'backend/src/main', 'scripts/customer_support'):
        files.update(p for p in (root/directory).rglob('*')
                     if p.is_file() and p.suffix in {'.py', '.json', '.java', '.sql', '.yml', '.yaml', '.properties'})
    for name in ('.env', 'agent/.env', 'requirements.txt', 'agent/requirements.txt', 'pyproject.toml', 'uv.lock'):
        if (root/name).is_file():
            files.add(root/name)
    result = {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(files)}
    # Store a digest, never credentials or environment values.
    relevant = {k: v for k, v in os.environ.items()
                if k.startswith(('DEEPSEEK_', 'OPENAI_', 'CUSTOMER_SUPPORT_'))}
    result['process-config-sha256'] = hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()
    return result


def assert_fixed(expected, actual):
    if expected != actual:
        changed = sorted(k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k))
        raise RuntimeError('fixed-version contract changed: ' + ', '.join(changed))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def run(args):
    root = Path(__file__).resolve().parents[2]
    dataset = args.dataset.resolve()
    plan = build_plan(dataset)
    review_method=getattr(args,'reviewer_type','HUMAN')
    plan['requiredReviewMethod']=review_method
    fixed = fingerprint(root, dataset)
    args.output.mkdir(parents=True, exist_ok=False)
    write(args.output/'PLAN.json', plan)
    write(args.output/'FIXED_VERSION.json', fixed)
    cases = [json.loads(line) for line in dataset.read_text(encoding='utf-8').splitlines()]
    observations = []
    state = {'status': 'PLANNED_NOT_EXECUTED', 'batches': [], 'acceptance': False}
    write(args.output/'STATE.json', state)
    if args.plan_only:
        return
    args.runtime.mkdir(parents=True, exist_ok=False)
    try:
        state['status'] = 'RUNNING'
        for index, batch in enumerate(plan['batches'], 1):
            assert_fixed(fixed, fingerprint(root, dataset))
            name = f"{index:02d}-r{batch['repetition']}-{batch['split']}-{batch['profile']}"
            folder = args.output/name
            command = [sys.executable, str(root/'scripts/customer_support/run_batch.py'),
                       '--runtime', str(args.runtime/name), '--output', str(folder),
                       '--dataset', str(dataset), '--dataset-sha256', plan['datasetSha256'],
                       '--split', batch['split'], '--profile', batch['profile'],
                       '--repetition', str(batch['repetition'])]
            state['activeBatch'] = name
            write(args.output/'STATE.json', state)
            print('starting ' + name, flush=True)
            # One owned batch at a time; child owns and reclaims Java/BFF in finally.
            result = subprocess.run(command, cwd=root)
            source = folder/'batch/observations.json'
            received = json.loads(source.read_text(encoding='utf-8')) if source.exists() else []
            allowed = set(batch['caseIds'])
            if any(r['caseId'] not in allowed or r['repetition'] != batch['repetition'] for r in received):
                raise ValueError('unexpected batch observations')
            observations.extend(received)
            write(args.output/'observations.json', observations)
            state['batches'].append({'name': name, 'exitCode': result.returncode,
                                     'observed': len(received), 'planned': len(allowed)})
            assert_fixed(fixed, fingerprint(root, dataset))
            write(args.output/'REPORT.json', summarize(cases, observations,required_review_method=review_method))
            if result.returncode:
                raise RuntimeError('batch process failed; missing observations retained in planned denominator')
        state['status'] = 'EXECUTED_REQUIRES_ACCEPTANCE_REVIEW'
    except BaseException as error:
        state['status'] = 'STOPPED_INCOMPLETE'
        state['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        write(args.output/'STATE.json', state)
        # This report remains descriptive even when source drift invalidates comparison.
        report = summarize(cases, observations,required_review_method=review_method)
        report['fixedVersionVerified'] = fixed == fingerprint(root, dataset)
        report['acceptance'] = False
        write(args.output/'REPORT.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--reviewer-type', choices=('HUMAN','AGENT'), default='HUMAN')
    run(parser.parse_args())
