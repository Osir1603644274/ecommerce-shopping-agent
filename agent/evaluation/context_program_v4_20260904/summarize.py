"""Predeclared first-attempt and coherent-conversation descriptive views."""
from collections import Counter, defaultdict
import json
import random
import statistics
from xml.etree import ElementTree as ET
from .common import HERE, V3, ROOT, file_sha, json_new, now, rows, sha
from .audit_ledger import audit
from agent.evaluation.context_program_v3_20260904.analyze_multiturn import percentile

ARMS = ('RAW_FULL_CONTROL', 'CONTEXT_TREATMENT')


def build_views(original, recovered, schedule):
    keys = [r['executionOrdinal'] for r in schedule]
    old = {r['executionOrdinal']: r for r in original}
    new = {r['executionOrdinal']: r for r in recovered}
    assert len(old) == len(original) and len(new) == len(recovered)
    expected_recovery = {r['executionOrdinal'] for r in schedule
                         if int(r['conversationId'].rsplit('-', 1)[1]) >= 55}
    assert set(new) == expected_recovery
    first = [old.get(k) or new[k] for k in keys]
    coherent = [new[k] if k in expected_recovery else old[k] for k in keys]
    for record, scheduled in zip(first, schedule):
        assert all(record[k] == scheduled[k] for k in ('conversationId', 'turnId', 'arm'))
        assert record['inputMessageSha256'] == scheduled['inputMessageSha256']
    for record, scheduled in zip(coherent, schedule):
        assert all(record[k] == scheduled[k] for k in ('conversationId', 'turnId', 'arm'))
        assert record['inputMessageSha256'] == scheduled['inputMessageSha256']
    return first, coherent


