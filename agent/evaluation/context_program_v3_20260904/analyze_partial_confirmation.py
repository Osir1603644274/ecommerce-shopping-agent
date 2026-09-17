"""Offline-only accounting of confirmation censored by provider billing refusal.

This is explicitly not the preregistered full-confirmation inferential analysis.
It retains every failure and reservation and never promotes a partial run.
"""
from collections import Counter, defaultdict
import json
from .common import HERE, file_sha, json_new, now, rows, sha
from .analyze_multiturn import percentile


def main():
    out = HERE / 'p4/confirm001'
    result = json.loads((out / 'result.json').read_text(encoding='utf-8'))
    outputs = rows(out / 'outputs.jsonl')
    ledger = rows(HERE / 'provider_ledger.jsonl')
    starts = {r['requestId']: r for r in ledger if r['event'] == 'START'}
    ends = {r['requestId']: r for r in ledger if r['event'] == 'END'}
    selected = {k: s for k, s in starts.items()
                if s['phase'] == 'P4' and s['binding'].get('attempt') == 'confirm001'}
    refused = {k: s for k, s in selected.items() if ends[k].get('httpStatus') == 402}
    assert len(refused) == 3 and result['completedArmTurns'] < result['plannedArmTurns']
    by_binding = {(r['conversationId'], r['turnId'], r['arm']): r for r in outputs}
    failures = []
    for request_id, start in refused.items():
        binding = start['binding']
        record = by_binding[tuple(binding[k] for k in ('conversationId', 'turnId', 'arm'))]
        assert record['safeStopLikeAnswer'] and record['runIdentityMatched']
        assert record['runId'] == binding['runId'] and record['status'] != 'SUCCEEDED'
        failures.append({'requestId': request_id, 'httpStatus': 402,
                         'executionOrdinal': record['executionOrdinal'], 'binding': binding,
                         'originalStatus': record['status'], 'originalOracleErrors': record['oracleErrors'],
                         'preStateRevision': record['preStateRevision'],
                         'postStateRevision': record['postStateRevision'],
                         'businessToolCalls': len(record['toolCalls']),
                         'conservativeReservedTokens': start['reservedTokens']})
    failure_by_ordinal = {r['executionOrdinal']: r for r in failures}
    with (out / 'private_states.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            state = json.loads(line)
            failure = failure_by_ordinal.get(state['executionOrdinal'])
            if failure is None:
                continue
            before, after = state['before'], state['after']
            failure.update(beforeStateSha256=sha(before), afterStateSha256=sha(after),
                           capturedStateUnchanged=before == after,
                           shoppingGuideUnchanged=before['domainState'].get('shoppingGuide') ==
                           after['domainState'].get('shoppingGuide'))
    assert all('capturedStateUnchanged' in r for r in failures)
    assert {r['executionOrdinal'] for r in outputs if r['status'] != 'SUCCEEDED'} == set(failure_by_ordinal)
    assert ledger[-1]['requestId'] == list(refused)[-1] and ledger[-1]['event'] == 'END'
    diagnosis = {
        'at': now(), 'status': 'BLOCKED_PROVIDER_BALANCE', 'httpStatus': 402,
        'officialDocumentation': 'https://api-docs.deepseek.com/quick_start/error_codes/',
        'officialMeaning': 'Insufficient Balance', 'failures': failures,
        'newRequestsInThisAudit': 0, 'originalScorerLabelsPreserved': True,
        'lastLedgerRequestId': ledger[-1]['requestId'], 'noDispatchAfterThird402': True,
        'charged402Reservations': sum(s['reservedTokens'] for s in refused.values()),
        'interpretation': 'Three provider billing refusals led to safe stops. The storage oracle failure remains recorded; it is an unmet request after rejection, not evidence of a silently accepted invalid state.',
        'billingBoundary': 'No provider balance amount or exact billed money is known; missing usage retains conservative token reservations.',
        'ledgerSha256': file_sha(HERE / 'provider_ledger.jsonl'),
        'outputsSha256': file_sha(out / 'outputs.jsonl'),
    }
    json_new(out / 'provider_balance_diagnosis.json', diagnosis)
    arms = ('RAW_FULL_CONTROL', 'CONTEXT_TREATMENT')
    by_arm = {}
    for arm in arms:
        selected_rows = [r for r in outputs if r['arm'] == arm]
        requests = {k: s for k, s in selected.items() if s['binding']['arm'] == arm}
        by_arm[arm] = {
            'armTurns': len(selected_rows), 'successes': sum(r['status'] == 'SUCCEEDED' for r in selected_rows),
            'statusCounts': dict(Counter(r['status'] for r in selected_rows)), 'calls': len(requests),
            'chargedTokens': sum((ends[k].get('usage') or {}).get('total_tokens', s['reservedTokens'])
                                 for k, s in requests.items()),
            'p95Ms': percentile([r['durationMs'] for r in selected_rows], .95),
        }
    pairs = defaultdict(set)
    for r in outputs:
        pairs[r['conversationId'], r['turnId']].add(r['arm'])
    control, treatment = (by_arm[a] for a in arms)
    analysis = {
        'at': now(), 'status': 'DESCRIPTIVE_PARTIAL_CONFIRMATION_ONLY',
        'sourceResultSha256': file_sha(out / 'result.json'),
        'analysisPlanSha256': file_sha(HERE / 'p4/analysis_plan_v1.json'),
        'diagnosisSha256': file_sha(out / 'provider_balance_diagnosis.json'),
        'plannedArmTurns': result['plannedArmTurns'], 'completedArmTurns': len(outputs),
        'unexecutedArmTurns': result['plannedArmTurns'] - len(outputs),
        'completeUserTurnPairs': sum(len(v) == 2 for v in pairs.values()),
        'singleArmUserTurns': sum(len(v) == 1 for v in pairs.values()),
        'byArm': by_arm, 'totalTokenRatio': treatment['chargedTokens'] / control['chargedTokens'],
        'p95LatencyRatio': treatment['p95Ms'] / control['p95Ms'],
        'tokenRatioClusterBootstrap95': None, 'pairedFamilyBootstrap95': None,
        'executionPass': False, 'costThresholdMet': False,
        'costGateStatus': 'NOT_EVALUABLE_INCOMPLETE_CONFIRMATION',
        'formalPopulationNI': 'HOLD_INCOMPLETE_AND_SYNTHETIC_FEW_FAMILIES',
        'humanAnswerQuality': 'NOT_MEASURED', 'productionDefaultSwitch': False,
        'missingDataPolicy': 'Keep all observed failures and all unknown-usage reservations. Do not impute or silently drop 165 unexecuted arm turns.',
        'inferenceBoundary': 'Administrative billing censoring, unequal observed arm counts, and absent planned outcomes invalidate a full-confirmation decision. Ratios describe only captured records; no acceptance CI is issued.',
        'planDeviation': 'Post-stop descriptive accounting only; original preregistered plan is unchanged and not claimed fulfilled.',
    }
    json_new(out / 'analysis.json', analysis)
    print(json.dumps({'diagnosis': diagnosis, 'analysis': analysis}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
