"""Independent checks that a recovery view cannot erase first-attempt failures."""
from collections import Counter
import copy
import json
import pytest
from .common import HERE, V3, ROOT, file_sha, rows
from .summarize import build_views


def fixture():
    original = rows(V3 / 'p4/confirm001/outputs.jsonl')
    recovery = rows(HERE / 'p4/recovery001/outputs.jsonl')
    schedule = json.loads((V3 / 'p4/confirm001/schedule.json').read_text(encoding='utf-8'))
    return original, recovery, schedule


def test_first_attempt_failures_not_replaced():
    old, new, schedule = fixture()
    first, coherent = build_views(old, new, schedule)
    assert Counter(r['status'] for r in first) == {'SUCCEEDED': 1197, 'FAILED': 2, 'SEMANTIC_FAILURE': 1}
    assert Counter(r['status'] for r in coherent) == {'SUCCEEDED': 1200}
    assert len(old + new) == 1221


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_arm', 'wrong_input'])
def test_corrupt_recovery_not_accepted(mutation):
    old, new, schedule = fixture()
    if mutation == 'missing':
        new.pop()
    elif mutation == 'duplicate':
        new.append(new[0])
    elif mutation == 'wrong_arm':
        new[0]['arm'] = 'WRONG'
    else:
        new[0]['inputMessageSha256'] = '0' * 64
    with pytest.raises(AssertionError):
        build_views(old, new, schedule)


def test_global_cost_keeps_inherited_reservations():
    analysis = json.loads((HERE / 'p8/analysis.json').read_text(encoding='utf-8'))
    budget = json.loads((HERE / 'p8/ledger_final.json').read_text(encoding='utf-8'))
    events = rows(HERE / 'provider_ledger.jsonl')
    ends = [r for r in events if r['event'] == 'END']
    assert len(ends) == 89 and all(r['status'] == 'SUCCEEDED' for r in ends)
    assert sum(r['usage']['total_tokens'] for r in ends) == 446367
    assert budget['chargedTokens'] == 6692647 + 446367
    assert sum(a['chargedTokens'] for a in analysis['byArmAllPhysicalCosts'].values()) == 2997773
    assert budget['totalRequests'] == 1718 + 89


def test_no_default_promotion_or_population_inference():
    decision = json.loads((HERE / 'FINAL_DECISION.json').read_text(encoding='utf-8'))
    assert decision['analysis']['formalPopulationNI'].startswith('HOLD')
    assert not decision['allP0P8Passed'] and not decision['allOnlineExtensionsExecuted']
    assert decision['analysis']['originalOnceThroughConfirmationPass'] is False
    assert decision['analysis']['descriptiveCostTargetMet'] is False
    assert not decision['productionDefaultsChanged']


def test_runtime_sources_same_as_last_full_suite():
    source = json.loads((V3 / 'p8/full005_sources.json').read_text(encoding='utf-8'))
    assert len(source['sources']) == 463
    assert all(file_sha(ROOT / r['path']) == r['sha256'] for r in source['sources'])
