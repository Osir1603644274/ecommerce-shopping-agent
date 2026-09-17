"""Enforce current extraction and fixed-input fidelity gates before P4."""
import argparse
import json
from pathlib import Path
from .common import HERE, file_sha, json_new, now
from .multiturn import main

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--attempt', required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    args = parser.parse_args()
    paths = [HERE / 'p2/attempt003/result.json', HERE / 'p3/attempt002/result.json']
    a, b = [json.loads(p.read_text(encoding='utf-8')) for p in paths]
    if a['status'] != 'PASS' or not b['allExact']: raise RuntimeError('current_quality_gate_not_passed')
    repair = HERE / 'p4/repair002_diagnostic_v2.json'
    if not json.loads(repair.read_text(encoding='utf-8'))['passed']:
        raise RuntimeError('P4_repair_diagnostic_failed')
    paths += [repair, HERE / 'p8/targeted006.xml']
    if not __import__('re').fullmatch('[a-z]+[0-9]{3}', args.attempt): raise ValueError('invalid_attempt')
    json_new(HERE / 'p4' / f'{args.attempt}_gate.json', {'at': now(), 'sourceGates': {str(p.relative_to(HERE)): file_sha(p) for p in paths},
        'costGateIsSeparate': True, 'productionPromotionAllowed': False})
    main(args)