def main():
    out = HERE / 'p4/recovery001'
    result = json.loads((out / 'result.json').read_text(encoding='utf-8'))
    assert result['completedArmTurns'] == result['plannedArmTurns'] == 186
    assert json.loads((out / 'integrity_v2.json').read_text(encoding='utf-8'))['status'] == 'PASS_RUNNER_EVIDENCE_EDGES'
    original = rows(V3 / 'p4/confirm001/outputs.jsonl')
    recovered = rows(out / 'outputs.jsonl')
    schedule = json.loads((V3 / 'p4/confirm001/schedule.json').read_text(encoding='utf-8'))
    first, coherent = build_views(original, recovered, schedule)
    assert len(first) == len(coherent) == 1200
    expected = {r['executionOrdinal']: r for r in rows(V3 / 'p4/confirm001/outputs.jsonl')}
    assert all(r['runId'] != expected[r['executionOrdinal']]['runId'] for r in recovered
               if r['executionOrdinal'] in expected), 'new_recovery_identity_reused'
    json_new(HERE / 'p8/view_mapping.json', {'at': now(), 'rows': [
        {'executionOrdinal': a['executionOrdinal'], 'firstAttemptSource': 'v3' if a['executionOrdinal'] in expected else 'v4',
         'coherentSource': 'v4' if int(b['conversationId'].rsplit('-',1)[1]) >= 55 else 'v3',
         'firstAttemptRecordSha256': sha(a), 'coherentRecordSha256': sha(b)} for a,b in zip(first,coherent)]})
    data = rows(V3 / 'p1/breadth001/confirm.jsonl')
    family = {c['conversationId']: c['primitiveFamily'] for c in data}
    all_requests = []
    for root, attempt, version in ((V3, 'confirm001', 'v3'), (HERE, 'recovery001', 'v4')):
        events = rows(root / 'provider_ledger.jsonl')
        ends = {e['requestId']: e for e in events if e['event'] == 'END'}
        for start in events:
            if start['event'] != 'START' or start['binding'].get('attempt') != attempt:
                continue
            usage = ends.get(start['requestId'], {}).get('usage') or {}
            all_requests.append({**start['binding'], 'version': version, 'requestId': start['requestId'],
                'chargedTokens': usage.get('total_tokens', start['reservedTokens']),
                'usageKnown': 'total_tokens' in usage})
    def summary(records):
        return {'armTurns': len(records), 'statusCounts': dict(Counter(r['status'] for r in records)),
                'safeStops': sum(bool(r.get('safeStopLikeAnswer')) for r in records),
                'identityMismatches': sum(r.get('runIdentityMatched') is False for r in records),
                'oracleErrorTurns': sum(bool(r.get('oracleErrors')) for r in records)}
    def costs(requests):
        return {arm: {'calls': sum(r['arm'] == arm for r in requests),
                      'chargedTokens': sum(r['chargedTokens'] for r in requests if r['arm'] == arm)} for arm in ARMS}
    coherent_requests = [r for r in all_requests if r['version'] == 'v4'
                         or int(r['conversationId'].rsplit('-',1)[1]) < 55]
    by_arm = costs(coherent_requests)
    total_costs = costs(all_requests)
    family_cost = defaultdict(int)
    for r in coherent_requests:
        family_cost[family[r['conversationId']], r['arm']] += r['chargedTokens']
    families = sorted(set(family.values()))
    family_scores = {}
    for f in families:
        for arm in ARMS:
            selected = [r for r in coherent if family[r['conversationId']] == f and r['arm'] == arm]
            family_scores[f, arm] = sum(r['status'] == 'SUCCEEDED' for r in selected) / len(selected)
    rng = random.Random(20260904); ratios=[]; differences=[]
    for _ in range(5000):
        chosen = rng.choices(families, k=len(families))
        ratios.append(sum(family_cost[f, ARMS[1]] for f in chosen) / sum(family_cost[f, ARMS[0]] for f in chosen))
        differences.append(statistics.mean(family_scores[f, ARMS[1]] - family_scores[f, ARMS[0]] for f in chosen))
    p95 = {arm: percentile([r['durationMs'] for r in coherent if r['arm'] == arm], .95) for arm in ARMS}
    ratio = by_arm[ARMS[1]]['chargedTokens'] / by_arm[ARMS[0]]['chargedTokens']
    upper = percentile(ratios, .975)
    ledger = audit(); assert ledger['status'] == 'PASS_LEDGER_EDGES'
    source = json.loads((V3 / 'p8/full005_sources.json').read_text(encoding='utf-8'))
    drift = [r['path'] for r in source['sources'] if file_sha(ROOT / r['path']) != r['sha256']]
    assert not drift, 'old_full_suite_sources_no_longer_current'
    json_new(HERE / 'p8/source_verification.json', {'at': now(), 'passed': not drift, 'sourceFiles': len(source['sources']),
        'drift': drift, 'basis': 'v3 full005 3728 passed, 8 skipped; not reexecuted in v4 because application and its tested sources are identical'})
    selected_success = all(r['status'] == 'SUCCEEDED' for r in coherent)
    analysis = {
        'at': now(), 'status': 'ANALYZED_VERSIONED_RECOVERY_NOT_FRESH_CONFIRMATION',
        'firstAttemptCompleteCoverage': summary(first), 'recoveredCoherentConversationView': summary(coherent),
        'allPhysicalExecutions': summary(original + recovered), 'recoveryOnly': summary(recovered),
        'byArmCoherentCosts': by_arm, 'byArmAllPhysicalCosts': total_costs,
        'allPhysicalConfirmationAndRecoveryRequests': len(all_requests),
        'allPhysicalConfirmationAndRecoveryChargedTokens': sum(r['chargedTokens'] for r in all_requests),
        'coherentTokenRatio': ratio, 'coherentTokenRatioFamilyBootstrap95': [percentile(ratios, .025), upper],
        'coherentSuccessDifferenceFamilyBootstrap95': [percentile(differences,.025), percentile(differences,.975)],
        'coherentP95Ratio': p95[ARMS[1]] / p95[ARMS[0]],
        'allPhysicalTokenRatio': total_costs[ARMS[1]]['chargedTokens'] / total_costs[ARMS[0]]['chargedTokens'],
        'descriptiveCostTargetMet': ratio <= .9 and upper < 1 and p95[ARMS[1]] / p95[ARMS[0]] <= 1.2,
        'formalPopulationNI': 'HOLD_SYNTHETIC_EIGHT_FAMILIES_AND_VERSIONED_RECOVERY',
        'originalOnceThroughConfirmationPass': False, 'recoveredStructuredExecutionPass': selected_success,
        'humanAnswerQuality': 'UNJUDGED', 'productionDefaultSwitchAllowed': False,
        'method': 'Original paired-family bootstrap, 5000 draws, seed 20260904, applied descriptively to the predeclared coherent-conversation view. Not original once-through confirmatory acceptance.',
        'ciCaution': 'All-success [0,0] intervals cannot establish population NI; eight same-agent synthetic families and recovery retries do not provide independent human quality evidence.',
        'noOutcomeFiltering': 'Original 1035 rows and all 186 recovery rows retained. Whole conversation version selection was specified before v4 outcomes. Billing includes failed requests and prerequisite reruns.',
        'sourceHashes': {str(p.relative_to(ROOT)): file_sha(p) for p in
            (V3/'p4/confirm001/outputs.jsonl', out/'outputs.jsonl', HERE/'PLAN.md', HERE/'p1/recovery_mapping.json')},
        'reproducibility': 'N/A exact live-response reproduction: external API nondeterminism',
    }
    json_new(HERE / 'p8/analysis.json', analysis)
    json_new(HERE / 'p8/ledger_final.json', ledger)
    decision = {'at': now(), 'status': 'RECOVERED_EXECUTION_COMPLETE_EFFECT_ACCEPTANCE_HOLD',
        'allP0P8Passed': False, 'allOnlineExtensionsExecuted': False, 'productionDefaultsChanged': False,
        'holdReasons': ['FORMAL_POPULATION_NI_NOT_ESTABLISHED', 'HUMAN_ANSWER_QUALITY_UNJUDGED',
                        'ORIGINAL_CONFIRMATION_CONTAINS_THREE_EXTERNAL_FAILURES'],
        'analysis': analysis, 'budget': ledger, 'sameSourceFullSuite': {'passed': 3728, 'skipped': 8, 'failures': 0, 'rerunInV4': False},
        'newRunnerTests': 15,
        'dependentOnlineP5P7': 'NOT_RUN_MAIN_ACCEPTANCE_GATE',
        'providerBalanceBlockResolvedBySuccessfulRegisteredCalls': not ledger['httpErrorCounts']}
    if not analysis['descriptiveCostTargetMet']:
        decision['holdReasons'].append('TEN_PERCENT_TOKEN_TARGET_NOT_MET')
    if not selected_success:
        decision['status'] = 'HOLD_RECOVERED_EXECUTION_FAILURE'
    json_new(HERE / 'FINAL_DECISION.json', decision)
    json_new(HERE / 'p5/online_NOT_RUN.json', {'status': 'NOT_RUN_MAIN_ACCEPTANCE_GATE', 'newCalls': 0,
        'reasons': decision['holdReasons'], 'balanceIsNoLongerTheBlocker': decision['providerBalanceBlockResolvedBySuccessfulRegisteredCalls']})
    json_new(HERE / 'p7/online_NOT_RUN.json', {'status': 'NOT_RUN_MAIN_ACCEPTANCE_GATE', 'newCalls': 0,
        'reasons': decision['holdReasons'], 'balanceIsNoLongerTheBlocker': decision['providerBalanceBlockResolvedBySuccessfulRegisteredCalls']})
    print(json.dumps({'status': decision['status'], 'recovery': analysis['recoveryOnly'],
        'firstAttempt': analysis['firstAttemptCompleteCoverage'], 'coherent': analysis['recoveredCoherentConversationView'],
        'tokenRatio': ratio, 'tokenCI': analysis['coherentTokenRatioFamilyBootstrap95'],
        'allCosts': total_costs, 'ledger': ledger}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
