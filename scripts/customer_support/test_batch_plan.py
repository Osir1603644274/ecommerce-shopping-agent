import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from batch_plan import build_plan, profile_for, require_environment, select_cases

DATASET = Path(__file__).resolve().parents[2] / 'docs/implementation/customer-support-20260919/evaluation/dataset-draft-003/scenarios.jsonl'


def test_contract_routes_every_case_once_per_repetition():
    plan = build_plan(DATASET)
    rows = [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines()]
    for repetition in (1, 2, 3):
        ids = [case for batch in plan['batches'] if batch['repetition'] == repetition for case in batch['caseIds']]
        assert Counter(ids) == Counter(r['id'] for r in rows)
    assert plan['plannedObservations'] == 720
    assert plan['status'] == 'PLANNED_NOT_EXECUTED'
    assert not plan['humanReviewed']


def test_fault_presteps_select_real_worker_profile():
    rows = [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines()]
    cases = select_cases(rows, split='all', profile='receipt-retry')
    assert {r['id'] for r in cases} == {'CS-recovery-11-1', 'CS-recovery-11-2'}
    incompatible = copy.deepcopy(cases[0])
    incompatible['fixture']['kind'] = 'dispatch_review'
    with pytest.raises(ValueError, match='incompatible'):
        profile_for(incompatible)


def test_heldout_selection_does_not_silently_run_dev():
    rows = [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines()]
    dev = select_cases(rows)
    heldout = select_cases(rows, split='heldout')
    assert len(dev) == len(heldout) == 120
    assert not ({r['id'] for r in dev} & {r['id'] for r in heldout})
    with pytest.raises(ValueError, match='no cases'):
        select_cases(rows, split='dev', case_id=heldout[0]['id'])
    with pytest.raises(ValueError, match='positive'):
        select_cases(rows, limit=0)
    with pytest.raises(ValueError, match='duplicate'):
        select_cases(rows + [rows[0]])


def test_family_leakage_rejected_before_any_execution(tmp_path):
    rows = [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines()]
    heldout = next(r for r in rows if r['split'] == 'heldout')
    heldout['familyId'] = rows[0]['familyId']
    dataset = tmp_path / 'leaked.jsonl'
    dataset.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
    with pytest.raises(ValueError, match='family crosses'):
        build_plan(dataset)


def test_wrong_runtime_rejected_before_fixture_or_model():
    rows = [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines()]
    cases = select_cases(rows, split='all', profile='receipt-retry')
    with pytest.raises(ValueError, match='not ready'):
        require_environment(cases, {})
    config = {'inventoryRuntime': 'isolated', 'supportRecoveryActive': True}
    with pytest.raises(ValueError, match='not ready'):
        require_environment(cases, config)
    assert require_environment(cases, config, has_inventory_fault=True) == 'receipt-retry'
    with pytest.raises(ValueError, match='mixed runtime'):
        require_environment(rows, config, has_inventory_fault=True)
