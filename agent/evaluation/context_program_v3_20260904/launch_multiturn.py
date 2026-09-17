"""Enforce current extraction and fixed-input fidelity gates before P4."""
import argparse
import json
import xml.etree.ElementTree as ET
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
    tests = HERE / 'p8/targeted010.xml'
    suites = ET.parse(tests).getroot().findall('testsuite')
    if not suites or any(int(s.get('failures','0')) or int(s.get('errors','0')) for s in suites):
        raise RuntimeError('current_development_tests_not_passed')
    forced = HERE / 'p2/forcedchoice001/result.json'
    if not json.loads(forced.read_text(encoding='utf-8'))['passed']:
        raise RuntimeError('non_thinking_forced_choice_not_supported')
    paths += [repair, tests, forced]
    joint = HERE / 'p2/jointrepair001/verification.json'
    if not json.loads(joint.read_text(encoding='utf-8'))['passed']:
        raise RuntimeError('joint_repair_probe_not_passed')
    paths.append(joint)
    if not __import__('re').fullmatch('[a-z]+[0-9]{3}', args.attempt): raise ValueError('invalid_attempt')
    json_new(HERE / 'p4' / f'{args.attempt}_gate.json', {'at': now(), 'sourceGates': {str(p.relative_to(HERE)): file_sha(p) for p in paths},
        'costGateIsSeparate': True, 'productionPromotionAllowed': False})
    main(args)
